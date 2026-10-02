"""Extension management MCP tools (instructor/TA only)."""

import datetime
import json
import re
import zoneinfo

from bs4 import BeautifulSoup
from gradescopeapi.classes.extensions import (
    get_extensions as gs_get_extensions,
    update_student_extension,
)

from gradescope_mcp.auth import get_connection, AuthError
from gradescope_mcp.tools.assignments import check_date_order, parse_date_input
from gradescope_mcp.tools.common import escape_md_cell
from gradescope_mcp.tools.safety import write_confirmation_required


def _format_datetime(dt: datetime.datetime | None) -> str:
    if dt is None:
        return "N/A"
    return dt.strftime("%Y-%m-%d %H:%M %Z")


# gradescopeapi raises RuntimeError("Failed to get extensions for assignment
# {assignment_id}. Status code: {status}"). Match the status at the end only:
# the assignment ID itself may contain "401".
_STATUS_CODE_RE = re.compile(r"Status code: (\d{3})\s*$")


def get_extensions(course_id: str, assignment_id: str) -> str:
    """Get all extensions for a specific assignment.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
    """
    if not course_id or not assignment_id:
        return "Error: both course_id and assignment_id are required."

    try:
        conn = get_connection()
        extensions = gs_get_extensions(
            session=conn.session,
            course_id=course_id,
            assignment_id=assignment_id,
        )
    except AuthError as e:
        return f"Authentication error: {e}"
    except RuntimeError as e:
        match = _STATUS_CODE_RE.search(str(e))
        if match is None:
            return f"Error fetching extensions: {e}"
        if match.group(1) == "401":
            return (
                f"Error: Extensions are not available for assignment `{assignment_id}` "
                "(Gradescope returned HTTP 401). "
                "Some assignment types (e.g. exam-style or scanned PDF assignments) "
                "do not support the extensions API endpoint, even for instructors. "
                "You can still manage extensions via the Gradescope web UI."
            )
        return (
            f"Error fetching extensions: Gradescope returned HTTP {match.group(1)} "
            f"for assignment `{assignment_id}`. Check course_id and assignment_id "
            "and that you have staff access to the course."
        )
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        # gradescopeapi got a 200 page without the extensions table or the
        # per-student props it expects.
        return (
            f"Error: the extensions page for assignment `{assignment_id}` did not "
            f"have the expected extensions table ({type(e).__name__}), so "
            "extensions could not be read. This happens on an unexpected page "
            "(for example the login page after the session expired) or for an "
            "assignment type without extensions."
        )
    except Exception as e:
        return f"Error fetching extensions: {e}"

    if not extensions:
        return f"No extensions found for assignment `{assignment_id}` in course `{course_id}`."

    lines = [f"## Extensions for Assignment {assignment_id}\n"]
    lines.append("| User ID | Name | Release Date | Due Date | Late Due Date |")
    lines.append("|---------|------|-------------|----------|---------------|")

    for user_id, ext in extensions.items():
        lines.append(
            f"| `{escape_md_cell(user_id)}` | {escape_md_cell(ext.name)} | "
            f"{_format_datetime(ext.release_date)} | "
            f"{_format_datetime(ext.due_date)} | "
            f"{_format_datetime(ext.late_due_date)} |"
        )

    lines.append(f"\n**Total extensions:** {len(extensions)}")
    return "\n".join(lines)


# (tool argument, key in Gradescope's override settings)
_EXTENSION_FIELDS = (
    ("release_date", "release_date"),
    ("due_date", "due_date"),
    ("late_due_date", "hard_due_date"),
)


class _ExtensionsPageError(Exception):
    """The assignment's extensions page could not be read."""


def _collect_timezones(obj, found: set[str]) -> None:
    """Collect every ``timezone.identifier`` in a React props tree."""
    if isinstance(obj, dict):
        zone = obj.get("timezone")
        if isinstance(zone, dict) and isinstance(zone.get("identifier"), str):
            found.add(zone["identifier"])
        for value in obj.values():
            _collect_timezones(value, found)
    elif isinstance(obj, list):
        for value in obj:
            _collect_timezones(value, found)


def _read_extensions_page(conn, course_id: str, assignment_id: str) -> dict:
    """Read the course timezone and the current overrides from the extensions page.

    Returns ``timezones`` (identifiers found in the page's React props) and
    ``overrides`` (user ID -> override settings exactly as Gradescope
    renders them).
    """
    url = (
        f"{conn.gradescope_base_url}/courses/{course_id}"
        f"/assignments/{assignment_id}/extensions"
    )
    resp = conn.session.get(url)
    if resp.status_code != 200:
        raise _ExtensionsPageError(
            f"the extensions page returned HTTP {resp.status_code}"
        )
    soup = BeautifulSoup(resp.text, "html.parser")
    timezones: set[str] = set()
    overrides: dict[str, dict] = {}
    for element in soup.find_all(attrs={"data-react-props": True}):
        try:
            props = json.loads(element["data-react-props"])
        except (TypeError, ValueError):
            continue
        _collect_timezones(props, timezones)
        if element.get("data-react-class") != "EditExtension" or not isinstance(props, dict):
            continue
        override = props.get("override")
        if isinstance(override, dict) and override.get("user_id") is not None:
            settings = override.get("settings")
            overrides[str(override["user_id"])] = settings if isinstance(settings, dict) else {}
    return {"timezones": timezones, "overrides": overrides}


def _course_zone(page: dict) -> tuple[zoneinfo.ZoneInfo | None, str]:
    """Return (course timezone, reason it is unknown) from a parsed page."""
    names = sorted(page["timezones"])
    if not names:
        return None, (
            "the extensions page does not state the course timezone (Gradescope "
            "publishes it alongside existing extensions; this assignment may "
            "have none yet)"
        )
    if len(names) > 1:
        return None, f"the extensions page lists several timezones ({', '.join(names)})"
    try:
        return zoneinfo.ZoneInfo(names[0]), ""
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return None, f"the course timezone {names[0]!r} is not known on this server"


def _localize(naive: datetime.datetime, zone: zoneinfo.ZoneInfo, field: str) -> datetime.datetime:
    """Attach ``zone`` to a wall-clock time, rejecting DST gaps and overlaps."""
    aware = naive.replace(tzinfo=zone)
    wall = f"{naive:%Y-%m-%dT%H:%M}"
    round_trip = aware.astimezone(datetime.timezone.utc).astimezone(zone).replace(tzinfo=None)
    if round_trip != naive:
        raise ValueError(
            f"{field} {wall} does not exist in {zone.key} (the clocks skip it "
            "for daylight saving time); pick another time or give a UTC offset."
        )
    if aware.utcoffset() != naive.replace(tzinfo=zone, fold=1).utcoffset():
        raise ValueError(
            f"{field} {wall} is ambiguous in {zone.key} (it occurs twice when "
            "the clocks fall back); give a UTC offset to choose one."
        )
    return aware


def _utc_text(value: datetime.datetime) -> str:
    """The instant exactly as gradescopeapi posts it."""
    return value.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _describe_instant(value: datetime.datetime, zone: zoneinfo.ZoneInfo | None) -> str:
    if zone is not None:
        local = value.astimezone(zone)
        return f"{local:%Y-%m-%d %H:%M} {zone.key} ({local:%Z}) = {_utc_text(value)}"
    return f"{value.isoformat(timespec='minutes')} = {_utc_text(value)}"


def _raw_setting(settings: dict, key: str):
    entry = settings.get(key)
    return entry.get("value") if isinstance(entry, dict) else entry


def _describe_override(settings: dict | None) -> str:
    if settings is None:
        return "This student has no extension on this assignment yet."
    parts = [
        f"{arg}={_raw_setting(settings, key)}"
        for arg, key in _EXTENSION_FIELDS
        if _raw_setting(settings, key)
    ]
    return (
        "Current extension for this student (as Gradescope stores it): "
        + (", ".join(parts) or "no date overrides")
    )


def _stored_instant(raw, zone: zoneinfo.ZoneInfo | None) -> datetime.datetime | None:
    """Interpret a stored override value; naive values are course-local."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        if zone is None:
            return None
        parsed = parsed.replace(tzinfo=zone)
    return parsed


def set_extension(
    course_id: str,
    assignment_id: str,
    user_id: str,
    release_date: str | None = None,
    due_date: str | None = None,
    late_due_date: str | None = None,
    confirm_write: bool = False,
    timezone: str | None = None,
) -> str:
    """Add or update an extension for a student on an assignment.

    Dates without a UTC offset are wall-clock times in the course's
    timezone, read from Gradescope's extensions page (or given with
    ``timezone``); dates with an offset (``Z``, ``-07:00``) are absolute.
    Never the MCP host's timezone. Gradescope stores each date as a UTC
    instant: the preview shows the resolved instant and the extension is
    read back after writing. Only the dates passed are sent.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        user_id: The student's Gradescope user ID. Use get_course_roster to find user IDs.
        release_date: Extension release date (YYYY-MM-DDTHH:MM, optional UTC offset), or None/"".
        due_date: Extension due date (YYYY-MM-DDTHH:MM, optional UTC offset), or None/"".
        late_due_date: Extension late due date (YYYY-MM-DDTHH:MM, optional UTC offset), or None/"".
        confirm_write: Must be True to perform the update.
        timezone: IANA timezone (e.g. "America/New_York") for dates without
            an offset. Defaults to the course timezone Gradescope reports;
            needed when it reports none.
    """
    if not all([course_id, assignment_id, user_id]):
        return "Error: course_id, assignment_id, and user_id are all required."

    try:
        requested = [
            (arg, parse_date_input(value, arg, allow_offset=True))
            for arg, value in (
                ("release_date", release_date),
                ("due_date", due_date),
                ("late_due_date", late_due_date),
            )
        ]
    except ValueError as e:
        return f"Error: {e}"

    provided = [(arg, value) for arg, value in requested if value is not None]
    if not provided:
        return "Error: at least one date must be provided."
    naive_args = [arg for arg, value in provided if value.tzinfo is None]
    if naive_args and len(naive_args) != len(provided):
        return (
            "Error: give all dates either without a UTC offset (course-local "
            "time) or all with one; don't mix the two."
        )
    order_error = check_date_order(requested)
    if order_error:
        return f"Error: {order_error}"

    zone_arg = None
    if timezone is not None and str(timezone).strip():
        try:
            zone_arg = zoneinfo.ZoneInfo(str(timezone).strip())
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            return (
                f"Error: unknown timezone '{timezone}'. Use an IANA name such "
                "as 'America/New_York'."
            )

    header = [
        f"course_id=`{course_id}`",
        f"assignment_id=`{assignment_id}`",
        f"user_id=`{user_id}`",
    ]

    # The extensions page provides the course timezone and the student's
    # current extension (shown in the preview, compared after writing).
    page = None
    page_problem = ""
    auth_failed = False
    try:
        conn = get_connection()
        page = _read_extensions_page(conn, course_id, assignment_id)
    except AuthError as e:
        if confirm_write:
            return f"Authentication error: {e}"
        page_problem = f"Authentication error: {e}"
        auth_failed = True
    except Exception as e:
        page_problem = str(e) or repr(e)

    course_zone, zone_problem = _course_zone(page) if page is not None else (None, page_problem)
    zone = zone_arg or course_zone
    zone_note = ""
    if zone_arg is not None:
        zone_note = f"Timezone for dates without an offset: {zone_arg.key} (timezone argument)"
        if course_zone is not None and course_zone.key != zone_arg.key:
            zone_note += f". ⚠️ Gradescope reports the course timezone as {course_zone.key}"
    elif course_zone is not None:
        zone_note = (
            f"Timezone for dates without an offset: {course_zone.key} (course "
            "timezone reported by Gradescope)"
        )

    if naive_args and zone is None:
        if auth_failed:
            details = header + [
                f"{arg}={value:%Y-%m-%dT%H:%M} (course-local time; timezone not resolved yet)"
                for arg, value in provided
            ]
            details.append(
                f"⚠️ Gradescope could not be read ({page_problem}), so the course "
                "timezone and the student's current extension are unknown. With "
                "confirm_write=True the tool looks up the course timezone first "
                "and refuses to write if it cannot."
            )
            return write_confirmation_required("set_extension", details)
        return (
            f"Error: cannot tell which timezone {', '.join(naive_args)} "
            f"{'is' if len(naive_args) == 1 else 'are'} in: {zone_problem}. "
            "Pass timezone='Area/City' (the course's timezone) or give the dates "
            "with a UTC offset. Nothing was changed."
        )

    try:
        resolved = [
            (arg, _localize(value, zone, arg) if value.tzinfo is None else value)
            for arg, value in provided
        ]
    except ValueError as e:
        return f"Error: {e}"

    date_lines = [f"{arg}: {_describe_instant(value, zone)}" for arg, value in resolved]

    if not confirm_write:
        details = header + date_lines
        if zone_note:
            details.append(zone_note)
        if page is None:
            details.append(f"Current extension: could not be read ({page_problem})")
        else:
            details.append(_describe_override(page["overrides"].get(str(user_id))))
        details.append(
            "Only the dates above are sent (as UTC instants). Whether Gradescope "
            "keeps the student's other existing extension dates is not "
            "verified; pass them again to be sure they are kept."
        )
        return write_confirmation_required("set_extension", details)

    values = dict(resolved)
    try:
        success = update_student_extension(
            session=conn.session,
            course_id=course_id,
            assignment_id=assignment_id,
            user_id=user_id,
            release_date=values.get("release_date"),
            due_date=values.get("due_date"),
            late_due_date=values.get("late_due_date"),
        )
    except AuthError as e:
        return f"Authentication error: {e}"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error setting extension: {e}"

    if not success:
        return "❌ Failed to set extension. Check your permissions and verify the user ID."

    summary = "\n".join(f"- {arg} → {_describe_instant(value, zone)}" for arg, value in resolved)
    target = f"user `{user_id}` on assignment `{assignment_id}`"
    try:
        after = _read_extensions_page(conn, course_id, assignment_id)
    except Exception as e:
        return (
            f"⚠️ Extension for {target} was submitted (HTTP 200) but could not "
            f"be verified: {e}. Check it with get_extensions.\n{summary}"
        )
    settings = after["overrides"].get(str(user_id))
    if settings is None:
        return (
            f"⚠️ Extension for {target} was submitted (HTTP 200), but no "
            "extension for this user appears on the extensions page. Check the "
            f"user ID and the Gradescope web UI.\n{summary}"
        )

    read_zone = zone or _course_zone(after)[0]
    differences = []
    for arg, value in resolved:
        key = dict(_EXTENSION_FIELDS)[arg]
        raw = _raw_setting(settings, key)
        stored = _stored_instant(raw, read_zone)
        if stored is None or abs(stored - value) >= datetime.timedelta(minutes=1):
            differences.append(
                f"- {arg}: sent {_utc_text(value)}, extensions page shows {raw!r}"
            )
    if differences:
        return (
            f"⚠️ Extension for {target} was submitted (HTTP 200), but the "
            "extensions page does not show the expected dates:\n"
            + "\n".join(differences)
            + "\nCheck the extension in the Gradescope web UI."
        )
    return f"✅ Extension for {target} updated (read back from Gradescope):\n{summary}"

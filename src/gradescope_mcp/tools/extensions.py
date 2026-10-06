"""Extension management MCP tools (instructor/TA only)."""

import copy
import datetime
import json
import re
import zoneinfo

from bs4 import BeautifulSoup
from gradescopeapi.classes.extensions import get_extensions as gs_get_extensions

from gradescope_mcp.auth import get_connection, AuthError
from gradescope_mcp.tools.assignments import (
    WriteInProgressError,
    check_date_order,
    parse_date_input,
    serialized_write,
)
from gradescope_mcp.tools.common import escape_md_cell
from gradescope_mcp.tools.safety import write_confirmation_required


# gradescopeapi raises RuntimeError("Failed to get extensions for assignment
# {assignment_id}. Status code: {status}"). Match the status at the end only:
# the assignment ID itself may contain "401".
_STATUS_CODE_RE = re.compile(r"Status code: (\d{3})\s*$")


class _PageRecorder:
    """Session stand-in that keeps the last response fetched through it.

    gradescopeapi's ``get_extensions`` attaches the course timezone to each
    stored value instead of converting it, so a UTC value such as
    ``2026-10-02T06:59:00Z`` came out as 06:59 course time. The raw values
    are re-read from the same response instead.
    """

    def __init__(self, session):
        self._session = session
        self.response = None

    def get(self, *args, **kwargs):
        self.response = self._session.get(*args, **kwargs)
        return self.response


def get_extensions(course_id: str, assignment_id: str) -> str:
    """Get all extensions for a specific assignment.

    Each date is shown as course-local wall-clock time and the UTC instant
    Gradescope stores (stored values with ``Z`` or an offset are absolute,
    values without one are course-local). Other override settings, such as
    a time limit, are listed as well.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
    """
    if not course_id or not assignment_id:
        return "Error: both course_id and assignment_id are required."

    try:
        conn = get_connection()
        recorder = _PageRecorder(conn.session)
        extensions = gs_get_extensions(
            session=recorder,
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
    if recorder.response is None:
        return (
            f"Error: the stored extension values for assignment `{assignment_id}` "
            "could not be read."
        )

    page = _parse_extensions_page(recorder.response.text)
    zone, zone_problem = _course_zone(page)
    lines = [f"## Extensions for Assignment {assignment_id}\n"]
    if zone is not None:
        lines.append(
            f"Dates are course-local wall-clock times ({zone.key}) followed by "
            "the UTC instant Gradescope stores. set_extension accepts either: "
            "the local time without an offset, or the UTC instant with its Z.\n"
        )
    else:
        lines.append(
            f"Dates are shown as Gradescope stores them; the course timezone is "
            f"unknown ({zone_problem}).\n"
        )
    lines.append(
        "| User ID | Name | Release Date | Due Date | Late Due Date | Other Settings |"
    )
    lines.append(
        "|---------|------|-------------|----------|---------------|----------------|"
    )

    for user_id, ext in extensions.items():
        settings = page["overrides"].get(str(user_id))
        if settings is None:
            cells = ["(unreadable)"] * 4
        else:
            cells = [
                _show_stored(_raw_setting(settings, key), zone)
                for _arg, key in _EXTENSION_FIELDS
            ]
            cells.append(", ".join(_other_settings(settings)) or "—")
        lines.append(
            f"| `{escape_md_cell(user_id)}` | {escape_md_cell(ext.name)} | "
            + " | ".join(escape_md_cell(cell) for cell in cells)
            + " |"
        )

    lines.append(f"\n**Total extensions:** {len(extensions)}")
    return "\n".join(lines)


# (tool argument, key in Gradescope's override settings)
_EXTENSION_FIELDS = (
    ("release_date", "release_date"),
    ("due_date", "due_date"),
    ("late_due_date", "hard_due_date"),
)
_FIELD_KEYS = dict(_EXTENSION_FIELDS)
_ARG_NAMES = {key: arg for arg, key in _EXTENSION_FIELDS}
_DATE_KEYS = frozenset(_FIELD_KEYS.values())
# Sent with every extension (as gradescopeapi's update_student_extension does).
_VISIBLE_KEY = "visible"


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


def _parse_extensions_page(text: str) -> dict:
    """Parse an extensions page.

    Returns ``timezones`` (identifiers found in the page's React props),
    ``overrides`` (user ID -> override settings exactly as Gradescope renders
    them) and ``is_extensions_page`` (it has the extensions table or at least
    one extension).
    """
    soup = BeautifulSoup(text or "", "html.parser")
    timezones: set[str] = set()
    overrides: dict[str, dict] = {}
    found_extension = False
    for element in soup.find_all(attrs={"data-react-props": True}):
        try:
            props = json.loads(element["data-react-props"])
        except (TypeError, ValueError):
            continue
        _collect_timezones(props, timezones)
        if element.get("data-react-class") != "EditExtension" or not isinstance(props, dict):
            continue
        found_extension = True
        override = props.get("override")
        if isinstance(override, dict) and override.get("user_id") is not None:
            settings = override.get("settings")
            overrides[str(override["user_id"])] = settings if isinstance(settings, dict) else {}
    return {
        "timezones": timezones,
        "overrides": overrides,
        "is_extensions_page": (
            found_extension or soup.select_one("table.js-overridesTable") is not None
        ),
    }


def _read_extensions_page(conn, course_id: str, assignment_id: str) -> dict:
    """Fetch and parse the assignment's extensions page (see ``_parse_extensions_page``).

    Raises:
        _ExtensionsPageError: on a non-200 answer or a page that is not the
            extensions page, where "no extension" can't be told from "not
            readable".
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
    page = _parse_extensions_page(resp.text)
    if not page["is_extensions_page"]:
        raise _ExtensionsPageError(
            "the extensions page has no extensions table (unexpected page; "
            "check the IDs and that you have staff access to the course)"
        )
    return page


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


def _entry_value(entry):
    """A setting's value: settings are usually ``{"type": ..., "value": ...}``."""
    return entry.get("value") if isinstance(entry, dict) else entry


def _raw_setting(settings: dict, key: str):
    return _entry_value(settings.get(key))


def _setting_text(entry) -> str:
    """One override setting's value for display (``{"value": ...}`` unwrapped)."""
    value = entry.get("value") if isinstance(entry, dict) and "value" in entry else entry
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


def _other_settings(settings: dict) -> list[str]:
    """``key=value`` for every override setting other than the three dates."""
    return [
        f"{key}={_setting_text(entry)}"
        for key, entry in settings.items()
        if key not in _DATE_KEYS
    ]


def _parse_stored(raw) -> datetime.datetime | None:
    """Parse a stored override date; naive when it has no offset, None if not a date."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


def _stored_instant(raw, zone: zoneinfo.ZoneInfo | None) -> datetime.datetime | None:
    """Interpret a stored override value; naive values are course-local."""
    parsed = _parse_stored(raw)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        if zone is None:
            return None
        parsed = parsed.replace(tzinfo=zone)
    return parsed


def _show_stored(raw, zone: zoneinfo.ZoneInfo | None) -> str:
    """A stored override date as ``local time = UTC instant`` for listings."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "N/A"
    parsed = _parse_stored(raw)
    if parsed is None:
        return f"{raw} (unrecognized format)"
    if parsed.tzinfo is None and zone is None:
        return f"{raw} (course-local; timezone unknown)"
    instant = _stored_instant(raw, zone)
    if zone is None:
        return _utc_text(instant)
    return f"{instant.astimezone(zone):%Y-%m-%d %H:%M %Z} = {_utc_text(instant)}"


def _describe_override(settings: dict | None, zone: zoneinfo.ZoneInfo | None) -> str:
    """Every current override setting, dates with their course-local time."""
    if settings is None:
        return "This student has no extension on this assignment yet."
    parts = []
    for arg, key in _EXTENSION_FIELDS:
        if key not in settings:
            continue
        raw = _raw_setting(settings, key)
        part = f"{arg}={raw}"
        instant = _stored_instant(raw, zone)
        if instant is not None and zone is not None:
            part += f" ({instant.astimezone(zone):%Y-%m-%d %H:%M %Z})"
        parts.append(part)
    parts += _other_settings(settings)
    return (
        "Current extension for this student (as Gradescope stores it): "
        + (", ".join(parts) or "no settings")
    )


def _describe_sent(settings: dict) -> str:
    """The override settings as they will be posted."""
    return ", ".join(
        f"{_ARG_NAMES.get(key, key)}={_setting_text(entry)}" for key, entry in settings.items()
    )


def _same_setting(before, after, zone: zoneinfo.ZoneInfo | None) -> bool:
    """Whether a re-read setting still holds the value it had before."""
    raw_before, raw_after = _entry_value(before), _entry_value(after)
    first, second = _stored_instant(raw_before, zone), _stored_instant(raw_after, zone)
    if first is not None and second is not None:
        return abs(first - second) < datetime.timedelta(minutes=1)
    return raw_before == raw_after


def _date_change(
    arg: str,
    value: datetime.datetime,
    current: dict | None,
    zone: zoneinfo.ZoneInfo | None,
    stored_zone: zoneinfo.ZoneInfo | None,
    preview: bool,
) -> tuple[str, bool]:
    """(How a sent date compares to the current extension, whether it already had it).

    Wording differs between the preview and the result after the write; a
    date that was already set is never called "unchanged", so a write
    retried after it went through doesn't read as if nothing changed.
    """
    key = _FIELD_KEYS[arg]
    verb = "currently" if preview else "was"
    if current is None or key not in current:
        return ("(not set now)" if preview else "(newly set)"), False
    raw = _raw_setting(current, key)
    before = _stored_instant(raw, stored_zone)
    if before is None:
        return f"({verb} {raw!r})", False
    if abs(before - value) < datetime.timedelta(minutes=1):
        return (
            "(already the current value)" if preview else "(already set before this write)"
        ), True
    return f"({verb} {_describe_instant(before, zone)})", False


def _plan_settings(current: dict | None, resolved: list[tuple[str, datetime.datetime]]) -> dict:
    """The full override to post: current settings with the requested dates replaced.

    Settings that are not passed are re-sent exactly as Gradescope renders
    them, so they are kept whether Gradescope merges or replaces the
    override. ``visible`` is always sent as true, as gradescopeapi does.
    """
    settings = copy.deepcopy(current) if current else {}
    for arg, value in resolved:
        settings[_FIELD_KEYS[arg]] = {"type": "absolute", "value": _utc_text(value)}
    settings[_VISIBLE_KEY] = True
    return settings


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
    timezone, read from Gradescope's extensions page; ``timezone`` supplies
    it when Gradescope reports none and is refused when it differs from the
    one Gradescope reports, or when the page reports several zones or one
    this server can't load (then no argument can be checked). When it stood
    in for an unreported zone and the read-back after the write reveals a
    different one, the result warns (⚠️) that dates without an offset were
    resolved in the wrong zone. Dates with an offset (``Z``, ``-07:00``) are
    absolute. Never the MCP host's timezone. Gradescope stores each date as
    a UTC instant: the preview shows the resolved instant.

    The student's whole override is sent: the dates passed, every other
    current setting (other dates, a time limit, ...) re-sent unchanged, and
    ``visible=true``. The preview lists all of it, so it needs the
    extensions page to be readable; a stored visible that is not true is
    reported as changing (``visible: false → true``) in preview and result.
    After writing, the extension is read back and any setting Gradescope
    changed or dropped is reported. Confirmed changes to the same student's
    extension run one at a time.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        user_id: The student's Gradescope user ID. Use get_course_roster to find user IDs.
        release_date: Extension release date (YYYY-MM-DDTHH:MM, optional UTC offset), or None/"".
        due_date: Extension due date (YYYY-MM-DDTHH:MM, optional UTC offset), or None/"".
        late_due_date: Extension late due date (YYYY-MM-DDTHH:MM, optional UTC offset), or None/"".
        confirm_write: Must be True to perform the update.
        timezone: IANA timezone (e.g. "America/New_York") for dates without
            an offset. Needed only when Gradescope reports no course
            timezone; if given, it must be the course timezone Gradescope
            reports (the only one), or the call is an Error.
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

    if not confirm_write:
        return _apply_extension(
            course_id, assignment_id, user_id, provided, naive_args, zone_arg, False
        )
    try:
        with serialized_write("extension", course_id, assignment_id, user_id):
            return _apply_extension(
                course_id, assignment_id, user_id, provided, naive_args, zone_arg, True
            )
    except WriteInProgressError:
        return (
            f"Error: another extension change for user `{user_id}` on assignment "
            f"`{assignment_id}` is still running; try again when it has "
            "finished. Nothing was changed."
        )


def _apply_extension(
    course_id: str,
    assignment_id: str,
    user_id: str,
    provided: list[tuple[str, datetime.datetime]],
    naive_args: list[str],
    zone_arg: zoneinfo.ZoneInfo | None,
    confirm_write: bool,
) -> str:
    """Read, plan and (with ``confirm_write``) write and verify an extension."""
    header = [
        f"course_id=`{course_id}`",
        f"assignment_id=`{assignment_id}`",
        f"user_id=`{user_id}`",
    ]

    # The extensions page provides the course timezone and the student's
    # current extension, which is merged into the write. Without it the
    # preview can't show what will be sent, so both preview and write stop.
    try:
        conn = get_connection()
        page = _read_extensions_page(conn, course_id, assignment_id)
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return (
            f"Error: cannot read the extensions page of assignment "
            f"`{assignment_id}` ({str(e) or repr(e)}), so the student's current "
            "extension is unknown and could not be kept. Nothing was changed."
        )

    course_zone, zone_problem = _course_zone(page)
    # Dates without an offset are course-local, so a timezone argument may
    # only stand in for a course timezone Gradescope doesn't report. When the
    # page reports several zones or one this server can't load, no argument
    # can be checked against the course's, so it is refused as well.
    reported = sorted(page["timezones"])
    if zone_arg is not None and any(name != zone_arg.key for name in reported):
        if course_zone is not None:
            return (
                f"Error: timezone='{zone_arg.key}' differs from the course "
                f"timezone Gradescope reports ({course_zone.key}). Omit timezone "
                f"or pass '{course_zone.key}': dates without an offset are "
                "wall-clock times in the course timezone, and a date meant in "
                "another zone needs its UTC offset (e.g. -04:00) instead of a "
                "timezone argument. Nothing was changed."
            )
        return (
            f"Error: timezone='{zone_arg.key}' cannot be checked against the "
            f"course timezone: {zone_problem}. Omit timezone and give every "
            "date with its UTC offset (e.g. 2026-10-04T23:59-07:00). Nothing "
            "was changed."
        )
    zone = zone_arg or course_zone
    # Stored values without an offset are in the course's timezone.
    stored_zone = course_zone or zone_arg
    zone_note = ""
    if zone_arg is not None:
        source = (
            "timezone argument, the course timezone Gradescope reports"
            if course_zone is not None
            else "timezone argument; Gradescope reports no course timezone"
        )
        zone_note = f"Timezone for dates without an offset: {zone_arg.key} ({source})"
    elif course_zone is not None:
        zone_note = (
            f"Timezone for dates without an offset: {course_zone.key} (course "
            "timezone reported by Gradescope)"
        )

    if naive_args and zone is None:
        # A timezone argument is refused when the page reports zones it
        # can't be checked against, so only offsets are suggested then.
        fix = (
            "Give the dates with a UTC offset" if reported
            else "Pass timezone='Area/City' (the course's timezone) or give "
            "the dates with a UTC offset"
        )
        return (
            f"Error: cannot tell which timezone {', '.join(naive_args)} "
            f"{'is' if len(naive_args) == 1 else 'are'} in: {zone_problem}. "
            f"{fix}. Nothing was changed."
        )

    try:
        resolved = [
            (arg, _localize(value, zone, arg) if value.tzinfo is None else value)
            for arg, value in provided
        ]
    except ValueError as e:
        return f"Error: {e}"

    current = page["overrides"].get(str(user_id))
    settings = _plan_settings(current, resolved)
    merged_order = check_date_order([
        (arg, _stored_instant(_raw_setting(settings, key), stored_zone))
        for arg, key in _EXTENSION_FIELDS
    ])
    if merged_order:
        return (
            f"Error: {merged_order} The student's current extension dates you "
            "don't pass are kept, so pass the conflicting one as well. Nothing "
            "was changed."
        )
    sent_keys = {_FIELD_KEYS[arg] for arg, _value in resolved}
    kept = {
        key: entry
        for key, entry in (current or {}).items()
        if key not in sent_keys and key != _VISIBLE_KEY
    }
    # visible=true is always sent, so a stored visible other than true flips.
    visible_before = None
    visible_note = ""
    if current is not None and _VISIBLE_KEY in current:
        if _setting_text(current[_VISIBLE_KEY]) != "true":
            visible_before = _setting_text(current[_VISIBLE_KEY])
        else:
            visible_note = " and visible=true"

    def change_lines(preview: bool) -> tuple[list[str], bool]:
        """(One line per requested date and a visible flip, whether all were already set)."""
        lines, unchanged = [], True
        for arg, value in resolved:
            note, same = _date_change(arg, value, current, zone, stored_zone, preview)
            unchanged = unchanged and same
            separator = ": " if preview else " → "
            lines.append(f"{arg}{separator}{_describe_instant(value, zone)} {note}")
        if visible_before is not None:
            lines.append(f"visible: {visible_before} → true (visible=true is always sent)")
            unchanged = False
        return lines, unchanged

    if not confirm_write:
        lines, unchanged = change_lines(preview=True)
        details = header + lines
        if zone_note:
            details.append(zone_note)
        details.append(_describe_override(current, stored_zone))
        details.append(
            "With confirm_write=True the student's whole extension is sent: "
            f"{_describe_sent(settings)}. Settings you don't pass are re-sent "
            "unchanged so they are kept; visible=true is always sent."
        )
        if unchanged:
            details.append(
                f"The extension already has every requested date{visible_note}; "
                "confirming re-sends the same values."
            )
        return write_confirmation_required("set_extension", details)

    url = (
        f"{conn.gradescope_base_url}/courses/{course_id}"
        f"/assignments/{assignment_id}/extensions"
    )
    body = {"override": {"user_id": user_id, "settings": settings}}
    try:
        resp = conn.session.post(url, json=body)
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return (
            f"Error setting extension: the request failed ({e!r}); Gradescope "
            "may or may not have applied it. Check with get_extensions."
        )

    if resp.status_code != 200:
        return (
            f"❌ Failed to set extension (HTTP {resp.status_code}). Check your "
            "permissions and verify the user ID."
        )

    lines, unchanged = change_lines(preview=False)
    summary = "\n".join(f"- {line}" for line in lines)
    if kept:
        summary += f"\n- Kept unchanged: {_describe_sent(kept)}"
    if unchanged:
        # E.g. a call re-run after its first write went through.
        summary += (
            f"\nThe extension already had every requested date{visible_note} "
            "when this call read it, so this write changed nothing itself (an "
            "earlier attempt of the same change, e.g. one interrupted by a "
            "session expiry, may have applied it)."
        )
    target = f"user `{user_id}` on assignment `{assignment_id}`"
    try:
        after = _read_extensions_page(conn, course_id, assignment_id)
    except Exception as e:
        return (
            f"⚠️ Extension for {target} was submitted (HTTP 200) but could not "
            f"be verified: {e}. Check it with get_extensions.\n{summary}"
        )
    stored = after["overrides"].get(str(user_id))
    if stored is None:
        return (
            f"⚠️ Extension for {target} was submitted (HTTP 200), but no "
            "extension for this user appears on the extensions page. Check the "
            f"user ID and the Gradescope web UI.\n{summary}"
        )

    after_zone = _course_zone(after)[0]
    read_zone = course_zone or after_zone or zone_arg
    differences = []
    for arg, value in resolved:
        raw = _raw_setting(stored, _FIELD_KEYS[arg])
        instant = _stored_instant(raw, read_zone)
        if instant is None or abs(instant - value) >= datetime.timedelta(minutes=1):
            differences.append(
                f"- {arg}: sent {_utc_text(value)}, extensions page shows {raw!r}"
            )
    for key, entry in kept.items():
        name = _ARG_NAMES.get(key, key)
        if key not in stored:
            differences.append(
                f"- {name}: was {_setting_text(entry)}, re-sent unchanged, but it "
                "is no longer on the extensions page (removed)"
            )
        elif not _same_setting(entry, stored[key], read_zone):
            differences.append(
                f"- {name}: was {_setting_text(entry)}, re-sent unchanged, but the "
                f"extensions page now shows {_setting_text(stored[key])} (changed)"
            )
    if _VISIBLE_KEY in stored and _setting_text(stored[_VISIBLE_KEY]) != "true":
        differences.append(
            f"- visible: sent true, extensions page shows "
            f"{_setting_text(stored[_VISIBLE_KEY])}"
        )
    # The timezone argument stood in for a course timezone Gradescope did not
    # report before the write; the read-back (now with an extension) may
    # reveal it.
    zone_mismatch = ""
    after_names = sorted(after["timezones"])
    if (
        zone_arg is not None
        and course_zone is None
        and any(name != zone_arg.key for name in after_names)
    ):
        revealed = ", ".join(after_names)
        if naive_args:
            zone_mismatch = (
                f"- timezone: {', '.join(naive_args)} had no UTC offset and "
                f"{'was' if len(naive_args) == 1 else 'were'} resolved in "
                f"timezone='{zone_arg.key}', because Gradescope reported no "
                "course timezone before the write, but the extensions page now "
                f"reports {revealed}"
            )
            if after_zone is not None:
                zone_mismatch += " (" + "; ".join(
                    f"{arg} = {value.astimezone(after_zone):%Y-%m-%d %H:%M %Z} "
                    "course time"
                    for arg, value in resolved
                    if arg in naive_args
                ) + ")"
            if after_zone is not None:
                zone_mismatch += (
                    ". If the dates were meant as course-local times, the stored "
                    "dates are wrong: preview the extension again without "
                    "timezone and confirm it."
                )
            else:
                zone_mismatch += (
                    ". If the dates were meant as course-local times, the stored "
                    "dates are wrong: preview the extension again with every "
                    "date given as a UTC offset (no timezone argument) and "
                    "confirm it."
                )
        else:
            summary += (
                f"\n- The extensions page now reports the course timezone "
                f"{revealed}; the dates had UTC offsets, so timezone='"
                f"{zone_arg.key}' only affected how they are shown."
            )
    if zone_mismatch:
        return (
            f"⚠️ Extension for {target} was written (HTTP 200), but its dates "
            "may be in the wrong timezone:\n"
            + zone_mismatch
            + "".join(f"\n{line}" for line in differences)
            + f"\nWhat was sent:\n{summary}"
        )
    if differences:
        return (
            f"⚠️ Extension for {target} was submitted (HTTP 200), but the "
            "extensions page does not show what was sent:\n"
            + "\n".join(differences)
            + "\nCheck the extension in the Gradescope web UI."
        )
    return f"✅ Extension for {target} updated (read back from Gradescope):\n{summary}"

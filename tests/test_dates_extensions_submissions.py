"""Regression tests for date, extension, upload, submission and roster fixes.

Every HTTP call is served by in-memory fakes; nothing touches the network.
"""

from __future__ import annotations

import html
import json
import os
import time
from types import SimpleNamespace

import anyio
import pytest
import requests

from gradescope_mcp import server
from gradescope_mcp.auth import AuthError
from gradescope_mcp.tools import assignments, extensions


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None, json_data=None):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()
        self.headers = headers if headers is not None else {"Content-Type": "text/html; charset=utf-8"}
        self._json = json_data

    def json(self):
        if self._json is not None:
            return self._json
        return json.loads(self.text)


class FakeSession:
    """Routes requests by (method, URL suffix); records every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    def _dispatch(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for route_method, suffix, handler in self.routes:
            if route_method == method and url.endswith(suffix):
                return handler(url, kwargs) if callable(handler) else handler
        raise AssertionError(f"unexpected request {method} {url}")

    def get(self, url, **kwargs):
        return self._dispatch("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._dispatch("POST", url, **kwargs)

    def methods(self) -> list[str]:
        return [method for method, _url, _kw in self.calls]


def _conn(session):
    return SimpleNamespace(session=session, gradescope_base_url="https://gs.test", logged_in=True)


def _call_tool(name: str, args: dict) -> str:
    result = anyio.run(server.mcp.call_tool, name, args)
    return result.content[0].text


def _no_connection():
    raise AssertionError("must not contact Gradescope")


# ---------------------------------------------------------------------------
# modify_assignment_dates (V1-1, V2-2, NEW-V1, V3-15)
# ---------------------------------------------------------------------------


def _edit_form(release, due, late, allow_late) -> str:
    checked = ' checked="checked"' if allow_late else ""
    return f"""
    <form class="edit_assignment" action="/courses/1/assignments/2" method="post">
      <input type="hidden" name="authenticity_token" value="TOKEN">
      <input type="datetime-local" name="assignment[release_date_string]" value="{release}">
      <input type="datetime-local" name="assignment[due_date_string]" value="{due}">
      <input name="assignment[allow_late_submissions]" type="hidden" value="0">
      <input type="checkbox" value="1" name="assignment[allow_late_submissions]"{checked}>
      <input type="datetime-local" name="assignment[hard_due_date_string]" value="{late}">
    </form>
    """


class DateServer:
    """Fake assignment settings form; applies POSTed fields when ``apply``."""

    def __init__(self, *, release="2026-09-01T00:00", due="2026-10-01T23:59",
                 late="2026-10-04T23:59", allow_late=True, apply=True,
                 post_status=200, post_text="<html>Assignment updated</html>",
                 edit_html=None):
        self.state = {"release": release, "due": due, "late": late, "allow_late": allow_late}
        self.apply = apply
        self.post_status = post_status
        self.post_text = post_text
        self.edit_html = edit_html
        self.posted: dict | None = None
        self.session = FakeSession([
            ("GET", "/assignments/2/edit", self._get_edit),
            ("POST", "/courses/1/assignments/2", self._post),
        ])

    def _get_edit(self, _url, _kwargs):
        return FakeResponse(text=self.edit_html or _edit_form(**self.state))

    def _post(self, _url, kwargs):
        self.posted = {name: value for name, (_filename, value) in kwargs["files"]}
        if self.apply and self.post_status < 400:
            self.state = {
                "release": self.posted["assignment[release_date_string]"],
                "due": self.posted["assignment[due_date_string]"],
                "late": self.posted["assignment[hard_due_date_string]"],
                "allow_late": self.posted["assignment[allow_late_submissions]"] == "1",
            }
        return FakeResponse(self.post_status, self.post_text)


def _use(monkeypatch, module, session) -> None:
    monkeypatch.setattr(module, "get_connection", lambda: _conn(session))


def test_modify_dates_preview_shows_every_value_that_will_be_sent(monkeypatch) -> None:
    srv = DateServer()
    _use(monkeypatch, assignments, srv.session)

    text = _call_tool("tool_modify_assignment_dates", {
        "course_id": "1", "assignment_id": "2", "due_date": "2026-10-02T23:59",
    })

    assert "Write confirmation required" in text
    assert "due_date=2026-10-02T23:59 (currently 2026-10-01T23:59)" in text
    assert "release_date=2026-09-01T00:00 (unchanged)" in text
    assert "allow_late_submissions=on (unchanged)" in text
    assert "late_due_date=2026-10-04T23:59 (unchanged)" in text
    assert "course's timezone" in text
    assert srv.session.methods() == ["GET"]


def test_modify_dates_due_only_keeps_release_late_date_and_late_flag(monkeypatch) -> None:
    """V1-1: omitted dates used to go out as '' with allow_late_submissions=0."""
    srv = DateServer()
    _use(monkeypatch, assignments, srv.session)

    text = _call_tool("tool_modify_assignment_dates", {
        "course_id": "1", "assignment_id": "2", "due_date": "2026-10-02T23:59",
        "confirm_write": True,
    })

    assert srv.posted["authenticity_token"] == "TOKEN"
    assert srv.posted["_method"] == "patch"
    assert srv.posted["assignment[release_date_string]"] == "2026-09-01T00:00"
    assert srv.posted["assignment[due_date_string]"] == "2026-10-02T23:59"
    assert srv.posted["assignment[allow_late_submissions]"] == "1"
    assert srv.posted["assignment[hard_due_date_string]"] == "2026-10-04T23:59"
    assert text.startswith("✅")
    assert "read back" in text
    assert "due_date=2026-10-02T23:59 (was 2026-10-01T23:59)" in text
    # GET form, POST, GET form again to verify.
    assert srv.session.methods() == ["GET", "POST", "GET"]


def test_modify_dates_keeps_late_submissions_off(monkeypatch) -> None:
    srv = DateServer(late="", allow_late=False)
    _use(monkeypatch, assignments, srv.session)

    text = assignments.modify_assignment_dates(
        "1", "2", release_date="2026-09-02T08:00", due_date="2026-10-02T23:59",
        confirm_write=True,
    )

    assert srv.posted["assignment[allow_late_submissions]"] == "0"
    assert srv.posted["assignment[hard_due_date_string]"] == ""
    assert text.startswith("✅")


def test_modify_dates_late_due_date_turns_late_submissions_on(monkeypatch) -> None:
    srv = DateServer(late="", allow_late=False)
    _use(monkeypatch, assignments, srv.session)

    preview = assignments.modify_assignment_dates("1", "2", late_due_date="2026-10-05T23:59")
    assert "allow_late_submissions=on (currently off)" in preview

    assignments.modify_assignment_dates(
        "1", "2", late_due_date="2026-10-05T23:59", confirm_write=True
    )
    assert srv.posted["assignment[allow_late_submissions]"] == "1"
    assert srv.posted["assignment[hard_due_date_string]"] == "2026-10-05T23:59"
    assert srv.posted["assignment[due_date_string]"] == "2026-10-01T23:59"


def test_modify_dates_reports_rerendered_form_as_failure(monkeypatch) -> None:
    """V1-1: a 200 page re-rendering the form with errors used to count as success."""
    srv = DateServer(
        apply=False,
        post_text='<form><label><span class="form--requiredFieldStar error">*</span>'
                  "Due date can't be blank</label></form>",
    )
    _use(monkeypatch, assignments, srv.session)

    text = assignments.modify_assignment_dates(
        "1", "2", due_date="2026-10-02T23:59", confirm_write=True
    )

    assert text.startswith("❌")
    assert "due_date: sent 2026-10-02T23:59, Gradescope shows 2026-10-01T23:59" in text
    assert "can't be blank" in text


def test_modify_dates_reports_http_rejection(monkeypatch) -> None:
    srv = DateServer(post_status=422, post_text="unprocessable")
    _use(monkeypatch, assignments, srv.session)

    text = assignments.modify_assignment_dates(
        "1", "2", due_date="2026-10-02T23:59", confirm_write=True
    )

    assert text.startswith("❌")
    assert "HTTP 422" in text


def test_modify_dates_refuses_when_current_dates_cannot_be_read(monkeypatch) -> None:
    srv = DateServer(edit_html='<form><input name="authenticity_token" value="T"></form>')
    _use(monkeypatch, assignments, srv.session)

    text = assignments.modify_assignment_dates(
        "1", "2", due_date="2026-10-02T23:59", confirm_write=True
    )

    assert text.startswith("Error: cannot safely update")
    assert "pass release_date explicitly" in text
    assert "POST" not in srv.session.methods()


def test_modify_dates_rejects_due_date_after_current_late_due_date(monkeypatch) -> None:
    srv = DateServer()
    _use(monkeypatch, assignments, srv.session)

    text = assignments.modify_assignment_dates(
        "1", "2", due_date="2026-10-07T23:59", confirm_write=True
    )

    assert text.startswith("Error: due_date")
    assert "late_due_date" in text
    assert "POST" not in srv.session.methods()


@pytest.mark.parametrize(
    "value, message",
    [
        ("2026-10-02", "has no time of day"),  # NEW-V1: used to become 00:00
        ("2026-10-01T06:59:00+00:00", "includes a UTC offset"),  # V2-2: offset was dropped
        ("2026-10-01T06:59Z", "includes a UTC offset"),
        ("2026-10-01T06:59:30", "whole minutes"),
        ("2026-02-30T10:00", "not a real calendar date"),
    ],
)
def test_modify_dates_rejects_ambiguous_inputs_before_any_request(monkeypatch, value, message) -> None:
    monkeypatch.setattr(assignments, "get_connection", _no_connection)

    text = _call_tool("tool_modify_assignment_dates", {
        "course_id": "1", "assignment_id": "2", "due_date": value, "confirm_write": True,
    })

    assert text.startswith("Error: Invalid date")
    assert message in text


def test_modify_dates_empty_string_means_unchanged(monkeypatch) -> None:
    """V3-15(4): '' used to be rejected as an invalid format."""
    srv = DateServer()
    _use(monkeypatch, assignments, srv.session)

    text = _call_tool("tool_modify_assignment_dates", {
        "course_id": "1", "assignment_id": "2", "release_date": "",
        "due_date": "2026-10-02T23:59", "late_due_date": "  ", "confirm_write": True,
    })

    assert text.startswith("✅")
    assert srv.posted["assignment[release_date_string]"] == "2026-09-01T00:00"
    assert srv.posted["assignment[hard_due_date_string]"] == "2026-10-04T23:59"


def test_modify_dates_preview_without_session_says_it_is_incomplete(monkeypatch) -> None:
    def no_login():
        raise AuthError("Missing Gradescope credentials.")

    monkeypatch.setattr(assignments, "get_connection", no_login)

    preview = assignments.modify_assignment_dates("1", "2", due_date="2026-10-02T23:59")
    assert "Write confirmation required" in preview
    assert "due_date=2026-10-02T23:59" in preview
    assert "could not be read" in preview

    write = assignments.modify_assignment_dates(
        "1", "2", due_date="2026-10-02T23:59", confirm_write=True
    )
    assert write.startswith("Authentication error:")


# ---------------------------------------------------------------------------
# rename_assignment (V3-15(2))
# ---------------------------------------------------------------------------


def test_rename_reports_invalid_title_only_for_invalid_title(monkeypatch) -> None:
    monkeypatch.setattr(assignments, "get_connection", lambda: _conn(object()))

    def invalid_title(**_kwargs):
        raise assignments.InvalidTitleName("Assignment title 'x' is invalid")

    monkeypatch.setattr(assignments, "update_assignment_title", invalid_title)
    assert assignments.rename_assignment("1", "2", "HW 1", confirm_write=True) == (
        "Error: The title 'HW 1' is invalid."
    )

    def bad_header(**_kwargs):
        raise requests.exceptions.InvalidHeader("Invalid leading whitespace in header value")

    monkeypatch.setattr(assignments, "update_assignment_title", bad_header)
    text = assignments.rename_assignment("1", "2", "HW 1", confirm_write=True)
    assert text.startswith("Error renaming assignment:")
    assert "title 'HW 1' is invalid" not in text


# ---------------------------------------------------------------------------
# set_extension (V2-2, NEW-V1, V3-15) and get_extensions (V3-14)
# ---------------------------------------------------------------------------


def _extensions_page(overrides: dict, timezone: str | None = "America/Los_Angeles") -> str:
    rows = []
    for user_id, settings in overrides.items():
        props = {
            "override": {"user_id": int(user_id), "settings": settings},
            "deletePath": f"/x/{user_id}",
            "studentName": "Student",
        }
        if timezone:
            props["timezone"] = {"identifier": timezone}
        rows.append(
            '<tr><td><div data-react-class="EditExtension" data-react-props="'
            f'{html.escape(json.dumps(props))}"></div></td></tr>'
        )
    return f'<table class="table js-overridesTable"><tbody>{"".join(rows)}</tbody></table>'


class ExtensionServer:
    """Fake extensions page; stores POSTed overrides the way they were sent."""

    def __init__(self, overrides=None, timezone="America/Los_Angeles", store=True):
        self.overrides = dict(overrides or {})
        self.timezone = timezone
        self.store = store
        self.posted: list[dict] = []
        self.session = FakeSession([
            ("GET", "/assignments/2/extensions", self._get),
            ("POST", "/assignments/2/extensions", self._post),
        ])

    def _get(self, _url, _kwargs):
        return FakeResponse(text=_extensions_page(self.overrides, self.timezone))

    def _post(self, _url, kwargs):
        body = kwargs["json"]
        self.posted.append(body)
        if self.store:
            settings = {k: v for k, v in body["override"]["settings"].items() if k != "visible"}
            self.overrides[str(body["override"]["user_id"])] = settings
        return FakeResponse(200, "{}")


@pytest.fixture
def host_timezone():
    """Switch the process timezone (what naive datetimes use) and restore it."""
    original = os.environ.get("TZ")

    def set_tz(name: str) -> None:
        os.environ["TZ"] = name
        time.tzset()

    yield set_tz
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


@pytest.mark.parametrize("host_tz", ["UTC", "Asia/Shanghai", "America/New_York"])
def test_set_extension_uses_course_timezone_not_host_timezone(monkeypatch, host_timezone, host_tz) -> None:
    """V2-2: a naive date used to be read in the MCP host's timezone."""
    host_timezone(host_tz)
    srv = ExtensionServer(overrides={"99": {"due_date": {"type": "absolute", "value": "2026-09-30T06:59:00Z"}}})
    _use(monkeypatch, extensions, srv.session)

    text = _call_tool("tool_set_extension", {
        "course_id": "1", "assignment_id": "2", "user_id": "3",
        "due_date": "2026-10-01T23:59", "confirm_write": True,
    })

    assert srv.posted[0]["override"]["settings"]["due_date"] == {
        "type": "absolute", "value": "2026-10-02T06:59:00Z",
    }
    assert text.startswith("✅")
    assert "2026-10-01 23:59 America/Los_Angeles (PDT) = 2026-10-02T06:59:00Z" in text


def test_set_extension_preview_shows_resolved_instant_and_current_extension(monkeypatch) -> None:
    srv = ExtensionServer(overrides={"3": {"due_date": {"type": "absolute", "value": "2026-09-30T06:59:00Z"}}})
    _use(monkeypatch, extensions, srv.session)

    text = extensions.set_extension("1", "2", "3", due_date="2026-10-01T23:59")

    assert "Write confirmation required" in text
    assert "due_date: 2026-10-01 23:59 America/Los_Angeles (PDT) = 2026-10-02T06:59:00Z" in text
    assert "America/Los_Angeles (course timezone reported by Gradescope)" in text
    assert "due_date=2026-09-30T06:59:00Z" in text
    assert srv.posted == []


def test_set_extension_refuses_naive_dates_without_a_known_timezone(monkeypatch) -> None:
    srv = ExtensionServer(overrides={}, timezone=None)
    _use(monkeypatch, extensions, srv.session)

    text = extensions.set_extension("1", "2", "3", due_date="2026-10-01T23:59", confirm_write=True)

    assert text.startswith("Error: cannot tell which timezone due_date is in")
    assert "timezone=" in text
    assert srv.posted == []


def test_set_extension_timezone_argument_and_explicit_offsets(monkeypatch) -> None:
    srv = ExtensionServer(overrides={}, timezone=None)
    _use(monkeypatch, extensions, srv.session)

    text = extensions.set_extension(
        "1", "2", "3", due_date="2026-10-01T23:59", confirm_write=True,
        timezone="America/New_York",
    )
    assert srv.posted[-1]["override"]["settings"]["due_date"]["value"] == "2026-10-02T03:59:00Z"
    assert text.startswith("✅")

    text = extensions.set_extension(
        "1", "2", "4", due_date="2026-10-01T23:59-07:00", confirm_write=True,
    )
    assert srv.posted[-1]["override"]["settings"]["due_date"]["value"] == "2026-10-02T06:59:00Z"
    assert text.startswith("✅")

    assert extensions.set_extension("1", "2", "3", due_date="2026-10-01T23:59", timezone="Mars/Base").startswith(
        "Error: unknown timezone"
    )


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"release_date": "2026-10-05T00:00", "due_date": "2026-10-01T00:00"}, "Dates must be in order"),
        ({"due_date": "2026-10-01T23:59", "late_due_date": "2026-10-03T23:59+00:00"}, "don't mix"),
        ({"due_date": "2026-10-02"}, "has no time of day"),
    ],
)
def test_set_extension_preview_validates_like_the_write(monkeypatch, kwargs, message) -> None:
    """V3-15(3): order and naive/aware mixing used to fail only at confirm_write=True."""
    monkeypatch.setattr(extensions, "get_connection", _no_connection)

    text = extensions.set_extension("1", "2", "3", **kwargs)

    assert text.startswith("Error:")
    assert message in text


def test_set_extension_empty_string_dates_are_unset(monkeypatch) -> None:
    srv = ExtensionServer(overrides={"99": {}})
    _use(monkeypatch, extensions, srv.session)

    text = _call_tool("tool_set_extension", {
        "course_id": "1", "assignment_id": "2", "user_id": "3",
        "release_date": "", "due_date": "2026-10-01T23:59", "late_due_date": "",
        "confirm_write": True,
    })

    assert text.startswith("✅")
    assert set(srv.posted[0]["override"]["settings"]) == {"visible", "due_date"}


def test_set_extension_rejects_nonexistent_local_time(monkeypatch) -> None:
    srv = ExtensionServer(overrides={"9": {}}, timezone="America/New_York")
    _use(monkeypatch, extensions, srv.session)

    text = extensions.set_extension("1", "2", "3", due_date="2026-03-08T02:30")

    assert text.startswith("Error: due_date 2026-03-08T02:30 does not exist in America/New_York")


def test_set_extension_flags_read_back_mismatch(monkeypatch) -> None:
    srv = ExtensionServer(overrides={"99": {}}, store=False)
    _use(monkeypatch, extensions, srv.session)

    text = extensions.set_extension("1", "2", "3", due_date="2026-10-01T23:59", confirm_write=True)

    assert text.startswith("⚠️")
    assert "no extension for this user appears" in text

    # Stored, but with a different instant than was sent.
    srv.overrides["3"] = {"due_date": {"type": "absolute", "value": "2026-10-01T23:59:00Z"}}
    text = extensions.set_extension("1", "2", "3", due_date="2026-10-01T23:59", confirm_write=True)

    assert text.startswith("⚠️")
    assert "due_date: sent 2026-10-02T06:59:00Z, extensions page shows '2026-10-01T23:59:00Z'" in text


def test_set_extension_preview_without_session_says_timezone_is_unresolved(monkeypatch) -> None:
    def no_login():
        raise AuthError("Missing Gradescope credentials.")

    monkeypatch.setattr(extensions, "get_connection", no_login)

    text = extensions.set_extension("1", "2", "3", due_date="2026-10-01T23:59")

    assert "Write confirmation required" in text
    assert "timezone not resolved yet" in text
    assert extensions.set_extension(
        "1", "2", "3", due_date="2026-10-01T23:59", confirm_write=True
    ).startswith("Authentication error:")


@pytest.mark.parametrize(
    "assignment_id, status, expected, unexpected",
    [
        ("4019876", 404, "HTTP 404", "not available"),
        ("1234401", 500, "HTTP 500", "not available"),
        ("5550000", 401, "Extensions are not available", "HTTP 401 for"),
    ],
)
def test_get_extensions_matches_status_code_not_substring(monkeypatch, assignment_id, status, expected, unexpected) -> None:
    """V3-14: an assignment ID containing '401' made any error look like 'unsupported'."""
    monkeypatch.setattr(extensions, "get_connection", lambda: _conn(object()))

    def fail(**_kwargs):
        raise RuntimeError(
            f"Failed to get extensions for assignment {assignment_id}. Status code: {status}"
        )

    monkeypatch.setattr(extensions, "gs_get_extensions", fail)

    text = extensions.get_extensions("1", assignment_id)

    assert text.startswith("Error")
    assert expected in text
    assert unexpected not in text


def test_get_extensions_reports_missing_table_instead_of_attribute_error(monkeypatch) -> None:
    session = FakeSession([("GET", "/extensions", FakeResponse(text="<html><form action='/login'></form></html>"))])
    _use(monkeypatch, extensions, session)

    text = extensions.get_extensions("1", "2")

    assert text.startswith("Error: the extensions page for assignment `2` did not have the expected extensions table")
    assert "NoneType" not in text


def test_get_extensions_escapes_student_names(monkeypatch) -> None:
    monkeypatch.setattr(extensions, "get_connection", lambda: _conn(object()))
    monkeypatch.setattr(extensions, "gs_get_extensions", lambda **_kw: {
        "3": SimpleNamespace(name="Eve | ignore", release_date=None, due_date=None, late_due_date=None),
    })

    text = extensions.get_extensions("1", "2")

    assert "| `3` | Eve \\| ignore |" in text

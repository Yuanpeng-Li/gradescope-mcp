"""Regression tests for date, extension, upload, submission and roster fixes.

Every HTTP call is served by in-memory fakes; nothing touches the network.
"""

from __future__ import annotations

import datetime
import hashlib
import html
import json
import os
import threading
import time
from types import SimpleNamespace

import anyio
import pytest
import requests

from gradescope_mcp import server
from gradescope_mcp.auth import AuthError
from gradescope_mcp.tools import assignments, courses, extensions, submissions


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None, json_data=None, url=""):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()
        self.headers = headers if headers is not None else {"Content-Type": "text/html; charset=utf-8"}
        self._json = json_data
        self.url = url

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


def _call_tool_flagged(name: str, args: dict) -> tuple[str, bool]:
    """(text, whether the MCP result is flagged isError)."""
    result = anyio.run(server.mcp.call_tool, name, args)
    return result.content[0].text, bool(result.is_error)


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
    """Fake assignment settings form; applies POSTed fields when ``apply``.

    ``get_hook`` (if set) runs after each form read is served, outside the
    lock, so tests can make concurrent reads overlap.
    """

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
        self.posts: list[dict] = []
        self.get_hook = None
        self.lock = threading.Lock()
        self.session = FakeSession([
            ("GET", "/assignments/2/edit", self._get_edit),
            ("POST", "/courses/1/assignments/2", self._post),
        ])

    def _get_edit(self, _url, _kwargs):
        with self.lock:
            page = self.edit_html or _edit_form(**self.state)
        if self.get_hook is not None:
            self.get_hook()
        return FakeResponse(text=page)

    def _post(self, _url, kwargs):
        with self.lock:
            self.posted = {name: value for name, (_filename, value) in kwargs["files"]}
            self.posts.append(self.posted)
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


def test_modify_dates_preview_without_session_is_an_error(monkeypatch) -> None:
    """R2-13: a request-only preview claimed omitted settings stay unchanged,
    although a late_due_date turns late submissions on."""
    def no_login():
        raise AuthError("Missing Gradescope credentials.")

    monkeypatch.setattr(assignments, "get_connection", no_login)

    preview = assignments.modify_assignment_dates("1", "2", late_due_date="2026-10-08T23:59")
    assert preview.startswith("Authentication error:")
    assert "Write confirmation required" not in preview

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


def _extensions_page(
    overrides: dict, timezone: str | None = "America/Los_Angeles", names: dict | None = None
) -> str:
    rows = []
    for user_id, settings in overrides.items():
        props = {
            "override": {"user_id": int(user_id), "settings": settings},
            "deletePath": f"/x/{user_id}",
            "studentName": (names or {}).get(user_id, "Student"),
        }
        if timezone:
            props["timezone"] = {"identifier": timezone}
        rows.append(
            '<tr><td><div data-react-class="EditExtension" data-react-props="'
            f'{html.escape(json.dumps(props))}"></div></td></tr>'
        )
    return f'<table class="table js-overridesTable"><tbody>{"".join(rows)}</tbody></table>'


class ExtensionServer:
    """Fake extensions page; stores POSTed overrides the way they were sent.

    A POST replaces the student's override (minus ``visible`` and any
    ``drop_keys``) when ``store``; ``get_hook`` runs after each page read.
    """

    def __init__(self, overrides=None, timezone="America/Los_Angeles", store=True,
                 drop_keys=(), post_status=200):
        self.overrides = {k: dict(v) for k, v in (overrides or {}).items()}
        self.timezone = timezone
        self.store = store
        self.drop_keys = set(drop_keys)
        self.post_status = post_status
        self.posted: list[dict] = []
        self.get_hook = None
        self.lock = threading.Lock()
        self.session = FakeSession([
            ("GET", "/assignments/2/extensions", self._get),
            ("POST", "/assignments/2/extensions", self._post),
        ])

    def _get(self, _url, _kwargs):
        with self.lock:
            page = _extensions_page(self.overrides, self.timezone)
        if self.get_hook is not None:
            self.get_hook()
        return FakeResponse(text=page)

    def _post(self, _url, kwargs):
        body = kwargs["json"]
        with self.lock:
            self.posted.append(json.loads(json.dumps(body)))
            if self.store and self.post_status == 200:
                settings = {
                    k: v for k, v in body["override"]["settings"].items()
                    if k != "visible" and k not in self.drop_keys
                }
                self.overrides[str(body["override"]["user_id"])] = settings
        return FakeResponse(self.post_status, "{}")


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


def test_set_extension_preview_without_session_is_an_error(monkeypatch) -> None:
    """R2-13: the preview can't show the timezone or the merged extension."""
    def no_login():
        raise AuthError("Missing Gradescope credentials.")

    monkeypatch.setattr(extensions, "get_connection", no_login)

    for kwargs in ({"due_date": "2026-10-01T23:59"}, {"due_date": "2026-10-01T23:59Z"}):
        text = extensions.set_extension("1", "2", "3", **kwargs)
        assert text.startswith("Authentication error:")
        assert "Write confirmation required" not in text
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
    page = _extensions_page({"3": {}}, names={"3": "Eve | ignore"})
    session = FakeSession([("GET", "/extensions", FakeResponse(text=page))])
    _use(monkeypatch, extensions, session)

    text = extensions.get_extensions("1", "2")

    assert "| `3` | Eve \\| ignore |" in text


# ---------------------------------------------------------------------------
# upload_submission (V2-7)
# ---------------------------------------------------------------------------


@pytest.fixture
def no_upload(monkeypatch):
    monkeypatch.delenv(submissions.UPLOAD_ROOT_ENV, raising=False)

    def fail_upload(*_args, **_kwargs):
        raise AssertionError("must not upload")

    monkeypatch.setattr(submissions, "upload_assignment", fail_upload)
    monkeypatch.setattr(submissions, "get_connection", lambda: _conn(object()))


def test_upload_refuses_symlinked_dotenv_through_mcp(tmp_path, no_upload) -> None:
    """V2-7 repro: notes.pdf -> .env uploaded the project's credentials."""
    secret = tmp_path / "project" / ".env"
    secret.parent.mkdir()
    secret.write_text("GRADESCOPE_PASSWORD=hunter2\n")
    link = tmp_path / "project" / "notes.pdf"
    link.symlink_to(secret)

    text = _call_tool("tool_upload_submission", {
        "course_id": "1", "assignment_id": "2", "file_paths": [str(link)], "confirm_write": True,
    })

    assert text.startswith("Error: refusing to upload")
    assert "symbolic link" in text


@pytest.mark.parametrize(
    "relative",
    [".env", ".ssh/config", "keys/server.pem", "id_ed25519", "deploy/credentials.json", "prod.env"],
)
def test_upload_refuses_hidden_and_credential_files(tmp_path, no_upload, relative) -> None:
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("secret")

    text = submissions.upload_submission("1", "2", [str(target)], confirm_write=True)

    assert text.startswith("Error: refusing to upload")


def test_upload_refuses_system_files(no_upload) -> None:
    if not os.path.isfile("/proc/self/status"):
        pytest.skip("no /proc on this platform")

    text = submissions.upload_submission("1", "2", ["/proc/self/status"], confirm_write=True)

    assert text.startswith("Error: refusing to upload")


def test_upload_refuses_oversized_files(tmp_path, no_upload, monkeypatch) -> None:
    monkeypatch.setattr(submissions, "_MAX_UPLOAD_BYTES", 4)
    big = tmp_path / "big.pdf"
    big.write_bytes(b"12345")

    text = submissions.upload_submission("1", "2", [str(big)], confirm_write=True)

    assert text.startswith("Error:")
    assert "upload limit" in text


def test_upload_root_restricts_paths_and_allows_links_inside(tmp_path, no_upload, monkeypatch) -> None:
    root = tmp_path / "homework"
    root.mkdir()
    inside = root / "answers.pdf"
    inside.write_bytes(b"%PDF")
    link = root / "link.pdf"
    link.symlink_to(inside)
    outside = tmp_path / "other.pdf"
    outside.write_bytes(b"%PDF")
    monkeypatch.setenv(submissions.UPLOAD_ROOT_ENV, str(root))

    refused = submissions.upload_submission("1", "2", [str(outside)], confirm_write=True)
    assert refused.startswith("Error: refusing to upload")
    assert "outside the allowed upload directory" in refused

    preview = submissions.upload_submission("1", "2", [str(link)])
    assert "Write confirmation required" in preview
    assert f"Uploads are restricted to {submissions.UPLOAD_ROOT_ENV}" in preview


def test_upload_preview_lists_size_and_hash(tmp_path, no_upload) -> None:
    file_path = tmp_path / "hw1.py"
    file_path.write_bytes(b"print('hi')\n")

    text = submissions.upload_submission("1", "2", [str(file_path)])

    assert "Write confirmation required" in text
    digest = hashlib.sha256(b"print('hi')\n").hexdigest()
    assert f"`hw1.py` (12 bytes, sha256 {digest})" in text
    assert "GRADESCOPE_MCP_UPLOAD_ROOT is not set" in text


# ---------------------------------------------------------------------------
# get_assignment_submissions / review_grades fallback (extra defect, V3-2)
# ---------------------------------------------------------------------------


def _review_grades_page(rows: list[str]) -> str:
    return (
        "<table><thead><tr><th></th><th>User</th><th>Last Graded By</th><th>Sections</th>"
        "<th>Score</th><th>Graded?</th><th></th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _rg_row(index, score, flag, sid, index_tag="td") -> str:
    return (
        f"<tr><{index_tag}>{index}</{index_tag}><td>S{index} (s{index}@x.edu)</td><td>TA</td>"
        f"<td>A</td><td>{score}</td><td>{flag}</td>"
        f'<td><a href="/courses/1/assignments/2/submissions/{sid}">view</a></td></tr>'
    )


def test_submissions_html_200_falls_back_to_review_grades(monkeypatch) -> None:
    session = FakeSession([
        ("GET", "/submissions.json", FakeResponse(text="<html>login</html>")),
        ("GET", "/review_grades", FakeResponse(text=_review_grades_page([_rg_row(1, "2.0", "", 11)]))),
    ])
    _use(monkeypatch, submissions, session)

    text = submissions.get_assignment_submissions("1", "2")

    assert "review_grades fallback" in text
    assert "`11`" in text


def test_submissions_json_ids_sort_numerically(monkeypatch) -> None:
    data = {"submissions": {"100": {"graded": True}, "9": {}, "10": {}}}
    session = FakeSession([
        ("GET", "/submissions.json", FakeResponse(text=json.dumps(data), headers={"Content-Type": "application/json"})),
    ])
    _use(monkeypatch, submissions, session)

    text = submissions.get_assignment_submissions("1", "2")

    assert text.index("`9`") < text.index("`10`") < text.index("`100`")


def test_review_grades_scores_flags_and_row_headers(monkeypatch) -> None:
    rows = [
        _rg_row(1, "-1.0", "", 1),        # signed score: graded
        _rg_row(2, ".5", "", 2),          # leading-dot score: graded
        _rg_row(3, "1.0 pts", "", 3),     # score with units: graded
        _rg_row(4, "10.0", "No", 4),      # explicit "No" wins over the score
        _rg_row(5, "", "", 5, "th"),      # <th> row header must not shift columns
        _rg_row(6, "3.0", "", 6, "th"),
    ]
    session = FakeSession([("GET", "/review_grades", FakeResponse(text=_review_grades_page(rows)))])
    conn = _conn(session)

    text = submissions._get_submissions_from_review_grades(conn, "1", "2")

    graded = {
        line.split("`")[1]: line.rstrip(" |").endswith("✅")
        for line in text.splitlines() if line.startswith("| ") and "`" in line
    }
    assert graded == {"1": True, "2": True, "3": True, "4": False, "5": False, "6": True}
    assert "| `6` | 3.0 |" in text


# ---------------------------------------------------------------------------
# get_assignment_graders (V3-13)
# ---------------------------------------------------------------------------


def _question_page(header: list[str], rows: list[list[str]], index_tag="td") -> str:
    head = "".join(f"<th>{h}</th>" for h in header)
    body = "".join(
        f"<tr><{index_tag}>{r[0]}</{index_tag}>" + "".join(f"<td>{c}</td>" for c in r[1:]) + "</tr>"
        for r in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def test_graders_read_by_header_in_seven_column_layout(monkeypatch) -> None:
    page = _question_page(
        ["", "User", "Last Graded By", "Sections", "Score", "Graded?", ""],
        [
            ["1", "Alice (a@x.edu)", "TA Bob", "Sec A", "2.5", "", "link"],
            ["2", "Carol (c@x.edu)", "TA Eve", "Sec B", "3", "", "link"],
            ["3", "Dan (d@x.edu)", "TA Bob", "Sec A", "4.0", "", "link"],
            ["4", "Erin (e@x.edu)", "", "Sec C", "", "", "link"],
        ],
        index_tag="th",
    )
    session = FakeSession([("GET", "/questions/7/submissions", FakeResponse(text=page))])
    _use(monkeypatch, submissions, session)

    text = submissions.get_assignment_graders("1", "7")

    assert "**Total graders:** 2" in text
    assert "- TA Bob (2 submissions)" in text
    assert "- TA Eve (1 submission)" in text
    assert "Alice" not in text and "Sec A" not in text
    assert "**Submissions without a grader:** 1 of 4" in text
    assert "not the list of graders assigned" in text


def test_graders_four_column_layout(monkeypatch) -> None:
    page = _question_page(
        ["#", "Student", "Score", "Grader"],
        [["1", "Alice", "2.5", "TA Bob"], ["2", "Carol Diaz", "3", "TA Eve"]],
    )
    session = FakeSession([("GET", "/questions/7/submissions", FakeResponse(text=page))])
    _use(monkeypatch, submissions, session)

    text = submissions.get_assignment_graders("1", "7")

    assert "- TA Bob (1 submission)" in text
    assert "- TA Eve (1 submission)" in text
    assert "Carol" not in text and "2.5" not in text


def test_graders_without_grader_column_are_not_guessed(monkeypatch) -> None:
    page = "<table><tr><td>Alice</td><td>2.5</td><td>TA Bob</td></tr></table>"
    session = FakeSession([("GET", "/questions/7/submissions", FakeResponse(text=page))])
    _use(monkeypatch, submissions, session)

    text = submissions.get_assignment_graders("1", "7")

    assert text.startswith("Error: the submissions page for question `7` has no")


# ---------------------------------------------------------------------------
# get_course_roster (extra defect)
# ---------------------------------------------------------------------------


def _roster_row(name: str, email: str, with_button: bool = True) -> str:
    cm = html.escape(json.dumps({"full_name": name, "sid": "S1"}))
    button = (
        f'<button class="rosterCell--editIcon" data-cm="{cm}" data-email="{email}" '
        'data-role="0" data-sections="[]"></button>'
        if with_button else ""
    )
    return (
        f'<tr class="rosterRow"><td>{button}'
        f'<button class="js-rosterName" data-url="/x?user_id=42">{name}</button></td>'
        "<td>3</td></tr>"
    )


def test_roster_reports_skipped_rows_and_escapes_cells(monkeypatch) -> None:
    page = (
        '<table class="js-rosterTable"><tr><th>Name</th><th>Submissions</th></tr>'
        + _roster_row("Ann | Admin", "ann@x.edu")
        + _roster_row("Me Myself", "me@x.edu", with_button=False)
        + "</table>"
    )
    session = FakeSession([("GET", "/memberships", FakeResponse(text=page))])
    _use(monkeypatch, courses, session)

    text = courses.get_course_roster("1")

    assert "**Total members:** 1" in text
    assert "1 roster row(s) had no member data" in text
    assert "| Ann \\| Admin | ann@x.edu |" in text


def test_roster_page_without_table_is_an_error(monkeypatch) -> None:
    session = FakeSession([("GET", "/memberships", FakeResponse(text="<html>Log in</html>"))])
    _use(monkeypatch, courses, session)

    assert courses.get_course_roster("1").startswith("Error: the memberships page")


# ===========================================================================
# Round-2 fixes (R2-2, 3, 4, 11, 13, 15, 26, 28, 29)
# ===========================================================================


def _abs(value) -> dict:
    return {"type": "absolute", "value": value}


def _overlapping_reads(parties: int = 2, wait: float = 0.5):
    """A read hook that lets the first ``parties`` reads overlap if they can.

    Unserialized writers all read the same state here; serialized ones can't
    overlap, so the barrier times out and each proceeds on its own.
    """
    barrier = threading.Barrier(parties)
    count = {"n": 0}
    guard = threading.Lock()

    def hook():
        with guard:
            count["n"] += 1
            first_reads = count["n"] <= parties
        if first_reads:
            try:
                barrier.wait(wait)
            except threading.BrokenBarrierError:
                pass

    return hook


# ---------------------------------------------------------------------------
# R2-2: get_extensions shows stored instants correctly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    ["2026-10-02T06:59:00Z", "2026-10-01T23:59:00-07:00", "2026-10-01T23:59:00"],
)
def test_get_extensions_converts_stored_values_to_course_time(monkeypatch, stored) -> None:
    """A UTC value used to be shown as '2026-10-02 06:59 PDT' (offset dropped)."""
    srv = ExtensionServer({"3": {"due_date": _abs(stored)}})
    _use(monkeypatch, extensions, srv.session)

    text, is_error = _call_tool_flagged("tool_get_extensions", {"course_id": "1", "assignment_id": "2"})

    assert not is_error
    row = next(line for line in text.splitlines() if line.startswith("| `3`"))
    assert "| 2026-10-01 23:59 PDT = 2026-10-02T06:59:00Z |" in row
    assert "06:59 PDT" not in text
    assert "course-local wall-clock times (America/Los_Angeles)" in text


def test_get_extensions_lists_other_settings(monkeypatch) -> None:
    srv = ExtensionServer({"3": {"time_limit": _abs(135), "due_date": _abs("2026-10-02T06:59:00Z")}})
    _use(monkeypatch, extensions, srv.session)

    text = extensions.get_extensions("1", "2")

    assert "Other Settings" in text
    assert "| time_limit=135 |" in text


def test_get_extensions_shown_time_round_trips_through_set_extension(monkeypatch) -> None:
    """The manage_extensions_workflow chain: keep the date get_extensions shows."""
    srv = ExtensionServer({"3": {"due_date": _abs("2026-10-02T06:59:00Z")}})
    _use(monkeypatch, extensions, srv.session)

    listing = extensions.get_extensions("1", "2")
    row = next(line for line in listing.splitlines() if line.startswith("| `3`"))
    shown_due = row.split("|")[4].strip()
    keep_due = shown_due[:16].replace(" ", "T")

    text = extensions.set_extension(
        "1", "2", "3", due_date=keep_due, late_due_date="2026-10-03T23:59", confirm_write=True
    )

    assert text.startswith("✅")
    assert srv.overrides["3"]["due_date"]["value"] == "2026-10-02T06:59:00Z"
    assert "(already set before this write)" in text


def test_get_extensions_without_a_single_timezone_shows_stored_values(monkeypatch) -> None:
    first = _extensions_page({"3": {"due_date": _abs("2026-10-02T06:59:00Z")}}, "America/Los_Angeles")
    second = _extensions_page({"4": {"due_date": _abs("2026-10-01T23:59:00")}}, "America/New_York")
    # One table whose two rows report different timezones.
    page = first.replace("</tbody>", second.split("<tbody>")[1].split("</tbody>")[0] + "</tbody>")
    session = FakeSession([("GET", "/extensions", FakeResponse(text=page))])
    _use(monkeypatch, extensions, session)

    text = extensions.get_extensions("1", "2")

    assert "course timezone is unknown" in text
    assert "| 2026-10-02T06:59:00Z |" in text
    assert "2026-10-01T23:59:00 (course-local; timezone unknown)" in text


# ---------------------------------------------------------------------------
# R2-3: set_extension keeps the student's other settings
# ---------------------------------------------------------------------------


def test_set_extension_resends_existing_settings_under_replace_semantics(monkeypatch) -> None:
    """An extended due date and a time limit used to vanish when only a late
    due date was added, and the tool still said ✅."""
    srv = ExtensionServer({"3": {"due_date": _abs("2026-10-03T06:59:00Z"), "time_limit": _abs(135)}})
    _use(monkeypatch, extensions, srv.session)

    text, is_error = _call_tool_flagged("tool_set_extension", {
        "course_id": "1", "assignment_id": "2", "user_id": "3",
        "late_due_date": "2026-10-05T23:59", "confirm_write": True,
    })

    sent = srv.posted[0]["override"]["settings"]
    assert sent["due_date"] == _abs("2026-10-03T06:59:00Z")
    assert sent["time_limit"] == _abs(135)
    assert sent["hard_due_date"] == _abs("2026-10-06T06:59:00Z")
    assert sent["visible"] is True
    assert srv.overrides["3"]["due_date"] == _abs("2026-10-03T06:59:00Z")
    assert text.startswith("✅") and not is_error
    assert "Kept unchanged: due_date=2026-10-03T06:59:00Z, time_limit=135" in text


def test_set_extension_preview_lists_all_current_settings_and_visible(monkeypatch) -> None:
    srv = ExtensionServer({"3": {
        "due_date": _abs("2026-10-03T06:59:00Z"), "time_limit": _abs(135), "visible": False,
    }})
    _use(monkeypatch, extensions, srv.session)

    text = extensions.set_extension("1", "2", "3", due_date="2026-10-03T23:59")

    assert "Write confirmation required" in text
    assert "time_limit=135" in text
    assert "visible=false" in text  # the current value
    assert (
        "the student's whole extension is sent: due_date=2026-10-04T06:59:00Z, "
        "time_limit=135, visible=true." in text
    )
    assert "visible=true is always sent" in text
    assert "(currently 2026-10-02 23:59 America/Los_Angeles (PDT) = 2026-10-03T06:59:00Z)" in text
    assert "Only the dates above are sent" not in text
    assert srv.posted == []


@pytest.mark.parametrize("dropped", ["time_limit", "due_date"])
def test_set_extension_reports_settings_gradescope_dropped(monkeypatch, dropped) -> None:
    srv = ExtensionServer(
        {"3": {"due_date": _abs("2026-10-03T06:59:00Z"), "time_limit": _abs(135)}},
        drop_keys=[dropped],
    )
    _use(monkeypatch, extensions, srv.session)

    text = extensions.set_extension("1", "2", "3", late_due_date="2026-10-05T23:59", confirm_write=True)

    assert text.startswith("⚠️")
    assert f"- {dropped}: was " in text
    assert "(removed)" in text


def test_set_extension_reports_changed_kept_setting(monkeypatch) -> None:
    srv = ExtensionServer({"3": {"time_limit": _abs(135)}})
    _use(monkeypatch, extensions, srv.session)
    original_post = srv._post

    def post_and_alter(url, kwargs):
        response = original_post(url, kwargs)
        srv.overrides["3"]["time_limit"] = _abs(90)
        return response

    srv.session.routes[1] = ("POST", "/assignments/2/extensions", post_and_alter)

    text = extensions.set_extension("1", "2", "3", due_date="2026-10-03T23:59", confirm_write=True)

    assert text.startswith("⚠️")
    assert "time_limit: was 135, re-sent unchanged, but the extensions page now shows 90 (changed)" in text


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(text="<html><form action='/login'></form></html>"),
        FakeResponse(500, "oops"),
    ],
)
@pytest.mark.parametrize("confirm", [False, True])
def test_set_extension_refuses_when_current_extension_is_unreadable(monkeypatch, response, confirm) -> None:
    session = FakeSession([("GET", "/extensions", response)])
    _use(monkeypatch, extensions, session)

    text = extensions.set_extension(
        "1", "2", "3", due_date="2026-10-03T23:59Z", confirm_write=confirm
    )

    assert text.startswith("Error: cannot read the extensions page")
    assert "Nothing was changed" in text
    assert session.methods() == ["GET"]


def test_set_extension_checks_order_against_kept_dates(monkeypatch) -> None:
    srv = ExtensionServer({"3": {"due_date": _abs("2026-10-03T06:59:00Z")}})
    _use(monkeypatch, extensions, srv.session)

    text = extensions.set_extension("1", "2", "3", late_due_date="2026-10-01T23:59", confirm_write=True)

    assert text.startswith("Error: due_date")
    assert "pass the conflicting one as well" in text
    assert srv.posted == []


def test_set_extension_reports_rejected_post(monkeypatch) -> None:
    srv = ExtensionServer({}, post_status=422)
    _use(monkeypatch, extensions, srv.session)

    text, is_error = _call_tool_flagged("tool_set_extension", {
        "course_id": "1", "assignment_id": "2", "user_id": "3",
        "due_date": "2026-10-03T23:59Z", "confirm_write": True,
    })

    assert text.startswith("❌ Failed to set extension (HTTP 422)")
    assert is_error


# ---------------------------------------------------------------------------
# R2-4: concurrent confirmed writes don't lose updates
# ---------------------------------------------------------------------------


def _run_threads(*targets) -> None:
    threads = [threading.Thread(target=target) for target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()


def test_concurrent_date_changes_on_one_assignment_both_survive(monkeypatch) -> None:
    srv = DateServer()
    srv.get_hook = _overlapping_reads()
    _use(monkeypatch, assignments, srv.session)
    out: dict[str, str] = {}

    _run_threads(
        lambda: out.__setitem__("a", assignments.modify_assignment_dates(
            "1", "2", due_date="2026-10-02T23:59", confirm_write=True)),
        lambda: out.__setitem__("b", assignments.modify_assignment_dates(
            "1", "2", late_due_date="2026-10-08T23:59", confirm_write=True)),
    )

    assert out["a"].startswith("✅") and out["b"].startswith("✅")
    assert srv.state["due"] == "2026-10-02T23:59"
    assert srv.state["late"] == "2026-10-08T23:59"


def test_concurrent_extension_changes_for_one_student_both_survive(monkeypatch) -> None:
    srv = ExtensionServer({"9": {}})
    srv.get_hook = _overlapping_reads()
    _use(monkeypatch, extensions, srv.session)
    out: dict[str, str] = {}

    _run_threads(
        lambda: out.__setitem__("a", extensions.set_extension(
            "1", "2", "3", due_date="2026-10-03T23:59", confirm_write=True)),
        lambda: out.__setitem__("b", extensions.set_extension(
            "1", "2", "3", late_due_date="2026-10-05T23:59", confirm_write=True)),
    )

    assert out["a"].startswith("✅") and out["b"].startswith("✅")
    assert srv.overrides["3"]["due_date"] == _abs("2026-10-04T06:59:00Z")
    assert srv.overrides["3"]["hard_due_date"] == _abs("2026-10-06T06:59:00Z")


def test_date_write_waits_for_a_running_write_and_gives_up(monkeypatch) -> None:
    srv = DateServer()
    _use(monkeypatch, assignments, srv.session)
    monkeypatch.setattr(assignments, "_WRITE_LOCK_TIMEOUT", 0.05)

    with assignments.serialized_write("assignment dates", "1", "2"):
        text = assignments.modify_assignment_dates(
            "1", "2", due_date="2026-10-02T23:59", confirm_write=True
        )
        # Previews don't take the lock.
        preview = assignments.modify_assignment_dates("1", "2", due_date="2026-10-02T23:59")
        # Other assignments are not blocked.
        assert assignments.serialized_write("assignment dates", "1", "3") is not None

    assert text.startswith("Error: another date change for assignment `2` is still running")
    assert srv.posts == []
    assert "Write confirmation required" in preview


# ---------------------------------------------------------------------------
# R2-13: previews fail when the current settings can't be read
# ---------------------------------------------------------------------------


def test_date_preview_auth_failure_is_an_mcp_error(monkeypatch) -> None:
    def fail():
        raise AuthError("Gradescope login failed: network error.")

    monkeypatch.setattr(assignments, "get_connection", fail)
    monkeypatch.setattr(extensions, "get_connection", fail)

    text, is_error = _call_tool_flagged("tool_modify_assignment_dates", {
        "course_id": "1", "assignment_id": "2", "late_due_date": "2026-10-08T23:59",
    })
    assert is_error and text.startswith("Authentication error:")
    assert "late-submission setting unchanged" not in text

    text, is_error = _call_tool_flagged("tool_set_extension", {
        "course_id": "1", "assignment_id": "2", "user_id": "3", "due_date": "2026-10-08T23:59",
    })
    assert is_error and text.startswith("Authentication error:")


# ---------------------------------------------------------------------------
# R2-26: a write that finds its values already set doesn't say "unchanged"
# ---------------------------------------------------------------------------


def test_date_write_with_values_already_set_is_not_reported_as_unchanged(monkeypatch) -> None:
    """The second run after a session expiry reads the already-updated form."""
    srv = DateServer(due="2026-10-03T23:59")
    _use(monkeypatch, assignments, srv.session)

    preview = assignments.modify_assignment_dates("1", "2", due_date="2026-10-03T23:59")
    assert "due_date=2026-10-03T23:59 (already the current value)" in preview
    assert "confirming changes nothing" in preview

    text = assignments.modify_assignment_dates(
        "1", "2", due_date="2026-10-03T23:59", confirm_write=True
    )

    assert text.startswith("✅")
    assert "due_date=2026-10-03T23:59 (already set before this write)" in text
    assert "due_date=2026-10-03T23:59 (unchanged)" not in text
    assert "release_date=2026-09-01T00:00 (unchanged)" in text
    assert "an earlier attempt of the same change" in text


def test_late_date_write_counts_the_late_flag_as_requested(monkeypatch) -> None:
    srv = DateServer(late="2026-10-08T23:59", allow_late=True)
    _use(monkeypatch, assignments, srv.session)

    text = assignments.modify_assignment_dates(
        "1", "2", late_due_date="2026-10-08T23:59", confirm_write=True
    )

    assert "allow_late_submissions=on (already set before this write)" in text
    assert "late_due_date=2026-10-08T23:59 (already set before this write)" in text


def test_partial_date_change_has_no_already_set_note(monkeypatch) -> None:
    srv = DateServer()
    _use(monkeypatch, assignments, srv.session)

    text = assignments.modify_assignment_dates(
        "1", "2", due_date="2026-10-01T23:59", late_due_date="2026-10-09T23:59",
        confirm_write=True,
    )

    assert "due_date=2026-10-01T23:59 (already set before this write)" in text
    assert "late_due_date=2026-10-09T23:59 (was 2026-10-04T23:59)" in text
    assert "changed nothing itself" not in text


def test_extension_write_with_values_already_set_says_so(monkeypatch) -> None:
    srv = ExtensionServer({"3": {"due_date": _abs("2026-10-04T06:59:00Z")}})
    _use(monkeypatch, extensions, srv.session)

    text = extensions.set_extension("1", "2", "3", due_date="2026-10-03T23:59", confirm_write=True)

    assert text.startswith("✅")
    assert "(already set before this write)" in text
    assert "an earlier attempt of the same change" in text


# ---------------------------------------------------------------------------
# R2-28 / R2-29: assignment details
# ---------------------------------------------------------------------------


def _assignment(assignment_id="5", **dates):
    return SimpleNamespace(
        assignment_id=assignment_id, name="HW", submissions_status=None, grade=None,
        max_grade=None, release_date=dates.get("release"), due_date=dates.get("due"),
        late_due_date=dates.get("late"),
    )


def _use_assignments(monkeypatch, items) -> None:
    conn = SimpleNamespace(account=SimpleNamespace(get_assignments=lambda _cid: items))
    monkeypatch.setattr(assignments, "get_connection", lambda: conn)


def test_assignment_details_not_found_is_an_mcp_error(monkeypatch) -> None:
    _use_assignments(monkeypatch, [_assignment()])

    text, is_error = _call_tool_flagged("tool_get_assignment_details", {"course_id": "1", "assignment_id": "999"})

    assert text == "Error: assignment `999` not found in course `1`."
    assert is_error


def test_assignment_dates_keep_their_utc_offset(monkeypatch) -> None:
    utc = datetime.datetime(2026, 10, 2, 6, 59, tzinfo=datetime.timezone.utc)
    pdt = datetime.datetime(2026, 10, 1, 23, 59, tzinfo=datetime.timezone(datetime.timedelta(hours=-7)))
    ist = datetime.datetime(2026, 10, 2, 12, 29, tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
    _use_assignments(monkeypatch, [_assignment(release=ist, due=utc, late=pdt)])

    details = assignments.get_assignment_details("1", "5")
    listing = assignments.get_assignments("1")

    assert "- **Due Date:** 2026-10-02 06:59 UTC" in details
    assert "- **Late Due Date:** 2026-10-01 23:59 UTC-07:00" in details
    assert "- **Release Date:** 2026-10-02 12:29 UTC+05:30" in details
    assert "modify_assignment_dates takes course-local wall-clock times" in details
    assert "| 2026-10-02 06:59 UTC |" in listing
    assert "modify_assignment_dates takes course-local wall-clock times" in listing


def test_naive_assignment_dates_are_unchanged_and_have_no_note(monkeypatch) -> None:
    _use_assignments(monkeypatch, [_assignment(due=datetime.datetime(2026, 10, 1, 23, 59))])

    details = assignments.get_assignment_details("1", "5")

    assert "- **Due Date:** 2026-10-01 23:59\n" in details
    assert "UTC" not in details

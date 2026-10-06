"""Regression tests for defects seen against real Gradescope (L1-L6).

The fixtures are synthetic but mirror the page structures observed live:
the assignment settings page that keeps its dates in React props, the group
grade page that redirects to the representative submission in group mode
(or to the grouping overview for a group without confirmed members), the
course box text, the AssignmentEditor outline and AssignmentsTable rows for
assignment containers. Nothing touches the network.
"""

from __future__ import annotations

import html
import io
import json
import re
from types import SimpleNamespace
from urllib.parse import urlsplit

import anyio
import pytest
import requests
from bs4 import BeautifulSoup
from gradescopeapi.classes._helpers._course_helpers import get_courses_info
from requests.adapters import BaseAdapter
from requests.models import Response

from gradescope_mcp import server
from gradescope_mcp.tools import answer_groups, assignments, courses, grading

BASE = "https://gs.test"
C, Q = "878373", "41509447"


def _react(component: str, props, quote: str = '"') -> str:
    raw = props if isinstance(props, str) else json.dumps(props)
    return (
        f'<div data-react-class="{component}" '
        f"data-react-props={quote}{html.escape(raw, quote=True)}{quote}></div>"
    )


def _call_mcp(name: str, args: dict) -> tuple[str, bool]:
    result = anyio.run(server.mcp.call_tool, name, args)
    return result.content[0].text, bool(getattr(result, "is_error", False))


class _Session:
    """GET/POST by URL suffix; records each call."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    def _send(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        for (route_method, suffix), handler in self.routes.items():
            if route_method == method and url.endswith(suffix):
                return handler(url, kwargs) if callable(handler) else handler
        raise AssertionError(f"unexpected request {method} {url}")

    def get(self, url, **kwargs):
        return self._send("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._send("POST", url, **kwargs)

    def methods(self) -> list[str]:
        return [method for method, _url, _kw in self.calls]


def _page(text: str, status: int = 200):
    return SimpleNamespace(status_code=status, text=text, headers={"content-type": "text/html"})


# ---------------------------------------------------------------------------
# L1: modify_assignment_dates reads the dates from SetupDueDateFormGroup props
# ---------------------------------------------------------------------------

PACIFIC = {"abbr": "PDT", "zone": "Pacific Time (US & Canada)", "identifier": "America/Los_Angeles"}


def _settings_page(props, *, extra_inputs: str = "", token: str = "TOKEN") -> str:
    """The real page's shape: one #assignment_form whose date inputs are
    rendered client-side; the current values sit in the component's props."""
    react = "" if props is None else _react("SetupDueDateFormGroup", props, quote="'")
    return f"""<html><head><meta name="csrf-token" content="META-TOKEN">
    <title>Edit Assignment | Gradescope</title></head><body>
    <form id="assignment_form" class="form js-assignmentForm"
          action="/courses/1/assignments/2" method="post">
      <input type="hidden" name="utf8" value="&#x2713;">
      <input type="hidden" name="_method" value="patch">
      <input type="hidden" name="authenticity_token" value="{token}">
      <input type="hidden" name="assignment[type]" value="OnlineAssignment">
      <input type="text" name="assignment[title]" value="Online Assignment">
      <input type="hidden" name="assignment[enforce_time_limit]" value="0">
      <input type="checkbox" name="assignment[enforce_time_limit]" value="1" checked>
      <input type="number" name="assignment[time_limit_in_minutes]" value="60">
      <input type="radio" name="assignment[submission_visibility]" value="always_visible" checked>
      <div id="basic_settings" class="js-settingsTab">
        <div id="assignment-form-dates-and-submission-format">{react}</div>
      </div>
      {extra_inputs}
    </form>
    {_react("SessionTimeoutManager", {"sessionTimeoutMinutes": 30,
                                      "sessionTimeoutFeatureEnabled": False})}
    </body></html>"""


class PropsDateServer:
    """Assignment settings page that keeps its dates in React props.

    A POST of the form fields (the names gradescopeapi uses) is applied to
    the state and shows up in the props of the next page load.
    """

    def __init__(self, *, release="2024-09-24T09:45", due="2024-11-05T09:45",
                 late=None, allow_late=False, lms_sync=False, timezone=PACIFIC,
                 apply=True, props_override=None, extra_inputs=""):
        self.state = {"release": release, "due": due, "late": late,
                      "allow_late": allow_late}
        self.lms_sync = lms_sync
        self.timezone = timezone
        self.apply = apply
        self.props_override = props_override
        self.extra_inputs = extra_inputs
        self.posts: list[dict] = []
        self.session = _Session({
            ("GET", "/courses/1/assignments/2/edit"): self._get,
            ("POST", "/courses/1/assignments/2"): self._post,
        })

    def props(self) -> dict:
        if self.props_override is not None:
            return self.props_override
        props = {
            "releaseDate": self.state["release"],
            "dueDate": self.state["due"],
            "hardDueDateEnabled": self.state["allow_late"],
            "syncLmsDueDate": self.lms_sync,
            "timezone": self.timezone,
        }
        if self.state["allow_late"]:
            props["hardDueDate"] = self.state["late"]
        return props

    def _get(self, _url, _kwargs):
        return _page(_settings_page(self.props(), extra_inputs=self.extra_inputs))

    def _post(self, _url, kwargs):
        posted = {name: value for name, (_filename, value) in kwargs["files"]}
        self.posts.append(posted)
        if self.apply:
            self.state = {
                "release": posted["assignment[release_date_string]"],
                "due": posted["assignment[due_date_string]"],
                "late": posted["assignment[hard_due_date_string]"] or None,
                "allow_late": posted["assignment[allow_late_submissions]"] == "1",
            }
        return _page("<html>Assignment updated</html>")

    def install(self, monkeypatch) -> "PropsDateServer":
        conn = SimpleNamespace(gradescope_base_url=BASE, session=self.session)
        monkeypatch.setattr(assignments, "get_connection", lambda: conn)
        return self


DATE_ARGS = {"course_id": "1", "assignment_id": "2"}


def test_partial_date_update_reads_the_current_dates_from_the_props(monkeypatch) -> None:
    srv = PropsDateServer().install(monkeypatch)

    preview, is_error = _call_mcp(
        "tool_modify_assignment_dates", {**DATE_ARGS, "due_date": "2024-11-06T09:45"}
    )

    assert not is_error, preview
    assert "Write confirmation required" in preview
    assert "release_date=2024-09-24T09:45 (unchanged)" in preview
    assert "due_date=2024-11-06T09:45 (currently 2024-11-05T09:45)" in preview
    assert "allow_late_submissions=off (unchanged)" in preview
    assert "late_due_date=(none) (unchanged); no effect while late submissions are off" in preview
    assert "unreadable" not in preview
    # The IANA name only: the page's abbr (PDT) is wrong for winter dates.
    assert "course's timezone, America/Los_Angeles." in preview
    assert "(PDT)" not in preview
    assert "LMS" not in preview
    assert srv.session.methods() == ["GET"]

    result, is_error = _call_mcp(
        "tool_modify_assignment_dates",
        {**DATE_ARGS, "due_date": "2024-11-06T09:45", "confirm_write": True},
    )

    assert not is_error, result
    (posted,) = srv.posts
    # The upstream form field names, every value set explicitly.
    assert posted["authenticity_token"] == "TOKEN"
    assert posted["_method"] == "patch"
    assert posted["assignment[release_date_string]"] == "2024-09-24T09:45"
    assert posted["assignment[due_date_string]"] == "2024-11-06T09:45"
    assert posted["assignment[allow_late_submissions]"] == "0"
    assert posted["assignment[hard_due_date_string]"] == ""
    # The read-back uses the same props reader and confirms the change.
    assert result.startswith("✅ Assignment `2` dates updated successfully (read back")
    assert "due_date=2024-11-06T09:45 (was 2024-11-05T09:45)" in result
    assert "America/Los_Angeles" in result and "(PDT)" not in result
    assert srv.session.methods() == ["GET", "GET", "POST", "GET"]


def test_enabled_late_due_date_maps_to_allow_late_and_late(monkeypatch) -> None:
    srv = PropsDateServer(
        release="2026-01-05T08:00", due="2026-01-12T09:30",
        late="2026-01-19T09:30", allow_late=True,
    ).install(monkeypatch)

    preview = assignments.modify_assignment_dates("1", "2", due_date="2026-01-13T09:30")

    assert "allow_late_submissions=on (unchanged)" in preview
    assert "late_due_date=2026-01-19T09:30 (unchanged)" in preview

    result = assignments.modify_assignment_dates(
        "1", "2", due_date="2026-01-13T09:30", confirm_write=True
    )

    posted = srv.posts[-1]
    assert posted["assignment[allow_late_submissions]"] == "1"
    assert posted["assignment[hard_due_date_string]"] == "2026-01-19T09:30"
    assert posted["assignment[release_date_string]"] == "2026-01-05T08:00"
    assert result.startswith("✅")


def test_late_date_kept_from_the_props_still_bounds_a_new_due_date(monkeypatch) -> None:
    srv = PropsDateServer(
        due="2026-01-12T09:30", late="2026-01-19T09:30", allow_late=True,
    ).install(monkeypatch)

    result = assignments.modify_assignment_dates(
        "1", "2", due_date="2026-01-20T09:30", confirm_write=True
    )

    assert result.startswith("Error: due_date (2026-01-20T09:30) is after late_due_date")
    assert srv.posts == []


def test_all_dates_preview_compares_with_the_current_values(monkeypatch) -> None:
    PropsDateServer().install(monkeypatch)

    preview = assignments.modify_assignment_dates(
        "1", "2", release_date="2024-09-24T09:45", due_date="2024-11-06T09:45",
        late_due_date="2024-11-08T09:45",
    )

    assert "(current value unreadable)" not in preview
    assert "release_date=2024-09-24T09:45 (already the current value)" in preview
    assert "allow_late_submissions=on (currently off)" in preview
    assert "late_due_date=2024-11-08T09:45 (currently (none))" in preview


def test_lms_synced_due_date_is_flagged_in_preview_and_result(monkeypatch) -> None:
    PropsDateServer(lms_sync=True).install(monkeypatch)

    preview = assignments.modify_assignment_dates("1", "2", due_date="2024-11-06T09:45")
    result = assignments.modify_assignment_dates(
        "1", "2", due_date="2024-11-06T09:45", confirm_write=True
    )

    for text in (preview, result):
        assert "synced from the LMS (syncLmsDueDate is on)" in text
        assert "may overwrite the due date set here" in text


def test_read_back_from_the_props_reports_an_ignored_write(monkeypatch) -> None:
    PropsDateServer(apply=False).install(monkeypatch)

    result = assignments.modify_assignment_dates(
        "1", "2", due_date="2024-11-06T09:45", confirm_write=True
    )

    assert result.startswith("❌ Gradescope did not apply the date change")
    assert "due_date: sent 2024-11-06T09:45, Gradescope shows 2024-11-05T09:45" in result


@pytest.mark.parametrize(
    "props, kept, reason",
    [
        # A value the props don't carry is never guessed.
        ({"dueDate": "2024-11-05T09:45", "hardDueDateEnabled": False},
         "release_date", "have no release_date"),
        ({"releaseDate": "Sept 24", "dueDate": "2024-11-05T09:45", "hardDueDateEnabled": False},
         "release_date", "unrecognized format ('Sept 24')"),
        ({"releaseDate": "2024-09-24T09:45-07:00", "dueDate": "2024-11-05T09:45",
          "hardDueDateEnabled": False},
         "release_date", "unrecognized format"),
        ({"releaseDate": "2024-09-24T09:45", "dueDate": "2024-11-05T09:45"},
         "late_due_date", "don't say whether late submissions are on"),
        ({"releaseDate": "2024-09-24T09:45", "dueDate": "2024-11-05T09:45",
          "hardDueDateEnabled": True},
         "late_due_date", "have no late_due_date"),
        ("{not json", "release_date", "could not be parsed"),
    ],
)
def test_missing_or_unparseable_props_refuse_rather_than_guess(
    monkeypatch, props, kept, reason,
) -> None:
    srv = PropsDateServer(props_override=props).install(monkeypatch)
    if isinstance(props, str):
        srv._get = lambda _u, _k: _page(_settings_page(props))
        srv.session.routes[("GET", "/courses/1/assignments/2/edit")] = srv._get

    for confirm in (False, True):
        result = assignments.modify_assignment_dates(
            "1", "2", due_date="2024-11-06T09:45", confirm_write=confirm
        )
        assert result.startswith("Error: cannot safely update assignment `2`"), result
        assert reason in result
        assert f"pass {kept} explicitly" in result
    assert srv.posts == []


def test_props_take_precedence_over_server_rendered_inputs(monkeypatch) -> None:
    stale_inputs = (
        '<input type="datetime-local" name="assignment[release_date_string]" value="2020-01-01T00:00">'
        '<input type="datetime-local" name="assignment[due_date_string]" value="2020-01-02T00:00">'
    )
    srv = PropsDateServer(extra_inputs=stale_inputs).install(monkeypatch)

    assignments.modify_assignment_dates("1", "2", due_date="2024-11-06T09:45", confirm_write=True)

    assert srv.posts[-1]["assignment[release_date_string]"] == "2024-09-24T09:45"


def test_props_reader_matches_the_observed_page_shape() -> None:
    page = _settings_page({
        "releaseDate": "2024-09-24T09:45", "dueDate": "2024-11-05T09:45",
        "hardDueDateEnabled": False, "syncLmsDueDate": False, "timezone": PACIFIC,
    })
    conn = SimpleNamespace(
        gradescope_base_url=BASE,
        session=_Session({("GET", "/edit"): _page(page)}),
    )

    state = assignments._read_date_form(conn, "1", "2")

    assert state["token"] == "TOKEN"
    assert (state["release"], state["due"], state["late"], state["allow_late"]) == (
        "2024-09-24T09:45", "2024-11-05T09:45", "", False,
    )
    assert state["timezone"]["identifier"] == "America/Los_Angeles"
    assert state["timezone"]["abbr"] == "PDT"
    assert state["lms_sync"] is False
    assert state["problems"] == {}


# ---------------------------------------------------------------------------
# L2 / L3: grade_answer_group against the real redirect structure
# ---------------------------------------------------------------------------

RUBRIC = [
    {"id": 160700841, "description": "Correct", "weight": 0.0},
    {"id": 160700842, "description": "Missing lone pairs", "weight": 2.0},
    {"id": 160700843, "description": "Blank", "weight": 5.0},
]
QUESTION = {"id": int(Q), "weight": 5.0, "scoring_type": "negative",
            "floor": True, "ceiling": False}


class GroupGradescope(BaseAdapter):
    """Transport adapter mirroring the observed group grading pages.

    ``/answer_groups/{g}/grade`` 302-redirects to the representative
    submission's ``/submissions/{sid}/grade?group_mode=true`` (a
    SubmissionGrader in group mode whose save URL already ends in
    ``/save_many_grades``), or to the ``/answer_groups`` overview (an
    AnswerGrouper page) for a group with no confirmed members.
    """

    def __init__(self):
        super().__init__()
        self.groups = [
            {"id": 829644219, "title": "x = 2", "question_type": "complex", "hidden": False},
            {"id": 829644220, "title": "Blank", "question_type": "complex", "hidden": False},
            {"id": 829644221, "title": "x = 3", "question_type": "complex", "hidden": False},
        ]
        self.members = [
            {"id": 2417843897, "confirmed_group_id": 829644219, "graded": False},
            {"id": 2417843899, "confirmed_group_id": 829644219, "graded": False},
            {"id": 2417843901, "unconfirmed_group_id": 829644219, "graded": False},
            {"id": 2417843903, "unconfirmed_group_id": 829644220, "graded": False},
            {"id": 2417843905, "unconfirmed_group_id": 829644220, "graded": False},
            {"id": 2417843907, "confirmed_group_id": 829644221, "graded": False},
        ]
        self.redirects: dict[str, str] = {}
        self.page_patch: dict[str, dict] = {}
        self.log: list[tuple[str, str]] = []
        self.saved: list[tuple[str, dict]] = []

    # --- page builders -------------------------------------------------
    def confirmed(self, gid) -> list[dict]:
        return [m for m in self.members if str(m.get("confirmed_group_id")) == str(gid)]

    def grader_props(self, sid: str) -> dict:
        member = next(m for m in self.members if str(m["id"]) == sid)
        gid = member["confirmed_group_id"]
        group = next(g for g in self.groups if g["id"] == gid)
        props = {
            "group_mode": True,
            "answer_group": gid,
            "answer_group_size": len(self.confirmed(gid)),
            "answer_group_title": group["title"],
            "submission": {"id": int(sid), "owner_names": "Student", "graded": False},
            "question": dict(QUESTION),
            "rubric_items": [dict(ri) for ri in RUBRIC],
            "rubric_item_evaluations": [],
            "evaluation": {"points": None, "comments": None},
            "pages": [{"number": 1, "url": "https://s3.example/page1.jpg?X-Amz-Signature=abc"}],
            "crop_rects": [{"page_number": 1, "x1": 7.4, "x2": 90.2, "y1": 12.0, "y2": 40.5}],
            "urls": {
                "save_grade": f"/courses/{C}/questions/{Q}/submissions/{sid}/save_many_grades",
            },
        }
        props.update(self.page_patch.get(sid, {}))
        return props

    def overview_html(self) -> str:
        props = {
            "groups": self.groups, "submissions": self.members, "status": "ready",
            "urls": {"answer_groups_url": f"/courses/{C}/questions/{Q}/answer_groups"},
        }
        return (
            '<html><head><meta name="csrf-token" content="CSRF"><title>Review Answers '
            f'| Gradescope</title></head><body>{_react("AnswerGrouper", props)}</body></html>'
        )

    def grader_html(self, sid: str) -> str:
        return (
            '<html><head><meta name="csrf-token" content="CSRF-GROUP"></head><body>'
            f'{_react("SubmissionGrader", self.grader_props(sid))}</body></html>'
        )

    # --- transport -----------------------------------------------------
    def _respond(self, request, status: int, body: str = "", headers=None) -> Response:
        resp = Response()
        resp.request = request
        resp.url = request.url
        resp.status_code = status
        resp.encoding = "utf-8"
        resp.headers.update(headers or {})
        data = body.encode()
        resp.raw = io.BytesIO(data)
        resp._content = data
        resp._content_consumed = True
        return resp

    def send(self, request, **kwargs):
        parts = urlsplit(request.url)
        path, query = parts.path, parts.query
        self.log.append((request.method, path + (f"?{query}" if query else "")))
        prefix = f"/courses/{C}/questions/{Q}"
        if request.method == "GET" and path == f"{prefix}/answer_groups":
            if "application/json" in request.headers.get("Accept", ""):
                body = json.dumps({"groups": self.groups, "submissions": self.members,
                                   "status": "ready", "question": {"id": int(Q)}})
                return self._respond(request, 200, body, {"content-type": "application/json"})
            return self._respond(request, 200, self.overview_html())
        m = re.fullmatch(rf"{prefix}/answer_groups/(\d+)/grade", path)
        if request.method == "GET" and m:
            gid = m.group(1)
            target = self.redirects.get(gid)
            if target is None:
                confirmed = self.confirmed(gid)
                target = (
                    f"{prefix}/submissions/{confirmed[0]['id']}/grade?group_mode=true"
                    if confirmed else f"{prefix}/answer_groups"
                )
            return self._respond(request, 302, headers={"Location": BASE + target})
        m = re.fullmatch(rf"{prefix}/submissions/(\d+)/grade", path)
        if request.method == "GET" and m and query == "group_mode=true":
            return self._respond(request, 200, self.grader_html(m.group(1)))
        m = re.fullmatch(rf"{prefix}/submissions/(\d+)/save_many_grades", path)
        if request.method == "POST" and m:
            payload = json.loads(request.body)
            self.saved.append((m.group(1), payload))
            member = next(x for x in self.members if str(x["id"]) == m.group(1))
            for other in self.confirmed(member["confirmed_group_id"]):
                other["graded"] = True
            return self._respond(request, 200, '{"status": "ok"}',
                                 {"content-type": "application/json"})
        return self._respond(request, 404, "not found")

    def close(self):
        pass

    def install(self, monkeypatch) -> "GroupGradescope":
        session = requests.Session()
        session.mount("https://", self)
        conn = SimpleNamespace(gradescope_base_url=BASE, session=session)
        monkeypatch.setattr(answer_groups, "get_connection", lambda: conn)
        return self

    def posts(self) -> list[str]:
        return [path for method, path in self.log if method == "POST"]


GROUP = "829644219"
GROUP_ARGS = {"course_id": C, "question_id": Q, "group_id": GROUP,
              "rubric_item_ids": ["160700842"]}
SAVE_MANY = f"/courses/{C}/questions/{Q}/submissions/2417843897/save_many_grades"


def test_group_mode_page_with_a_save_many_grades_url_is_accepted(monkeypatch) -> None:
    gs = GroupGradescope().install(monkeypatch)

    preview, is_error = _call_mcp("tool_grade_answer_group", GROUP_ARGS)

    assert not is_error, preview
    assert "Write confirmation required" in preview
    assert f"endpoint: POST {SAVE_MANY}" in preview
    assert "group_size=2 confirmed submissions" in preview
    assert "inferred_members=1" in preview
    assert "projected score per member: 3/5.0 (negative scoring" in preview
    assert (
        "GET", f"/courses/{C}/questions/{Q}/submissions/2417843897/grade?group_mode=true"
    ) in gs.log
    assert gs.posts() == []

    result, is_error = _call_mcp(
        "tool_grade_answer_group",
        {**GROUP_ARGS, "confirm_write": True, "expected_member_count": 3},
    )

    assert not is_error, result
    assert gs.posts() == [SAVE_MANY]
    ((sid, payload),) = gs.saved
    assert sid == "2417843897"
    assert payload == {
        "rubric_items": {
            "160700841": {"score": "false"},
            "160700842": {"score": "true"},
            "160700843": {"score": "false"},
        },
        "question_submission_evaluation": {},
    }
    assert result.startswith(f"✅ Batch grade saved for answer group `{GROUP}`")
    assert "**Read-back:** 2/2 confirmed members graded" in result


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"group_mode": False}, "the page is not in group mode"),
        ({"group_mode": "true"}, "the page is not in group mode"),
        ({"submission": {"id": 2417843899}},
         "its submission `2417843897` is not the page's submission (`2417843899`)"),
        # An inferred member: the round-2 check already refuses it.
        ({"urls": {"save_grade": f"/courses/{C}/questions/{Q}/submissions/2417843901/save_many_grades"},
          "submission": {"id": 2417843901}},
         "`2417843901`, which is not a confirmed member of answer group `829644219`"),
        # Not listed at all: only the group-mode check knows to refuse it.
        ({"urls": {"save_grade": f"/courses/{C}/questions/{Q}/submissions/999/save_many_grades"},
          "submission": {"id": 999}},
         "its submission `999` is not a confirmed member of answer group"),
        ({"urls": {"save_grade": f"https://evil.test/courses/{C}/questions/{Q}/submissions/2417843897/save_many_grades"}},
         "unexpected save URL"),
    ],
)
def test_group_mode_save_url_is_refused_unless_tied_to_this_group(
    monkeypatch, patch, message,
) -> None:
    gs = GroupGradescope().install(monkeypatch)
    gs.page_patch["2417843897"] = patch

    for extra in ({}, {"confirm_write": True, "expected_member_count": 3}):
        result = answer_groups.grade_answer_group(**GROUP_ARGS, **extra)
        assert result.startswith("Error:"), result
        assert message in result
    assert gs.posts() == []


def test_group_mode_page_without_answer_group_is_refused(monkeypatch) -> None:
    gs = GroupGradescope().install(monkeypatch)
    gs.page_patch["2417843897"] = {"answer_group": None}

    result = answer_groups.grade_answer_group(**GROUP_ARGS)

    # Landing on a submission page that names no group is the round-2 check.
    assert result.startswith(f"Error: the grade page for answer group `{GROUP}` redirected to")
    assert gs.posts() == []

    # Served without a redirect, the group-mode check refuses it as well.
    props = gs.grader_props("2417843897")
    problem = answer_groups._group_mode_problem(
        props["urls"]["save_grade"], {**props, "answer_group": None}, C, Q, GROUP, gs.members,
    )
    assert problem == "the page does not name its answer group"


def test_redirect_to_another_groups_group_mode_page_is_refused(monkeypatch) -> None:
    gs = GroupGradescope().install(monkeypatch)
    gs.redirects[GROUP] = f"/courses/{C}/questions/{Q}/submissions/2417843907/grade?group_mode=true"

    for extra in ({}, {"confirm_write": True, "expected_member_count": 3}):
        result = answer_groups.grade_answer_group(**GROUP_ARGS, **extra)
        assert result.startswith(
            f"Error: the grade page loaded for answer group `{GROUP}` belongs to "
            "answer group `829644221`"
        ), result
    assert gs.posts() == []


def test_group_mode_save_url_in_another_question_is_refused(monkeypatch) -> None:
    gs = GroupGradescope().install(monkeypatch)
    gs.page_patch["2417843897"] = {
        "urls": {"save_grade": f"/courses/{C}/questions/1/submissions/2417843897/save_many_grades"},
    }

    result = answer_groups.grade_answer_group(**GROUP_ARGS)

    assert result.startswith("Error: the group grade page's save URL")
    assert f"is not in course `{C}`, question `{Q}`" in result
    assert gs.posts() == []


def test_group_without_confirmed_members_is_refused_before_its_page_is_fetched(
    monkeypatch,
) -> None:
    gs = GroupGradescope().install(monkeypatch)
    args = {**GROUP_ARGS, "group_id": "829644220"}

    for extra in ({}, {"confirm_write": True, "expected_member_count": 2}):
        result, is_error = _call_mcp("tool_grade_answer_group", {**args, **extra})
        assert is_error
        assert result.startswith("Error: answer group `829644220` has 0 confirmed members (2 inferred)")
        assert "Gradescope offers no group grading page" in result
        assert "answer-grouping UI" in result
        assert "grade the submissions individually" in result
        assert "`2417843903`, `2417843905`" in result
        assert "component not found" not in result
    assert not any("/answer_groups/829644220/grade" in path for _m, path in gs.log)
    assert gs.posts() == []


def test_grade_page_landing_on_the_grouping_overview_says_so(monkeypatch) -> None:
    gs = GroupGradescope().install(monkeypatch)
    # E.g. the members were unconfirmed between the JSON read and the page load.
    gs.redirects[GROUP] = f"/courses/{C}/questions/{Q}/answer_groups"

    result = answer_groups.grade_answer_group(**GROUP_ARGS)

    assert result.startswith(
        f"Error: the grade page for answer group `{GROUP}` redirected to the "
        f"answer-grouping overview (`/courses/{C}/questions/{Q}/answer_groups`)"
    )
    assert "Nothing was sent" in result
    assert "component not found" not in result
    assert gs.posts() == []


def test_grouping_overview_is_recognized_by_its_component_too() -> None:
    soup = BeautifulSoup(GroupGradescope().overview_html(), "html.parser")
    resp = SimpleNamespace(url=f"{BASE}/courses/{C}/questions/{Q}/answer_groups/1/grade")

    path = answer_groups._grouping_overview_path(resp, soup, C, Q)

    assert path == f"/courses/{C}/questions/{Q}/answer_groups/1/grade"
    other = BeautifulSoup("<html><body>Something else</body></html>", "html.parser")
    assert answer_groups._grouping_overview_path(resp, other, C, Q) is None


# ---------------------------------------------------------------------------
# L4: list_courses separates the course box's glued texts
# ---------------------------------------------------------------------------


def _account_page() -> str:
    def box(cid, short, name, count_html):
        return (
            f'<a class="courseBox" href="/courses/{cid}">'
            f'<h3 class="courseBox--shortname">{short}</h3>'
            f'<div class="courseBox--name">{name}</div>'
            f'<div class="courseBox--assignments">{count_html}</div></a>'
        )

    return (
        '<html><body><button class="js-createNewCourse">Create</button>'
        '<div id="account-show"><h2 class="pageHeading">Instructor Courses</h2>'
        '<div class="courseList"><div class="courseList--term">Fall 2026</div>'
        '<div class="courseList--coursesForTerm">'
        + box("111", "DEMO 1", "Demo One", '1 assignment<span class="courseBox--noGradesPublished">No Published Grades</span>')
        + box("222", "DEMO 2", "Demo Two", "5 assignments")
        + box("333", "DEMO 3", "Demo Three", '22 assignments<span>No Published Grades</span>')
        + "</div></div></div></body></html>"
    )


def test_list_courses_separates_the_assignment_count_from_the_grades_label(monkeypatch) -> None:
    parsed = get_courses_info(BeautifulSoup(_account_page(), "html.parser"))
    # The upstream parser glues the texts together.
    assert parsed["instructor"]["111"].num_assignments == "1 assignmentNo Published Grades"
    conn = SimpleNamespace(account=SimpleNamespace(get_courses=lambda: parsed))
    monkeypatch.setattr(courses, "get_connection", lambda: conn)

    result = courses.list_courses()

    assert "  - Assignments: 1 assignment · No Published Grades" in result
    assert "  - Assignments: 5 assignments\n" in result
    assert "  - Assignments: 22 assignments · No Published Grades" in result
    assert "assignmentNo" not in result


@pytest.mark.parametrize(
    "raw, shown",
    [
        ("1 assignment", "1 assignment"),
        ("5 assignments", "5 assignments"),
        ("8 assignmentsNo Published Grades", "8 assignments · No Published Grades"),
        ("3 assignments\n  No Published Grades", "3 assignments · No Published Grades"),
        (4, "4"),
        (None, "N/A"),
    ],
)
def test_assignment_count_text(raw, shown) -> None:
    assert courses._assignment_count_text(raw) == shown


# ---------------------------------------------------------------------------
# L5: outline type for online assignments
# ---------------------------------------------------------------------------


def _outline_conn(page: str):
    return SimpleNamespace(
        gradescope_base_url=BASE,
        session=_Session({("GET", "/outline/edit"): _page(page)}),
    )


def test_online_assignment_outline_reports_online_assignment(monkeypatch) -> None:
    props = {
        # AssignmentEditor props carry no assignment type field.
        "assignment": {"id": 5030450, "title": "Online Assignment"},
        "questions": {
            "41509407": {"id": 41509407, "title": "Math", "weight": 4.0, "index": 1,
                         "parent_id": None, "type": "QuestionGroup", "content": []},
            "41509408": {"id": 41509408, "title": "", "weight": 2.0, "index": 1,
                         "parent_id": 41509407, "type": "OnlineQuestion",
                         "content": [{"type": "text", "value": "Which number is odd?"}]},
        },
    }
    page = f"<html><body>{_react('AssignmentEditor', props)}</body></html>"
    monkeypatch.setattr(grading, "get_connection", lambda: _outline_conn(page))

    result = grading.get_assignment_outline("878372", "5030450")

    assert "**Type:** Online assignment" in result
    assert "Unknown" not in result
    assert "| 1.1 | `41509408` | 2.0 | OnlineQuestion | Which number is odd? |" in result


def test_pdf_assignment_outline_keeps_the_reported_type(monkeypatch) -> None:
    props = {
        "assignment": {"id": 5030457, "type": "PDFAssignment"},
        "outline": [
            {"id": 41509447, "title": "Chemistry: Lewis Structure", "weight": 5.0,
             "index": 1, "children": []},
        ],
    }
    page = f"<html><body>{_react('AssignmentOutline', props)}</body></html>"
    monkeypatch.setattr(grading, "get_connection", lambda: _outline_conn(page))

    result = grading.get_assignment_outline(C, "5030457")

    assert "**Type:** PDFAssignment" in result
    assert "**Question ID:** `41509447`" in result


def test_editor_outline_with_a_type_keeps_it(monkeypatch) -> None:
    props = {"assignment": {"type": "ProgrammingAssignment"},
             "questions": {"1": {"id": 1, "title": "Q", "weight": 1.0, "index": 1}}}
    page = f"<html><body>{_react('AssignmentEditor', props)}</body></html>"
    monkeypatch.setattr(grading, "get_connection", lambda: _outline_conn(page))

    assert "**Type:** ProgrammingAssignment" in grading.get_assignment_outline("1", "2")


# ---------------------------------------------------------------------------
# L6: get_assignments names the assignment containers it can't list
# ---------------------------------------------------------------------------


def _assignments_page(rows: list[dict]) -> str:
    return (
        "<html><body>"
        + _react("AssignmentsTable", {"table_data": rows, "sections": []})
        + "</body></html>"
    )


ASSIGNMENT_ROW = {
    "id": "assignment_5030450", "type": "assignment", "title": "Online Assignment",
    "url": "/courses/878372/assignments/5030450", "total_points": "18.0",
    "submission_window": {
        "release_date": "2024-09-24T09:45:00.000-07:00",
        "due_date": "2024-11-05T09:45:00.000-08:00",
        "hard_due_date": None,
    },
}
CONTAINER_ROW = {
    "id": "assignment_container_901", "type": "assignment_container",
    "title": "Bubble Sheet Exam", "url": "/courses/878372/assignment_containers/901",
}


def _listing_conn(routes: dict):
    session = _Session(routes)
    return SimpleNamespace(gradescope_base_url=BASE, session=session), session


def test_get_assignments_lists_containers_in_a_note_without_extra_requests(monkeypatch) -> None:
    conn, session = _listing_conn({
        ("GET", "/courses/878372/assignments"): _page(_assignments_page([ASSIGNMENT_ROW, CONTAINER_ROW])),
    })
    monkeypatch.setattr(assignments, "get_connection", lambda: conn)

    result = assignments.get_assignments("878372")

    assert "| 1 | Online Assignment | `5030450` | 2024-09-24 09:45 UTC-07:00 |" in result
    assert "**Total assignments:** 1" in result
    assert "**Assignment containers (not listed or counted above):** 1." in result
    assert (
        "- Bubble Sheet Exam: container `901` "
        "(`/courses/878372/assignment_containers/901`)"
    ) in result
    assert "can't be used as an assignment_id" in result
    assert session.methods() == ["GET"]


def test_get_assignments_without_containers_has_no_note(monkeypatch) -> None:
    conn, _ = _listing_conn({
        ("GET", "/courses/878372/assignments"): _page(_assignments_page([ASSIGNMENT_ROW])),
    })
    monkeypatch.setattr(assignments, "get_connection", lambda: conn)

    result = assignments.get_assignments("878372")

    assert "**Total assignments:** 1" in result
    assert "container" not in result


def test_course_with_only_a_container_says_so(monkeypatch) -> None:
    conn, _ = _listing_conn({
        ("GET", "/courses/878372/assignments"): _page(_assignments_page([CONTAINER_ROW])),
    })
    monkeypatch.setattr(assignments, "get_connection", lambda: conn)

    result = assignments.get_assignments("878372")

    assert result.startswith("No assignments found for course `878372`.")
    assert "Bubble Sheet Exam: container `901`" in result


def test_get_assignments_still_falls_back_to_the_student_course_page(monkeypatch) -> None:
    student_page = (
        "<html><body><table><tr role='row'><th>Name</th><td>Status</td></tr>"
        "<tr role='row'><th><a href='/courses/878372/assignments/42/submissions/7'>HW 1</a></th>"
        "<td>8.0 / 10.0</td><td></td></tr>"
        "<tr role='row'><th></th><td></td></tr></table></body></html>"
    )
    conn, session = _listing_conn({
        ("GET", "/courses/878372/assignments"): _page(
            '{"error": "You are not authorized to access this page."}', status=401
        ),
        ("GET", "/courses/878372"): _page(student_page),
    })
    monkeypatch.setattr(assignments, "get_connection", lambda: conn)

    result = assignments.get_assignments("878372")

    assert "| 1 | HW 1 | `42` |" in result
    assert "| Submitted | 8.0/10.0 |" in result
    assert session.methods() == ["GET", "GET"]


def test_get_assignments_reports_an_unreadable_listing_as_an_error(monkeypatch) -> None:
    conn, _ = _listing_conn({("GET", "/courses/878372/assignments"): _page("oops", status=500)})
    monkeypatch.setattr(assignments, "get_connection", lambda: conn)

    result, is_error = _call_mcp("tool_get_assignments", {"course_id": "878372"})

    assert is_error
    assert result.startswith("Error fetching assignments: Gradescope answered")


def test_timezone_text_never_shows_a_seasonal_abbreviation_as_fixed() -> None:
    zone = {"abbr": "PDT", "zone": "Pacific Time (US & Canada)", "identifier": "America/Los_Angeles"}
    assert assignments._timezone_text(zone) == "America/Los_Angeles"
    no_identifier = {"abbr": "PDT", "zone": "Pacific Time (US & Canada)"}
    assert assignments._timezone_text(no_identifier) == "Pacific Time (US & Canada) (currently PDT)"
    assert assignments._timezone_text(None) is None

"""Regression tests for the workflow-helper and artifact-cache review fixes.

Each test reproduces a verified defect (V4-x / V2-4 / NEW-V4) offline. HTTP
goes through a real ``requests.Session`` whose transport adapter is a fake
Gradescope, so session headers, cookies and redirects behave as in
production while nothing touches the network.
"""

from __future__ import annotations

import html
import inspect
import io
import json
import os
import re
import stat
import tempfile
import types
from urllib.parse import urlsplit

import anyio
import pytest
import requests
from gradescopeapi.classes.account import Account
from requests.adapters import BaseAdapter
from requests.models import Response
from requests.structures import CaseInsensitiveDict

from gradescope_mcp import cache, server
from gradescope_mcp.tools import common, grading, grading_ops
from gradescope_mcp.tools import grading_workflow as gw

BASE = "https://www.gradescope.com"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
LOGIN_HTML = "<html><body><form action='/login'>Log In</form></body></html>"


# ---------------------------------------------------------------------------
# Fake Gradescope
# ---------------------------------------------------------------------------

def _react(cls: str, props: dict) -> str:
    return (
        '<html><head><meta name="csrf-token" content="page-tok"></head><body>'
        f'<div data-react-class="{cls}" '
        f'data-react-props="{html.escape(json.dumps(props))}"></div></body></html>'
    )


class _Adapter(BaseAdapter):
    def __init__(self, world: "World"):
        super().__init__()
        self.world = world
        self.log: list[dict] = []

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        self.log.append(
            {"method": request.method, "url": request.url, "headers": dict(request.headers)}
        )
        status, body, headers = self.world.route(request)
        resp = Response()
        resp.status_code = status
        resp.raw = io.BytesIO(body if isinstance(body, bytes) else body.encode())
        resp.headers = CaseInsensitiveDict(headers or {})
        resp.url = request.url
        resp.request = request
        resp.encoding = "utf-8"
        return resp

    def close(self) -> None:
        pass


class World:
    """assignments: {aid: {"questions": {...}, "outline": props | None,
    "outline_status": int, "grade_json": (status, body, headers) override}}."""

    def __init__(self, assignments, props, submissions_html=None, pages=None):
        self.assignments = assignments
        self.props = props
        self.submissions_html = submissions_html or {}
        self.pages = pages or {}

    def route(self, req):
        parts = urlsplit(req.url)
        host, path = parts.netloc, parts.path
        if host != "www.gradescope.com" or path.startswith("/files/"):
            return self.pages.get(req.url, (200, JPEG, {"Content-Type": "image/jpeg"}))
        m = re.fullmatch(r"/courses/(\d+)/assignments", path)
        if m:
            rows = [
                {
                    "type": "assignment",
                    "url": f"/courses/{m.group(1)}/assignments/{aid}",
                    "title": f"A {aid}",
                    "submission_window": {"release_date": None, "due_date": None},
                    "total_points": 10,
                }
                for aid in self.assignments
            ]
            return 200, _react("AssignmentsTable", {"table_data": rows}), {}
        m = re.fullmatch(r"/courses/(\d+)/assignments/(\d+)/grade\.json", path)
        if m:
            a = self.assignments.get(m.group(2))
            if a is None:
                return 404, '{"error": "not found"}', {"Content-Type": "application/json"}
            if "grade_json" in a:
                return a["grade_json"]
            body = {"assignments": {m.group(2): {"questions": a["questions"]}}}
            return 200, json.dumps(body), {"Content-Type": "application/json"}
        m = re.fullmatch(r"/courses/(\d+)/assignments/(\d+)/outline/edit", path)
        if m:
            a = self.assignments.get(m.group(2))
            if a is None:
                return 404, "nf", {}
            if a.get("outline_status", 200) != 200:
                return a["outline_status"], "Forbidden", {"Content-Type": "text/html"}
            if a.get("outline") is None:
                return 200, "<html>no component</html>", {"Content-Type": "text/html"}
            return 200, _react("AssignmentEditor", a["outline"]), {"Content-Type": "text/html"}
        m = re.fullmatch(r"/courses/(\d+)/questions/(\d+)/submissions", path)
        if m:
            return 200, self.submissions_html.get(m.group(2), "<html></html>"), {}
        m = re.fullmatch(r"/courses/(\d+)/questions/(\d+)/submissions/([^/]*)/grade", path)
        if m:
            props = self.props(m.group(2), m.group(3)) if callable(self.props) else self.props
            if props is None:
                return 404, "nf", {}
            return 200, _react("SubmissionGrader", props), {"Content-Type": "text/html"}
        return 404, "nf", {}


def _install(monkeypatch, world: World):
    session = requests.Session()
    adapter = _Adapter(world)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    # Mimic gradescopeapi's login: session-wide CSRF header + domain cookie.
    session.headers.update({"X-CSRF-Token": "SESSION-CSRF-SECRET"})
    session.cookies.set("_gradescope_session", "COOKIE", domain="www.gradescope.com", path="/")
    conn = types.SimpleNamespace(
        session=session, gradescope_base_url=BASE, logged_in=True,
        account=Account(session, BASE),
    )
    for module in (gw, grading_ops, grading):
        monkeypatch.setattr(module, "get_connection", lambda: conn)
    return conn, adapter


def _call(tool: str, args: dict) -> str:
    result = anyio.run(server.mcp.call_tool, tool, args)
    return "\n".join(c.text for c in result.content)


def _props(pages=(), crop=(), rubric=(), scoring="negative", answers=None, **question):
    q = {"weight": 4, "type": "FreeResponseQuestion", "scoring_type": scoring,
         "parameters": {"crop_rect_list": list(crop)}, **question}
    submission = {"owner_names": "Alice"}
    if answers is not None:
        submission["answers"] = answers
    return {"question": q, "submission": submission, "rubric_items": list(rubric),
            "pages": list(pages), "evaluation": {}, "rubric_item_evaluations": []}


Q = {"11": {"id": 11, "index": 1, "weight": 4, "type": "FreeResponseQuestion", "parent_id": None}}
ARGS = {"course_id": "1", "assignment_id": "7", "question_id": "11", "submission_id": "21"}
RUBRIC = [{"id": 1, "description": "Correct", "weight": 0}]


def _readiness(tool_output: str) -> tuple[str, str]:
    for pattern in (
        r"Readiness: `([\d.]+)` \((\w+)\)",
        r"readiness: `([\d.]+)`\n- status: `(\w+)`",
        r"Readiness:\*\* `([\d.]+)` → `(\w+)`",
    ):
        m = re.search(pattern, tool_output)
        if m:
            return m.groups()
    raise AssertionError(f"no readiness in output:\n{tool_output}")


def _three_tools(monkeypatch, props, outline_content=(), outline=True):
    outline_props = {"questions": {"11": {"content": list(outline_content)}}} if outline else None
    world = World({"7": {"questions": Q, "outline": outline_props}}, props)
    _install(monkeypatch, world)
    return (
        _call("tool_prepare_grading_artifact", ARGS),
        _call("tool_assess_submission_readiness", ARGS),
        _call("tool_smart_read_submission", ARGS),
    )


PROMPT_AND_EXPLANATION = [
    {"type": "text", "value": "Explain X."},
    {"type": "explanation", "value": "X is Y."},
]


# ---------------------------------------------------------------------------
# V4-1 — readiness is pre-read context availability, not an auto-grade gate
# ---------------------------------------------------------------------------

def test_readiness_zero_pages_no_answer_is_not_ready_and_gets_no_page_bonus() -> None:
    score, reasons, action = gw._compute_readiness(
        "Explain X.", "X is Y.", [], [], RUBRIC
    )
    assert action == "not_ready"
    assert score <= 0.5
    assert not any("Few relevant pages" in r for r in reasons)
    assert any("No student work found" in r for r in reasons)


def test_readiness_counts_typed_answer_as_student_work() -> None:
    score, reasons, action = gw._compute_readiness(
        "Explain X.", "X is Y.", [], [], RUBRIC, typed_answer="X is Y because..."
    )
    assert action == "ready"
    assert any("typed answer is present" in r for r in reasons)


def test_readiness_flags_crop_pages_missing_from_submission() -> None:
    score, reasons, _ = gw._compute_readiness(
        None, None, [{"page_number": 9}], [{"number": 1, "url": "u"}], RUBRIC,
        missing_crop_pages=[9],
    )
    assert any("not among this submission's pages" in r for r in reasons)
    # No "student work located" bonus when the crop points elsewhere.
    assert score == pytest.approx(0.25 + 0.15 + 0.15 + 0.1)


def test_zero_page_submission_is_not_ready_in_every_tool(monkeypatch) -> None:
    outputs = _three_tools(monkeypatch, _props(rubric=RUBRIC), PROMPT_AND_EXPLANATION)

    for out in outputs:
        assert _readiness(out)[1] == "not_ready"
        assert "safe to auto-grade" not in out.lower()
    assert "No Student Work Found" in outputs[2]
    assert "All key context available" not in outputs[2]


def test_server_wrappers_are_not_described_as_confidence_gates() -> None:
    # Pinned against the implementation docstrings this unit owns.
    for func in (gw.prepare_grading_artifact, gw.assess_submission_readiness,
                 gw.smart_read_submission):
        doc = inspect.getdoc(func).lower()
        assert "confidence gate" not in doc
        assert "should auto-grade" not in doc
        assert "safely an agent can auto-grade" not in doc


# ---------------------------------------------------------------------------
# V4-2 — every tool scores the same pages; placeholders are never listed
# ---------------------------------------------------------------------------

def test_tools_agree_on_readiness_with_placeholder_page(monkeypatch) -> None:
    pages = [
        {"number": 1, "url": "https://s3.example/p1.jpg"},
        {"number": 2, "url": "https://s3.example/p2.jpg"},
        {"number": 3, "url": "https://www.gradescope.com/images/missing_pdf.png"},
    ]
    outputs = _three_tools(
        monkeypatch, _props(pages=pages, rubric=RUBRIC), [{"type": "text", "value": "Compute."}]
    )

    assert len({_readiness(out) for out in outputs}) == 1
    for out in outputs[1:]:
        assert "missing_pdf" not in out
    artifact = gw.get_artifact_path("gradescope-grading-7-11.md").read_text()
    assert "missing_pdf" not in artifact
    assert "placeholder" in outputs[2]


def test_tools_agree_on_readiness_when_crop_is_on_first_of_four_pages(monkeypatch) -> None:
    pages = [{"number": n, "url": f"https://s3.example/p{n}.jpg"} for n in (1, 2, 3, 4)]
    crop = [{"page_number": 1, "x1": 0, "x2": 100, "y1": 10, "y2": 30}]
    outputs = _three_tools(monkeypatch, _props(pages=pages, crop=crop, rubric=[]))

    assert len({_readiness(out) for out in outputs}) == 1


def test_prepare_labels_readiness_and_pages_as_sample_submission(monkeypatch) -> None:
    pages = [{"number": 1, "url": "https://s3.example/p1.jpg"}]
    out, _, _ = _three_tools(monkeypatch, _props(pages=pages, rubric=RUBRIC))

    assert "sample submission `21`" in out
    artifact = gw.get_artifact_path("gradescope-grading-7-11.md").read_text()
    assert "### Relevant Pages (sample submission `21` only" in artifact
    assert re.search(r"- generated_at: `\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ`", artifact)


# ---------------------------------------------------------------------------
# V4-3 / V4-6 — rubric summary and scoring metadata in the artifact
# ---------------------------------------------------------------------------

def _artifact(monkeypatch, rubric, scoring="negative", **question) -> str:
    pages = [{"number": 1, "url": "https://s3.example/p1.jpg"}]
    world = World({"7": {"questions": Q, "outline": {"questions": {"11": {"content": []}}}}},
                  _props(pages=pages, rubric=rubric, scoring=scoring, **question))
    _install(monkeypatch, world)
    out = _call("tool_prepare_grading_artifact", ARGS)
    assert out.startswith("Prepared grading artifact"), out
    return gw.get_artifact_path("gradescope-grading-7-11.md").read_text()


def _section(text: str, start: str, stop: str = "## Read Strategy") -> str:
    return text[text.index(start):text.index(stop)]


def test_rubric_summary_uses_scoring_type_not_substrings(monkeypatch) -> None:
    rubric = [
        {"id": 1, "description": "Correct", "weight": 0.0},
        {"id": 2, "description": "Incorrect sign in final answer", "weight": 1.0},
        {"id": 3, "description": "Incorrectly applies chain rule", "weight": 2.0},
    ]
    text = _artifact(monkeypatch, rubric, "negative")
    summary = _section(text, "## Rubric Summary (not a reference answer)")

    assert "Expected full-credit elements" not in text
    assert "Reference Answer (⚠️ Rubric-Based Fallback)" not in text
    deductions = summary[summary.index("Deductions:"):]
    assert "Incorrect sign in final answer" in deductions
    assert "Incorrectly applies chain rule" in deductions
    assert "-2 pts, deduction" in deductions
    assert "Zero-point items" in summary


def test_rubric_summary_marks_positive_items_as_earned(monkeypatch) -> None:
    rubric = [
        {"id": 11, "description": "Sets up the integral", "weight": 2.0},
        {"id": 12, "description": "Final answer 3/4", "weight": 1.0},
    ]
    text = _artifact(monkeypatch, rubric, "positive")
    summary = _section(text, "## Rubric Summary")

    assert "Common issues to watch for" not in text
    assert "Items that add points:" in summary
    assert "`11` (+2 pts, earned): Sets up the integral" in summary
    assert "Deductions:" not in summary


def test_rubric_null_description_and_long_rubrics(monkeypatch) -> None:
    rubric = [{"id": 7, "description": None, "weight": 1}] + [
        {"id": 100 + i, "description": f"Deduction {i}", "weight": 1} for i in range(12)
    ]
    text = _artifact(monkeypatch, rubric)

    assert "None" not in _section(text, "## Rubric", "## Rubric Summary")
    assert "`7` (-1 pts, deduction): (no description)" in text
    assert _section(text, "## Rubric Summary").count("Deduction ") == 12


def test_artifact_reports_scoring_type_floor_ceiling_and_signed_weights(monkeypatch) -> None:
    rubric = [{"id": 5, "description": "Missing units", "weight": 0.5}]
    text = _artifact(monkeypatch, rubric, "negative", floor=0.0, ceiling=4.0)

    assert "- scoring_type: `negative` (starts at full credit; rubric items deduct points)" in text
    assert "- floor: `0.0`" in text
    assert "- ceiling: `4.0`" in text
    assert "- `5` (-0.5 pts, deduction): Missing units" in text


def test_artifact_marks_missing_scoring_type_as_unknown(monkeypatch) -> None:
    text = _artifact(monkeypatch, [{"id": 5, "description": "x", "weight": 2}], scoring=None)

    assert "- scoring_type: `unknown`" in text
    assert "weight 2, direction unknown" in text


# ---------------------------------------------------------------------------
# V4-4 — assignment resolution
# ---------------------------------------------------------------------------

def _resolution_world(n=30, owner="30"):
    assignments = {
        str(i): {"questions": {str(500 + i): {"index": 1, "weight": 1}}, "outline": {"questions": {}}}
        for i in range(1, n + 1)
    }
    assignments[owner]["questions"] = {"11": {"index": 1, "weight": 4}}
    pages = [{"number": 1, "url": "https://s3.example/p1.jpg"}]
    return World(assignments, _props(pages=pages, rubric=RUBRIC))


def _grade_json_gets(adapter) -> int:
    return sum(1 for e in adapter.log if e["url"].endswith("grade.json"))


def test_wrong_or_nonexistent_assignment_id_falls_back(monkeypatch) -> None:
    _, adapter = _install(monkeypatch, _resolution_world())
    out = _call("tool_smart_read_submission", {**ARGS, "assignment_id": "999"})

    assert not out.startswith("Error"), out
    assert "assignment `999` could not be used" in out
    assert "status 404" in out
    assert "auto-resolved question `11` to assignment `30`" in out


def test_resolution_is_memoized_per_process(monkeypatch) -> None:
    _, adapter = _install(monkeypatch, _resolution_world())
    args = {k: v for k, v in ARGS.items() if k != "assignment_id"}

    first = _call("tool_smart_read_submission", args)
    assert "auto-resolved question `11` to assignment `30`" in first
    assert _grade_json_gets(adapter) == 30

    adapter.log.clear()
    _call("tool_assess_submission_readiness", args)
    assert _grade_json_gets(adapter) == 1


def test_scan_skips_unreadable_assignments_but_reports_them(monkeypatch) -> None:
    world = _resolution_world(n=3, owner="3")
    world.assignments["2"]["grade_json"] = (403, "forbidden", {"Content-Type": "text/html"})
    _install(monkeypatch, world)
    args = {k: v for k, v in ARGS.items() if k != "assignment_id"}

    assert "auto-resolved question `11` to assignment `3`" in _call("tool_smart_read_submission", args)

    world.assignments["3"]["questions"] = {"12": {"index": 1}}
    gw._ASSIGNMENT_BY_QUESTION.clear()
    out = _call("tool_smart_read_submission", args)
    assert out.startswith("Error: Could not resolve an assignment for question `11`")
    assert "1 other assignment(s) could not be read" in out


def test_scan_stops_on_non_json_response_instead_of_masking_it(monkeypatch) -> None:
    world = _resolution_world(n=5, owner="5")
    for a in world.assignments.values():
        a["grade_json"] = (200, LOGIN_HTML, {"Content-Type": "text/html"})
    _, adapter = _install(monkeypatch, world)
    args = {k: v for k, v in ARGS.items() if k != "assignment_id"}

    out = _call("tool_smart_read_submission", args)

    assert out.startswith("Error:")
    assert "non-JSON page" in out
    assert "Could not resolve" not in out
    assert _grade_json_gets(adapter) == 1


def test_answer_key_reports_html_grade_json_clearly(monkeypatch) -> None:
    world = World({"7": {"questions": Q, "outline": None,
                         "grade_json": (200, LOGIN_HTML, {"Content-Type": "text/html"})}}, None)
    _install(monkeypatch, world)

    out = _call("tool_prepare_answer_key", {"course_id": "1", "assignment_id": "7"})

    assert out.startswith("Error:")
    assert "Expecting value" not in out
    assert "non-JSON page" in out


# ---------------------------------------------------------------------------
# V4-5 — outline failures are surfaced, not reported as "no answers"
# ---------------------------------------------------------------------------

def test_outline_403_is_reported_in_answer_key_and_artifact(monkeypatch) -> None:
    outline = {"assignment": {"title": "Online HW"}, "questions": {"11": {"content": PROMPT_AND_EXPLANATION}}}
    world = World({"7": {"questions": Q, "outline": outline, "outline_status": 403}},
                  _props(pages=[{"number": 1, "url": "https://s3.example/p1.jpg"}], rubric=[]))
    _install(monkeypatch, world)

    key = _call("tool_prepare_answer_key", {"course_id": "1", "assignment_id": "7"})
    assert "status 403" in key
    assert "Questions with instructor reference answers: unknown (outline fetch failed)" in key
    key_text = gw.get_artifact_path("gradescope-answerkey-7.md").read_text()
    assert "typical for scanned PDF" not in key_text
    assert "⚠️ Unknown" in key_text

    out = _call("tool_prepare_grading_artifact", ARGS)
    assert "Outline unavailable" in out and "status 403" in out
    artifact = gw.get_artifact_path("gradescope-grading-7-11.md").read_text()
    assert "## Reference Answer — Unknown (outline fetch failed)" in artifact
    assert "Rubric-Based Fallback" not in artifact


def test_outline_markup_change_is_reported(monkeypatch) -> None:
    world = World({"7": {"questions": Q, "outline": None}}, _props(rubric=RUBRIC))
    _install(monkeypatch, world)

    key = _call("tool_prepare_answer_key", {"course_id": "1", "assignment_id": "7"})

    assert "Outline unavailable" in key
    assert "Neither AssignmentEditor nor AssignmentOutline" in key


# ---------------------------------------------------------------------------
# V4-7 — cache_relevant_pages robustness
# ---------------------------------------------------------------------------

def _pages_world(pages, crop=(), page_responses=None):
    return World({"7": {"questions": Q, "outline": None}},
                 _props(pages=pages, crop=crop), pages=page_responses)


def test_cache_pages_impl_default_matches_mcp_wrapper() -> None:
    impl_default = inspect.signature(gw.cache_relevant_pages).parameters["include_all_pages"].default
    tools = {t.name: t for t in anyio.run(server.mcp.list_tools)}
    schema = tools["tool_cache_relevant_pages"].input_schema
    assert impl_default is True
    assert schema["properties"]["include_all_pages"]["default"] is impl_default


def test_cache_pages_rejects_html_and_reports_partial_results(monkeypatch) -> None:
    pages = [{"number": n, "url": f"https://s3.example/p{n}.jpg"} for n in (1, 2, 3, 4)]
    responses = {
        "https://s3.example/p2.jpg": (200, LOGIN_HTML, {"Content-Type": "text/html"}),
        "https://s3.example/p3.jpg?sig=1": (403, "denied", {}),
    }
    pages[2]["url"] = "https://s3.example/p3.jpg?sig=1"
    _install(monkeypatch, _pages_world(pages, page_responses=responses))

    out = _call("tool_cache_relevant_pages", ARGS)

    assert out.startswith("Cached 2 of 4 relevant page(s)")
    assert "page 2: response is not an image (Content-Type: text/html)" in out
    assert "page 3: HTTP 403" in out
    out_dir = gw.get_artifact_dir("gradescope-pages-7-11-21")
    assert sorted(p.name for p in out_dir.iterdir()) == ["page_1.jpg", "page_4.jpg"]
    assert (out_dir / "page_1.jpg").read_bytes() == JPEG


def test_cache_pages_all_failures_is_an_error(monkeypatch) -> None:
    pages = [{"number": 1, "url": "https://s3.example/p1.jpg"}]
    responses = {"https://s3.example/p1.jpg": (500, "boom", {})}
    _install(monkeypatch, _pages_world(pages, page_responses=responses))

    out = _call("tool_cache_relevant_pages", ARGS)

    assert out.startswith("Error: could not cache any of the 1 selected page(s)")
    assert "page 1: HTTP 500" in out


def test_cache_pages_without_numbers_get_distinct_files(monkeypatch) -> None:
    pages = [{"url": "https://s3.example/a.jpg"}, {"url": "https://s3.example/b.jpg"}]
    _install(monkeypatch, _pages_world(pages))

    out = _call("tool_cache_relevant_pages", ARGS)

    assert out.startswith("Cached 2 relevant page(s)")
    out_dir = gw.get_artifact_dir("gradescope-pages-7-11-21")
    assert sorted(p.name for p in out_dir.iterdir()) == ["page_index1.jpg", "page_index2.jpg"]


def test_cache_pages_sends_no_session_headers_to_third_party_hosts(monkeypatch) -> None:
    pages = [
        {"number": 1, "url": "https://s3.example/p1.jpg"},
        {"number": 2, "url": f"{BASE}/files/p2.jpg"},
    ]
    _, adapter = _install(monkeypatch, _pages_world(pages))

    _call("tool_cache_relevant_pages", ARGS)

    s3 = next(e for e in adapter.log if e["url"] == "https://s3.example/p1.jpg")
    gs = next(e for e in adapter.log if e["url"] == f"{BASE}/files/p2.jpg")
    assert "X-CSRF-Token" not in s3["headers"]
    assert "Cookie" not in s3["headers"]
    assert gs["headers"].get("X-CSRF-Token") == "SESSION-CSRF-SECRET"
    assert "_gradescope_session" in gs["headers"].get("Cookie", "")


def test_cache_pages_rejects_oversized_pages(monkeypatch) -> None:
    pages = [{"number": 1, "url": "https://s3.example/p1.jpg"}]
    size = str(gw._MAX_PAGE_BYTES + 1)
    responses = {
        "https://s3.example/p1.jpg": (200, JPEG, {"Content-Type": "image/jpeg", "Content-Length": size}),
    }
    _install(monkeypatch, _pages_world(pages, page_responses=responses))

    out = _call("tool_cache_relevant_pages", ARGS)

    assert out.startswith("Error:")
    assert "too large" in out


def test_cache_pages_auth_error_mid_loop_reports_saved_pages(monkeypatch) -> None:
    pages = [{"number": n, "url": f"{BASE}/files/p{n}.jpg"} for n in (1, 2, 3)]
    world = _pages_world(pages)
    original_route = world.route

    def route(req):
        if req.url.endswith("/p2.jpg"):
            raise gw.AuthError("session expired")
        return original_route(req)

    world.route = route
    _install(monkeypatch, world)

    out = _call("tool_cache_relevant_pages", ARGS)

    assert out.startswith("Authentication error: session expired")
    assert "1 page(s) were cached before the error" in out
    assert "page_1.jpg" in out
    assert "page_3.jpg" not in out


def test_cache_pages_skips_placeholders(monkeypatch) -> None:
    pages = [
        {"number": 1, "url": "https://s3.example/p1.jpg"},
        {"number": 2, "url": f"{BASE}/images/missing_pdf.png"},
    ]
    _, adapter = _install(monkeypatch, _pages_world(pages))

    out = _call("tool_cache_relevant_pages", ARGS)

    assert out.startswith("Cached 1 relevant page(s)")
    assert "Skipped 1 missing-PDF placeholder page(s)" in out
    assert not any("missing_pdf" in e["url"] for e in adapter.log)


# ---------------------------------------------------------------------------
# V4-8 — prepare_answer_key question selection and ordering
# ---------------------------------------------------------------------------

def _answer_key(monkeypatch, questions) -> tuple[str, str]:
    world = World({"7": {"questions": questions,
                         "outline": {"assignment": {"title": "Exam"}, "questions": {}}}}, None)
    _install(monkeypatch, world)
    out = _call("tool_prepare_answer_key", {"course_id": "1", "assignment_id": "7"})
    text = gw.get_artifact_path("gradescope-answerkey-7.md").read_text()
    return out, text


def _sections(text: str) -> list[str]:
    return [line.split(":")[0][3:] for line in text.splitlines() if line.startswith("## ")]


def test_answer_key_lists_leaves_in_label_order(monkeypatch) -> None:
    grouped = {
        "500": {"id": 500, "index": 1, "title": "Q1", "weight": 6.0, "parent_id": None, "question_group": True},
        "501": {"id": 501, "index": 1, "title": "a", "weight": 3.0, "parent_id": 500},
        "502": {"id": 502, "index": 2, "title": "b", "weight": 3.0, "parent_id": 500},
        "400": {"id": 400, "index": 2, "title": "Q2", "weight": 4.0, "parent_id": None},
        "401": {"id": 401, "index": 1, "title": "a", "weight": 4.0, "parent_id": 400},
        "600": {"id": 600, "index": 3, "title": "Bonus", "weight": 0.0, "parent_id": None},
    }
    out, text = _answer_key(monkeypatch, grouped)

    # Group parents (flagged, or named as someone's parent) are not questions.
    assert _sections(text) == ["Q1.1", "Q1.2", "Q2.1", "Q3"]
    assert "- Questions: 4" in out
    assert "- Missing reference answers: 4 (Q1.1, Q1.2, Q2.1, Q3)" in out
    # The weight-0 leaf is kept and marked rather than silently dropped.
    assert "Weight-0 questions: Q3" in out


def test_answer_key_orders_creation_ordered_ids_by_label(monkeypatch) -> None:
    seq = {
        "100": {"index": 1, "title": "Q1", "weight": 4.0, "parent_id": None, "question_group": True},
        "101": {"index": 1, "title": "a", "weight": 4.0, "parent_id": 100},
        "102": {"index": 2, "title": "Q2", "weight": 2.0, "parent_id": None},
        "103": {"index": 3, "title": "Q3", "weight": 3.0, "parent_id": None, "question_group": True},
        "104": {"index": 1, "title": "a", "weight": 3.0, "parent_id": 103},
    }
    _, text = _answer_key(monkeypatch, seq)

    assert _sections(text) == ["Q1.1", "Q2", "Q3.1"]


def test_answer_key_tolerates_mixed_id_and_index_types(monkeypatch) -> None:
    mixed = {
        "1": {"index": "2", "title": "Q2", "weight": 2, "parent_id": None},
        "2": {"index": 1, "title": "Q1", "weight": 2, "parent_id": None, "question_group": True},
        "3": {"index": 1, "title": "a", "weight": 2, "parent_id": "2"},
        "4": {"index": "x", "title": "odd", "weight": 1, "parent_id": None},
    }
    out, text = _answer_key(monkeypatch, mixed)

    assert out.startswith("✅ Grading basis prepared")
    assert _sections(text) == ["Q1.1", "Q2", "Qx"]


# ---------------------------------------------------------------------------
# V4-9 / NEW-V4 — smart_read reading plan
# ---------------------------------------------------------------------------

def test_smart_read_lists_crop_page_once_with_normalized_urls(monkeypatch) -> None:
    pages = [{"number": n, "url": f"//s3.example/p{n}.jpg"} for n in range(1, 9)]
    crop = [{"page_number": 2, "x1": 5, "x2": 95, "y1": 10, "y2": 30}]
    world = World({"7": {"questions": Q, "outline": {"questions": {}}}},
                  _props(pages=pages, crop=crop, rubric=RUBRIC))
    _install(monkeypatch, world)

    out = _call("tool_smart_read_submission", ARGS)

    assert out.count("https://s3.example/p2.jpg") == 1
    assert re.search(r"(?<!https:)//s3\.example", out) is None
    assert "Page 2 (crop x=5%..95%, y=10%..30%)" in out
    # Every other page is still listed (students mis-tag pages).
    for n in range(1, 9):
        assert out.count(f"https://s3.example/p{n}.jpg") == 1
    assert "### Tier 3 — Adjacent Pages" in out
    assert "### Other Pages" in out


def test_smart_read_without_crop_lists_every_page(monkeypatch) -> None:
    pages = [{"number": n, "url": f"https://s3.example/p{n}.jpg"} for n in range(1, 9)]
    world = World({"7": {"questions": Q, "outline": {"questions": {}}}},
                  _props(pages=pages, rubric=RUBRIC))
    _install(monkeypatch, world)

    out = _call("tool_smart_read_submission", ARGS)

    assert re.findall(r"📄 Page (\d+)", out) == [str(n) for n in range(1, 9)]
    assert "more pages" not in out


def test_smart_read_shows_typed_answer_as_untrusted_block(monkeypatch) -> None:
    props = _props(rubric=RUBRIC, answers={"0": "The derivative is 2x+1 (STUDENT TYPED ANSWER)"})
    world = World({"7": {"questions": Q, "outline": {"questions": {"11": {
        "content": PROMPT_AND_EXPLANATION}}}}}, props)
    _install(monkeypatch, world)

    out = _call("tool_smart_read_submission", ARGS)

    assert "### Student Typed Answer" in out
    block = out[out.index("<<<BEGIN UNTRUSTED STUDENT ANSWER"):out.index("<<<END UNTRUSTED STUDENT ANSWER>>>")]
    assert "STUDENT TYPED ANSWER" in block
    assert _readiness(out) == ("0.90", "ready")
    assert "No Student Work Found" not in out


def test_smart_read_reports_blank_submission(monkeypatch) -> None:
    props = _props(rubric=RUBRIC, answers={"0": ""},
                   pages=[{"number": 1, "url": f"{BASE}/x/missing_pdf.png"}])
    world = World({"7": {"questions": Q, "outline": {"questions": {"11": {
        "content": PROMPT_AND_EXPLANATION}}}}}, props)
    _install(monkeypatch, world)

    out = _call("tool_smart_read_submission", ARGS)

    assert "### No Student Work Found" in out
    assert "1 missing-PDF placeholder page(s) skipped" in out
    assert _readiness(out)[1] == "not_ready"


def test_smart_read_describes_answer_key_honestly(monkeypatch) -> None:
    world = World({"7": {"questions": Q, "outline": {"assignment": {"title": "Exam"},
                                                    "questions": {"11": {"content": []}}}}},
                  _props(pages=[{"number": 1, "url": "https://s3.example/p1.jpg"}], rubric=RUBRIC))
    _install(monkeypatch, world)

    before = _call("tool_smart_read_submission", ARGS)
    assert "No answer key cached" in before

    _call("tool_prepare_answer_key", {"course_id": "1", "assignment_id": "7"})
    after = _call("tool_smart_read_submission", ARGS)

    assert "Answer key available" not in after
    key_line = next(line for line in after.splitlines() if "Answer key file" in line)
    assert str(gw.get_artifact_path("gradescope-answerkey-7.md")) in key_line
    assert "instructor reference answers: 0/1" in key_line
    assert "this question: no instructor reference answer" in key_line
    assert "UTC" in key_line


def test_no_tool_output_hardcodes_shared_tmp_path(monkeypatch) -> None:
    world = World({"7": {"questions": Q, "outline": {"questions": {}}}},
                  _props(pages=[{"number": 1, "url": "https://s3.example/p1.jpg"}], rubric=RUBRIC))
    _install(monkeypatch, world)

    outputs = [
        _call("tool_prepare_grading_artifact", ARGS),
        _call("tool_prepare_answer_key", {"course_id": "1", "assignment_id": "7"}),
        _call("tool_cache_relevant_pages", ARGS),
        _call("tool_smart_read_submission", ARGS),
    ]
    root = str(cache.get_cache_root())
    for out in outputs:
        assert "/tmp/gradescope-mcp" not in out
        assert root in out


# ---------------------------------------------------------------------------
# Extra defects: '' submission_id, ID validation, regex escaping
# ---------------------------------------------------------------------------

def test_prepare_treats_empty_submission_id_as_omitted(monkeypatch) -> None:
    world = World({"7": {"questions": Q, "outline": {"questions": {}}}},
                  _props(pages=[{"number": 1, "url": "https://s3.example/p1.jpg"}], rubric=RUBRIC),
                  submissions_html={"11": '<a href="/courses/1/questions/11/submissions/77/grade">x</a>'})
    _, adapter = _install(monkeypatch, world)

    out = _call("tool_prepare_grading_artifact", {**ARGS, "submission_id": ""})

    assert out.startswith("Prepared grading artifact"), out
    assert "sample submission `77`" in out
    assert not any("/submissions//grade" in e["url"] for e in adapter.log)


@pytest.mark.parametrize("tool", [
    "tool_cache_relevant_pages", "tool_smart_read_submission",
    "tool_assess_submission_readiness", "tool_prepare_grading_artifact",
])
def test_workflow_tools_reject_non_numeric_ids_before_any_request(monkeypatch, tmp_path, tool) -> None:
    _, adapter = _install(monkeypatch, _pages_world([{"number": 1, "url": "https://s3.example/p1.jpg"}]))

    out = _call(tool, {**ARGS, "submission_id": "21/grade#/../../../escaped-dir"})

    assert out.startswith("Error: submission_id must be a numeric Gradescope ID")
    assert adapter.log == []
    assert not (tmp_path / "escaped-dir").exists()


@pytest.mark.parametrize("call", [
    lambda: gw.prepare_grading_artifact("1", "7", "11", "21"),
    lambda: gw.assess_submission_readiness("1", "7", "11", "21"),
    lambda: gw.cache_relevant_pages("1", "7", "11", "21"),
    lambda: gw.smart_read_submission("1", "7", "11", "21"),
])
def test_unexpected_failures_use_the_error_prefix(monkeypatch, call) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("You must be logged in to access this page.")

    monkeypatch.setattr(gw, "_resolve_assignment_questions", boom)

    assert call().startswith("Error: ")


def test_answer_key_unexpected_failure_uses_the_error_prefix(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(gw, "_fetch_assignment_questions", boom)

    assert gw.prepare_answer_key("1", "7") == "Error: could not prepare the answer key: connection reset"


def test_ids_copied_from_markdown_are_accepted() -> None:
    assert gw._clean_id(" `123` ", "x") == "123"
    assert gw._clean_id("", "x", required=False) is None
    with pytest.raises(ValueError):
        gw._clean_id("12a", "x")


def test_find_first_submission_id_matches_literal_ids(monkeypatch) -> None:
    world = World({}, None, submissions_html={
        "11": '<a href="/courses/1/questions/11/submissions/88/grade">a</a>',
    })
    _install(monkeypatch, world)

    assert gw._find_first_submission_id("1", "11") == "88"


# ---------------------------------------------------------------------------
# V4-13 — shared helpers instead of local copies
# ---------------------------------------------------------------------------

def test_workflow_uses_shared_page_helpers() -> None:
    for name in ("_normalize_url", "_is_placeholder_page", "_MISSING_PDF_MARKER",
                 "json", "pathlib", "BeautifulSoup"):
        assert not hasattr(gw, name), name
    assert gw.normalize_url is common.normalize_url
    assert gw.is_placeholder_page is common.is_placeholder_page


# ---------------------------------------------------------------------------
# V2-4 — private, verified cache root and safe artifact writes
# ---------------------------------------------------------------------------

def _mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_cache_root_and_files_are_private(tmp_path, monkeypatch) -> None:
    root = tmp_path / "fresh-root"
    monkeypatch.setenv("GRADESCOPE_MCP_CACHE_DIR", str(root))

    path = cache.write_artifact(cache.get_artifact_path("x.md"), "secret")
    page_dir = cache.get_artifact_dir("gradescope-pages-1-2-3")
    page = cache.write_artifact(page_dir / "page_1.jpg", JPEG)

    assert _mode(root) == 0o700
    assert _mode(page_dir) == 0o700
    assert _mode(path) == 0o600
    assert _mode(page) == 0o600
    assert path.read_text() == "secret"
    assert [p.name for p in root.iterdir() if p.name.endswith(".tmp")] == []


def test_new_cache_dirs_are_0700_under_any_umask(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GRADESCOPE_MCP_CACHE_DIR", str(tmp_path / "root"))
    old_umask = os.umask(0o277)
    try:
        root = cache.get_cache_root()
        page_dir = cache.get_artifact_dir("gradescope-pages-1-2-3")
    finally:
        os.umask(old_umask)

    assert _mode(root) == 0o700
    assert _mode(page_dir) == 0o700


@pytest.mark.parametrize("mode", [0o777, 0o755, 0o770])
def test_cache_refuses_root_accessible_to_others(tmp_path, monkeypatch, mode) -> None:
    root = tmp_path / "shared-root"
    root.mkdir()
    os.chmod(root, mode)
    monkeypatch.setenv("GRADESCOPE_MCP_CACHE_DIR", str(root))

    with pytest.raises(cache.CacheError, match="group/other access"):
        cache.get_cache_root()


def test_cache_refuses_symlinked_root(tmp_path, monkeypatch) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    link = tmp_path / "link-root"
    link.symlink_to(target)
    monkeypatch.setenv("GRADESCOPE_MCP_CACHE_DIR", str(link))

    with pytest.raises(cache.CacheError, match="symlink"):
        cache.get_cache_root()


def test_cache_refuses_root_owned_by_another_user(tmp_path, monkeypatch) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("GRADESCOPE_MCP_CACHE_DIR", str(root))
    real_uid = os.getuid()
    monkeypatch.setattr(cache.os, "getuid", lambda: real_uid + 4242)

    with pytest.raises(cache.CacheError, match="not the current user"):
        cache.get_cache_root()


def test_tool_reports_unsafe_cache_root_instead_of_writing(tmp_path, monkeypatch) -> None:
    root = tmp_path / "shared-root"
    root.mkdir()
    os.chmod(root, 0o777)
    monkeypatch.setenv("GRADESCOPE_MCP_CACHE_DIR", str(root))
    world = World({"7": {"questions": Q, "outline": None}}, None)
    _install(monkeypatch, world)

    out = _call("tool_prepare_answer_key", {"course_id": "1", "assignment_id": "7"})

    assert out.startswith("Error: could not write the answer key")
    assert list(root.iterdir()) == []


def test_planted_symlinks_are_never_followed(tmp_path, monkeypatch) -> None:
    root = tmp_path / "root"
    monkeypatch.setenv("GRADESCOPE_MCP_CACHE_DIR", str(root))
    cache.get_cache_root()
    victim = tmp_path / "victim.txt"
    victim.write_text("ORIGINAL")
    planted = root / "gradescope-answerkey-77.md"
    planted.symlink_to(victim)

    # A link that escapes the root is refused when the path is looked up ...
    with pytest.raises(cache.CacheError, match="escapes"):
        cache.get_artifact_path("gradescope-answerkey-77.md")

    # ... and writing to the link path directly replaces the link, not its target.
    cache.write_artifact(planted, "# new")
    assert victim.read_text() == "ORIGINAL"
    assert not planted.is_symlink()
    assert planted.read_text() == "# new"
    assert _mode(planted) == 0o600


def test_planted_symlinked_page_dir_is_refused(tmp_path, monkeypatch) -> None:
    root = tmp_path / "root"
    monkeypatch.setenv("GRADESCOPE_MCP_CACHE_DIR", str(root))
    cache.get_cache_root()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (root / "gradescope-pages-1-2-3").symlink_to(elsewhere)

    with pytest.raises(cache.CacheError):
        cache.get_artifact_dir("gradescope-pages-1-2-3")
    with pytest.raises(cache.CacheError):
        cache.write_artifact(root / "gradescope-pages-1-2-3" / "page_1.jpg", JPEG)
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("name", ["..", ".", "", "a/b", "../escape", "x y", "page_1.jpg/..", "évil"])
def test_artifact_names_are_validated(name) -> None:
    with pytest.raises(cache.CacheError):
        cache.get_artifact_path(name)


def test_write_artifact_refuses_paths_outside_the_root(tmp_path) -> None:
    with pytest.raises(cache.CacheError):
        cache.write_artifact(tmp_path / "outside" / "x.md", "x")


def test_default_root_prefers_private_runtime_dir(tmp_path, monkeypatch) -> None:
    runtime = tmp_path / "run-user"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    assert cache.default_cache_root() == runtime / "gradescope-mcp"

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    assert cache.default_cache_root() == tmp_path / f"gradescope-mcp-{os.getuid()}"

    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.delenv("GRADESCOPE_MCP_CACHE_DIR")
    root = cache.get_cache_root()
    assert root == tmp_path / f"gradescope-mcp-{os.getuid()}"
    assert _mode(root) == 0o700


def test_configure_process_cache_env_pins_private_root(tmp_path, monkeypatch) -> None:
    for key in ("TMPDIR", "TEMP", "TMP", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, "unchanged")
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)
    root = tmp_path / "pinned"
    monkeypatch.setenv("GRADESCOPE_MCP_CACHE_DIR", str(root))

    assert cache.configure_process_cache_env() == root
    assert os.environ["GRADESCOPE_MCP_CACHE_DIR"] == str(root)
    assert os.environ["TMPDIR"] == str(root)
    assert os.environ["XDG_CACHE_HOME"] == str(root / "xdg-cache")
    assert _mode(root / "xdg-cache") == 0o700
    assert tempfile.gettempdir() == str(root)


# ---------------------------------------------------------------------------
# V4-12 — the suite itself is hermetic
# ---------------------------------------------------------------------------

def test_suite_uses_a_per_test_cache(tmp_path) -> None:
    root = cache.get_cache_root()
    assert root.is_relative_to(tmp_path)
    assert "GRADESCOPE_EMAIL" not in os.environ
    assert "GRADESCOPE_PASSWORD" not in os.environ

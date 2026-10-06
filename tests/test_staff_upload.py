"""Staff uploads on behalf of a student (issue #13).

``tool_upload_submission_for_student`` and ``tool_inspect_submission_upload_form``
run against an in-memory fake Gradescope: the roster, the scores export,
Manage Submissions, a submission page and the upload POST.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import anyio
import pytest
from requests.structures import CaseInsensitiveDict

from gradescope_mcp import server
from gradescope_mcp.tools import grading, submissions

BASE = "https://www.gradescope.com"
ASSIGNMENT = f"{BASE}/courses/1/assignments/2"


def _roster_row(user_id: str, name: str, email: str, role: str) -> str:
    data_cm = json.dumps({"full_name": name, "first_name": name.split()[0], "last_name": "", "sid": ""})
    return (
        '<tr class="rosterRow"><td>'
        f"<button class=\"rosterCell--editIcon\" data-cm='{data_cm}' "
        f'data-email="{email}" data-role="{role}" data-sections=""></button>'
        f'<button class="js-rosterName" data-url="/courses/1/memberships?user_id={user_id}">'
        f"{name}</button></td><td>0</td></tr>"
    )


ROSTER = (
    '<html><table class="js-rosterTable"><tr><th>Name</th><th>Submissions</th></tr>'
    + _roster_row("3", "Ada Lovelace", "ada@example.edu", "0")
    + _roster_row("4", "Tim TA", "tim@example.edu", "2")
    + "</table></html>"
)

MANAGE_FORM = """
<form method="post" action="/courses/1/assignments/2/submissions" enctype="multipart/form-data">
  <input type="hidden" name="authenticity_token" value="TOKEN">
  <select name="submission[owner_id]">
    <option value="">Pick a student</option>
    <option value="3">Ada Lovelace</option>
  </select>
  <input type="checkbox" name="submission[notify]" value="1">
  <input type="checkbox" name="submission[keep_history]" value="1" checked>
  <input type="text" name="submission[disabled_note]" value="x" disabled>
  <input type="file" name="submission[pdf_attachment]">
  <button type="submit" name="commit" value="Upload">Upload Submission</button>
</form>
"""

OTHER_FORMS = """
<form method="post" action="https://evil.example/courses/1/assignments/2/submissions">
  <input type="hidden" name="authenticity_token" value="TOKEN">
  <select name="submission[owner_id]"><option value="3">Ada</option></select>
  <input type="file" name="file">
</form>
<form method="post" action="/courses/1/assignments/9/submissions">
  <select name="submission[owner_id]"><option value="3">Ada</option></select>
  <input type="file" name="file">
</form>
"""

SUBMISSION_FORM = """
<form method="post" action="/courses/1/assignments/2/submissions/41/resubmit">
  <input type="hidden" name="authenticity_token" value="TOKEN2">
  <input type="file" name="submission[files][]">
  <button>Replace PDF</button>
</form>
"""


class Resp:
    def __init__(self, url, text="", status=200, headers=None, history=None):
        self.url = url
        self.text = text
        self.content = text.encode()
        self.status_code = status
        self.headers = CaseInsensitiveDict(
            headers if headers is not None else {"Content-Type": "text/html; charset=utf-8"}
        )
        self.history = history or []
        self.encoding = "utf-8"


def _scores_csv(rows: list[tuple[str, str, str]]) -> str:
    lines = ["First Name,Last Name,SID,Email,Total Score,Max Points,Status,Submission ID"]
    lines += [f"X,Y,,{email},,10,{status},{sub}" for email, status, sub in rows]
    return "\n".join(lines) + "\n"


class FakeGradescope:
    """Routes GET/POST by path; records every request."""

    def __init__(self) -> None:
        self.manage_page = f"<html><a href='/courses/1/assignments/2/submissions/40'>Bo</a>{MANAGE_FORM}</html>"
        self.submission_page = f"<html>{SUBMISSION_FORM}</html>"
        self.scores = _scores_csv([("ada@example.edu", "Missing", ""), ("bo@example.edu", "Graded", "40")])
        self.scores_type = "text/csv"
        self.post_redirect = f"{ASSIGNMENT}/submissions/42"
        self.posts: list[dict] = []
        self.gets: list[str] = []

    def get(self, url, **_kwargs):
        self.gets.append(url)
        path = urlsplit(url).path
        if path == "/courses/1/memberships":
            return Resp(url, ROSTER)
        if path == "/courses/1/assignments/2/scores":
            return Resp(url, self.scores, headers={"Content-Type": self.scores_type})
        if path == "/courses/1/assignments/2/submissions":
            return Resp(url, self.manage_page)
        if path == "/courses/1/assignments/2/submissions/41":
            return Resp(url, self.submission_page)
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, data=None, files=None, headers=None, **_kwargs):
        self.posts.append({"url": url, "data": data, "files": files, "headers": headers})
        hop = Resp(url, "", status=302, headers={"Location": self.post_redirect})
        return Resp(self.post_redirect, "<html>Submission</html>", history=[hop])


@pytest.fixture
def gs(monkeypatch) -> FakeGradescope:
    fake = FakeGradescope()
    conn = SimpleNamespace(session=fake, gradescope_base_url=BASE, logged_in=True)
    monkeypatch.setattr(submissions, "get_connection", lambda: conn)
    monkeypatch.setattr(grading, "get_connection", lambda: conn)
    monkeypatch.delenv(submissions.UPLOAD_ROOT_ENV, raising=False)
    return fake


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.4 scanned exam")
    return path


def _upload(pdf, **kwargs) -> str:
    args = {"course_id": "1", "assignment_id": "2", "user_id": "3", "file_paths": [str(pdf)]}
    args.update(kwargs)
    return submissions.upload_submission_for_student(**args)


def _call_tool(name: str, args: dict) -> tuple[str, bool]:
    result = anyio.run(server.mcp.call_tool, name, args)
    return result.content[0].text, bool(result.is_error)


# --- Preview ---------------------------------------------------------------


def test_preview_names_the_student_and_says_nothing_is_replaced(gs, pdf) -> None:
    text = _upload(pdf)

    assert "Write confirmation required" in text
    assert "Student: Ada Lovelace (ada@example.edu), user ID `3`" in text
    assert "Creates the student's first submission" in text
    assert "it replaces nothing" in text
    assert (
        f"Form: POST {ASSIGNMENT}/submissions, student field "
        "`submission[owner_id]`=`3`, file field `submission[pdf_attachment]`"
    ) in text
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert f"sha256 {digest}" in text
    assert f'expected_sha256=["{digest}"]' in text
    assert gs.posts == []


def test_preview_refuses_to_replace_an_existing_submission_implicitly(gs, pdf) -> None:
    gs.scores = _scores_csv([("ada@example.edu", "Graded", "41")])

    text = _upload(pdf)

    assert text.startswith("Error: Ada Lovelace (ada@example.edu), user ID `3` already has submission `41`")
    assert "pass submission_id=`41`" in text
    assert gs.posts == []


def test_preview_of_a_replacement_names_the_replaced_submission(gs, pdf) -> None:
    gs.scores = _scores_csv([("ada@example.edu", "Graded", "41")])

    text = _upload(pdf, submission_id="41")

    assert "Student: Ada Lovelace" in text
    assert "Replaces the student's current submission `41` (status: Graded)" in text
    assert f"Form: POST {ASSIGNMENT}/submissions/41/resubmit, file field `submission[files][]`" in text
    assert gs.posts == []


@pytest.mark.parametrize(
    "rows, message",
    [
        ([("ada@example.edu", "Graded", "41")], "their current submission is `41`"),
        ([("ada@example.edu", "Missing", "")], "they have no submission yet"),
    ],
)
def test_submission_id_must_be_the_students_current_one(gs, pdf, rows, message) -> None:
    gs.scores = _scores_csv(rows)

    text = _upload(pdf, submission_id="77", confirm_write=True)

    assert text.startswith("Error: submission `77` is not Ada Lovelace")
    assert message in text
    assert gs.posts == []


@pytest.mark.parametrize(
    "user_id, message",
    [
        ("4", "user `4` (Tim TA (tim@example.edu), user ID `4`) is a TA in course `1`, not a student"),
        ("99", "user `99` is not on the roster of course `1`"),
    ],
)
def test_only_roster_students_can_be_targeted(gs, pdf, user_id, message) -> None:
    text = _upload(pdf, user_id=user_id, confirm_write=True)

    assert text.startswith("Error:")
    assert message in text
    assert gs.posts == []


def test_unreadable_scores_export_uploads_nothing(gs, pdf) -> None:
    gs.scores = "<html>Log in</html>"
    gs.scores_type = "text/html"

    text = _upload(pdf, confirm_write=True)

    assert text.startswith("Error: could not check whether Ada Lovelace")
    assert "unknown whether this upload would replace one. Nothing was uploaded." in text
    assert gs.posts == []


def test_changed_file_is_refused_before_contacting_gradescope(monkeypatch, pdf) -> None:
    monkeypatch.setattr(submissions, "get_connection", lambda: pytest.fail("contacted Gradescope"))
    approved = hashlib.sha256(pdf.read_bytes()).hexdigest()
    pdf.write_bytes(b"%PDF-1.4 different content")

    text = _upload(pdf, confirm_write=True, expected_sha256=[approved])

    assert text.startswith("Error: the file content differs from the approved preview")


# --- Form discovery ----------------------------------------------------------


def test_forms_posting_off_site_or_to_another_assignment_are_never_used(gs, pdf) -> None:
    gs.manage_page = f"<html>{OTHER_FORMS}</html>"

    text = _upload(pdf, confirm_write=True)

    assert text.startswith("Error:")
    assert "has no staff upload form that posts to assignment `2`" in text
    assert gs.posts == []


def test_student_missing_from_the_forms_student_list_is_an_error(gs, pdf) -> None:
    gs.manage_page = "<html>" + MANAGE_FORM.replace('<option value="3">Ada Lovelace</option>', "") + "</html>"

    text = _upload(pdf, confirm_write=True)

    assert "the upload form's student list (`submission[owner_id]`) has no entry for user `3`" in text
    assert gs.posts == []


def test_unknown_student_field_name_is_an_error(gs, pdf) -> None:
    text = _upload(pdf, student_field_name="submission[user]")

    assert "no upload form on" in text
    assert "has a field named `submission[user]`" in text


def test_inspect_marks_which_forms_would_be_used(gs) -> None:
    gs.manage_page = f"<html>{MANAGE_FORM}{OTHER_FORMS}</html>"

    text, is_error = _call_tool(
        "tool_inspect_submission_upload_form", {"course_id": "1", "assignment_id": "2"}
    )

    assert not is_error
    assert "**File-upload forms found:** 3" in text
    assert text.count("usable for tool_upload_submission_for_student: yes") == 1
    assert text.count("no (not a POST to this assignment on this site)") == 2
    assert "`submission[owner_id]` (list of 2 choices)" in text
    assert "`submission[pdf_attachment]`" in text
    assert gs.posts == []


# --- Write -------------------------------------------------------------------


def test_upload_posts_the_form_as_a_browser_would_and_confirms_the_new_submission(gs, pdf) -> None:
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()

    text, is_error = _call_tool("tool_upload_submission_for_student", {
        "course_id": "1", "assignment_id": "2", "user_id": 3, "file_paths": [str(pdf)],
        "confirm_write": True, "expected_sha256": [digest],
    })

    assert not is_error, text
    assert text.startswith("✅ Submission uploaded successfully!")
    assert "- **Student:** Ada Lovelace (ada@example.edu), user ID `3`" in text
    assert "- **Replaced:** nothing (the student's first submission)" in text
    assert "- **Submission ID:** `42`" in text
    assert "Every file matched the approved expected_sha256." in text
    [post] = gs.posts
    assert post["url"] == f"{ASSIGNMENT}/submissions"
    assert post["data"] == [
        ("authenticity_token", "TOKEN"),
        ("submission[keep_history]", "1"),
        ("submission[owner_id]", "3"),
    ]
    assert post["files"] == [
        ("submission[pdf_attachment]", ("scan.pdf", pdf.read_bytes(), "application/pdf"))
    ]
    assert post["headers"]["Referer"] == f"{ASSIGNMENT}/submissions"


def test_replacement_posts_to_the_submission_pages_form(gs, pdf) -> None:
    gs.scores = _scores_csv([("ada@example.edu", "Graded", "41")])
    gs.post_redirect = f"{ASSIGNMENT}/submissions/43"

    text = _upload(pdf, submission_id="41", confirm_write=True)

    assert text.startswith("✅ Submission uploaded successfully!")
    assert "- **Replaced:** submission `41`" in text
    assert "- **Submission ID:** `43`" in text
    [post] = gs.posts
    assert post["url"] == f"{ASSIGNMENT}/submissions/41/resubmit"
    assert post["data"] == [("authenticity_token", "TOKEN2")]


@pytest.mark.parametrize(
    "redirect, problem",
    [
        (f"{ASSIGNMENT}/submissions", "did not open a new submission"),
        (f"{ASSIGNMENT}/submissions/40", "which already existed before this upload"),
    ],
)
def test_upload_without_a_new_submission_page_is_not_confirmed(gs, pdf, redirect, problem) -> None:
    gs.post_redirect = redirect

    text, is_error = _call_tool("tool_upload_submission_for_student", {
        "course_id": "1", "assignment_id": "2", "user_id": "3", "file_paths": [str(pdf)],
        "confirm_write": True,
    })

    assert is_error
    assert text.startswith("❌ Upload not confirmed")
    assert problem in text
    assert "- **Student:** Ada Lovelace" in text
    assert "- **Was to replace:** nothing (the student's first submission)" in text
    assert "uploaded successfully" not in text


def test_replacing_keeps_the_old_id_out_of_the_success_check(gs, pdf) -> None:
    gs.scores = _scores_csv([("ada@example.edu", "Graded", "41")])
    gs.post_redirect = f"{ASSIGNMENT}/submissions/41"

    text = _upload(pdf, submission_id="41", confirm_write=True)

    assert text.startswith("❌ Upload not confirmed")
    assert "Gradescope opened submission `41`, which already existed" in text

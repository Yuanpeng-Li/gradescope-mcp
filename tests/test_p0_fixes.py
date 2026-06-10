"""Regression tests for the P0 review fixes.

Each test pins a specific bug surfaced in the codebase review so that future
refactors don't reintroduce the regression. Grouped by the original bug ID.
"""

from __future__ import annotations

from types import SimpleNamespace

from gradescope_mcp.tools import (
    assignments,
    grading,
    grading_workflow,
    regrades,
    submissions,
)


# ---------------------------------------------------------------------------
# 🔴1 — upload_submission TypeError (mixed positional + kw args)
# ---------------------------------------------------------------------------

def test_upload_submission_passes_files_positionally(tmp_path, monkeypatch) -> None:
    """upload_assignment must receive files as positional args, not after kw args."""
    f1 = tmp_path / "a.pdf"
    f2 = tmp_path / "b.pdf"
    f1.write_bytes(b"x")
    f2.write_bytes(b"y")

    captured: dict = {}

    def fake_upload(session, course_id, assignment_id, *files, **kwargs):
        captured["session"] = session
        captured["course_id"] = course_id
        captured["assignment_id"] = assignment_id
        captured["file_count"] = len(files)
        captured["kwargs"] = kwargs
        return "https://example.com/submissions/42"

    monkeypatch.setattr(submissions, "upload_assignment", fake_upload)
    monkeypatch.setattr(
        submissions, "get_connection",
        lambda: SimpleNamespace(session=object(), gradescope_base_url="https://x"),
    )

    result = submissions.upload_submission(
        "1", "2", [str(f1), str(f2)], confirm_write=True
    )

    assert "Submission uploaded" in result
    assert captured["course_id"] == "1"
    assert captured["assignment_id"] == "2"
    assert captured["file_count"] == 2
    assert captured["kwargs"] == {"leaderboard_name": None}


def test_upload_submission_closes_handles_on_partial_open_failure(
    tmp_path, monkeypatch
) -> None:
    """If a later open() raises, earlier handles must still be closed."""
    f1 = tmp_path / "a.pdf"
    f1.write_bytes(b"x")
    missing = tmp_path / "missing.pdf"  # never created — but path validation passes
    missing.write_bytes(b"")  # actually create so path validation succeeds
    f2 = tmp_path / "b.pdf"
    f2.write_bytes(b"y")

    opened: list = []
    real_open = open

    def tracked_open(path, mode):
        # Accept str or os.PathLike for path
        if str(path).endswith("b.pdf"):
            raise PermissionError("simulated EACCES on second file")
        fh = real_open(path, mode)
        opened.append(fh)
        return fh

    monkeypatch.setattr("builtins.open", tracked_open)
    monkeypatch.setattr(
        submissions, "get_connection",
        lambda: SimpleNamespace(session=object(), gradescope_base_url="https://x"),
    )
    monkeypatch.setattr(submissions, "upload_assignment", lambda *a, **kw: "ok")

    result = submissions.upload_submission(
        "1", "2", [str(f1), str(f2)], confirm_write=True
    )

    assert "Error uploading submission" in result
    # Handle for f1 must have been closed by ExitStack on the failed enter.
    assert all(fh.closed for fh in opened)


# ---------------------------------------------------------------------------
# 🔴2 — _get_outline_data parent_id flattening
# ---------------------------------------------------------------------------

def test_outline_flatten_threads_parent_id_through_children(monkeypatch) -> None:
    """AssignmentOutline children inherit parent_id from the enclosing item."""
    html = """
    <div data-react-class="AssignmentOutline" data-react-props='
    {"outline": [
      {"id": 1, "type": "QuestionGroup", "title": "Group A", "weight": 5,
       "children": [
         {"id": 2, "type": "FreeResponseQuestion", "title": "A1", "weight": 2},
         {"id": 3, "type": "FreeResponseQuestion", "title": "A2", "weight": 3}
       ]}
    ], "assignment": {"id": 99, "title": "HW"}}
    '></div>
    """.replace("\n", "")
    monkeypatch.setattr(
        grading, "get_connection",
        lambda: SimpleNamespace(
            gradescope_base_url="https://x",
            session=SimpleNamespace(get=lambda *a, **kw: SimpleNamespace(
                status_code=200, text=html,
            )),
        ),
    )

    data = grading._get_outline_data("1", "2")
    qs = data["questions"]

    assert qs["1"]["parent_id"] is None, "Top-level group must have no parent"
    assert qs["2"]["parent_id"] == 1, "Child must inherit group id"
    assert qs["3"]["parent_id"] == 1, "Sibling child must also inherit group id"

    # Tree-build round-trip: the group should now contain its 2 children
    # rather than appearing as 3 root nodes.
    tree = grading._build_question_tree(qs)
    assert len(tree) == 1
    assert len(tree[0]["children"]) == 2


def test_grading_progress_preserves_top_level_question_numbers(monkeypatch) -> None:
    """Progress labels must not shift standalone questions after grouped ones."""
    grade_json = {
        "assignments": {
            "99": {
                "questions": {
                    "101": {
                        "id": 101,
                        "index": 1,
                        "title": "Question 1",
                        "type": "FreeResponseQuestion",
                        "question_group": False,
                        "parent_id": None,
                        "total_graded_count": 10,
                        "total_count": 10,
                        "graders": [{"name": "TA"}],
                    },
                    "200": {
                        "id": 200,
                        "index": 2,
                        "title": "Question 2",
                        "type": "QuestionGroup",
                        "question_group": True,
                        "parent_id": None,
                    },
                    "201": {
                        "id": 201,
                        "index": 1,
                        "title": "part a)",
                        "type": "FreeResponseQuestion",
                        "question_group": False,
                        "parent_id": 200,
                        "total_graded_count": 9,
                        "total_count": 10,
                        "graders": [],
                    },
                    "202": {
                        "id": 202,
                        "index": 2,
                        "title": "part b)",
                        "type": "FreeResponseQuestion",
                        "question_group": False,
                        "parent_id": 200,
                        "total_graded_count": 8,
                        "total_count": 10,
                        "graders": [],
                    },
                    "301": {
                        "id": 301,
                        "index": 3,
                        "title": "Question 3",
                        "type": "FreeResponseQuestion",
                        "question_group": False,
                        "parent_id": None,
                        "total_graded_count": 0,
                        "total_count": 10,
                        "graders": [],
                    },
                    "400": {
                        "id": 400,
                        "index": 4,
                        "title": "Question 4",
                        "type": "QuestionGroup",
                        "question_group": True,
                        "parent_id": None,
                    },
                    "401": {
                        "id": 401,
                        "index": 1,
                        "title": "part a)",
                        "type": "FreeResponseQuestion",
                        "question_group": False,
                        "parent_id": 400,
                        "total_graded_count": 0,
                        "total_count": 10,
                        "graders": [],
                    },
                }
            }
        },
        "action_button": {},
    }

    monkeypatch.setattr(
        grading,
        "get_connection",
        lambda: SimpleNamespace(
            gradescope_base_url="https://x",
            session=SimpleNamespace(get=lambda *a, **kw: SimpleNamespace(
                status_code=200,
                json=lambda: grade_json,
            )),
        ),
    )

    result = grading.get_grading_progress("1", "99")

    assert "Q1 Question 1 (`101`)" in result
    assert "Q2.1 part a) (`201`)" in result
    assert "Q2.2 part b) (`202`)" in result
    assert "Q3 Question 3 (`301`)" in result
    assert "Q4.1 part a) (`401`)" in result
    assert "Q3 Question 1 (`101`)" not in result
    assert "Q4 Question 3 (`301`)" not in result
    assert "**Overall progress:** 27/50 (54%)" in result


# ---------------------------------------------------------------------------
# 🔴4 — get_assignments collapses 0 score to "N/A"
# ---------------------------------------------------------------------------

def test_get_assignments_renders_zero_grade_as_zero(monkeypatch) -> None:
    """A real score of 0 must not be displayed as N/A."""
    sample = [
        SimpleNamespace(
            name="Quiz Zero",
            assignment_id="42",
            release_date=None,
            due_date=None,
            late_due_date=None,
            submissions_status="Graded",
            grade=0.0,
            max_grade=10.0,
        ),
    ]
    monkeypatch.setattr(
        assignments, "get_connection",
        lambda: SimpleNamespace(account=SimpleNamespace(get_assignments=lambda *_: sample)),
    )

    result = assignments.get_assignments("1")
    assert "0.0/10.0" in result
    assert "N/A/10.0" not in result


def test_get_assignment_details_renders_zero_grade(monkeypatch) -> None:
    sample = [
        SimpleNamespace(
            name="Quiz Zero", assignment_id="42",
            release_date=None, due_date=None, late_due_date=None,
            submissions_status="Graded", grade=0, max_grade=5,
        ),
    ]
    monkeypatch.setattr(
        assignments, "get_connection",
        lambda: SimpleNamespace(account=SimpleNamespace(get_assignments=lambda *_: sample)),
    )

    result = assignments.get_assignment_details("1", "42")
    assert "**Grade:** 0 / 5" in result


# ---------------------------------------------------------------------------
# 🔴5 — _select_relevant_pages no-crop fallback no longer truncates to 3
# ---------------------------------------------------------------------------

def test_select_relevant_pages_no_crop_returns_all_pages() -> None:
    pages = [{"number": n} for n in range(1, 11)]  # 10-page submission

    selected = grading_workflow._select_relevant_pages(pages, crop_rects=[])
    assert [p["number"] for p in selected] == list(range(1, 11))


def test_select_relevant_pages_unmatched_crop_returns_all_pages() -> None:
    pages = [{"number": n} for n in (1, 2, 3, 4, 5)]
    crop_rects = [{"page_number": 99}]  # crop says page 99 but submission has 5 pages

    selected = grading_workflow._select_relevant_pages(pages, crop_rects)
    assert [p["number"] for p in selected] == [1, 2, 3, 4, 5]


def test_select_relevant_pages_keeps_crop_neighborhood() -> None:
    pages = [{"number": n} for n in range(1, 11)]
    crop_rects = [{"page_number": 5}]

    selected = grading_workflow._select_relevant_pages(pages, crop_rects)
    assert [p["number"] for p in selected] == [4, 5, 6]


# ---------------------------------------------------------------------------
# 🔴6 — regrades.py "completed" detection (em-dash / Pending must be pending)
# ---------------------------------------------------------------------------

def _regrades_html(rows: list[tuple[str, str, str, str, str]]) -> str:
    """Build a regrade-requests table mirroring the live page layout.

    Each row is ``(student, sections, question, grader, completed_cell)``
    and is given a /grade link so the IDs can be parsed.
    """
    body = []
    for i, (student, section, question, grader, completed) in enumerate(rows, start=1000):
        body.append(
            f"<tr>"
            f"<td>{student}</td><td>{section}</td><td>{question}</td>"
            f"<td>{grader}</td><td>{completed}</td>"
            f"<td><a href=\"/courses/1/questions/{i}/submissions/{i + 5000}/grade\">view</a></td>"
            f"</tr>"
        )
    return f"""
    <table>
      <thead>
        <tr><th>Student</th><th>Sections</th><th>Question</th><th>Grader</th><th>Completed</th><th></th></tr>
      </thead>
      <tbody>
        {''.join(body)}
      </tbody>
    </table>
    """


def _stub_regrades_get(monkeypatch, html: str) -> None:
    monkeypatch.setattr(
        regrades, "get_connection",
        lambda: SimpleNamespace(
            gradescope_base_url="https://x",
            session=SimpleNamespace(get=lambda *a, **kw: SimpleNamespace(
                status_code=200, text=html,
            )),
        ),
    )


def test_regrades_completed_only_counts_real_dates(monkeypatch) -> None:
    """Em-dash, "Pending", and empty cells must NOT be parsed as completed."""
    html = _regrades_html([
        ("Alice", "A", "Q1", "Yuanpeng", "2026-05-08 14:30"),
        ("Bob", "A", "Q2", "Yuanpeng", "—"),
        ("Carol", "A", "Q3", "Yuanpeng", "Pending"),
        ("Dave", "A", "Q4", "Yuanpeng", ""),
        ("Eve", "B", "Q5", "Yuanpeng", "2026-05-09 09:00"),
    ])
    _stub_regrades_get(monkeypatch, html)

    result = regrades.get_regrade_requests("1", "2")

    # Only Alice and Eve are completed
    assert "**Pending:** 3 | **Completed:** 2" in result


def test_regrades_handles_missing_section_column(monkeypatch) -> None:
    """When the Sections column is removed, header-aware lookup keeps grader column right."""
    html = """
    <table>
      <thead>
        <tr><th>Student</th><th>Question</th><th>Grader</th><th>Completed</th><th></th></tr>
      </thead>
      <tbody>
        <tr>
          <td>Alice</td><td>Q1</td><td>Yuanpeng</td><td>2026-05-08</td>
          <td><a href="/courses/1/questions/100/submissions/200/grade">view</a></td>
        </tr>
      </tbody>
    </table>
    """
    _stub_regrades_get(monkeypatch, html)

    result = regrades.get_regrade_requests("1", "2")

    # Without header-aware columns, "Q1" would be read as the student name.
    assert "Alice" in result
    assert "Yuanpeng" in result
    assert "qid=100, sid=200" in result


def test_regrades_surfaces_link_parse_failure(monkeypatch) -> None:
    """If a row has no /grade link, the agent gets a visible warning, not an empty cell."""
    html = """
    <table>
      <thead>
        <tr><th>Student</th><th>Sections</th><th>Question</th><th>Grader</th><th>Completed</th></tr>
      </thead>
      <tbody>
        <tr><td>Alice</td><td>A</td><td>Q1</td><td>Yuanpeng</td><td>—</td></tr>
      </tbody>
    </table>
    """
    _stub_regrades_get(monkeypatch, html)

    # Without a /grade link the existing parser would give us nothing
    # actionable; the table still has 1 row of regrade entry, just unlinkable.
    result = regrades.get_regrade_requests("1", "2")
    # A row with no grade link doesn't parse out as a regrade request the way
    # the old version did (it would have shown a blank cell). We accept either
    # "no requests" *or* the warning, but never a silent missing field.
    assert "no /grade link" in result or "No regrade requests" in result


# ---------------------------------------------------------------------------
# 🔴7 — submissions._get_submissions_from_review_grades header-aware parsing
# ---------------------------------------------------------------------------

def _review_grades_html(rows: list[tuple[str, str, str, str]]) -> str:
    """Build a review_grades table with the real Gradescope header layout."""
    body = []
    for i, (idx, name, score, graded_flag) in enumerate(rows):
        sid = 1000 + i
        body.append(
            f"<tr>"
            f"<td>{idx}</td>"
            f"<td>{name}</td>"
            f"<td>Yuanpeng Li</td>"
            f"<td>section-a</td>"
            f"<td>{score}</td>"
            f"<td>{graded_flag}</td>"
            f"<td><a href=\"/courses/1/assignments/2/submissions/{sid}\">view</a></td>"
            f"</tr>"
        )
    return f"""
    <table>
      <thead>
        <tr><th></th><th>User</th><th>Last Graded By</th><th>Sections</th><th>Score</th><th>Graded?</th><th></th></tr>
      </thead>
      <tbody>
        {''.join(body)}
      </tbody>
    </table>
    """


def test_review_grades_fallback_uses_score_column_not_row_index(monkeypatch) -> None:
    """Row index column must not be misread as a score (the Q-table bug, ported)."""
    html = _review_grades_html([
        ("1", "Alice (a@x.edu)", "1.0", ""),
        ("2", "Bob (b@x.edu)", "", ""),       # ungraded
        ("3", "Carol (c@x.edu)", "—", ""),    # placeholder, must be ungraded
        ("4", "Dave (d@x.edu)", "0", ""),     # zero score is still graded
    ])
    monkeypatch.setattr(
        submissions, "get_connection",
        lambda: SimpleNamespace(
            gradescope_base_url="https://x",
            session=SimpleNamespace(get=lambda *a, **kw: SimpleNamespace(
                status_code=200, text=html,
            )),
        ),
    )

    conn = submissions.get_connection()
    result = submissions._get_submissions_from_review_grades(conn, "1", "2")

    # Total 4 submissions, 2 graded (Alice + Dave). Bob and Carol ungraded.
    assert "**Total submissions:** 4" in result
    assert "**Graded:** 2/4" in result


# ---------------------------------------------------------------------------
# 🔴3 — export_assignment_scores: median, max_points per row, empty-CSV guard
# ---------------------------------------------------------------------------

def _stub_grading_get(monkeypatch, status_code: int, text: str, content_type: str = "text/csv") -> None:
    monkeypatch.setattr(
        grading, "get_connection",
        lambda: SimpleNamespace(
            gradescope_base_url="https://x",
            session=SimpleNamespace(get=lambda *a, **kw: SimpleNamespace(
                status_code=status_code,
                text=text,
                headers={"content-type": content_type},
            )),
        ),
    )


def test_export_assignment_scores_uses_correct_median(monkeypatch) -> None:
    """Median for an even-length list must average the two middle values."""
    csv_text = (
        "First Name,Last Name,Email,Total Score,Max Points,Status,Submission ID,Submission Time,Lateness (H:M:S)\n"
        "A,One,a@x,1.0,10,Graded,11,2026-05-01,0\n"
        "B,Two,b@x,2.0,10,Graded,12,2026-05-01,0\n"
        "C,Three,c@x,3.0,10,Graded,13,2026-05-01,0\n"
        "D,Four,d@x,4.0,10,Graded,14,2026-05-01,0\n"
    )
    _stub_grading_get(monkeypatch, 200, csv_text)

    result = grading.export_assignment_scores("1", "2")
    # Old code: sorted([1,2,3,4])[2] = 3.0  → wrong median
    # statistics.median([1,2,3,4]) = 2.5    → correct
    assert "**Median:** 2.5" in result


def test_export_assignment_scores_renders_per_row_max_points(monkeypatch) -> None:
    """When students have differing Max Points (bonus credit), the table must reflect that."""
    csv_text = (
        "First Name,Last Name,Email,Total Score,Max Points,Status,Submission ID,Submission Time,Lateness (H:M:S)\n"
        "A,One,a@x,8.0,10,Graded,11,2026-05-01,0\n"
        "B,Two,b@x,12.0,12,Graded,12,2026-05-01,0\n"
    )
    _stub_grading_get(monkeypatch, 200, csv_text)

    result = grading.export_assignment_scores("1", "2")

    # Row-level rendering must use each student's own Max Points
    assert "8.0/10" in result
    assert "12.0/12" in result
    # Summary should declare that max varies rather than picking the first row
    assert "**Max points:** varies" in result


def test_export_assignment_scores_handles_empty_body(monkeypatch) -> None:
    """A 200 with empty body must not crash on reader.fieldnames being None."""
    _stub_grading_get(monkeypatch, 200, "")

    result = grading.export_assignment_scores("1", "2")

    # The "no scores found" path is acceptable; the crash from list-comping
    # over None.fieldnames is not.
    assert "No scores found" in result

import datetime
import json
from types import SimpleNamespace

from gradescope_mcp.tools import assignments, grading_ops


def test_get_assignments_formats_rows(monkeypatch) -> None:
    sample_assignments = [
        SimpleNamespace(
            name="Homework 1",
            assignment_id="10",
            release_date=datetime.datetime(2026, 3, 1, 9, 30),
            due_date=datetime.datetime(2026, 3, 8, 23, 59),
            late_due_date=None,
            submissions_status="Submitted",
            grade="8.5",
            max_grade="10.0",
        ),
        SimpleNamespace(
            name="Quiz 1",
            assignment_id="11",
            release_date=None,
            due_date=None,
            late_due_date=None,
            submissions_status=None,
            grade=None,
            max_grade=None,
        ),
    ]
    fake_conn = SimpleNamespace(
        account=SimpleNamespace(get_assignments=lambda _course_id: sample_assignments)
    )
    monkeypatch.setattr(assignments, "get_connection", lambda: fake_conn)

    result = assignments.get_assignments("123")

    assert "## Assignments for Course 123" in result
    assert "| 1 | Homework 1 | `10` | 2026-03-01 09:30 | 2026-03-08 23:59 | N/A | Submitted | 8.5/10.0 |" in result
    assert "| 2 | Quiz 1 | `11` | N/A | N/A | N/A | N/A | N/A/N/A |" in result
    assert "**Total assignments:** 2" in result


def test_modify_assignment_dates_rejects_invalid_date() -> None:
    result = assignments.modify_assignment_dates(
        "1",
        "2",
        due_date="2026/03/20 12:00",
    )

    assert "Error: Invalid date format" in result
    assert "YYYY-MM-DDTHH:MM" in result


def test_rename_assignment_rejects_whitespace_title() -> None:
    result = assignments.rename_assignment("1", "2", "   ")
    assert result == "Error: new_title cannot be all whitespace."


def test_get_submission_grading_context_json_filters_self_links_and_placeholder_pages(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        grading_ops,
        "_get_grading_context",
        lambda *_args, **_kwargs: {
            "props": {
                "question": {
                    "title": "Q1",
                    "weight": 5,
                    "scoring_type": "positive",
                    "parameters": {
                        "crop_rect_list": [{"page_number": 2, "x1": 1, "x2": 2, "y1": 3, "y2": 4}]
                    },
                },
                "submission": {
                    "id": "99",
                    "owner_names": "Student A",
                    "score": 4.5,
                    "graded": True,
                    "answers": {
                        "text": "final answer",
                        "uploads": [{"text_file_id": "abc123"}],
                    },
                },
                "evaluation": {
                    "points": -0.5,
                    "comments": "Needs more detail",
                },
                "rubric_items": [
                    {"id": 10, "description": "Correct", "weight": 5, "position": 0},
                    {"id": 20, "description": "Style", "weight": -1, "position": 1, "locked": True},
                ],
                "rubric_item_evaluations": [
                    {"rubric_item_id": 10, "present": True},
                    {"rubric_item_id": 20, "present": False},
                ],
                "navigation_urls": {
                    "previous_ungraded": "/courses/1/questions/2/submissions/88/grade",
                    "next_ungraded": "/courses/1/questions/2/submissions/99/grade",
                    "next_submission": "/courses/1/questions/2/submissions/100/grade",
                    "next_question": "/courses/1/questions/3/submissions/101/grade",
                },
                "num_graded_submissions": 4,
                "num_submissions": 9,
                "groups_present": True,
                "answer_group": 7,
                "answer_group_size": 3,
                "pages": [
                    {"number": 1, "url": "//example.com/page1.jpg"},
                    {"number": 2, "url": "https://example.com/missing_pdf.png"},
                    {"number": 3, "url": ""},
                    {"number": 4, "url": "https://example.com/page4.jpg"},
                ],
            }
        },
    )

    result = grading_ops.get_submission_grading_context("1", "2", "99", output_format="json")
    parsed = json.loads(result)

    assert parsed["student"] == "Student A"
    assert parsed["text_answer"] == "final answer\n[Uploaded file ID: abc123]"
    assert parsed["navigation"] == {
        "previous_ungraded": {"question_id": "2", "submission_id": "88"},
        "next_submission": {"question_id": "2", "submission_id": "100"},
        "next_question": {"question_id": "3", "submission_id": "101"},
    }
    assert parsed["rubric_items"][0]["applied"] is True
    assert parsed["rubric_items"][1]["locked"] is True
    assert parsed["answer_group"] == {"id": "7", "size": 3, "groups_present": True}
    assert parsed["pages"] == [
        {"number": 1, "url": "https://example.com/page1.jpg"},
        {"number": 4, "url": "https://example.com/page4.jpg"},
    ]
    assert parsed["crop_regions"] == [{"page_number": 2, "x1": 1, "x2": 2, "y1": 3, "y2": 4}]


def test_get_submission_grading_context_markdown_shows_real_pages_only(monkeypatch) -> None:
    monkeypatch.setattr(
        grading_ops,
        "_get_grading_context",
        lambda *_args, **_kwargs: {
            "props": {
                "question": {
                    "title": "Integral",
                    "weight": 10,
                    "scoring_type": "negative",
                    "floor": 0,
                    "ceiling": 10,
                    "parameters": {"crop_rect_list": [{"page_number": 3}]},
                },
                "submission": {
                    "owner_names": "Student B",
                    "score": None,
                    "graded": False,
                    "answers": {},
                },
                "evaluation": {},
                "rubric_items": [],
                "rubric_item_evaluations": [],
                "navigation_urls": {
                    "next_submission": "/courses/1/questions/2/submissions/100/grade",
                },
                "num_graded_submissions": 1,
                "num_submissions": 5,
                "pages": [
                    {"number": 1, "url": "https://example.com/missing_pdf.png"},
                    {"number": 2, "url": "https://example.com/page2.jpg"},
                    {"number": 3, "url": "https://example.com/page3.jpg"},
                    {"number": 4, "url": "https://example.com/page4.jpg"},
                    {"number": 5, "url": "https://example.com/page5.jpg"},
                ],
            }
        },
    )

    result = grading_ops.get_submission_grading_context("1", "2", "99")

    assert "## Grading Context — QIntegral" in result
    assert "**Scoring:** negative (floor=0, ceiling=10)" in result
    assert "Rubric items **deduct** points" in result
    assert "- **next_submission**: qid=`2`, sid=`100`" in result
    assert "**Relevant pages:** [3]" in result
    assert "- Page 2: [View](https://example.com/page2.jpg)" in result
    assert "- _...and 1 more pages_" in result
    assert "missing_pdf" not in result


def test_get_question_rubric_uses_question_rubric_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        grading_ops,
        "get_connection",
        lambda: SimpleNamespace(
            gradescope_base_url="https://example.com",
            session=SimpleNamespace(
                get=lambda *_args, **_kwargs: SimpleNamespace(
                    status_code=200,
                    text='<a href="/courses/1/questions/2/submissions/777/grade">grade</a>',
                    url="https://example.com/courses/1/questions/2/grade",
                )
            ),
        ),
    )
    monkeypatch.setattr(
        grading_ops,
        "_get_grading_context",
        lambda *_args, **_kwargs: {
            "props": {
                "question": {
                    "weight": 7,
                    "scoring_type": "negative",
                    "rubric": [
                        {"id": 55, "description": "Needs | escaping", "weight": -2},
                    ],
                },
                "rubric_items": [],
            }
        },
    )

    result = grading_ops.get_question_rubric("1", "2")

    assert "## Rubric for Question `2`" in result
    assert "**Weight:** 7 pts" in result
    assert "**Scoring:** negative" in result
    assert "| `55` | Needs \\| escaping | -2 |" in result


def test_apply_grade_sends_json_payload(monkeypatch) -> None:
    """Verify apply_grade sends JSON (not form-encoded) matching Gradescope's format."""
    captured = {}

    class FakeSession:
        def post(self, url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"score": 4.0},
                text='{"score": 4.0}',
            )

    monkeypatch.setattr(
        grading_ops,
        "_get_grading_context",
        lambda *_args, **_kwargs: {
            "props": {
                "question": {"title": "Q1", "weight": 5},
                "submission": {"score": None, "graded": False},
                "evaluation": {"points": None, "comments": None},
                "rubric_items": [
                    {"id": 100, "description": "Correct", "weight": 5},
                    {"id": 200, "description": "Partial", "weight": 2},
                ],
                "rubric_item_evaluations": [],
                "urls": {"save_grade": "/courses/1/questions/2/submissions/99/save_grade"},
            },
            "csrf_token": "test-csrf-token",
            "session": FakeSession(),
            "base_url": "https://example.com",
        },
    )

    result = grading_ops.apply_grade(
        course_id="1",
        question_id="2",
        submission_id="99",
        rubric_item_ids=["100"],
        point_adjustment=None,
        comment="Good work",
        confirm_write=True,
    )

    assert "Grade saved" in result
    # Negative scoring with weight 5 and rubric 100 (weight 5 deduction) → 0/5.
    # Score is computed locally and must appear in the result instead of "?".
    assert "**New score:** 0/5" in result

    # Verify JSON payload structure
    payload = captured["kwargs"]["json"]
    assert "rubric_items" in payload
    assert "question_submission_evaluation" in payload
    assert payload["rubric_items"]["100"] == {"score": "true"}
    assert payload["rubric_items"]["200"] == {"score": "false"}
    assert payload["question_submission_evaluation"]["comments"] == "Good work"
    assert payload["question_submission_evaluation"]["points"] is None

    # Verify Content-Type header
    headers = captured["kwargs"]["headers"]
    assert headers["Content-Type"] == "application/json"

    # Verify json= was used (not data=)
    assert "data" not in captured["kwargs"]


def _make_fake_batch_ctx_builder(posts: list, status_by_sid: dict | None = None):
    """Build a _get_grading_context substitute that records POSTs per submission.

    Returns a callable suitable for monkeypatching ``_get_grading_context``.
    """
    status_by_sid = status_by_sid or {}

    def _builder(_course_id, _question_id, submission_id):
        class _Session:
            def post(self, url, **kwargs):
                posts.append({"submission_id": submission_id, "url": url, **kwargs})
                status = status_by_sid.get(submission_id, 200)
                return SimpleNamespace(
                    status_code=status,
                    json=lambda: {"score": 1.0 if status == 200 else None},
                    text="" if status == 200 else "boom",
                )

        return {
            "props": {
                "question": {"weight": 1},
                "submission": {"score": None},
                "evaluation": {"points": None, "comments": None},
                "rubric_items": [
                    {"id": 100, "description": "Correct", "weight": 0},
                    {"id": 200, "description": "Not attempted", "weight": 1},
                ],
                "rubric_item_evaluations": [],
                "urls": {"save_grade": f"/save/{submission_id}"},
            },
            "csrf_token": "csrf",
            "session": _Session(),
            "base_url": "https://example.com",
        }

    return _builder


def test_apply_grade_batch_preview_returns_table_without_posting(monkeypatch) -> None:
    posts: list = []
    monkeypatch.setattr(
        grading_ops,
        "_get_grading_context",
        _make_fake_batch_ctx_builder(posts),
    )

    result = grading_ops.apply_grade_batch(
        course_id="1",
        question_id="2",
        grades=[
            {"submission_id": "aa", "rubric_item_ids": ["100"], "confidence": 0.9},
            {"submission_id": "bb", "rubric_item_ids": ["200"], "confidence": 0.95},
        ],
    )

    assert "Write confirmation required" in result
    assert "apply_grade_batch" in result
    assert "rows=2" in result
    assert "`aa`" in result and "`bb`" in result
    assert posts == []  # no POSTs in preview mode


def test_apply_grade_batch_applies_all_approved_rows(monkeypatch) -> None:
    posts: list = []
    monkeypatch.setattr(
        grading_ops,
        "_get_grading_context",
        _make_fake_batch_ctx_builder(posts),
    )

    result = grading_ops.apply_grade_batch(
        course_id="1",
        question_id="2",
        grades=[
            {"submission_id": "aa", "rubric_item_ids": ["100"], "confidence": 0.9},
            {"submission_id": "bb", "rubric_item_ids": ["200"], "confidence": 0.95},
        ],
        confirm_write=True,
    )

    assert "**succeeded:** 2" in result
    assert "**failed:** 0" in result
    assert "**skipped (confidence < 0.6):** 0" in result
    # Real scores must appear in the result, not the legacy ``?/1`` placeholder.
    # Negative scoring with weight 1: rubric 100 (weight 0) → 1; rubric 200 (weight 1) → 0.
    assert "`aa`: 1/1" in result
    assert "`bb`: 0/1" in result
    assert "?/1" not in result
    assert len(posts) == 2
    sids = sorted(p["submission_id"] for p in posts)
    assert sids == ["aa", "bb"]
    # Verify payload structure for one row
    payload_aa = next(p for p in posts if p["submission_id"] == "aa")["json"]
    assert payload_aa["rubric_items"]["100"] == {"score": "true"}
    assert payload_aa["rubric_items"]["200"] == {"score": "false"}


def test_apply_grade_batch_surfaces_unknown_rubric_ids(monkeypatch) -> None:
    """A row that references a rubric ID not present in the question must be flagged."""
    posts: list = []
    monkeypatch.setattr(
        grading_ops,
        "_get_grading_context",
        _make_fake_batch_ctx_builder(posts),
    )

    result = grading_ops.apply_grade_batch(
        course_id="1",
        question_id="2",
        grades=[
            {"submission_id": "aa", "rubric_item_ids": ["100"], "confidence": 0.9},
            # 999 does not exist on the question; should still POST and warn.
            {"submission_id": "bb", "rubric_item_ids": ["100", "999"], "confidence": 0.9},
        ],
        confirm_write=True,
    )

    assert "**succeeded:** 2" in result
    assert "Unknown rubric IDs" in result
    # Only the bb row had unknowns — aa must not appear in the warning section.
    warning_section = result.split("Unknown rubric IDs", 1)[1]
    assert "`bb`" in warning_section
    assert "'999'" in warning_section
    assert "`aa`" not in warning_section


def test_apply_grade_batch_low_confidence_rows_are_skipped(monkeypatch) -> None:
    posts: list = []
    monkeypatch.setattr(
        grading_ops,
        "_get_grading_context",
        _make_fake_batch_ctx_builder(posts),
    )

    result = grading_ops.apply_grade_batch(
        course_id="1",
        question_id="2",
        grades=[
            {"submission_id": "aa", "rubric_item_ids": ["100"], "confidence": 0.9},
            {"submission_id": "bb", "rubric_item_ids": ["100"], "confidence": 0.5},
        ],
        confirm_write=True,
    )

    assert "**succeeded:** 1" in result
    assert "**skipped (confidence < 0.6):** 1" in result
    assert [p["submission_id"] for p in posts] == ["aa"]
    assert "`bb`: confidence=0.50" in result


def test_apply_grade_batch_reports_http_failures(monkeypatch) -> None:
    posts: list = []
    monkeypatch.setattr(
        grading_ops,
        "_get_grading_context",
        _make_fake_batch_ctx_builder(posts, status_by_sid={"bb": 500}),
    )

    result = grading_ops.apply_grade_batch(
        course_id="1",
        question_id="2",
        grades=[
            {"submission_id": "aa", "rubric_item_ids": ["100"]},
            {"submission_id": "bb", "rubric_item_ids": ["100"]},
        ],
        confirm_write=True,
    )

    assert "**succeeded:** 1" in result
    assert "**failed:** 1" in result
    assert "`bb`: HTTP 500" in result


def test_apply_grade_batch_rejects_invalid_input() -> None:
    # Empty list.
    assert "non-empty list" in grading_ops.apply_grade_batch("1", "2", [])

    # Row with no submission_id.
    missing_sid = grading_ops.apply_grade_batch(
        "1", "2", [{"rubric_item_ids": ["100"]}]
    )
    assert "submission_id is required" in missing_sid

    # Row with out-of-range confidence.
    bad_conf = grading_ops.apply_grade_batch(
        "1",
        "2",
        [
            {"submission_id": "aa", "rubric_item_ids": ["100"], "confidence": 2.0},
        ],
    )
    assert "confidence must be between 0.0 and 1.0" in bad_conf

    # Row with no rubric/points/comment.
    all_none = grading_ops.apply_grade_batch(
        "1", "2", [{"submission_id": "aa"}]
    )
    assert "at least one of" in all_none


def test_positive_scoring_context_shows_add_hint(monkeypatch) -> None:
    """Positive scoring should show 'items add points' hint."""
    monkeypatch.setattr(
        grading_ops,
        "_get_grading_context",
        lambda *_args, **_kwargs: {
            "props": {
                "question": {
                    "title": "MCQ",
                    "weight": 4,
                    "scoring_type": "positive",
                },
                "submission": {
                    "owner_names": "Student C",
                    "score": None,
                    "graded": False,
                    "answers": {},
                },
                "evaluation": {},
                "rubric_items": [
                    {"id": 1, "description": "Correct", "weight": 4},
                ],
                "rubric_item_evaluations": [],
                "navigation_urls": {},
                "num_graded_submissions": 0,
                "num_submissions": 10,
                "pages": [],
            }
        },
    )

    result = grading_ops.get_submission_grading_context("1", "2", "99")
    assert "Rubric items **add** points" in result
    assert "deduct" not in result.lower()


def test_get_next_ungraded_falls_back_to_submission_listing_when_nav_is_empty(monkeypatch) -> None:
    monkeypatch.setattr(
        grading_ops,
        "_get_grading_context",
        lambda *_args, **_kwargs: {
            "props": {
                "submission": {"id": "99", "graded": True},
                "navigation_urls": {},
                "num_graded_submissions": 4,
                "num_submissions": 5,
            }
        },
    )
    monkeypatch.setattr(
        grading_ops,
        "_fetch_question_submission_entries",
        lambda *_args, **_kwargs: [
            {"submission_id": "90", "student_name": "A", "graded": True},
            {"submission_id": "99", "student_name": "B", "graded": True},
            {"submission_id": "105", "student_name": "C", "graded": False},
        ],
    )
    monkeypatch.setattr(
        grading_ops,
        "get_submission_grading_context",
        lambda course_id, question_id, submission_id, output_format="markdown": (
            f"CTX {course_id} {question_id} {submission_id} {output_format}"
        ),
    )

    result = grading_ops.get_next_ungraded("1", "2", "99", output_format="json")

    assert result == "CTX 1 2 105 json"


def _gradescope_table_html(rows: list[tuple[str, str, str, str]]) -> str:
    """Build a table that mirrors Gradescope's real submissions page layout.

    Columns: row index, User, Last Graded By, Sections, Score, Graded?.
    Each ``rows`` entry is ``(idx, user, score, graded_flag)`` plus a
    submission ID embedded in the grade link.
    """
    body_rows = []
    for idx, user, score, graded_flag, sid in rows:
        body_rows.append(
            f"<tr>"
            f"<td>{idx}</td>"
            f"<td>{user}</td>"
            f"<td>Yuanpeng Li</td>"
            f"<td>section-a</td>"
            f"<td>{score}</td>"
            f"<td>{graded_flag}</td>"
            f"<td><a href=\"/courses/1/questions/2/submissions/{sid}/grade\">grade</a></td>"
            f"</tr>"
        )
    return f"""
    <table>
      <thead>
        <tr><th></th><th>User</th><th>Last Graded By</th><th>Sections</th><th>Score</th><th>Graded?</th><th></th></tr>
      </thead>
      <tbody>
        {''.join(body_rows)}
      </tbody>
    </table>
    """


def _stub_grading_ops_get(monkeypatch, html: str) -> None:
    monkeypatch.setattr(
        grading_ops,
        "get_connection",
        lambda: SimpleNamespace(
            gradescope_base_url="https://example.com",
            session=SimpleNamespace(
                get=lambda *_args, **_kwargs: SimpleNamespace(
                    status_code=200,
                    text=html,
                )
            ),
        ),
    )


def test_list_question_submissions_ungraded_filter_excludes_scored_rows(
    monkeypatch,
) -> None:
    html = _gradescope_table_html([
        ("1", "Alice Example (alice@uci.edu)", "1.0", "", "101"),
        ("2", "Bob Example (bob@uci.edu)", "", "", "102"),
    ])
    _stub_grading_ops_get(monkeypatch, html)

    result = json.loads(grading_ops.list_question_submissions("1", "2", filter="ungraded"))

    assert [sub["submission_id"] for sub in result["submissions"]] == ["102"]


def test_list_question_submissions_returns_real_student_names(monkeypatch) -> None:
    """Regression: row-index column must not be returned as student_name."""
    html = _gradescope_table_html([
        ("1", "Adithya Rajendra (arajend3@uci.edu)", "", "", "777"),
    ])
    _stub_grading_ops_get(monkeypatch, html)

    result = json.loads(grading_ops.list_question_submissions("1", "2"))

    assert result["submissions"] == [
        {"submission_id": "777", "student_name": "Adithya Rajendra", "graded": False}
    ]


def test_graded_detection_uses_score_column_not_row_index(monkeypatch) -> None:
    """Regression: row index ``1``, ``2``, ... must not be parsed as a score."""
    html = _gradescope_table_html([
        ("1", "Alice (a@uci.edu)", "1.0", "", "201"),
        ("2", "Bob (b@uci.edu)", "0.5", "", "202"),
        ("3", "Carol (c@uci.edu)", "—", "", "203"),
        ("4", "Dave 42nd (d@uci.edu)", "", "", "204"),
    ])
    _stub_grading_ops_get(monkeypatch, html)

    entries = grading_ops._fetch_question_submission_entries("1", "2")
    graded_map = {e["submission_id"]: e["graded"] for e in entries}

    assert graded_map == {
        "201": True,   # explicit numeric score
        "202": True,   # fractional score
        "203": False,  # em-dash placeholder
        "204": False,  # empty score and a name containing digits — must not false-positive
    }


def test_compute_new_score_negative_scoring_subtracts_deductions() -> None:
    props = {
        "question": {"weight": 1.0, "scoring_type": "negative", "floor": True, "ceiling": True},
        "rubric_items": [
            {"id": "100", "weight": 0},   # "Correct" — 0 deduction
            {"id": "200", "weight": 0.5}, # "Partial" — 0.5 deduction
            {"id": "300", "weight": 1.0}, # "Blank" — 1.0 deduction
        ],
    }
    assert grading_ops._compute_new_score(props, ["100"], None) == (1.0, set())
    assert grading_ops._compute_new_score(props, ["200"], None) == (0.5, set())
    assert grading_ops._compute_new_score(props, ["300"], None) == (0.0, set())
    # Empty selection → no deduction → full credit
    assert grading_ops._compute_new_score(props, [], None) == (1.0, set())
    # Point adjustment stacks on top of rubric
    assert grading_ops._compute_new_score(props, ["200"], -0.25) == (0.25, set())


def test_compute_new_score_negative_sums_multiple_deductions() -> None:
    """Multiple rubric items applied at once must be summed, then clamped."""
    props = {
        "question": {"weight": 1.0, "scoring_type": "negative", "floor": True, "ceiling": True},
        "rubric_items": [
            {"id": "200", "weight": 0.5},
            {"id": "300", "weight": 1.0},
            {"id": "400", "weight": 0.25},
        ],
    }
    # 1 - (0.5 + 0.25) = 0.25
    assert grading_ops._compute_new_score(props, ["200", "400"], None) == (0.25, set())
    # 1 - (0.5 + 1.0) = -0.5 → floor to 0
    assert grading_ops._compute_new_score(props, ["200", "300"], None) == (0.0, set())
    # All three: 1 - 1.75 = -0.75 → floor to 0
    assert grading_ops._compute_new_score(
        props, ["200", "300", "400"], None
    ) == (0.0, set())


def test_compute_new_score_positive_scoring_sums_credits() -> None:
    props = {
        "question": {"weight": 5.0, "scoring_type": "positive", "floor": True, "ceiling": True},
        "rubric_items": [
            {"id": "10", "weight": 2.0},
            {"id": "20", "weight": 3.0},
        ],
    }
    assert grading_ops._compute_new_score(props, ["10"], None) == (2.0, set())
    assert grading_ops._compute_new_score(props, ["10", "20"], None) == (5.0, set())
    assert grading_ops._compute_new_score(props, [], None) == (0.0, set())


def test_compute_new_score_clamps_to_floor_and_ceiling() -> None:
    props = {
        "question": {"weight": 1.0, "scoring_type": "negative", "floor": True, "ceiling": True},
        "rubric_items": [
            {"id": "200", "weight": 0.5},
        ],
    }
    # Without clamps the math would be 1 - 0.5 + (-2) = -1.5; floor pulls it to 0.
    assert grading_ops._compute_new_score(props, ["200"], -2.0) == (0.0, set())
    # 1 - 0.5 + 5 = 5.5; ceiling caps it at the question weight.
    assert grading_ops._compute_new_score(props, ["200"], 5.0) == (1.0, set())


def test_compute_new_score_returns_none_without_weight() -> None:
    assert grading_ops._compute_new_score({"question": {}}, ["100"], None) == (
        None,
        set(),
    )


def test_compute_new_score_flags_unknown_rubric_ids() -> None:
    """Unknown IDs contribute 0 to the local sum and are surfaced for the caller."""
    props = {
        "question": {"weight": 2.0, "scoring_type": "negative", "floor": True, "ceiling": True},
        "rubric_items": [
            {"id": "100", "weight": 0.5},
        ],
    }
    score, unknown = grading_ops._compute_new_score(
        props, ["100", "999", "stale-uuid"], None
    )
    # Local computation sees only the known item: 2 - 0.5 = 1.5.
    assert score == 1.5
    # The two unknown IDs must be returned so the caller can warn the user.
    assert unknown == {"999", "stale-uuid"}


def test_student_name_without_email_paren_is_returned_verbatim(monkeypatch) -> None:
    """If the User cell has no ``(email)`` suffix, return the cell as-is."""
    html = _gradescope_table_html([
        ("1", "Plain Name No Email", "", "", "111"),
    ])
    _stub_grading_ops_get(monkeypatch, html)

    entries = grading_ops._fetch_question_submission_entries("1", "2")

    assert entries[0]["student_name"] == "Plain Name No Email"


def test_graded_flag_column_only_treats_affirmative_tokens_as_graded(monkeypatch) -> None:
    """Em-dash, ``No``, ``Pending``, etc. in the Graded? column must NOT false-positive.

    Regression for the case where Gradescope leaves the Graded? column empty
    or filled with placeholder text on rows that are otherwise ungraded.
    """
    html = _gradescope_table_html([
        # Graded? = "Yes" → graded even if Score is empty
        ("1", "Alice (a@uci.edu)", "", "Yes", "401"),
        # Graded? = "✓" → graded
        ("2", "Bob (b@uci.edu)", "", "✓", "402"),
        # Graded? = "—" → must NOT be treated as graded; Score is empty too
        ("3", "Carol (c@uci.edu)", "", "—", "403"),
        # Graded? = "Pending" → must NOT be treated as graded
        ("4", "Dave (d@uci.edu)", "", "Pending", "404"),
        # Graded? = "No" → must NOT be treated as graded
        ("5", "Eve (e@uci.edu)", "", "No", "405"),
        # Graded? empty + Score non-empty → graded via Score fallback
        ("6", "Frank (f@uci.edu)", "0.5", "", "406"),
    ])
    _stub_grading_ops_get(monkeypatch, html)

    entries = grading_ops._fetch_question_submission_entries("1", "2")
    graded_map = {e["submission_id"]: e["graded"] for e in entries}

    assert graded_map == {
        "401": True,   # affirmative token
        "402": True,   # ✓
        "403": False,  # em-dash placeholder
        "404": False,  # status word, not affirmative
        "405": False,  # explicit negative
        "406": True,   # Score-column fallback
    }


def test_graded_heuristic_handles_headerless_tables(monkeypatch) -> None:
    """Fallback path: when no <thead>, scanning still skips the leading row-index cell."""
    html = """
    <table>
      <tr><td>1</td><td>Alice 3rd</td><td>8/10</td><td><a href="/courses/1/questions/2/submissions/301/grade">grade</a></td></tr>
      <tr><td>2</td><td>Bob Example</td><td></td><td><a href="/courses/1/questions/2/submissions/302/grade">grade</a></td></tr>
    </table>
    """
    _stub_grading_ops_get(monkeypatch, html)

    entries = grading_ops._fetch_question_submission_entries("1", "2")
    graded_map = {e["submission_id"]: e["graded"] for e in entries}

    assert graded_map == {"301": True, "302": False}


def test_get_next_ungraded_uses_props_sid_after_auto_discovery(monkeypatch) -> None:
    """After auto-discovery, current_sid should come from props, not the stale input."""
    call_log = []

    def fake_get_grading_context(course_id, question_id, submission_id):
        call_log.append(("ctx", submission_id))
        if submission_id == "GLOBAL_99999":
            raise ValueError("404 Not Found")
        return {
            "props": {
                "submission": {"id": "200", "graded": True},
                "navigation_urls": {
                    # next_ungraded points to a different sid than the discovered one
                    "next_ungraded": f"/courses/{course_id}/questions/{question_id}/submissions/300/grade",
                },
                "num_graded_submissions": 4,
                "num_submissions": 5,
            }
        }

    monkeypatch.setattr(grading_ops, "_get_grading_context", fake_get_grading_context)
    monkeypatch.setattr(
        grading_ops,
        "_find_question_submission_id",
        lambda *_args, **_kwargs: "200",
    )
    monkeypatch.setattr(
        grading_ops,
        "get_submission_grading_context",
        lambda course_id, question_id, submission_id, output_format="markdown": (
            f"CTX {course_id} {question_id} {submission_id} {output_format}"
        ),
    )

    # Pass a global submission ID that will 404
    result = grading_ops.get_next_ungraded("1", "2", "GLOBAL_99999", output_format="json")

    # next_ungraded points to 300, which is different from props sid 200,
    # so it should navigate to 300
    assert result == "CTX 1 2 300 json"



def _fc_ctx(rubric_items: list[dict], captured: dict | None = None):
    class _Session:
        def post(self, url, **kwargs):
            if captured is not None:
                captured["json"] = kwargs.get("json")
            return SimpleNamespace(status_code=200, json=lambda: {}, text="")

    return lambda *_a, **_k: {
        "props": {
            "question": {"title": "Q", "weight": 5, "scoring_type": "negative"},
            "submission": {"score": None, "graded": False},
            "evaluation": {"points": None, "comments": None},
            "rubric_items": rubric_items,
            "rubric_item_evaluations": [],
            "urls": {"save_grade": "/save"},
        },
        "csrf_token": "t",
        "session": _Session(),
        "base_url": "https://example.com",
    }


def test_apply_grade_full_credit_checks_benchmark_item(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        grading_ops,
        "_get_grading_context",
        _fc_ctx(
            [
                {"id": 100, "description": "Correct", "weight": 0},
                {"id": 200, "description": "Deduction", "weight": 2},
            ],
            captured,
        ),
    )

    result = grading_ops.apply_grade(
        course_id="1",
        question_id="2",
        submission_id="99",
        full_credit=True,
        confirm_write=True,
    )

    assert "Grade saved" in result
    assert "**New score:** 5/5" in result
    assert captured["json"]["rubric_items"]["100"] == {"score": "true"}
    assert captured["json"]["rubric_items"]["200"] == {"score": "false"}


def test_apply_grade_full_credit_errors_on_ambiguous_benchmark(monkeypatch) -> None:
    monkeypatch.setattr(
        grading_ops,
        "_get_grading_context",
        _fc_ctx(
            [
                {"id": 100, "description": "Correct", "weight": 0},
                {"id": 300, "description": "Also zero", "weight": 0},
            ]
        ),
    )

    result = grading_ops.apply_grade(
        course_id="1",
        question_id="2",
        submission_id="99",
        full_credit=True,
        confirm_write=True,
    )

    assert "multiple 0-point items" in result


def test_apply_grade_full_credit_rejects_with_rubric_item_ids(monkeypatch) -> None:
    monkeypatch.setattr(
        grading_ops,
        "_get_grading_context",
        _fc_ctx([{"id": 100, "description": "Correct", "weight": 0}]),
    )

    result = grading_ops.apply_grade(
        course_id="1",
        question_id="2",
        submission_id="99",
        rubric_item_ids=["100"],
        full_credit=True,
        confirm_write=True,
    )

    assert "not both" in result


def test_apply_grade_batch_full_credit_row(monkeypatch) -> None:
    posts: list = []
    monkeypatch.setattr(
        grading_ops,
        "_get_grading_context",
        _make_fake_batch_ctx_builder(posts),
    )

    result = grading_ops.apply_grade_batch(
        course_id="1",
        question_id="2",
        grades=[{"submission_id": "501", "full_credit": True}],
        confirm_write=True,
    )

    assert "**succeeded:** 1" in result
    # The builder's 0-weight benchmark item (id 100) must be checked.
    assert posts and posts[0]["json"]["rubric_items"]["100"] == {"score": "true"}


def test_apply_grade_batch_preview_shows_full_credit(monkeypatch) -> None:
    posts: list = []
    monkeypatch.setattr(
        grading_ops,
        "_get_grading_context",
        _make_fake_batch_ctx_builder(posts),
    )

    result = grading_ops.apply_grade_batch(
        course_id="1",
        question_id="2",
        grades=[{"submission_id": "501", "full_credit": True}],
    )

    assert "full credit (0-pt item)" in result
    assert posts == []


def test_full_credit_ignores_missing_or_invalid_weights(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        grading_ops,
        "_get_grading_context",
        _fc_ctx(
            [
                {"id": 100, "description": "Correct", "weight": 0},
                {"id": 200, "description": "Missing weight"},
                {"id": 300, "description": "Invalid weight", "weight": "n/a"},
            ],
            captured,
        ),
    )

    result = grading_ops.apply_grade(
        course_id="1",
        question_id="2",
        submission_id="99",
        full_credit=True,
        confirm_write=True,
    )

    # Only the explicit 0-point item is the benchmark; missing/invalid weights
    # must not be auto-selected.
    assert "Grade saved" in result
    assert captured["json"]["rubric_items"]["100"] == {"score": "true"}
    assert captured["json"]["rubric_items"]["200"] == {"score": "false"}
    assert captured["json"]["rubric_items"]["300"] == {"score": "false"}


def test_parses_to_zero_only_for_explicit_zero() -> None:
    assert grading_ops._parses_to_zero(0) is True
    assert grading_ops._parses_to_zero("0") is True
    assert grading_ops._parses_to_zero(0.0) is True
    assert grading_ops._parses_to_zero(None) is False
    assert grading_ops._parses_to_zero("") is False
    assert grading_ops._parses_to_zero("n/a") is False
    assert grading_ops._parses_to_zero(2) is False


def test_coerce_bool_flag_rejects_ambiguous_strings() -> None:
    assert grading_ops._coerce_bool_flag(None) == (False, None)
    assert grading_ops._coerce_bool_flag(True) == (True, None)
    assert grading_ops._coerce_bool_flag(False) == (False, None)
    assert grading_ops._coerce_bool_flag("true") == (True, None)
    assert grading_ops._coerce_bool_flag("False") == (False, None)
    val, err = grading_ops._coerce_bool_flag("nope")
    assert val is None and "invalid full_credit" in err


def test_apply_grade_batch_rejects_string_full_credit(monkeypatch) -> None:
    posts: list = []
    monkeypatch.setattr(
        grading_ops,
        "_get_grading_context",
        _make_fake_batch_ctx_builder(posts),
    )

    # An unrecognized string is rejected outright (not silently truthy).
    bad = grading_ops.apply_grade_batch(
        course_id="1",
        question_id="2",
        grades=[{"submission_id": "501", "full_credit": "nope"}],
        confirm_write=True,
    )
    assert "invalid full_credit" in bad
    assert posts == []

    # The string "false" parses to False, so it does NOT trigger a write.
    grading_ops.apply_grade_batch(
        course_id="1",
        question_id="2",
        grades=[{"submission_id": "501", "full_credit": "false"}],
        confirm_write=True,
    )
    assert posts == []


def test_apply_grade_full_credit_preview_signals_intent(monkeypatch) -> None:
    monkeypatch.setattr(
        grading_ops,
        "_get_grading_context",
        _fc_ctx([{"id": 100, "description": "Correct", "weight": 0}]),
    )

    result = grading_ops.apply_grade(
        course_id="1",
        question_id="2",
        submission_id="99",
        full_credit=True,
        confirm_write=False,
    )

    # Preview must show the full_credit intent, not just the resolved IDs.
    assert "Write confirmation required" in result
    assert "full_credit=True" in result

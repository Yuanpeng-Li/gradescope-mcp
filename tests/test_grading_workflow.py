from gradescope_mcp.tools import grading_workflow
import types


class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self) -> None:
        return None


class _FakeSession:
    def __init__(self):
        self.urls: list[str] = []

    def get(self, url: str):
        self.urls.append(url)
        return _FakeResponse(b"image-bytes")


class _FakeConn:
    def __init__(self, session: _FakeSession):
        self.session = session


def test_cache_relevant_pages_uses_authenticated_session(monkeypatch) -> None:
    session = _FakeSession()
    monkeypatch.setattr(
        grading_workflow,
        "_resolve_assignment_questions",
        lambda *_args, **_kwargs: ("test-assign", {"test-question": {"index": 1}}, None),
    )
    monkeypatch.setattr(grading_workflow, "_get_grading_context", lambda *_args, **_kwargs: {
        "props": {
            "question": {
                "parameters": {
                    "crop_rect_list": [{"page_number": 2, "x1": 0, "x2": 100, "y1": 0, "y2": 100}]
                }
            },
            "pages": [
                {"number": 1, "url": "https://example.com/1.jpg"},
                {"number": 2, "url": "https://example.com/2.jpg"},
                {"number": 3, "url": "https://example.com/3.jpg"},
            ],
        }
    })
    monkeypatch.setattr(grading_workflow, "get_connection", lambda: _FakeConn(session))

    result = grading_workflow.cache_relevant_pages("1", "test-assign", "test-question", "test-submission")

    assert "Cached 3 relevant page(s)" in result
    assert session.urls == [
        "https://example.com/1.jpg",
        "https://example.com/2.jpg",
        "https://example.com/3.jpg",
    ]
    assert (
        grading_workflow.get_artifact_dir(
            "gradescope-pages-test-assign-test-question-test-submission"
        ).joinpath("page_2.jpg")
        .read_bytes()
        == b"image-bytes"
    )


def test_cache_relevant_pages_include_all_pages_bypasses_filter(monkeypatch) -> None:
    """include_all_pages=True downloads every page regardless of crop tagging."""
    session = _FakeSession()
    monkeypatch.setattr(
        grading_workflow,
        "_resolve_assignment_questions",
        lambda *_args, **_kwargs: ("a", {"q": {"index": 1}}, None),
    )
    monkeypatch.setattr(grading_workflow, "_get_grading_context", lambda *_args, **_kwargs: {
        "props": {
            "question": {
                "parameters": {
                    "crop_rect_list": [{"page_number": 2, "x1": 0, "x2": 100, "y1": 0, "y2": 100}]
                }
            },
            "pages": [
                {"number": n, "url": f"https://example.com/{n}.jpg"}
                for n in (1, 2, 3, 4, 5, 6, 7)
            ],
        }
    })
    monkeypatch.setattr(grading_workflow, "get_connection", lambda: _FakeConn(session))

    # Default filter: only pages 1, 2, 3 (crop on page 2 ± 1).
    result_default = grading_workflow.cache_relevant_pages("1", "a", "q", "s")
    assert "Cached 3 relevant page(s)" in result_default
    assert len(session.urls) == 3

    # Reset and fetch all pages.
    session.urls.clear()
    result_all = grading_workflow.cache_relevant_pages(
        "1", "a", "q", "s", include_all_pages=True
    )
    assert "Cached 7 relevant page(s)" in result_all
    assert len(session.urls) == 7
    assert session.urls[-1] == "https://example.com/7.jpg"


def test_prepare_grading_artifact_auto_resolves_assignment(monkeypatch) -> None:
    class _Assignment:
        def __init__(self, assignment_id: str):
            self.assignment_id = assignment_id

    class _FakeConn:
        def __init__(self):
            self.account = types.SimpleNamespace(
                get_assignments=lambda _course_id: [_Assignment("bad"), _Assignment("good")]
            )

    def _fake_fetch_assignment_questions(_course_id: str, assignment_id: str) -> dict[str, dict]:
        if assignment_id == "bad":
            return {"other": {"index": 1}}
        if assignment_id == "good":
            return {"q1": {"index": 4, "weight": 2, "type": "free_response"}}
        raise AssertionError(f"unexpected assignment_id: {assignment_id}")

    monkeypatch.setattr(grading_workflow, "get_connection", lambda: _FakeConn())
    monkeypatch.setattr(grading_workflow, "_fetch_assignment_questions", _fake_fetch_assignment_questions)
    monkeypatch.setattr(grading_workflow, "_find_first_submission_id", lambda *_args: "sub1")
    monkeypatch.setattr(
        grading_workflow,
        "_get_grading_context",
        lambda *_args, **_kwargs: {
            "props": {
                "question": {
                    "weight": 2,
                    "type": "free_response",
                    "parameters": {"crop_rect_list": [{"page_number": 1, "x1": 0, "x2": 100, "y1": 0, "y2": 20}]},
                },
                "rubric_items": [{"id": 10, "description": "Correct", "weight": 2}],
                "pages": [{"number": 1, "url": "https://example.com/1.jpg"}],
            }
        },
    )
    monkeypatch.setattr(
        grading_workflow,
        "_extract_outline_prompt_and_reference",
        lambda *_args, **_kwargs: ("Prompt text", None),
    )

    result = grading_workflow.prepare_grading_artifact("course1", "bad", "q1")

    assert "Resolution: question `q1` was not found in assignment `bad`; auto-resolved to `good`." in result
    artifact = grading_workflow.get_artifact_path(
        "gradescope-grading-good-q1.md"
    ).read_text(encoding="utf-8")
    assert "- assignment_id: `good`" in artifact
    assert "- resolution: question `q1` was not found in assignment `bad`; auto-resolved to `good`." in artifact


def test_compute_readiness_treats_scanned_rubric_context_as_partially_ready() -> None:
    readiness, reasons, action = grading_workflow._compute_readiness(
        prompt_text=None,
        reference_answer=None,
        crop_rects=[{"page_number": 1, "x1": 0, "x2": 100, "y1": 0, "y2": 20}],
        pages=[{"number": 1, "url": "https://example.com/1.jpg"}],
        rubric_items=[{"id": "10", "description": "Correct", "weight": 2}],
    )

    assert readiness >= 0.55
    assert action == "partially_ready"
    assert any("rubric items are available" in reason for reason in reasons)

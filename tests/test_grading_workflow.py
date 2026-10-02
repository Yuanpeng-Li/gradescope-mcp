from gradescope_mcp.tools import grading_workflow
import types


_JPEG = b"\xff\xd8\xff\xe0image-bytes"
_GS = "https://www.gradescope.com"


class _FakeResponse:
    status_code = 200
    headers = {"Content-Type": "image/jpeg"}

    def __init__(self, content: bytes):
        self.content = content

    def close(self) -> None:
        return None


class _FakeSession:
    def __init__(self):
        self.urls: list[str] = []

    def get(self, url: str, **_kwargs):
        self.urls.append(url)
        return _FakeResponse(_JPEG)


class _FakeConn:
    gradescope_base_url = _GS

    def __init__(self, session: _FakeSession):
        self.session = session


def test_cache_relevant_pages_uses_authenticated_session(monkeypatch) -> None:
    session = _FakeSession()
    monkeypatch.setattr(
        grading_workflow,
        "_resolve_assignment_questions",
        lambda *_args, **_kwargs: ("70", {"80": {"index": 1}}, None),
    )
    monkeypatch.setattr(grading_workflow, "_get_grading_context", lambda *_args, **_kwargs: {
        "props": {
            "question": {
                "parameters": {
                    "crop_rect_list": [{"page_number": 2, "x1": 0, "x2": 100, "y1": 0, "y2": 100}]
                }
            },
            "pages": [
                {"number": 1, "url": f"{_GS}/files/1.jpg"},
                {"number": 2, "url": f"{_GS}/files/2.jpg"},
                {"number": 3, "url": f"{_GS}/files/3.jpg"},
            ],
        }
    })
    monkeypatch.setattr(grading_workflow, "get_connection", lambda: _FakeConn(session))

    result = grading_workflow.cache_relevant_pages("1", "70", "80", "90")

    assert "Cached 3 relevant page(s)" in result
    assert session.urls == [
        f"{_GS}/files/1.jpg",
        f"{_GS}/files/2.jpg",
        f"{_GS}/files/3.jpg",
    ]
    assert (
        grading_workflow.get_artifact_dir(
            "gradescope-pages-70-80-90"
        ).joinpath("page_2.jpg")
        .read_bytes()
        == _JPEG
    )


def test_cache_relevant_pages_include_all_pages_bypasses_filter(monkeypatch) -> None:
    """include_all_pages=True downloads every page regardless of crop tagging."""
    session = _FakeSession()
    monkeypatch.setattr(
        grading_workflow,
        "_resolve_assignment_questions",
        lambda *_args, **_kwargs: ("70", {"80": {"index": 1}}, None),
    )
    monkeypatch.setattr(grading_workflow, "_get_grading_context", lambda *_args, **_kwargs: {
        "props": {
            "question": {
                "parameters": {
                    "crop_rect_list": [{"page_number": 2, "x1": 0, "x2": 100, "y1": 0, "y2": 100}]
                }
            },
            "pages": [
                {"number": n, "url": f"{_GS}/files/{n}.jpg"}
                for n in (1, 2, 3, 4, 5, 6, 7)
            ],
        }
    })
    monkeypatch.setattr(grading_workflow, "get_connection", lambda: _FakeConn(session))

    # Crop-only filter: only pages 1, 2, 3 (crop on page 2 ± 1).
    result_crop = grading_workflow.cache_relevant_pages(
        "1", "70", "80", "90", include_all_pages=False
    )
    assert "Cached 3 relevant page(s)" in result_crop
    assert len(session.urls) == 3

    # Reset and fetch all pages (the default, matching the MCP wrapper).
    session.urls.clear()
    result_all = grading_workflow.cache_relevant_pages("1", "70", "80", "90")
    assert "Cached 7 relevant page(s)" in result_all
    assert len(session.urls) == 7
    assert session.urls[-1] == f"{_GS}/files/7.jpg"


def test_prepare_grading_artifact_auto_resolves_assignment(monkeypatch) -> None:
    class _Assignment:
        def __init__(self, assignment_id: str):
            self.assignment_id = assignment_id

    class _FakeConn:
        def __init__(self):
            self.account = types.SimpleNamespace(
                get_assignments=lambda _course_id: [_Assignment("111"), _Assignment("222")]
            )

    def _fake_fetch_assignment_questions(_course_id: str, assignment_id: str) -> dict[str, dict]:
        if assignment_id == "111":
            return {"999": {"index": 1}}
        if assignment_id == "222":
            return {"301": {"index": 4, "weight": 2, "type": "free_response"}}
        raise AssertionError(f"unexpected assignment_id: {assignment_id}")

    monkeypatch.setattr(grading_workflow, "get_connection", lambda: _FakeConn())
    monkeypatch.setattr(grading_workflow, "_fetch_assignment_questions", _fake_fetch_assignment_questions)
    monkeypatch.setattr(grading_workflow, "_find_first_submission_id", lambda *_args: "401")
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
        lambda *_args, **_kwargs: ("Prompt text", None, None),
    )

    result = grading_workflow.prepare_grading_artifact("100", "111", "301")

    assert "Resolution: question `301` was not found in assignment `111`; auto-resolved to `222`." in result
    artifact = grading_workflow.get_artifact_path(
        "gradescope-grading-222-301.md"
    ).read_text(encoding="utf-8")
    assert "- assignment_id: `222`" in artifact
    assert "- resolution: question `301` was not found in assignment `111`; auto-resolved to `222`." in artifact


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

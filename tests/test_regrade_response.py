"""Tests for tool_respond_to_regrade_request (reply to and close a regrade request)."""

from types import SimpleNamespace

from gradescope_mcp.tools import regrades

BASE = "https://gs"
C, Q, S = "1", "20", "300"
RR_URL = "/courses/1/assignments/8/submissions/77/regrade_requests"


class FakeGradescope:
    """One question submission with an open regrade request."""

    def __init__(self, monkeypatch, *, open_request=True, apply_put=True, status=200):
        self.open = (
            {"id": 9, "student_comment": "Please look again.", "staff_comment": None,
             "completed": False, "created_at": "2026-11-05T09:16:10-08:00"}
            if open_request else None
        )
        self.closed = []
        self.apply_put = apply_put
        self.status = status
        self.puts = []
        monkeypatch.setattr(regrades, "_get_grading_context", self.context)

    def context(self, course_id, question_id, submission_id):
        assert (course_id, question_id, submission_id) == (C, Q, S)
        props = {
            "question": {"id": 20, "title": "Timeouts", "weight": "8.0"},
            "submission": {"id": 300, "score": "6.0", "owner_names": "A Student"},
            "urls": {"regrade_request": RR_URL},
            "open_request": self.open,
            "closed_requests": list(self.closed),
        }
        session = SimpleNamespace(put=self.put)
        return {"props": props, "csrf_token": "TOKEN", "session": session, "base_url": BASE}

    def put(self, url, json, headers):
        self.puts.append({"url": url, "json": json, "headers": headers})
        if self.apply_put and self.status == 200:
            closed = dict(self.open, staff_comment=json["regrade_request"]["staff_comment"],
                          completed=True)
            self.closed.append(closed)
            self.open = None
        return SimpleNamespace(status_code=self.status, text="{}")


def test_preview_sends_nothing_and_shows_the_response(monkeypatch) -> None:
    gs = FakeGradescope(monkeypatch)

    result = regrades.respond_to_regrade_request(C, Q, S, "Agreed: two points back.")

    assert gs.puts == []
    assert "No changes were made." in result
    assert "regrade_request_id=`9`" in result
    assert "student=A Student" in result
    assert "current_score=6.0/8.0" in result
    assert "> Agreed: two points back." in result


def test_confirm_puts_the_staff_comment_and_reads_back_closed(monkeypatch) -> None:
    gs = FakeGradescope(monkeypatch)

    result = regrades.respond_to_regrade_request(
        C, Q, S, "Agreed: two points back.", confirm_write=True
    )

    assert gs.puts == [{
        "url": f"{BASE}{RR_URL}/9",
        "json": {"regrade_request": {"id": 9, "staff_comment": "Agreed: two points back."}},
        "headers": {"X-CSRF-Token": "TOKEN", **regrades._RESPONSE_HEADERS},
    }]
    assert "✅ Response sent" in result
    assert "**Now:** closed, with the staff response saved." in result
    assert "⚠️" not in result


def test_no_open_request_sends_nothing(monkeypatch) -> None:
    gs = FakeGradescope(monkeypatch, open_request=False)

    result = regrades.respond_to_regrade_request(C, Q, S, "Hi", confirm_write=True)

    assert gs.puts == []
    assert result.startswith("Error: submission `300` has no open regrade request")


def test_blank_response_is_refused(monkeypatch) -> None:
    gs = FakeGradescope(monkeypatch)

    result = regrades.respond_to_regrade_request(C, Q, S, "   ", confirm_write=True)

    assert gs.puts == []
    assert result == "Error: response must be non-empty text."


def test_rejected_put_is_reported(monkeypatch) -> None:
    FakeGradescope(monkeypatch, status=422)

    result = regrades.respond_to_regrade_request(C, Q, S, "Hi", confirm_write=True)

    assert result.startswith("Error: Gradescope rejected the response (status 422)")


def test_read_back_warns_when_still_open(monkeypatch) -> None:
    FakeGradescope(monkeypatch, apply_put=False)

    result = regrades.respond_to_regrade_request(C, Q, S, "Hi", confirm_write=True)

    assert "⚠️ Read-back: the request is still open on Gradescope." in result

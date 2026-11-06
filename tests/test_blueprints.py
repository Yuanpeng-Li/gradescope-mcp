import json
from types import SimpleNamespace
from gradescope_mcp import operations
from gradescope_mcp.tools import blueprints as bp


def test_clone_preview_remaps_ids_and_preserves_source(monkeypatch):
    tree = [
        {
            "id": 11,
            "parent_id": None,
            "type": "QuestionGroup",
            "title": "G",
            "weight": 4,
            "index": 1,
            "content": [],
            "children": [
                {
                    "id": 12,
                    "parent_id": 11,
                    "type": "OnlineQuestion",
                    "title": "Q",
                    "weight": 4,
                    "index": 1,
                    "content": [],
                }
            ],
        }
    ]
    basis = {
        "revision": "source",
        "rubrics": {"12": []},
        "scoring": {
            "12": {
                "id": 12,
                "weight": "4",
                "scoring_type": "positive",
                "floor": True,
                "ceiling": True,
            }
        },
    }
    monkeypatch.setattr(
        bp,
        "_source",
        lambda *a: (None, {"assignment[type]": "OnlineAssignment"}, tree, True, basis),
    )
    monkeypatch.setattr(bp, "get_connection", lambda: object())
    monkeypatch.setattr(
        bp.admin,
        "_prepare",
        lambda *a: (
            {
                "baseline": {},
                "fields": {},
                "file": None,
                "target": "/courses/1/assignments",
                "verify": {},
            },
            "csrf",
        ),
    )
    p = json.loads(
        bp.prepare_assignment_clone(
            "1",
            "2",
            "Copied",
            {
                "release_date_string": "2027-01-01T10:00",
                "due_date_string": "2027-01-02T10:00",
            },
        )
    )
    copied = p["preview"]["outline"]
    assert (
        copied[0]["id"] == -1
        and copied[0]["children"][0]["id"] == -2
        and copied[0]["children"][0]["parent_id"] == -1
    )
    assert tree[0]["id"] == 11


def test_unknown_creation_stops_clone(monkeypatch):
    fresh = {
        "baseline": {},
        "fields": {},
        "file": None,
        "target": "/courses/1/assignments",
        "verify": {},
    }
    request = {
        "course_id": "1",
        "source_assignment_id": "2",
        "options": {"kind": "online", "title": "Clone"},
        "template_path": None,
        "create": fresh,
        "outline": [],
        "source_outline": [],
        "rubrics": {},
        "scoring": {},
    }
    op = operations.create("assignment_clone", request, {"source": 1}, [])
    monkeypatch.setattr(
        bp, "_source", lambda *a: (None, None, None, None, {"source": 1})
    )
    monkeypatch.setattr(bp, "get_connection", lambda: object())
    monkeypatch.setattr(bp.admin, "_prepare", lambda *a: (fresh, "csrf"))
    calls = []

    def unknown(token, digest, confirm):
        calls.append(token)
        p = operations.load(token)
        p["status"] = "unknown"
        operations.save(p)
        return "{}"

    monkeypatch.setattr(bp.admin, "execute_assignment_plan", unknown)
    assert bp.execute_assignment_clone(
        op["operation_token"], op["request_digest"], True
    ).startswith("Error:")
    assert operations.load(op["operation_token"])["status"] == "needs_review"
    bp.execute_assignment_clone(op["operation_token"], op["request_digest"], True)
    assert len(calls) == 1

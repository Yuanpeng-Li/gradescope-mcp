import copy
import json
from gradescope_mcp.tools import rubric_plans as rp


def test_rubric_plan_create_and_update(monkeypatch):
    current = [{"id": "1", "description": "Old", "weight": 2.0}]
    monkeypatch.setattr(rp, "_rubric", lambda *a: copy.deepcopy(current))
    calls = []

    def update(c, q, r, **kwargs):
        current[0].update(description=kwargs["description"], weight=kwargs["weight"])
        calls.append("update")
        return "ok"

    def create(c, q, description, weight, *a):
        current.append({"id": "2", "description": description, "weight": weight})
        calls.append("create")
        return "ok"

    monkeypatch.setattr(rp.grading, "update_rubric_item", update)
    monkeypatch.setattr(rp.grading, "create_rubric_item", create)
    p = json.loads(
        rp.prepare_rubric_plan(
            "1",
            "2",
            [
                {"id": "1", "description": "Updated", "weight": 3},
                {"description": "New", "weight": 1},
            ],
        )
    )
    assert not calls
    result = json.loads(
        rp.execute_rubric_plan(p["operation_token"], p["request_digest"], True)
    )
    assert result["status"] == "completed" and calls == ["update", "create"]
    rp.execute_rubric_plan(p["operation_token"], p["request_digest"], True)
    assert len(calls) == 2


def test_rubric_source_change_refused(monkeypatch):
    rows = []
    monkeypatch.setattr(rp, "_rubric", lambda *a: rows.copy())
    p = json.loads(
        rp.prepare_rubric_plan("1", "2", [{"description": "x", "weight": 1}])
    )
    rows.append({"id": "9", "description": "Other", "weight": 2})
    assert "changed" in rp.execute_rubric_plan(
        p["operation_token"], p["request_digest"], True
    )


def test_pre_submission_editor_normalizes_question_urls():
    import html
    from bs4 import BeautifulSoup
    from gradescope_mcp.tools.rubric_data import assignment_rubric_props

    props = {
        "course": {"id": 1},
        "assignment": {"id": 7},
        "questions": [
            {
                "id": 2,
                "weight": "0.0",
                "scoring_type": "positive",
                "floor": True,
                "ceiling": False,
                "children": None,
            }
        ],
        "rubric_items": [
            {"id": 4, "question_id": 2, "description": "Correct", "weight": "1.0"},
            {"id": 5, "question_id": 99},
        ],
        "urls": {
            "question_urls": {
                "2": {
                    "save_grading_options": "/courses/1/questions/2/save_grading_options"
                }
            }
        },
    }
    soup = BeautifulSoup(
        '<div data-react-class="AssignmentRubric" data-react-props="'
        + html.escape(json.dumps(props), quote=True)
        + '"></div>',
        "html.parser",
    )
    result = assignment_rubric_props(soup, "1", "2")
    assert result["question"]["weight"] == "0.0" and len(result["rubric_items"]) == 1
    assert result["urls"]["save_grading_options"].endswith("/2/save_grading_options")

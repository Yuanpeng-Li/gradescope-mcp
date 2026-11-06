"""Normalize the pre-submission AssignmentRubric editor into grading context data."""

import json


def assignment_rubric_props(soup, course_id, question_id):
    tag = soup.select_one('[data-react-class="AssignmentRubric"]')
    if tag is None:
        return None
    p = json.loads(tag["data-react-props"])
    if str((p.get("course") or {}).get("id")) != str(course_id):
        raise ValueError("Rubric editor belongs to another course")
    questions = {}

    def visit(nodes):
        if isinstance(nodes, dict):
            nodes = list(nodes.values())
        for q in nodes:
            questions[str(q["id"])] = q
            visit(q.get("children") or [])

    visit(p.get("questions") or [])
    question = questions.get(str(question_id))
    if question is None:
        raise ValueError("Question absent from rubric editor")
    urls = (p.get("urls") or {}).get("question_urls", {}).get(str(question_id))
    if not isinstance(urls, dict):
        raise ValueError("Per-question rubric URLs missing")
    return {
        "question": question,
        "assignment": p.get("assignment"),
        "course": p.get("course"),
        "rubric_items": [
            r
            for r in (p.get("rubric_items") or [])
            if str(r.get("question_id")) == str(question_id)
        ],
        "rubric_item_groups": [
            r
            for r in (p.get("rubric_item_groups") or [])
            if str(r.get("question_id")) == str(question_id)
        ],
        "urls": urls,
        "rubrics_locked": p.get("rubrics_locked"),
        "evaluation": {},
        "submission": {},
    }

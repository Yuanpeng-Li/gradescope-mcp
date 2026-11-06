"""Shared parsing of the PDF and online outline editors."""

import json

from bs4 import BeautifulSoup


def parse_outline_page(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    # Try AssignmentEditor first (online/homework assignments)
    editor = soup.find(attrs={"data-react-class": "AssignmentEditor"})
    if editor is not None:
        props = json.loads(editor.get("data-react-props", "{}"))
        if isinstance(props, dict):
            props["_outline_component"] = "AssignmentEditor"
        return props

    # Fallback: AssignmentOutline (scanned PDF exams)
    outline_tag = soup.find(attrs={"data-react-class": "AssignmentOutline"})
    if outline_tag is not None:
        props = json.loads(outline_tag.get("data-react-props", "{}"))
        # Normalize: AssignmentOutline stores data in "outline" (list)
        # and "assignment" (dict). Convert outline list → questions dict
        # to match the format _build_question_tree expects.
        outline_list = props.get("outline", [])
        questions = {}

        def _flatten(items, parent_id=None):
            # AssignmentOutline encodes parenthood via the children list, not
            # via a per-item parent_id field. Without threading the parent_id
            # through the recursion, every question would land at the top
            # level and _build_question_tree would lose the grouping.
            for item in items:
                qid = str(item["id"])
                questions[qid] = {
                    "id": item["id"],
                    "type": item.get("type", ""),
                    "title": item.get("title", ""),
                    "weight": item.get("weight"),
                    "index": item.get("index", 0),
                    "parent_id": item.get("parent_id", parent_id),
                    "content": item.get("content", []),
                }
                children = item.get("children", [])
                if children:
                    _flatten(children, parent_id=item["id"])

        _flatten(outline_list)
        props["questions"] = questions
        props["_outline_component"] = "AssignmentOutline"
        return props

    raise ValueError(
        "Neither AssignmentEditor nor AssignmentOutline component found. "
        "The assignment type may not be supported."
    )

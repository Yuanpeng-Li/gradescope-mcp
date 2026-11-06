"""Outline editing using the browser editors' PATCH contracts."""

import copy
import hashlib
import json
import math
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from gradescope_mcp.auth import AuthError, get_connection
from gradescope_mcp.tools.safety import serialized_write
from gradescope_mcp.tools.outline_data import parse_outline_page
from gradescope_mcp.tools.safety import write_confirmation_required


def _read(conn, course_id, assignment_id):
    url = f"{conn.gradescope_base_url}/courses/{course_id}/assignments/{assignment_id}/outline/edit"
    response = conn.session.get(url)
    if response.status_code != 200:
        raise ValueError(f"Outline read failed (HTTP {response.status_code})")
    soup = BeautifulSoup(response.text, "html.parser")
    tag = soup.select_one(
        '[data-react-class="AssignmentEditor"], [data-react-class="AssignmentOutline"]'
    )
    token = soup.select_one('meta[name="csrf-token"]')
    if tag is None or token is None:
        raise ValueError("Outline editor or CSRF token missing")
    props = parse_outline_page(response.text)
    online = tag["data-react-class"] == "AssignmentEditor"
    if online:
        questions = props["questions"]

        def children(parent):
            return [
                dict(
                    copy.deepcopy(q),
                    **(
                        {"children": children(q["id"])}
                        if q["type"] == "QuestionGroup"
                        else {}
                    ),
                )
                for q in sorted(questions.values(), key=lambda q: q["index"])
                if q["parent_id"] == parent
            ]

        tree = children(None)

        def count(nodes):
            return sum(1 + count(q.get("children", [])) for q in nodes)

        if count(tree) != len(questions):
            raise ValueError("Online outline has orphaned or cyclic questions")
    else:
        tree = props["outline"]
    digest = hashlib.sha256(
        json.dumps(
            [tree, props.get("assignment"), props.get("baseHash")], sort_keys=True
        ).encode()
    ).hexdigest()
    return props, online, tree, digest, token["content"], url


def _weight(value):
    """Normalize Gradescope decimal strings without accepting bool/nonfinite values."""
    if isinstance(value, str):
        if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", value.strip()):
            raise ValueError("Weights must be finite nonnegative numbers")
        value = float(value)
    try:
        valid = (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and value >= 0
        )
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("Weights must be finite nonnegative numbers")
    return value


def _validate(tree, online):
    """Validate the tree and normalize weights in place for the save payload."""
    ids = set()

    def walk(nodes, depth=0, parent=None):
        if not isinstance(nodes, list) or depth > 1:
            raise ValueError(
                "Use a list of questions with at most one level of subquestions"
            )
        sibling_indices = set()
        for q in nodes:
            if not isinstance(q, dict) or not isinstance(q.get("title"), str):
                raise ValueError("Every question needs a string title")
            q["weight"] = _weight(q.get("weight"))
            if "id" in q:
                if (
                    isinstance(q["id"], bool)
                    or not isinstance(q["id"], int)
                    or q["id"] == 0
                ):
                    raise ValueError("Question IDs must be nonzero integers")
                key = str(q["id"])
                if key in ids:
                    raise ValueError("Duplicate question ID")
                ids.add(key)
            if online:
                if not all(
                    k in q for k in ("id", "type", "parent_id", "index", "content")
                ):
                    raise ValueError(
                        "Online nodes require id, type, parent_id, index and content"
                    )
                if q["type"] not in ("OnlineQuestion", "QuestionGroup"):
                    raise ValueError("Unsupported online question type")
                if (
                    isinstance(q["index"], bool)
                    or not isinstance(q["index"], int)
                    or q["index"] < 1
                ):
                    raise ValueError("Online index must be a positive integer")
                if q["index"] in sibling_indices:
                    raise ValueError("Online sibling indices must be unique")
                sibling_indices.add(q["index"])
                if isinstance(q["parent_id"], bool) or q["parent_id"] != parent:
                    raise ValueError(
                        "Online parent_id must match the enclosing question ID"
                    )
                if not isinstance(q["content"], list):
                    raise ValueError("Online content must be an editor content array")
                if any(
                    not isinstance(block, dict)
                    or not isinstance(block.get("type"), str)
                    for block in q["content"]
                ):
                    raise ValueError("Every content block needs a type")
                if q.get("children") and q["type"] != "QuestionGroup":
                    raise ValueError("Only QuestionGroup can contain children")
            else:
                if not isinstance(q.get("crop_rect_list", []), list):
                    raise ValueError("crop_rect_list must be a list")
                for rect in q.get("crop_rect_list", []):
                    if not isinstance(rect, dict) or any(
                        isinstance(rect.get(k), bool)
                        or not isinstance(rect.get(k), (int, float))
                        or not math.isfinite(rect[k])
                        for k in ("x1", "x2", "y1", "y2", "page_number")
                    ):
                        raise ValueError("Invalid crop rectangle")
                    if not (
                        0 <= rect["x1"] < rect["x2"] <= 100
                        and 0 <= rect["y1"] < rect["y2"] <= 100
                        and rect["page_number"] >= 1
                        and int(rect["page_number"]) == rect["page_number"]
                    ):
                        raise ValueError(
                            "Crop coordinates must be ordered percentages and page_number a positive integer"
                        )
            if "children" in q and not isinstance(q["children"], list):
                raise ValueError("children must be a list")
            if q.get("children"):
                walk(q["children"], depth + 1, q.get("id"))

    walk(tree)
    return ids


def _pdf(tree):
    return [
        {
            **{k: q[k] for k in ("id", "title", "weight") if k in q},
            "crop_rect_list": q.get("crop_rect_list", []),
            **({"children": _pdf(q["children"])} if q.get("children") else {}),
        }
        for q in tree
    ]


def update_assignment_outline(
    course_id: str,
    assignment_id: str,
    outline_json: str = "",
    expected_revision: str = "",
    confirm_write: bool = False,
    identification_regions: dict | None = None,
) -> str:
    """Read an editable tree or preview/write it. confirm_write is not user approval."""
    try:
        with serialized_write("assignment outline", course_id, assignment_id):
            conn = get_connection()
            props, online, current, revision, token, edit_url = _read(
                conn, course_id, assignment_id
            )
            if identification_regions is not None:
                if online or not isinstance(identification_regions, dict) or set(identification_regions) != {'name','sid'}:
                    raise ValueError('PDF identification_regions must contain name and sid (rectangle or null)')
                for rect in identification_regions.values():
                    if rect is not None:
                        _validate([{'title':'Identification','weight':0,'crop_rect_list':[rect]}],False)
                if not outline_json:
                    raise ValueError('Supply outline_json when changing identification regions')
            if not outline_json:
                return json.dumps(
                    {
                        "revision": revision,
                        "editor": "online" if online else "pdf",
                        "outline": current,
                    },
                    ensure_ascii=False,
                )
            proposed = json.loads(outline_json)
            json.dumps(proposed, allow_nan=False)

            # Keep untouched editor fields, especially crops and input blocks.
            def index(nodes):
                result = {}
                for q in nodes:
                    result[str(q.get("id"))] = q
                    result.update(index(q.get("children", [])))
                return result

            before = index(current)
            new_ids = _validate(proposed, online)
            old_ids = _validate(current, online)
            proposed_index = index(proposed)
            if online and any(int(qid) >= 0 for qid in new_ids - old_ids):
                return "Error: New online questions require unique negative integer IDs (-1, -2, ...)."
            for key, old in before.items():
                new = proposed_index.get(key)
                if new is not None:
                    for field in ("content",) if online else ("crop_rect_list",):
                        if field in old and field not in new:
                            return f"Error: Preserve {field} on existing question {key}; use an explicit empty list to clear it."
                    if not online and any(
                        new.get(k) != old.get(k)
                        for k in ("type", "content")
                        if k in old
                    ):
                        return "Error: PDF question type/content cannot be edited through the outline endpoint."
            if not old_ids <= new_ids:
                return "Error: Existing question IDs must be preserved; deletion is not supported."
            if not online and new_ids - old_ids:
                return "Error: Omit id for new PDF questions; existing IDs must belong to this assignment."
            if expected_revision and expected_revision != revision:
                return "Error: Outline changed. Read it again and review a new preview."
            target = edit_url.removesuffix("edit")
            if online:
                target = urljoin(edit_url, props["saveUrl"])
                if (
                    urlsplit(target).scheme != urlsplit(edit_url).scheme
                    or urlsplit(target).netloc != urlsplit(edit_url).netloc
                    or urlsplit(target).query
                    or urlsplit(target).fragment
                    or urlsplit(target).path.rstrip("/")
                    != urlsplit(edit_url).path.removesuffix("/edit")
                ):
                    return "Error: Unexpected online outline save URL."
                payload = {
                    "outline": json.dumps(proposed),
                    "base_hash": props["baseHash"],
                    "ignore_warnings": False,
                }
            else:
                payload = {
                    "question_data": _pdf(proposed),
                    "assignment": {
                        "identification_regions": identification_regions if identification_regions is not None else props["assignment"]["id_regions"]
                    },
                }
            if not confirm_write:
                return write_confirmation_required(
                    "update_assignment_outline",
                    [
                        f"Course {course_id}, assignment {assignment_id}",
                        f"expected_revision: {revision}",
                        "Complete save payload: "
                        + json.dumps(payload, ensure_ascii=False),
                    ],
                )
            if not expected_revision:
                return "Error: expected_revision from the preview is required."
            response = conn.session.patch(
                target,
                **({"data": payload} if online else {"json": payload}),
                headers={
                    "X-CSRF-Token": token,
                    "Referer": edit_url,
                    "Accept": "application/json",
                },
                allow_redirects=False,
            )
            if response.status_code != 200:
                return f"Error: Outline save returned HTTP {response.status_code}; inspect the outline before retrying."
            try:
                saved_props, _, saved, _, _, _ = _read(conn, course_id, assignment_id)
            except AuthError:
                raise
            except Exception as exc:
                return f"⚠️ Outline save accepted, but read-back failed: {exc}. Inspect before retrying."

            def comparable(nodes):
                result = []
                for q in nodes:
                    fields = (
                        ("title", "weight", "type", "content")
                        if online
                        else ("title", "weight", "crop_rect_list")
                    )
                    item = {
                        k: q.get(k, [] if k in ("content", "crop_rect_list") else None)
                        for k in fields
                    }
                    item["weight"] = _weight(item["weight"])
                    item["children"] = comparable(q.get("children", []))
                    # Newly allocated IDs differ; existing IDs must remain attached
                    # to the same question in the same position.
                    item["id"] = (
                        str(q.get("id")) if str(q.get("id")) in old_ids else None
                    )
                    result.append(item)
                return result

            if identification_regions is not None and saved_props.get("assignment",{}).get("id_regions") != identification_regions:
                return "⚠️ Outline saved but identification regions differ on read-back."
            if comparable(saved) != comparable(proposed):
                return "⚠️ Save request accepted, but read-back differs. Read the outline before retrying."
            return "Assignment outline saved and verified."
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error updating outline: {exc}"


def patch_assignment_outline(course_id: str, assignment_id: str, changes: list[dict], expected_revision: str = '', confirm_write: bool = False) -> str:
    """Patch named existing questions, preserving all unrelated outline fields."""
    try:
        if not isinstance(changes,list) or not changes:raise ValueError('Provide changes for existing question IDs')
        conn=get_connection();_,online,tree,revision,_,_=_read(conn,course_id,assignment_id)
        if expected_revision and revision!=expected_revision:raise ValueError('Outline changed; preview again')
        nodes={}
        def visit(items):
            for q in items:
                nodes[str(q['id'])]=q;visit(q.get('children',[]))
        visit(tree);seen=set()
        for change in changes:
            if not isinstance(change,dict) or set(change)-{'question_id','title','weight','crop_rect_list','content'}:raise ValueError('Unknown patch field')
            qid=str(change.get('question_id'))
            if qid not in nodes or qid in seen:raise ValueError('Unknown/duplicate question ID')
            seen.add(qid)
            if online and 'crop_rect_list' in change:raise ValueError('Online outline does not use PDF crop regions')
            if not online and 'content' in change:raise ValueError('PDF content blocks cannot be edited')
            nodes[qid].update({k:v for k,v in change.items() if k!='question_id'})
        if confirm_write and not expected_revision:return 'Error: expected_revision from preview required'
        return update_assignment_outline(course_id,assignment_id,json.dumps(tree),revision,confirm_write)
    except AuthError as exc:return f'Authentication error: {exc}'
    except Exception as exc:return f'Error: {exc}'

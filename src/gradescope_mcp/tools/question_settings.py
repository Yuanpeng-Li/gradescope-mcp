"""Write a question's scoring settings: scoring type, ceiling and floor.

The rubric editor (``/courses/{c}/assignments/{a}/rubric/edit``) saves them
per question with ``POST /courses/{c}/questions/{q}/save_grading_options``
and ``{"question": {"id", "scoring_type", "ceiling": "1"/"0", "floor":
"1"/"0"}}``. The same page lists every question's current settings and
rubric items, so it is also where the change is previewed and read back.
"""

import json
import re

from bs4 import BeautifulSoup

from gradescope_mcp.auth import AuthError, get_connection
from gradescope_mcp.tools.common import sanitize_inline
from gradescope_mcp.tools.safety import write_confirmation_required

SCORING_TYPES = ("positive", "negative")
_SAVE_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}


def _load_question_settings(course_id: str, question_id: str) -> dict:
    """Load a question's scoring settings and save URL from the rubric editor.

    The question's submissions page links to its assignment's rubric
    editor. Raises ``ValueError`` when either page or the question is not
    found.
    """
    conn = get_connection()
    base = conn.gradescope_base_url
    listing = conn.session.get(f"{base}/courses/{course_id}/questions/{question_id}/submissions")
    if listing.status_code != 200:
        raise ValueError(
            f"cannot open question `{question_id}` (status {listing.status_code}); "
            "check course_id, question_id and your permissions"
        )
    link = re.search(
        rf"/courses/{re.escape(str(course_id))}/assignments/(\d+)/rubric/edit", listing.text
    )
    if not link:
        raise ValueError(f"no rubric editor link found for question `{question_id}`")
    page = conn.session.get(f"{base}{link.group(0)}")
    if page.status_code != 200:
        raise ValueError(f"cannot open the rubric editor (status {page.status_code})")
    soup = BeautifulSoup(page.text, "html.parser")
    tag = soup.find(attrs={"data-react-class": "AssignmentRubric"})
    if tag is None:
        raise ValueError("the rubric editor page has no AssignmentRubric data")
    try:
        props = json.loads(tag.get("data-react-props", "{}"))
    except json.JSONDecodeError as e:
        raise ValueError(f"rubric editor props are not valid JSON ({e})") from e

    questions = props.get("questions") or []
    if isinstance(questions, dict):
        questions = list(questions.values())
    question = next(
        (q for q in questions if isinstance(q, dict) and str(q.get("id")) == str(question_id)),
        None,
    )
    if question is None:
        raise ValueError(f"question `{question_id}` is not in assignment `{link.group(1)}`")
    question_urls = ((props.get("urls") or {}).get("question_urls") or {}).get(str(question_id)) or {}
    save_url = question_urls.get("save_grading_options") or (
        f"/courses/{course_id}/questions/{question_id}/save_grading_options"
    )
    items = [
        ri for ri in props.get("rubric_items") or []
        if isinstance(ri, dict) and str(ri.get("question_id")) == str(question_id)
    ]
    csrf_meta = soup.find("meta", {"name": "csrf-token"})
    return {
        "conn": conn,
        "assignment_id": link.group(1),
        "question": question,
        "rubric_items": items,
        "has_submissions": bool(props.get("has_submissions_and_students")),
        "save_url": save_url,
        "csrf_token": csrf_meta.get("content", "") if csrf_meta else "",
    }


def _settings(question: dict) -> dict:
    """The three settings as this module compares them."""
    return {
        "scoring_type": question.get("scoring_type"),
        # Gradescope treats an absent floor/ceiling as on.
        "ceiling": True if question.get("ceiling") is None else bool(question.get("ceiling")),
        "floor": True if question.get("floor") is None else bool(question.get("floor")),
    }


def _nonzero(weight) -> bool:
    try:
        return float(weight) != 0
    except (TypeError, ValueError):
        return False


def _on_off(value: bool) -> str:
    return "on" if value else "off"


def _describe(key: str, value) -> str:
    return _on_off(value) if key in ("ceiling", "floor") else str(value)


def set_question_scoring(
    course_id: str,
    question_id: str,
    scoring_type: str | None = None,
    ceiling: bool | None = None,
    floor: bool | None = None,
    confirm_write: bool = False,
) -> str:
    """Change a question's scoring type, ceiling and/or floor.

    Settings left as ``None`` keep their current values. Switching the
    scoring type flips what every existing rubric item does (an item worth
    2 deducts 2 under negative scoring and adds 2 under positive), and
    changes the score of every submission already graded with them. A
    0-point bonus question needs positive scoring and no ceiling, or the
    points it awards are capped at 0.
    """
    if not course_id or not question_id:
        return "Error: course_id and question_id are required."
    if scoring_type is not None and scoring_type not in SCORING_TYPES:
        return 'Error: scoring_type must be "positive" or "negative".'
    for name, value in (("ceiling", ceiling), ("floor", floor)):
        if value is not None and not isinstance(value, bool):
            return f"Error: {name} must be true, false or null."
    if scoring_type is None and ceiling is None and floor is None:
        return "Error: at least one of scoring_type, ceiling or floor must be provided."

    try:
        ctx = _load_question_settings(course_id, question_id)
    except AuthError as e:
        return f"Authentication error: {e}"
    except ValueError as e:
        return f"Error: {e}."
    except Exception as e:
        return f"Error: could not load the question's settings: {e}"

    question = ctx["question"]
    current = _settings(question)
    wanted = {
        "scoring_type": scoring_type if scoring_type is not None else current["scoring_type"],
        "ceiling": ceiling if ceiling is not None else current["ceiling"],
        "floor": floor if floor is not None else current["floor"],
    }
    if wanted["scoring_type"] not in SCORING_TYPES:
        return (
            "Error: Gradescope did not report this question's scoring type "
            f"({current['scoring_type']!r}); pass scoring_type explicitly."
        )
    changes = [k for k in wanted if wanted[k] != current[k]]
    title = sanitize_inline(question.get("title") or "untitled")

    if not changes:
        return (
            f"✅ Question `{question_id}` ({title}) already has scoring "
            f"{current['scoring_type']}, ceiling {_on_off(current['ceiling'])}, floor "
            f"{_on_off(current['floor'])}; nothing was sent."
        )

    details = [
        f"course_id=`{course_id}`",
        f"question_id=`{question_id}` ({title}, {question.get('weight', '?')} pts, "
        f"assignment `{ctx['assignment_id']}`)",
    ]
    for key in ("scoring_type", "ceiling", "floor"):
        before, after = _describe(key, current[key]), _describe(key, wanted[key])
        details.append(
            f"{key}: {before} → {after}" if key in changes else f"{key}: {before} (unchanged)"
        )
    if "scoring_type" in changes and ctx["rubric_items"]:
        weighted = [ri for ri in ctx["rubric_items"] if _nonzero(ri.get("weight"))]
        if weighted:
            details.append(
                f"⚠️ {len(weighted)} existing rubric item(s) will flip direction "
                f"({'deduct → add' if wanted['scoring_type'] == 'positive' else 'add → deduct'}): "
                + ", ".join(
                    f"{sanitize_inline(ri.get('description') or '?')} ({ri.get('weight')})"
                    for ri in weighted
                )
            )
    if ctx["has_submissions"]:
        details.append(
            "⚠️ this assignment has submissions: graded scores on this question will be recomputed"
        )
    if wanted["scoring_type"] == "positive" and wanted["ceiling"] is False:
        details.append("scores on this question may exceed its points (bonus)")

    if not confirm_write:
        return write_confirmation_required("set_question_scoring", details)

    conn = ctx["conn"]
    payload = {
        "question": {
            "id": int(question["id"]),
            "scoring_type": wanted["scoring_type"],
            "ceiling": "1" if wanted["ceiling"] else "0",
            "floor": "1" if wanted["floor"] else "0",
        }
    }
    try:
        resp = conn.session.post(
            f"{conn.gradescope_base_url}{ctx['save_url']}",
            json=payload,
            headers={"X-CSRF-Token": ctx["csrf_token"], **_SAVE_HEADERS},
        )
    except Exception as e:
        return f"Error: saving the scoring settings failed: {e}"
    if resp.status_code != 200:
        return f"Error: Gradescope rejected the scoring settings (status {resp.status_code}): {resp.text[:300]}"

    lines = [f"✅ Scoring settings saved for question `{question_id}` ({title})."]
    try:
        after = _settings(_load_question_settings(course_id, question_id)["question"])
    except Exception as e:
        lines.append(f"⚠️ Read-back failed ({e}); check the question's rubric settings.")
        return "\n".join(lines)
    lines.append(
        f"**Now:** scoring {after['scoring_type']}, ceiling {_on_off(after['ceiling'])}, "
        f"floor {_on_off(after['floor'])}"
    )
    for key in ("scoring_type", "ceiling", "floor"):
        if after[key] != wanted[key]:
            lines.append(
                f"⚠️ Read-back: {key} is {_describe(key, after[key])} on Gradescope "
                f"(sent {_describe(key, wanted[key])})"
            )
    return "\n".join(lines)

"""Write a scanned-PDF assignment's outline: questions, points and regions.

The outline editor (``/outline/edit``) saves the whole outline at once with
``PATCH /courses/{c}/assignments/{a}/outline/``: the name/SID regions plus
every question (title, weight, crop rectangles, children). A question sent
without an ``id`` is created; an existing question left out is deleted.
This module sends the same request, so it replaces the outline as a whole.
"""

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

from bs4 import BeautifulSoup

from gradescope_mcp.auth import AuthError, get_connection
from gradescope_mcp.tools.common import escape_md_cell, sanitize_inline
from gradescope_mcp.tools.safety import write_confirmation_required

_OUTLINE_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}
_RECT_KEYS = ("x1", "x2", "y1", "y2", "page_number")
_QUESTION_KEYS = ("question_id", "title", "weight", "crop_rect_list", "children")


def _load_outline_editor(course_id: str, assignment_id: str) -> dict:
    """Load the outline editor page: its props plus the CSRF token.

    Raises ``ValueError`` when the page is not a scanned-PDF outline editor.
    """
    conn = get_connection()
    url = f"{conn.gradescope_base_url}/courses/{course_id}/assignments/{assignment_id}/outline/edit"
    resp = conn.session.get(url)
    if resp.status_code != 200:
        raise ValueError(
            f"cannot open the outline editor (status {resp.status_code}); "
            "check course_id, assignment_id and your permissions"
        )
    soup = BeautifulSoup(resp.text, "html.parser")
    tag = soup.find(attrs={"data-react-class": "AssignmentOutline"})
    if tag is None:
        raise ValueError(
            "this assignment has no scanned-PDF outline editor "
            "(only PDF/exam assignments have question regions)"
        )
    try:
        props = json.loads(tag.get("data-react-props", "{}"))
    except json.JSONDecodeError as e:
        raise ValueError(f"outline editor props are not valid JSON ({e})") from e
    csrf_meta = soup.find("meta", {"name": "csrf-token"})
    return {
        "props": props,
        "csrf_token": csrf_meta.get("content", "") if csrf_meta else "",
        "conn": conn,
    }


def _round1(value: float) -> float:
    """Round to 0.1, as the outline editor does before saving."""
    return round(float(value) * 10) / 10


def _clean_rect(rect: Mapping) -> dict:
    return {
        "x1": _round1(rect["x1"]),
        "x2": _round1(rect["x2"]),
        "y1": _round1(rect["y1"]),
        "y2": _round1(rect["y2"]),
        "page_number": int(rect["page_number"]),
    }


def _snapshot(props: dict) -> dict:
    """The parts of the outline a save replaces, in a canonical form."""

    def question(q: dict) -> dict:
        out = {
            "id": str(q.get("id")),
            "title": q.get("title") or "",
            "weight": _number(q.get("weight")),
            "crop_rect_list": [_clean_rect(r) for r in q.get("crop_rect_list") or []],
        }
        if q.get("children"):
            out["children"] = [question(c) for c in q["children"]]
        return out

    regions = (props.get("assignment") or {}).get("id_regions") or {}
    return {
        "name": _clean_rect(regions["name"]) if regions.get("name") else None,
        "sid": _clean_rect(regions["sid"]) if regions.get("sid") else None,
        "questions": [question(q) for q in props.get("outline") or []],
    }


def outline_fingerprint(props: dict) -> str:
    """Short hash of the current outline; changes when anyone saves it."""
    blob = json.dumps(_snapshot(props), sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _validate_rect(rect: Any, where: str, page_count: int | None) -> tuple[dict | None, str | None]:
    if hasattr(rect, "model_dump"):
        rect = rect.model_dump(exclude_unset=True)
    if not isinstance(rect, Mapping):
        return None, f"{where}: a region must be an object with {', '.join(_RECT_KEYS)}"
    unknown = sorted(str(k) for k in rect if k not in _RECT_KEYS)
    missing = [k for k in _RECT_KEYS if k not in rect]
    if unknown or missing:
        return None, f"{where}: region needs exactly {', '.join(_RECT_KEYS)} (unknown {unknown}, missing {missing})"
    coords = {k: _number(rect[k]) for k in ("x1", "x2", "y1", "y2")}
    if any(v is None for v in coords.values()):
        return None, f"{where}: region coordinates must be numbers"
    if any(not 0 <= v <= 100 for v in coords.values()):
        return None, f"{where}: region coordinates are page percentages, 0-100"
    if coords["x1"] >= coords["x2"] or coords["y1"] >= coords["y2"]:
        return None, f"{where}: a region needs x1 < x2 and y1 < y2"
    page = rect["page_number"]
    if isinstance(page, bool) or _number(page) is None or float(page) != int(float(page)):
        return None, f"{where}: page_number must be a whole number"
    page = int(float(page))
    if page < 1 or (page_count is not None and page > page_count):
        return None, f"{where}: page_number {page} is outside the template's 1-{page_count} pages"
    return _clean_rect({**coords, "page_number": page}), None


def _validate_questions(
    questions: Any, existing_ids: set[str], page_count: int | None,
) -> tuple[list[dict], list[str]]:
    """Validate the requested outline and turn it into the save payload shape."""
    errors: list[str] = []
    used_ids: set[str] = set()

    def one(q: Any, where: str, depth: int) -> dict | None:
        if hasattr(q, "model_dump"):
            q = q.model_dump(exclude_unset=True)
        if not isinstance(q, Mapping):
            errors.append(f"{where}: a question must be an object")
            return None
        unknown = sorted(str(k) for k in q if k not in _QUESTION_KEYS)
        if unknown:
            errors.append(f"{where}: unknown field(s) {unknown}; allowed: {', '.join(_QUESTION_KEYS)}")
            return None
        out: dict = {}
        qid = q.get("question_id")
        if qid is not None:
            qid = str(qid).strip().strip("`")
            if qid not in existing_ids:
                errors.append(f"{where}: id `{qid}` is not a question of this assignment (omit question_id to create one)")
                return None
            if qid in used_ids:
                errors.append(f"{where}: id `{qid}` is used twice")
                return None
            used_ids.add(qid)
            out["id"] = qid
        title = q.get("title", "")
        if title is None:
            title = ""
        if not isinstance(title, str):
            errors.append(f"{where}: title must be a string")
            return None
        out["title"] = title.strip()

        children = q.get("children") or []
        if children:
            if depth > 0:
                errors.append(f"{where}: questions can be nested only one level")
                return None
            if not isinstance(children, list):
                errors.append(f"{where}: children must be a list")
                return None
            kids = [one(c, f"{where}.{i}", depth + 1) for i, c in enumerate(children, 1)]
            if any(k is None for k in kids):
                return None
            out["children"] = kids

        weight = q.get("weight")
        if weight is None and children:
            weight = sum(k["weight"] for k in out["children"])
        weight = _number(weight)
        if weight is None or weight < 0:
            errors.append(f"{where}: weight must be a number of points (0 or more)")
            return None
        if children and abs(weight - sum(k["weight"] for k in out["children"])) > 1e-9:
            errors.append(f"{where}: a group's weight must equal the sum of its children's ({weight:g} given)")
            return None
        out["weight"] = weight

        rects = q.get("crop_rect_list")
        if rects is None and children:
            rects = list(out["children"][0]["crop_rect_list"])
        if not isinstance(rects, list) or not rects:
            errors.append(f"{where}: crop_rect_list needs at least one region")
            return None
        cleaned = []
        for i, r in enumerate(rects, 1):
            rect, err = _validate_rect(r, f"{where} region {i}", page_count)
            if err:
                errors.append(err)
                return None
            cleaned.append(rect)
        out["crop_rect_list"] = cleaned
        return out

    if not isinstance(questions, list) or not questions:
        return [], ["questions must be a non-empty list"]
    result = [one(q, f"Q{i}", 0) for i, q in enumerate(questions, 1)]
    return [q for q in result if q is not None], errors


def _payload_question(q: dict) -> dict:
    """A validated question as the editor sends it: existing IDs as integers.

    Gradescope matches ``id`` against its integer question IDs; a string ID
    is not matched, so the question would be deleted and created anew.
    """
    out = {k: v for k, v in q.items() if k != "children"}
    if "id" in out:
        out["id"] = int(out["id"])
    if q.get("children"):
        out["children"] = [_payload_question(c) for c in q["children"]]
    return out


def _all_ids(questions: list[dict]) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for q in questions:
        found[str(q["id"])] = q
        for c in q.get("children") or []:
            found[str(c["id"])] = c
    return found


def _describe_rects(rects: list[dict]) -> str:
    return "; ".join(
        f"p{r['page_number']} y {r['y1']:g}–{r['y2']:g}"
        + ("" if (r["x1"], r["x2"]) == (0, 100) else f" x {r['x1']:g}–{r['x2']:g}")
        for r in rects
    )


def _change(new: dict, old: dict | None) -> str:
    if old is None:
        return "new"
    changed = [
        label
        for label, key in (("title", "title"), ("points", "weight"), ("regions", "crop_rect_list"))
        if new[key] != old[key]
    ]
    return "changed: " + ", ".join(changed) if changed else "unchanged"


def _outline_table(questions: list[dict], current: dict[str, dict]) -> list[str]:
    lines = [
        "| # | id | title | points | regions | change |",
        "|---|----|-------|--------|---------|--------|",
    ]

    def row(num: str, q: dict) -> None:
        old = current.get(q["id"]) if "id" in q else None
        lines.append(
            "| " + " | ".join([
                num,
                f"`{q['id']}`" if "id" in q else "(new)",
                escape_md_cell(q["title"] or "(untitled)"),
                f"{q['weight']:g}",
                escape_md_cell(_describe_rects(q["crop_rect_list"])),
                _change(q, old),
            ]) + " |"
        )

    for i, q in enumerate(questions, 1):
        row(str(i), q)
        for j, c in enumerate(q.get("children") or [], 1):
            row(f"{i}.{j}", c)
    return lines


def update_assignment_outline(
    course_id: str,
    assignment_id: str,
    questions: list[dict],
    expected_fingerprint: str | None = None,
    allow_removals: bool = False,
    confirm_write: bool = False,
) -> str:
    """Replace a scanned-PDF assignment's outline (questions, points, regions).

    ``questions`` is the complete new outline, in order. Each entry has
    ``title``, ``weight`` (points), ``crop_rect_list`` (regions: ``x1``,
    ``x2``, ``y1``, ``y2`` as page percentages 0-100 and ``page_number``,
    1-based) and optionally ``question_id`` (an existing question to keep; omit to
    create one) and ``children`` (one level of sub-questions; a group's
    ``weight`` and ``crop_rect_list`` default to its children's sum and the
    first child's regions). The name and SID regions are kept.

    An existing question left out of ``questions`` is deleted with its
    rubric and grades, so that is refused unless ``allow_removals=True``.

    The preview shows the new outline next to the current one and a
    fingerprint of the current outline. ``confirm_write=True`` requires that
    fingerprint as ``expected_fingerprint``: if the outline was saved by
    anyone since the preview (e.g. in the browser), nothing is sent. After
    saving, the outline is read back and compared with what was sent.
    """
    if not course_id or not assignment_id:
        return "Error: course_id and assignment_id are required."

    try:
        editor = _load_outline_editor(course_id, assignment_id)
    except AuthError as e:
        return f"Authentication error: {e}"
    except ValueError as e:
        return f"Error: {e}."
    except Exception as e:
        return f"Error: could not load the outline editor: {e}"

    props = editor["props"]
    page_count = (props.get("template") or {}).get("page_count")
    snapshot = _snapshot(props)
    current = _all_ids(snapshot["questions"])
    fingerprint = outline_fingerprint(props)

    new_questions, errors = _validate_questions(questions, set(current), page_count)
    if errors:
        return "Error: outline refused; nothing was sent:\n" + "\n".join(f"- {e}" for e in errors)

    kept = {q["id"] for q in new_questions if "id" in q}
    kept |= {c["id"] for q in new_questions for c in q.get("children") or [] if "id" in c}
    removed = [qid for qid in current if qid not in kept]
    total = sum(q["weight"] for q in new_questions)

    details = [
        f"course_id=`{course_id}`",
        f"assignment_id=`{assignment_id}`",
        f"title={sanitize_inline((props.get('assignment') or {}).get('title', '?'))}",
        f"template pages={page_count}",
        f"total points={total:g}",
        f"current outline fingerprint=`{fingerprint}` (pass it as expected_fingerprint)",
    ]
    if removed:
        details.append(
            ("⚠️ DELETES" if allow_removals else "⚠️ would delete (refused without allow_removals=True)")
            + " existing question(s) with their rubrics and grades: "
            + ", ".join(
                f"`{qid}` ({sanitize_inline(current[qid]['title'] or 'untitled')})" for qid in removed
            )
        )
    if props.get("has_submissions_and_students"):
        details.append("⚠️ this assignment already has submissions: changed regions re-crop every submission")

    if not confirm_write:
        return (
            write_confirmation_required("update_assignment_outline", details)
            + "\n\n"
            + "\n".join(_outline_table(new_questions, current))
        )

    if removed and not allow_removals:
        return (
            "Error: the new outline leaves out existing question(s) "
            + ", ".join(f"`{q}`" for q in removed)
            + ", which would delete them with their rubrics and grades. Nothing "
            "was sent. Include them, or pass allow_removals=True if the user approved."
        )
    if not expected_fingerprint:
        return (
            "Error: confirm_write=True needs expected_fingerprint from the preview "
            f"(current: `{fingerprint}`). Nothing was sent."
        )
    if str(expected_fingerprint).strip().strip("`") != fingerprint:
        return (
            f"Error: the outline changed since the preview (fingerprint `{fingerprint}`, "
            f"expected `{expected_fingerprint}`), e.g. it was saved in the browser. "
            "Nothing was sent. Preview again and show the user the new state."
        )

    payload = {
        "assignment": {
            "identification_regions": {"name": snapshot["name"], "sid": snapshot["sid"]},
        },
        "question_data": [_payload_question(q) for q in new_questions],
    }
    conn = editor["conn"]
    try:
        resp = conn.session.patch(
            f"{conn.gradescope_base_url}/courses/{course_id}/assignments/{assignment_id}/outline/",
            json=payload,
            headers={"X-CSRF-Token": editor["csrf_token"], **_OUTLINE_HEADERS},
        )
    except Exception as e:
        return f"Error: saving the outline failed: {e}"
    if resp.status_code != 200:
        return f"Error: Gradescope rejected the outline (status {resp.status_code}): {resp.text[:300]}"

    lines = [f"✅ Outline saved for assignment `{assignment_id}` ({total:g} points)."]
    try:
        after = _snapshot(_load_outline_editor(course_id, assignment_id)["props"])
    except Exception as e:
        lines.append(f"⚠️ Read-back failed ({e}); check the outline with tool_get_assignment_outline.")
        return "\n".join(lines)
    lines.extend(_readback_mismatches(new_questions, after["questions"]))
    lines.append("")
    lines.extend(_outline_table(after["questions"], {}))
    return "\n".join(lines)


def _readback_mismatches(sent: list[dict], saved: list[dict]) -> list[str]:
    """Compare the saved outline with what was sent, position by position."""

    def flat(qs: list[dict]) -> list[tuple[str, dict]]:
        out = []
        for i, q in enumerate(qs, 1):
            out.append((str(i), q))
            for j, c in enumerate(q.get("children") or [], 1):
                out.append((f"{i}.{j}", c))
        return out

    a, b = flat(sent), flat(saved)
    if [n for n, _ in a] != [n for n, _ in b]:
        return [f"⚠️ Read-back: Gradescope saved {len(b)} question(s) in a different shape than the {len(a)} sent."]
    problems = []
    for (num, s), (_n, g) in zip(a, b):
        if "id" in s and s["id"] != g["id"]:
            problems.append(
                f"⚠️ Read-back: Q{num} was recreated as `{g['id']}` instead of keeping "
                f"`{s['id']}`; its rubric and any grades did not carry over"
            )
        for label, key in (("title", "title"), ("points", "weight"), ("regions", "crop_rect_list")):
            if s[key] != g[key]:
                problems.append(f"⚠️ Read-back: Q{num} {label} is {g[key]!r} on Gradescope (sent {s[key]!r})")
    return problems

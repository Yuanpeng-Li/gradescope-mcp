"""Previewed question scoring, grading-method and manual-group writes.

Contracts verified against submission_grader and answer_grouper browser assets.
"""

import hashlib
import json
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from gradescope_mcp.auth import AuthError, get_connection
from gradescope_mcp.tools.grading_ops import _load_question_rubric_context
from gradescope_mcp.tools.grading_workflow import _fetch_assignment_questions
from gradescope_mcp.tools.safety import serialized_write, write_confirmation_required


def _revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _preview(
    action, course_id, question_id, state, payload, expected_revision, confirm_write
):
    revision = _revision(state)
    if expected_revision and expected_revision != revision:
        return "Error: Question settings or membership changed; review a new preview."
    if not confirm_write:
        return write_confirmation_required(
            action,
            [
                f"course_id={course_id}, question_id={question_id}",
                f"expected_revision={revision}",
                "Before: " + json.dumps(state),
                "Request: " + json.dumps(payload),
                "Question-wide changes can affect existing grades.",
            ],
        )
    if not expected_revision:
        return "Error: expected_revision from the preview is required."
    return None


def _target(base, url, path):
    target = urljoin(base, url)
    parts, origin = urlsplit(target), urlsplit(base)
    if (
        (parts.scheme, parts.netloc) != (origin.scheme, origin.netloc)
        or parts.path != path
        or parts.query
        or parts.fragment
    ):
        raise ValueError("Unexpected write URL; target does not match this question")
    return target


def _headers(token):
    if not token:
        raise ValueError("CSRF token missing; nothing sent")
    return {"X-CSRF-Token": token, "Accept": "application/json"}


def _scoring(course_id, question_id):
    ctx = _load_question_rubric_context(course_id, question_id)
    props = ctx.get("props") or {}
    q = props.get("question") or {}
    if str(q.get("id")) != str(question_id):
        raise ValueError("Scoring context does not belong to this question")
    if q.get("scoring_type") not in ("positive", "negative") or any(
        type(q.get(k)) is not bool for k in ("floor", "ceiling")
    ):
        raise ValueError("Scoring options unavailable; refusing to guess defaults")
    state = {k: q[k] for k in ("id", "scoring_type", "floor", "ceiling", "weight")}
    url = _target(
        ctx["base_url"],
        props.get("urls", {}).get("save_grading_options", ""),
        f"/courses/{course_id}/questions/{question_id}/save_grading_options",
    )
    return ctx, state, url


def set_question_scoring(
    course_id: str,
    question_id: str,
    scoring_type: str | None = None,
    ceiling: bool | None = None,
    floor: bool | None = None,
    expected_revision: str = "",
    confirm_write: bool = False,
) -> str:
    """Change scoring options without changing the question weight or rubric."""
    try:
        if scoring_type is not None and scoring_type not in ("positive", "negative"):
            return "Error: scoring_type must be positive or negative."
        if any(v is not None and type(v) is not bool for v in (floor, ceiling)):
            return "Error: floor and ceiling must be booleans."
        with serialized_write("question settings", course_id, question_id):
            ctx, before, url = _scoring(course_id, question_id)
            headers = _headers(ctx["csrf_token"])
            after = {
                **before,
                **{
                    k: v
                    for k, v in {
                        "scoring_type": scoring_type,
                        "ceiling": ceiling,
                        "floor": floor,
                    }.items()
                    if v is not None
                },
            }
            payload = {
                "question": {
                    k: ("1" if v else "0") if k in ("floor", "ceiling") else v
                    for k, v in after.items()
                    if k != "weight"
                }
            }
            preview = _preview(
                "set_question_scoring",
                course_id,
                question_id,
                before,
                payload,
                expected_revision,
                confirm_write,
            )
            if preview:
                return preview
            if before == after:
                return "Question already has these scoring settings; nothing sent."
            try:
                resp = ctx["session"].post(
                    url,
                    json=payload,
                    headers=headers,
                    allow_redirects=False,
                )
            except AuthError:
                raise
            except Exception:
                return "Error: Scoring save outcome unknown; inspect the question before retrying."
            if resp.status_code != 200:
                return f"Error: Scoring save returned HTTP {resp.status_code}; inspect before retrying."
            try:
                _, saved, _ = _scoring(course_id, question_id)
                same = all(
                    saved[k] == after[k] for k in ("scoring_type", "ceiling", "floor")
                ) and float(saved["weight"]) == float(before["weight"])
            except AuthError:
                raise
            except Exception:
                return "⚠️ Scoring request accepted; read-back failed. Inspect before retrying."
            if not same:
                return "⚠️ Scoring request accepted; read-back differs, including possibly the question weight. Inspect before retrying."
            return "Question scoring saved and verified; weight preserved: " + str(
                saved["weight"]
            )
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


def _grouper(course_id, question_id):
    conn = get_connection()
    resp = conn.session.get(
        f"{conn.gradescope_base_url}/courses/{course_id}/questions/{question_id}/answer_groups"
    )
    if resp.status_code != 200:
        raise ValueError(f"Grouping page HTTP {resp.status_code}")
    soup = BeautifulSoup(resp.text, "html.parser")
    tag = soup.select_one('[data-react-class="AnswerGrouper"]')
    token = soup.select_one('meta[name="csrf-token"]')
    if tag is None or token is None:
        raise ValueError("Grouping editor or CSRF token missing")
    props = json.loads(tag["data-react-props"])
    if str(props["question"]["id"]) != str(question_id) or str(
        props["question"]["course_id"]
    ) != str(course_id):
        raise ValueError("Grouping page belongs to a different question/course")
    return conn, props, token["content"]


_METHODS = {
    "individual": "not_grouped",
    "groups": "complex",
    "multiple_choice": "mc",
    "math": "math",
    "text": "words",
}


def set_question_grading_method(
    course_id: str,
    question_id: str,
    method: str,
    apply_to_all: bool = False,
    expected_revision: str = "",
    confirm_write: bool = False,
) -> str:
    """Choose individual/manual/AI grouping, optionally for the assignment's leaf questions."""
    completed = []
    try:
        if method not in _METHODS or type(apply_to_all) is not bool:
            return "Error: Invalid grading method or apply_to_all."
        with serialized_write("grading method", course_id):
            conn, props, token = _grouper(course_id, question_id)
            aid = props["question"]["assignment_id"]
            targets = [str(question_id)]
            if apply_to_all:
                questions = _fetch_assignment_questions(course_id, str(aid))
                parents = {
                    str(q.get("parent_id"))
                    for q in questions.values()
                    if q.get("parent_id") is not None
                }
                targets = sorted(
                    k
                    for k, q in questions.items()
                    if k not in parents and q.get("type") != "QuestionGroup"
                )
                if str(question_id) not in targets:
                    raise ValueError(
                        "Question not found among assignment leaf questions"
                    )
            pages = {str(question_id): (conn, props, token)}
            for qid in targets:
                if qid not in pages:
                    pages[qid] = _grouper(course_id, qid)
            state = {qid: pages[qid][1]["question"] for qid in targets}
            if any(str(q["assignment_id"]) != str(aid) for q in state.values()):
                raise ValueError("Assignment mismatch")
            if method in ("multiple_choice", "math", "text"):
                region = (
                    "mc_mark_regions"
                    if method == "multiple_choice"
                    else "final_answer_regions"
                )
                if any(not q.get(region) for q in state.values()):
                    return "Error: This AI method requires configured answer regions; use groups for manual grouping or configure regions in Gradescope."
            payload = {"question": {"assisted_grading_type": _METHODS[method]}}
            requests = {}
            for qid in targets:
                c, p, t = pages[qid]
                requests[qid] = (
                    _target(
                        c.gradescope_base_url,
                        p["urls"]["question_url"],
                        f"/courses/{course_id}/questions/{qid}",
                    ),
                    _headers(t),
                )
            preview = _preview(
                "set_question_grading_method",
                course_id,
                question_id,
                state,
                {"targets": targets, "payload": payload},
                expected_revision,
                confirm_write,
            )
            if preview:
                return preview
            for qid in targets:
                c, p, t = pages[qid]
                if p["question"].get("assisted_grading_type") == _METHODS[method]:
                    continue
                url, headers = requests[qid]
                resp = c.session.patch(
                    url, json=payload, headers=headers, allow_redirects=False
                )
                if resp.status_code != 200:
                    raise ValueError(
                        f"Question {qid}: HTTP {resp.status_code}; outcome must be checked"
                    )
                completed.append(qid)
            mismatches = [
                qid
                for qid in targets
                if _grouper(course_id, qid)[1]["question"].get("assisted_grading_type")
                != _METHODS[method]
            ]
            if mismatches:
                return (
                    "⚠️ Method requests accepted; read-back differs for "
                    + json.dumps(mismatches)
                )
            return "Grading method verified for questions " + json.dumps(targets)
    except AuthError:
        raise
    except Exception as exc:
        return f"Error: {exc}. Accepted question updates: {json.dumps(completed)}. Inspect targets before retrying."


def create_answer_group(
    course_id: str,
    question_id: str,
    title: str,
    submission_ids: list[str],
    expected_revision: str = "",
    confirm_write: bool = False,
) -> str:
    """Create a manual group from ungraded, ungrouped question submissions."""
    created = None
    try:
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            return "Error: Supply a group title of 1–200 characters."
        ids = [str(s) for s in submission_ids]
        if (
            not ids
            or len(ids) > 500
            or len(set(ids)) != len(ids)
            or any(not s.isascii() or not s.isdigit() for s in ids)
        ):
            return "Error: Supply 1–500 distinct question submission IDs."
        with serialized_write("answer group membership", course_id, question_id):
            conn, props, token = _grouper(course_id, question_id)
            if props["question"].get("assisted_grading_type") in (None, "not_grouped"):
                return "Error: Select groups grading method first."
            if any(g.get("title") == title for g in props["groups"]):
                return "Error: Group title already exists; inspect existing groups."
            subs = {str(s["id"]): s for s in props["submissions"]}
            if any(s not in subs for s in ids):
                return "Error: Some submission IDs do not belong to this question."
            state = {
                s: {
                    k: subs[s].get(k)
                    for k in (
                        "graded",
                        "graded_individually",
                        "confirmed_group_id",
                        "unconfirmed_group_id",
                    )
                }
                for s in ids
            }
            if any(
                s.get("graded") is not False
                or s.get("graded_individually")
                or s.get("confirmed_group_id") is not None
                or s.get("unconfirmed_group_id") is not None
                for s in state.values()
            ):
                return "Error: Only explicitly ungraded and ungrouped submissions can be added; regrouping graded answers may change grades."
            payload = {"title": title, "internal_title": None}
            root = f"/courses/{course_id}/questions/{question_id}"
            url = _target(
                conn.gradescope_base_url,
                props["urls"]["answer_groups_url"],
                root + "/answer_groups",
            )
            member_url = _target(
                conn.gradescope_base_url,
                props["urls"]["confirmed_memberships_url"],
                root + "/answer_group_memberships/many",
            )
            headers = _headers(token)
            preview = _preview(
                "create_answer_group",
                course_id,
                question_id,
                {"members": state, "groups": [g["id"] for g in props["groups"]]},
                {"create": payload, "submission_ids": ids},
                expected_revision,
                confirm_write,
            )
            if preview:
                return preview
            resp = conn.session.post(
                url, json=payload, headers=headers, allow_redirects=False
            )
            if resp.status_code not in (200, 201):
                raise ValueError(f"Create returned HTTP {resp.status_code}")
            created = resp.json()["id"]
            # Re-read after creation: do not move answers another grader just graded/grouped.
            latest = _grouper(course_id, question_id)[1]
            selected = {
                str(s["id"]): {
                    k: s.get(k)
                    for k in (
                        "graded",
                        "graded_individually",
                        "confirmed_group_id",
                        "unconfirmed_group_id",
                    )
                }
                for s in latest["submissions"]
                if str(s["id"]) in ids
            }
            if selected != state:
                raise ValueError(
                    "Members changed after group creation; empty group retained"
                )
            resp = conn.session.post(
                member_url,
                json={
                    "answer_group_id": created,
                    "submission_ids": [int(s) for s in ids],
                },
                headers=headers,
                allow_redirects=False,
            )
            if resp.status_code != 200:
                raise ValueError(f"Membership returned HTTP {resp.status_code}")
            saved = _grouper(course_id, question_id)[1]
            actual = {
                str(s["id"])
                for s in saved["submissions"]
                if str(s.get("confirmed_group_id")) == str(created)
            }
            if actual != set(ids):
                raise ValueError("Membership read-back differs")
            return f"Answer group {created} created; {len(ids)} confirmed members verified. No grades submitted."
    except AuthError:
        raise
    except Exception as exc:
        return f"Error: {exc}. Created group ID: {created}; outcome may be partial. Inspect groups before retrying; do not blindly create again."

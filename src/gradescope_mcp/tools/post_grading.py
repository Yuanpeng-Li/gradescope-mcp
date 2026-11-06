"""Regrade response and student accommodation writes with current-state previews."""

import json
import math
import re
from urllib.parse import urlsplit

from gradescope_mcp.auth import get_connection, AuthError
from gradescope_mcp.tools.assignment_admin import _page, _token, _bound
from gradescope_mcp.tools.question_settings import _preview, _target, _headers
from gradescope_mcp.tools.grading_ops import _get_grading_context, _write_target_problem
from gradescope_mcp.tools.safety import serialize_question_write, serialized_write


def _requests(props):
    items = [
        dict(r, completed=r.get("completed", True))
        for r in (props.get("closed_requests") or [])
        if isinstance(r, dict)
    ]
    opened = props.get("open_request")
    if isinstance(opened, dict):
        items.append(dict(opened, completed=opened.get("completed", False)))
    return items


@serialize_question_write
def respond_regrade_request(
    course_id: str,
    question_id: str,
    submission_id: str,
    request_id: str,
    comment: str,
    completed: bool = True,
    expected_revision: str = "",
    confirm_write: bool = False,
) -> str:
    """Reply/resolve or request reopening, then verify the server's reported state."""
    try:
        if (
            not isinstance(comment, str)
            or not comment.strip()
            or type(completed) is not bool
        ):
            raise ValueError("Nonempty comment and boolean completed required")
        ctx = _get_grading_context(course_id, question_id, submission_id)
        props = ctx["props"]
        problem = _write_target_problem(props, course_id, question_id, submission_id)
        if problem:
            raise ValueError(problem)
        req = next(
            (r for r in _requests(props) if str(r.get("id")) == request_id), None
        )
        if req is None:
            raise ValueError("Request ID does not belong to this submission")
        base = (props.get("urls") or {}).get("regrade_request", "")
        path = urlsplit(base).path.rstrip("/") + "/" + request_id
        root = f"/courses/{course_id}/questions/{question_id}/submissions/{submission_id}/regrade_requests"
        # Some pages expose assignment-level request URLs. Only the exact
        # page-provided URL is used, with identity established by its request list.
        if (
            not path.startswith(f"/courses/{course_id}/")
            or "/regrade_requests/" not in path
        ):
            raise ValueError("Unexpected regrade endpoint")
        url = _target(ctx["base_url"], base.rstrip("/") + "/" + request_id, path)
        payload = {
            "regrade_request": {
                "id": int(request_id),
                "staff_comment": comment,
                "completed": completed,
            }
        }
        headers = _headers(ctx["csrf_token"])
        preview = _preview(
            "respond_regrade_request",
            course_id,
            question_id,
            req,
            payload,
            expected_revision,
            confirm_write,
        )
        if preview:
            return preview
        resp = ctx["session"].put(
            url, json=payload, headers=headers, allow_redirects=False
        )
        if resp.status_code != 200:
            return f"Error: Regrade response HTTP {resp.status_code}; inspect before retrying."
        try:
            saved = next(
                (
                    r
                    for r in _requests(
                        _get_grading_context(course_id, question_id, submission_id)[
                            "props"
                        ]
                    )
                    if str(r.get("id")) == request_id
                ),
                None,
            )
            if (
                saved is None
                or saved.get("staff_comment") != comment
                or saved.get("completed") is not completed
            ):
                return "⚠️ Regrade response accepted but requested text/completion state was not verified. Inspect before retrying."
        except AuthError:
            raise
        except Exception:
            return "⚠️ Regrade response accepted; read-back unavailable."
        return "Regrade response and completion state saved and verified. No grade changed."
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: Regrade operation failed or outcome unknown: {exc}. Inspect before retrying."


class _OverrideMissing(ValueError):
    pass


def _extension(conn, c, a, user):
    soup = _page(conn, f"/courses/{c}/assignments/{a}/extensions")
    for tag in soup.select('[data-react-class="EditExtension"]'):
        p = json.loads(tag["data-react-props"])
        override = p.get("override") or {}
        if str(override.get("user_id", override.get("userId"))) == str(user):
            return p, _token(soup)
    if soup.select_one("table.js-overridesTable") or soup.select_one(
        '[data-react-class="AddExtension"]'
    ):
        raise _OverrideMissing(
            "No editable override found for this user; inherited overrides must be managed at their source"
        )
    raise ValueError("Unrecognized extensions page")


def modify_extension_options(
    course_id: str,
    assignment_id: str,
    user_id: str,
    action: str,
    time_limit_type: str | None = None,
    value: float | None = None,
    expected_revision: str = "",
    confirm_write: bool = False,
) -> str:
    """Delete a local override or modify its time limit, preserving other settings."""
    try:
        if action not in ("delete", "time_limit"):
            raise ValueError("Invalid extension action")
        if action == "time_limit":
            if time_limit_type not in (
                "absolute_minutes",
                "extension_minutes",
                "extension_multiplier",
                "no_time_limit",
            ):
                raise ValueError("Invalid time limit type")
            if time_limit_type != "no_time_limit" and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError("Positive finite time limit value required")
        with serialized_write("extension", course_id, assignment_id, user_id):
            conn = get_connection()
            p, token = _extension(conn, course_id, assignment_id, user_id)
            if p.get("inherited_override") or p.get("inheritedOverride"):
                raise ValueError("Cannot edit inherited override here")
            current = p["override"]
            settings = dict(current.get("settings") or {})
            root = f"/courses/{course_id}/assignments/{assignment_id}/extensions"
            if action == "delete":
                raw = p.get("delete_path", p.get("deletePath", ""))
                path = urlsplit(raw).path
                if not re.fullmatch(re.escape(root) + r"/\d+", path):
                    raise ValueError("No assignment-local delete endpoint")
                url = _target(conn.gradescope_base_url, raw, path)
                payload = None
            else:
                raw = p.get("path", root)
                url = _target(conn.gradescope_base_url, raw, root)
                settings["time_limit"] = {
                    "type": time_limit_type,
                    "value": value if time_limit_type != "no_time_limit" else "",
                }
                payload = {"override": {"user_id": user_id, "settings": settings}}
            headers = _headers(token)
            preview = _preview(
                "modify_extension_options",
                course_id,
                assignment_id,
                current,
                {"action": action, "user_id": user_id, "payload": payload},
                expected_revision,
                confirm_write,
            )
            if preview:
                return preview
            resp = (
                conn.session.delete(url, headers=headers, allow_redirects=False)
                if action == "delete"
                else conn.session.post(
                    url, json=payload, headers=headers, allow_redirects=False
                )
            )
            if resp.status_code not in (200, 204):
                return f"Error: Extension request HTTP {resp.status_code}; inspect before retrying."
            try:
                after, _ = _extension(conn, course_id, assignment_id, user_id)
            except _OverrideMissing:
                if action == "delete":
                    return "Override removed and absence verified."
                return "⚠️ Extension request accepted; override not found on read-back."
            except AuthError:
                raise
            except Exception:
                return "⚠️ Extension request accepted; read-back could not verify the result."
            if action == "delete" or after["override"].get("settings") != settings:
                return "⚠️ Extension request accepted; read-back differs."
            return "Time-limit override saved and verified; other settings preserved."
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: Extension outcome not confirmed: {exc}"

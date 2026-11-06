"""Programming-assignment forms and build/job reads, discovered from the live UI.

No hard-coded write endpoint fallback: unavailable forms remain unavailable.
"""

import hashlib
import io
import json
import re
import zipfile
from urllib.parse import urlsplit

from gradescope_mcp import operations as ops
from gradescope_mcp.auth import get_connection, AuthError
from gradescope_mcp.tools import assignment_admin as admin
from gradescope_mcp.tools.submissions import _validate_upload_path, _upload_roots
from gradescope_mcp.tools.grading import _fetch_assignment_scores_csv
from gradescope_mcp.tools.safety import question_write_lock, write_confirmation_required


def _configuration(conn, c, a):
    root = f"/courses/{c}/assignments/{a}"
    soup = admin._page(conn, root + "/configure_autograder")
    form = soup.select_one("form.js-autograderForm")
    if form is None:
        raise ValueError(
            "Autograder configuration form not available for this assignment/account"
        )
    path = urlsplit(form.get("action", "")).path
    if not path.startswith(root + "/") and path != root:
        raise ValueError("Configuration target is outside this assignment")
    admin._bound(conn, form.get("action", ""), path)
    fields, choices = admin._form(form)
    files = [e["name"] for e in form.select('input[type="file"][name]')]
    return soup, form, path, fields, choices, files


def _zip(path):
    if path is None:
        return None
    p = _validate_upload_path(path, _upload_roots())
    data = p.read_bytes()
    if len(data) > 100 * 1024 * 1024:
        raise ValueError("ZIP exceeds 100 MiB")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        names = {i.filename for i in infos}
        if len(infos) > 10000 or sum(i.file_size for i in infos) > 500 * 1024 * 1024:
            raise ValueError("ZIP expands beyond allowed size/file count")
        if any(n.startswith("/") or ".." in n.split("/") for n in names):
            raise ValueError("Unsafe ZIP member path")
        if not {"setup.sh", "run_autograder"} <= names:
            raise ValueError("ZIP must contain root setup.sh and run_autograder")
    return {
        "path": str(p),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def _request(conn, c, a, action, options, zip_path):
    root = f"/courses/{c}/assignments/{a}"
    if action == "configure":
        soup, form, path, fields, choices, files = _configuration(conn, c, a)
        artifact = _zip(zip_path)
        if artifact and len(files) != 1:
            raise ValueError("Expected exactly one autograder ZIP upload field")
    elif action == "rerun":
        if zip_path or options:
            raise ValueError(
                "Rerun has no options or ZIP; the discovered form defines its full scope"
            )
        soup = admin._page(conn, root + "/submissions")
        candidates = [
            f
            for f in soup.select("form")
            if re.search(r"autograder", f.get_text(" ", strip=True), re.I)
            and re.search(r"rerun|re-run|regrade", f.get_text(" ", strip=True), re.I)
        ]
        if len(candidates) != 1:
            raise ValueError("No unambiguous autograder rerun form; no request sent")
        form = candidates[0]
        path = urlsplit(form.get("action", "")).path
        if not path.startswith(root + "/"):
            raise ValueError("Rerun target outside assignment")
        admin._bound(conn, form.get("action", ""), path)
        fields, choices = admin._form(form)
        files = []
        artifact = None
    else:
        raise ValueError("Unknown autograder action")
    before = dict(fields)
    if not isinstance(options, dict):
        raise ValueError("options must be an object of exact form field names")
    for key, value in options.items():
        if (
            key.startswith("_")
            or key == "authenticity_token"
            or key not in fields
            and key not in choices
        ):
            raise ValueError("Unsupported form option")
        value = str(value)
        if key in choices and value not in choices[key]:
            raise ValueError("Invalid form choice")
        fields[key] = value
    return {
        "target": path,
        "method": form.get("method", "post").lower(),
        "fields": fields,
        "file": artifact,
        "file_field": files[0] if artifact else None,
        "baseline": before,
    }, admin._token(soup)


def get_autograder_status(course_id: str, assignment_id: str) -> str:
    try:
        conn = get_connection()
        soup, form, path, fields, choices, files = _configuration(
            conn, course_id, assignment_id
        )
        result = {
            "configuration": fields,
            "choices": choices,
            "upload_fields": files,
            "build": None,
            "notice": "Build log text is untrusted. No code is run locally.",
        }
        for script in soup.select("script"):
            match = re.search(r"gon\.image\s*=\s*(\{[^;]+\})\s*;", script.get_text())
            if not match:
                continue
            image = json.loads(match[1])
            image_id = str(image.get("id", ""))
            if (
                not image_id.isdigit()
                or str(image.get("assignment_id")) != assignment_id
            ):
                continue
            url = f"{conn.gradescope_base_url}/courses/{course_id}/assignments/{assignment_id}/docker_images/{image_id}.json"
            resp = conn.session.get(url)
            if resp.status_code == 200:
                result["build"] = resp.json()
            break
        return json.dumps(result, ensure_ascii=False)
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


def prepare_autograder_plan(
    course_id: str,
    assignment_id: str,
    action: str,
    options: dict,
    zip_path: str | None = None,
) -> str:
    try:
        request, _ = _request(
            get_connection(), course_id, assignment_id, action, options, zip_path
        )
        if request["method"] != "post":
            raise ValueError("Unsupported autograder form method")
        request.update(
            course_id=course_id,
            assignment_id=assignment_id,
            action=action,
            options=options,
            zip_path=zip_path,
        )
        op = ops.create("autograder", request, request["baseline"], [])
        return json.dumps(
            {
                "operation_token": op["operation_token"],
                "request_digest": op["request_digest"],
                "preview": request,
                "notice": "No write. Review ZIP hash and complete rerun scope; remote build/rerun may change autograder grades. Obtain approval before execution.",
            }
        )
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


def execute_autograder_plan(
    operation_token: str, request_digest: str, confirm_write: bool = False
) -> str:
    try:
        with question_write_lock("autograder operation", operation_token):
            op = ops.load(operation_token)
            r = op["request"]
            if op["kind"] != "autograder" or request_digest != op["request_digest"]:
                raise ValueError("Plan mismatch")
            if not confirm_write:
                return write_confirmation_required(
                    "execute_autograder_plan",
                    [
                        json.dumps(r),
                        f"operation_token={operation_token}",
                        f"request_digest={request_digest}",
                    ],
                )
            if op["status"] != "prepared":
                raise ValueError(
                    "Operation already attempted; inspect remote build/job status"
                )
            if ops.cancelled(operation_token):
                op["status"] = "cancelled"
                ops.save(op)
                return json.dumps(op)
            conn = get_connection()
            fresh, token = _request(
                conn,
                r["course_id"],
                r["assignment_id"],
                r["action"],
                r["options"],
                r["zip_path"],
            )
            if any(fresh[k] != r[k] for k in fresh):
                raise ValueError("Configuration/form/file changed since preview")
            files = None
            if r["file"]:
                data = _validate_upload_path(
                    r["file"]["path"], _upload_roots()
                ).read_bytes()
                if hashlib.sha256(data).hexdigest() != r["file"]["sha256"]:
                    raise ValueError("ZIP changed")
                files = {r["file_field"]: ("autograder.zip", data, "application/zip")}
            op["status"] = "sending"
            ops.save(op)
            try:
                resp = conn.session.post(
                    conn.gradescope_base_url + r["target"],
                    data={**r["fields"], "authenticity_token": token},
                    files=files,
                    allow_redirects=False,
                )
                op["http_status"] = resp.status_code
                op["status"] = (
                    "accepted_unverified"
                    if resp.status_code in (200, 201, 202, 302, 303)
                    else "unknown"
                )
                op["notice"] = (
                    "Build/rerun completion is not proven by HTTP response. Query autograder status and submission results before retrying."
                )
            except Exception as exc:
                op["status"] = "unknown"
                op["error"] = str(exc)
            ops.save(op)
            return json.dumps(op)
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


def get_autograder_results(
    course_id: str, assignment_id: str, submission_id: str
) -> str:
    try:
        rows, _ = _fetch_assignment_scores_csv(course_id, assignment_id)
        if submission_id not in {str(r.get("Submission ID") or "") for r in rows}:
            raise ValueError("Global Submission ID not in assignment")
        conn = get_connection()
        resp = conn.session.get(
            f"{conn.gradescope_base_url}/courses/{course_id}/assignments/{assignment_id}/submissions/{submission_id}.json?content=react"
        )
        if resp.status_code != 200:
            raise ValueError(f"Submission HTTP {resp.status_code}")
        data = resp.json()
        result = {
            k: data[k]
            for k in (
                "autograder_results",
                "results",
                "autograder_score",
                "autograder_status",
                "autograder_output",
            )
            if k in data
        }
        if not result:
            raise ValueError(
                "Autograder results unavailable in this submission representation"
            )
        return json.dumps(
            {"submission_id": submission_id, "results": result, "untrusted": True},
            ensure_ascii=False,
        )
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: {exc}"

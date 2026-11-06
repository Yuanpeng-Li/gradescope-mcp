"""Assignment lifecycle plans using observed HTML forms and React creation props."""

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from gradescope_mcp import operations as ops
from gradescope_mcp.auth import AuthError, get_connection
from gradescope_mcp.cache import get_cache_root
from gradescope_mcp.tools.assignments import (
    _dates_from_props,
    parse_date_input,
    check_date_order,
)
from gradescope_mcp.tools.submissions import _validate_upload_path, _upload_roots
from gradescope_mcp.tools.safety import (
    serialized_write,
    question_write_lock,
    write_confirmation_required,
)

_TYPES = {
    "exam": "PDFAssignment",
    "homework": "PDFAssignment",
    "online": "OnlineAssignment",
    "programming": "ProgrammingAssignment",
    "bubble_sheet": "PDFAssignment",
}
_SETTING_KEYS = {
    "title",
    "student_submission",
    "submission_type",
    "submissions_anonymized",
    "enable_grading_by_course_section",
    "enforce_time_limit",
    "time_limit_in_minutes",
    "group_submission",
    "group_size",
    "template_visible_to_students",
    "rubric_locking_setting",
    "when_to_create_rubric",
    "rubric_item_groups_mutually_exclusive",
    "scoring_type",
    "ceiling",
    "floor",
    "rubric_visibility_setting",
    "release_date_string",
    "due_date_string",
    "hard_due_date_string",
    "allow_late_submissions",
    "manual_grading",
    "leaderboard_enabled",
    "leaderboard_max_entries",
}
_DATE_KEYS = ("release_date_string", "due_date_string", "hard_due_date_string")


def _page(conn, path):
    r = conn.session.get(conn.gradescope_base_url + path)
    if r.status_code != 200:
        raise ValueError(f"Page unavailable (HTTP {r.status_code})")
    return BeautifulSoup(r.text, "html.parser")


def _token(soup):
    meta = soup.select_one('meta[name="csrf-token"]')
    value = meta.get("content") if meta is not None else None
    if not value:
        field = soup.select_one('input[name="authenticity_token"]')
        value = field.get("value") if field is not None else None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("CSRF token missing")
    return value


def _form(form):
    fields = {}
    choices = {}
    for e in form.select("input[name],select[name],textarea[name]"):
        if e.has_attr("disabled"):
            continue
        name = e["name"]
        typ = e.get("type", "text")
        if name in ("authenticity_token", "utf8", "commit", "button") or typ in (
            "file",
            "submit",
            "button",
        ):
            continue
        if typ in ("checkbox", "radio"):
            if typ == "checkbox" and name in fields:
                choices.setdefault(name, []).append(fields[name])
            choices.setdefault(name, []).append(e.get("value", "on"))
            if not e.has_attr("checked"):
                continue
        if e.name == "select":
            choices[name] = [o.get("value", o.text) for o in e.select("option")]
            option = e.select_one("option[selected]") or e.select_one("option")
            fields[name] = option.get("value", option.text) if option else ""
        else:
            fields[name] = e.text if e.name == "textarea" else e.get("value", "")
    return fields, choices


def _bound(conn, url, path):
    target = urlsplit(urljoin(conn.gradescope_base_url, url))
    base = urlsplit(conn.gradescope_base_url)
    if (
        (target.scheme, target.netloc, target.path) != (base.scheme, base.netloc, path)
        or target.query
        or target.fragment
    ):
        raise ValueError("Unexpected form action; refusing cross-target write")
    return path


def _file(path, trusted_artifact=False):
    if path is None:
        return None
    roots = _upload_roots()
    if trusted_artifact and roots is None:
        p = Path(path)
        root = get_cache_root()
        if (p.parent != root or p.is_symlink() or not p.is_file()
                or not re.fullmatch(r'clone-template-[a-f0-9]{20}\.pdf', p.name)):
            raise ValueError('Invalid internally downloaded clone template')
        if p.stat().st_size > 100*1024*1024:
            raise ValueError('Template exceeds 100 MiB')
    else:
        # An explicitly configured upload-root policy always takes precedence.
        p = _validate_upload_path(path, roots)
    content = p.read_bytes()
    if len(content) > 100 * 1024 * 1024 or not content.startswith(b"%PDF-"):
        raise ValueError("Template must be a PDF of at most 100 MiB")
    return {
        "path": str(p),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def _values(options):
    if not isinstance(options, dict):
        raise ValueError("options must be an object")
    if any(k not in _SETTING_KEYS for k in options):
        raise ValueError("Unsupported setting field")
    result = {}
    for k, v in options.items():
        if not isinstance(v, (str, int, float, bool)) or v is None:
            raise ValueError("Setting values must be scalar, not null")
        if k in ("group_size", "time_limit_in_minutes", "leaderboard_max_entries"):
            if isinstance(v, bool) or not str(v).isdigit() or int(v) < 1:
                raise ValueError(f"{k} must be a positive integer")
        if k in _DATE_KEYS and v:
            parsed = parse_date_input(v, k, allow_offset=False)
            if parsed.second or parsed.microsecond:
                raise ValueError("Assignment dates require minute precision")
            v = parsed.strftime("%Y-%m-%dT%H:%M")
        result[f"assignment[{k}]"] = (
            ("true" if v else "false")
            if isinstance(v, bool)
            and k in ("student_submission", "rubric_item_groups_mutually_exclusive")
            else ("1" if v else "0")
            if isinstance(v, bool)
            else str(v)
        )
    if "title" in options and not str(options["title"]).strip():
        raise ValueError("Title cannot be blank")
    return result


def _settings(conn, c, a):
    root = f"/courses/{c}/assignments/{a}"
    soup = _page(conn, root + "/edit")
    form = soup.select_one("form#assignment_form")
    if form is None:
        raise ValueError("Assignment settings form not available")
    _bound(conn, form.get("action", ""), root)
    fields, choices = _form(form)
    dates = _dates_from_props(soup)
    if dates:
        for k, key in zip(_DATE_KEYS, ("release", "due", "late")):
            value = dates.get(key)
            if value is not None:
                fields[f"assignment[{k}]"] = value
        if dates.get("allow_late") is not None:
            fields["assignment[allow_late_submissions]"] = (
                "1" if dates["allow_late"] else "0"
            )
    return soup, fields, choices


def _prepare(conn, course_id, assignment_id, action, options, template_path, trusted_template=False):
    c, a = course_id, assignment_id
    root = f"/courses/{c}/assignments" + (f"/{a}" if a else "")
    artifact = _file(template_path, trusted_template)
    if action == "create":
        if a:
            raise ValueError("Creation must omit assignment_id")
        kind = options.get("kind")
        title = options.get("title")
        if kind not in _TYPES or not isinstance(title, str) or not title.strip():
            raise ValueError("Creation needs kind and title")
        if kind in ("exam", "homework") and artifact is None:
            raise ValueError("PDF assignments require template_path")
        if kind in ("online", "programming", "bubble_sheet") and artifact:
            raise ValueError("This assignment type does not use a PDF template upload")
        soup = _page(conn, root + "/new")
        tag = soup.select_one('[data-react-class="CreateAssignment"]')
        if tag is None:
            raise ValueError("CreateAssignment component unavailable")
        props = json.loads(tag["data-react-props"])
        _bound(conn, props["urls"]["createAssignment"], root)
        if (props.get("featuresAccess") or {}).get(kind.upper()) is not True:
            raise ValueError(
                "This assignment type is not enabled for this account/course"
            )
        values = _values({k: v for k, v in options.items() if k != "kind"})
        fields = {
            "assignment[type]": _TYPES[kind],
            "assignment[student_submission]": "false"
            if kind in ("exam", "bubble_sheet")
            else "true",
            "assignment[submission_type]": "image"
            if kind == "homework"
            else "online"
            if kind == "online"
            else "pdf",
            "assignment[bubble_sheet]": "true" if kind == "bubble_sheet" else "false",
            **values,
        }
        if fields["assignment[student_submission]"] == "true" and not all(
            fields.get("assignment[" + k + "]") for k in _DATE_KEYS[:2]
        ):
            raise ValueError(
                "Student-submitted assignments require explicit release and due dates"
            )
        baseline = {
            "create_url": props["urls"]["createAssignment"],
            "features": props.get("featuresAccess"),
        }
        verify = values
    elif action in ("settings", "template"):
        if not a:
            raise ValueError("assignment_id required")
        soup, fields, choices = _settings(conn, c, a)
        baseline = dict(fields)
        values = _values(options)
        if action == "settings" and not values:
            raise ValueError("Specify at least one setting to change")
        if action == "template" and artifact is None:
            raise ValueError("template_path required")
        if (
            action == "template"
            and soup.select_one('input[type="file"][name="template_pdf"]') is None
        ):
            raise ValueError("This assignment has no template upload field")
        if action == "settings" and artifact is not None:
            raise ValueError("Use template action for template replacement")
        for k, v in values.items():
            if k not in fields and k not in choices:
                raise ValueError(f"{k} is not available on this assignment form")
            if (
                k in choices
                and v not in choices[k]
                and not (v == "0" and fields.get(k) == "0")
            ):
                raise ValueError(f"Invalid option for {k}")
        fields.update(values)
        fields["_method"] = "patch"
        verify = values
    elif action in ("publish", "unpublish", "notify", "lms_post"):
        if not a or artifact:
            raise ValueError("Existing assignment required; no template allowed")
        soup = _page(conn, root + "/review_grades")
        if action in ("publish", "unpublish"):
            if options:
                raise ValueError(
                    "Publication takes no options; notification is separate"
                )
            node = soup.select_one('input[name="assignment[published]"]')
            if node is None:
                raise ValueError("Publication form unavailable")
            form = node.find_parent("form")
            _bound(conn, form.get("action", ""), root)
            fields, _ = _form(form)
            desired = "true" if action == "publish" else "false"
            baseline = {"published": fields["assignment[published]"] != "true"}
            fields["assignment[published]"] = desired
            # The assignment-container field belongs to a different scope.
            fields.pop("assignment_container[published]", None)
            verify = {"published": action == "publish"}
        elif action == "notify":
            form = next(
                (
                    f
                    for f in soup.select("form")
                    if urlsplit(f.get("action", "")).path == root + "/notify_students"
                ),
                None,
            )
            if form is None:
                raise ValueError("Student notification form unavailable")
            if set(options) - {"subject", "message_body", "message_footer"}:
                raise ValueError(
                    "Notification supports subject/message_body/message_footer; recipients are the displayed form recipients"
                )
            fields, _ = _form(form)
            baseline = dict(fields)
            if not options.get("subject") or not options.get("message_body"):
                raise ValueError("Supply explicit subject and message_body")
            if (
                any(not isinstance(v, str) for v in options.values())
                or not str(fields.get("to", "")).strip()
            ):
                raise ValueError(
                    "Notification text and an explicit form recipient scope are required"
                )
            fields.update(options)
            root += "/notify_students"
            verify = {}
        else:
            if options:
                raise ValueError(
                    "LMS posting takes no options; uses the existing linked resource"
                )
            tag = soup.select_one('[data-react-class="PostGrades"]')
            if tag is None:
                raise ValueError("LMS posting not available")
            props = json.loads(tag["data-react-props"])
            path = urlsplit(props.get("postGradesPath", "")).path
            if not props.get("resourceLinkName") or not path.startswith(root + "/"):
                raise ValueError(
                    "No linked LMS assignment or supported post-grades endpoint"
                )
            _bound(conn, props["postGradesPath"], path)
            root = path
            fields = {}
            baseline = {
                k: props.get(k)
                for k in (
                    "resourceLinkName",
                    "postGradesPath",
                    "gradesPresent",
                    "jobStatusPath",
                )
            }
            verify = {}
    else:
        raise ValueError("Unsupported assignment action")
    if action in ("create", "settings"):
        dates = [
            (
                k,
                parse_date_input(
                    fields["assignment[" + k + "]"], k, allow_offset=False
                ),
            )
            for k in _DATE_KEYS
            if fields.get("assignment[" + k + "]")
        ]
        problem = check_date_order(dates)
        if problem:
            raise ValueError(problem)
    if action in ("settings", "template"):
        # PATCH only the requested attributes; resending disabled/unrelated
        # checkbox defaults can reset settings the caller did not approve.
        fields = {"_method": "patch", **values}
        if action == "template" and not values:
            fields["assignment[title]"] = baseline["assignment[title]"]
    return {
        "target": root,
        "fields": fields,
        "file": artifact,
        "baseline": baseline,
        "verify": verify,
    }, _token(soup)


def prepare_assignment_plan(
    course_id: str,
    action: str,
    options: dict,
    assignment_id: str | None = None,
    template_path: str | None = None,
) -> str:
    try:
        request, _ = _prepare(
            get_connection(), course_id, assignment_id, action, options, template_path
        )
        request.update(
            course_id=course_id,
            assignment_id=assignment_id,
            action=action,
            options=options,
            template_path=template_path,
        )
        plan = ops.create("assignment", request, request["baseline"], [])
        return json.dumps(
            {
                "operation_token": plan["operation_token"],
                "request_digest": plan["request_digest"],
                "preview": request,
                "notice": "No remote changes. Show this exact request and obtain explicit approval before executing. Notifications send email; publication alone does not.",
            },
            ensure_ascii=False,
        )
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


def _verify_template(conn, soup, expected_hash):
    from gradescope_mcp.tools.submission_files import _download

    links = {
        a["href"]
        for a in soup.select("a[href]")
        if urlsplit(a["href"]).path.lower().endswith(".pdf")
    }
    if len(links) != 1:
        raise ValueError("Cannot uniquely identify the uploaded template for read-back")
    data = _download(conn, next(iter(links)))
    if hashlib.sha256(data).hexdigest() != expected_hash:
        raise ValueError("Template bytes differ on read-back; inspect before retrying")


def execute_assignment_plan(
    operation_token: str, request_digest: str, confirm_write: bool = False
) -> str:
    try:
        with (
            serialized_write("assignment operation", operation_token),
            question_write_lock("operation", operation_token),
        ):
            op = ops.load(operation_token)
            r = op["request"]
            if op["kind"] != "assignment" or request_digest != op["request_digest"]:
                raise ValueError("Plan digest/kind mismatch")
            if not confirm_write:
                return write_confirmation_required(
                    "execute_assignment_plan",
                    [
                        json.dumps(r, ensure_ascii=False),
                        f"operation_token={operation_token}",
                        f"request_digest={request_digest}",
                    ],
                )
            if op["status"] != "prepared":
                return "Error: Operation was already attempted; inspect its journal and remote state instead of replaying."
            if ops.cancelled(operation_token):
                op["status"] = "cancelled"
                ops.save(op)
                return json.dumps(op)
            conn = get_connection()
            fresh, token = _prepare(
                conn,
                r["course_id"],
                r["assignment_id"],
                r["action"],
                r["options"],
                r["template_path"],
                r.get("trusted_template", False),
            )
            if any(
                fresh[k] != r[k]
                for k in ("target", "fields", "file", "baseline", "verify")
            ):
                raise ValueError(
                    "Source state or uploaded file changed; prepare a new plan"
                )
            data = {**fresh["fields"], "authenticity_token": token}
            files = None
            if fresh["file"]:
                meta = fresh["file"]
                validated = _file(meta["path"], r.get("trusted_template", False))
                content = Path(validated["path"]).read_bytes()
                if hashlib.sha256(content).hexdigest() != meta["sha256"]:
                    raise ValueError("Template changed")
                files = {"template_pdf": ("template.pdf", content, "application/pdf")}
            if ops.cancelled(operation_token):
                op["status"] = "cancelled"
                ops.save(op)
                return json.dumps(op)
            op["status"] = "sending"
            ops.save(op)
            try:
                resp = conn.session.post(
                    conn.gradescope_base_url + r["target"],
                    data=data,
                    files=files,
                    allow_redirects=False,
                )
                op["http_status"] = resp.status_code
                if resp.status_code not in (200, 201, 302, 303):
                    op["status"] = "unknown"
                    ops.save(op)
                    return json.dumps(op)
                op["status"] = "accepted_unverified"
                ops.save(op)
                if r["action"] == "lms_post":
                    body = resp.json()
                    if isinstance(body, dict) and isinstance(body.get("jid"), str):
                        op["job_token"] = body["jid"]
                        ops.save(op)
                if r["action"] == "create":
                    path = urlsplit(
                        urljoin(
                            conn.gradescope_base_url, resp.headers.get("Location", "")
                        )
                    ).path
                    match = re.fullmatch(
                        r"/courses/"
                        + re.escape(r["course_id"])
                        + r"/assignments/(\d+)(?:/.*)?",
                        path,
                    )
                    if not match:
                        raise ValueError(
                            "Creation response has no new assignment URL; inspect listing"
                        )
                    created = match[1]
                    op["created_assignment_id"] = created
                    ops.save(op)
                    created_soup, fields, _ = _settings(conn, r["course_id"], created)
                    if any(fields.get(k) != v for k, v in r["verify"].items()):
                        raise ValueError(
                            "Created assignment settings differ from requested values"
                        )
                    if r["file"]:
                        _verify_template(conn, created_soup, r["file"]["sha256"])
                    if (
                        fields.get("assignment[type]")
                        != r["fields"]["assignment[type]"]
                    ):
                        raise ValueError("Created assignment type differs")
                    op["status"] = "verified"
                elif r["action"] in ("settings", "template"):
                    saved_soup, fields, _ = _settings(
                        conn, r["course_id"], r["assignment_id"]
                    )
                    if any(fields.get(k) != v for k, v in r["verify"].items()):
                        raise ValueError("Settings read-back differs")
                    if r["file"]:
                        _verify_template(conn, saved_soup, r["file"]["sha256"])
                    op["status"] = "verified"
                elif r["action"] in ("publish", "unpublish"):
                    after, _ = _prepare(
                        conn, r["course_id"], r["assignment_id"], r["action"], {}, None
                    )
                    if after["baseline"]["published"] != r["verify"]["published"]:
                        raise ValueError("Publication state differs")
                    op["status"] = "verified"
                # Upload bytes, mail delivery and asynchronous LMS jobs cannot
                # be proven by the redirect alone; leave accepted_unverified.
            except Exception as exc:
                op["error"] = str(exc)
                if op["status"] == "sending":
                    op["status"] = "unknown"
            ops.save(op)
            return json.dumps(op, ensure_ascii=False)
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


def get_assignment_admin(course_id: str, assignment_id: str) -> str:
    try:
        conn = get_connection()
        soup, fields, choices = _settings(conn, course_id, assignment_id)
        capabilities = {
            "settings": True,
            "template_upload": soup.select_one(
                'input[type="file"][name="template_pdf"]'
            )
            is not None,
        }
        review = _page(
            conn, f"/courses/{course_id}/assignments/{assignment_id}/review_grades"
        )
        node = review.select_one('input[name="assignment[published]"]')
        published = None if node is None else node.get("value") != "true"
        capabilities.update(
            publish=node is not None,
            notify=any(
                "notify_students" in f.get("action", "") for f in review.select("form")
            ),
        )
        tag = review.select_one('[data-react-class="PostGrades"]')
        lms = json.loads(tag["data-react-props"]) if tag else {}
        capabilities["lms_post"] = bool(
            lms.get("resourceLinkName") and lms.get("postGradesPath")
        )
        return json.dumps(
            {
                "settings": {
                    k[11:-1]: v
                    for k, v in fields.items()
                    if k.startswith("assignment[")
                },
                "choices": {
                    k[11:-1]: v
                    for k, v in choices.items()
                    if k.startswith("assignment[")
                },
                "published": published,
                "capabilities": capabilities,
                "lms": {
                    k: lms.get(k)
                    for k in ("lmsName", "resourceLinkName", "jobStatusPath")
                },
            },
            ensure_ascii=False,
        )
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


def poll_operation(operation_token: str) -> str:
    """Poll a discovered LMS job; never infer delivery/completion from submission alone."""
    try:
        with question_write_lock("operation", operation_token):
            op = ops.load(operation_token)
            if (
                op["kind"] != "assignment"
                or op["request"]["action"] != "lms_post"
                or not op.get("job_token")
            ):
                return json.dumps(
                    {
                        "status": op["status"],
                        "notice": "No supported asynchronous job handle; inspect the operation and relevant read tool.",
                    }
                )
            conn = get_connection()
            raw = op["request"]["baseline"].get("jobStatusPath")
            path = urlsplit(raw or "").path
            if not path or path == "/":
                raise ValueError("Job status path unavailable")
            _bound(conn, raw, path)
            resp = conn.session.get(
                conn.gradescope_base_url + path,
                params={"jid": op["job_token"]},
                headers={"Accept": "application/json"},
            )
            if resp.status_code != 200:
                raise ValueError(f"Job status HTTP {resp.status_code}")
            data = resp.json()
            op["job_status"] = data
            if data.get("failed"):
                op["status"] = "failed"
            elif data.get("complete"):
                result = data.get("result")
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except ValueError:
                        result = None
                op["status"] = (
                    "failed"
                    if isinstance(result, dict) and result.get("error")
                    else "verified"
                    if isinstance(result, dict)
                    else "accepted_unverified"
                )
            ops.save(op)
            return json.dumps(op)
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: Cannot poll operation: {exc}"

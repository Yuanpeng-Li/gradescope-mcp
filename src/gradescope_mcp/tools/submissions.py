"""Submission-related MCP tools."""

import contextlib
import mimetypes
import pathlib
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from gradescopeapi.classes.upload import upload_assignment
from requests_toolbelt import MultipartEncoder

from gradescope_mcp.auth import get_connection, AuthError
from gradescope_mcp.tools.grading import get_student_submission_content
from gradescope_mcp.tools.safety import write_confirmation_required


def upload_submission(
    course_id: str,
    assignment_id: str,
    file_paths: list[str],
    leaderboard_name: str | None = None,
    confirm_write: bool = False,
) -> str:
    """Upload files as a submission to a Gradescope assignment.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        file_paths: List of absolute file paths to upload.
        leaderboard_name: Optional leaderboard display name.
        confirm_write: Must be True to perform the upload.
    """
    error, validated_paths = _validate_upload_request(
        course_id, assignment_id, file_paths
    )
    if error:
        return error

    if not confirm_write:
        details = [
            f"course_id=`{course_id}`",
            f"assignment_id=`{assignment_id}`",
            f"files={', '.join(str(path) for path in validated_paths)}",
        ]
        if leaderboard_name:
            details.append(f"leaderboard_name={leaderboard_name}")
        return write_confirmation_required("upload_submission", details)

    try:
        conn = get_connection()
        # ExitStack guarantees every successfully-opened handle is closed even
        # if a later open() raises (EISDIR, EACCES, race-deleted file). The
        # earlier try/finally only protected handles after the loop completed.
        # The arguments are also passed positionally — `upload_assignment`'s
        # signature is (session, course_id, assignment_id, *files, ...) so
        # mixing kw-args with *file_handles raised TypeError.
        with contextlib.ExitStack() as stack:
            file_handles = [
                stack.enter_context(open(path, "rb")) for path in validated_paths
            ]
            result_url = upload_assignment(
                conn.session,
                course_id,
                assignment_id,
                *file_handles,
                leaderboard_name=leaderboard_name,
            )

    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error uploading submission: {e}"

    if result_url:
        filenames = [p.name for p in validated_paths]
        return (
            f"✅ Submission uploaded successfully!\n"
            f"- **Files:** {', '.join(filenames)}\n"
            f"- **Submission URL:** {result_url}"
        )
    else:
        return (
            "❌ Upload failed. Possible reasons:\n"
            "- Assignment is past the due date\n"
            "- You don't have permission to submit\n"
            "- Invalid course or assignment ID"
        )


def inspect_submission_upload_form(
    course_id: str,
    assignment_id: str,
    submission_id: str | None = None,
) -> str:
    """Inspect available Gradescope file-upload forms for staff workflows.

    This read-only helper fetches either Manage Submissions or one existing
    submission page and summarizes file-upload forms, including candidate
    student-owner fields. It is useful because Gradescope's staff upload UI can
    vary by assignment type and frontend rollout.
    """
    if not course_id or not assignment_id:
        return "Error: both course_id and assignment_id are required."

    try:
        conn = get_connection()
        page_url = _submission_page_url(
            conn, course_id, assignment_id, submission_id
        )
        resp = conn.session.get(page_url)
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error fetching upload form: {e}"

    if resp.status_code != 200:
        return (
            f"Error: cannot access upload form page (status {resp.status_code}) "
            f"at {page_url}."
        )

    forms = _file_upload_forms(BeautifulSoup(resp.text, "html.parser"))
    if not forms:
        return (
            f"No file-upload forms found at {page_url}. "
            "This assignment may render upload controls client-side, may not "
            "allow staff uploads, or may have anonymous grading enabled."
        )

    lines = [
        "## Submission Upload Forms",
        f"**URL:** {page_url}",
        f"**File-upload forms found:** {len(forms)}",
    ]
    for index, form in enumerate(forms, 1):
        action = _form_action(page_url, form)
        method = (form.get("method") or "get").upper()
        file_names = _file_input_names(form)
        student_fields = _student_field_candidates(form)
        hidden_count = len(_extract_non_file_fields(form))
        lines.extend(
            [
                "",
                f"### Form {index}",
                f"- method: `{method}`",
                f"- action: `{action}`",
                f"- file inputs: {', '.join(f'`{n}`' for n in file_names) or 'none'}",
                f"- candidate student fields: {', '.join(f'`{n}`' for n in student_fields) or 'none'}",
                f"- hidden/non-file fields: {hidden_count}",
                f"- text: {_clip(_form_text(form), 220) or 'N/A'}",
            ]
        )

    return "\n".join(lines)


def upload_submission_for_student(
    course_id: str,
    assignment_id: str,
    user_id: str,
    file_paths: list[str],
    submission_id: str | None = None,
    student_field_name: str | None = None,
    confirm_write: bool = False,
) -> str:
    """Upload files on behalf of one student through the staff UI.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        user_id: The student's Gradescope user ID from the roster.
        file_paths: List of absolute file paths to upload.
        submission_id: Optional existing assignment-level submission ID. When
            supplied, the tool looks for a replacement/resubmission file form on
            that submission page. When omitted, it looks for a new-upload form
            on Manage Submissions and fills the student owner field.
        student_field_name: Optional exact form field name to use for user_id if
            automatic student-field detection fails.
        confirm_write: Must be True to perform the upload.
    """
    if not user_id:
        return "Error: user_id is required."

    error, validated_paths = _validate_upload_request(
        course_id, assignment_id, file_paths
    )
    if error:
        return error

    if not confirm_write:
        details = [
            f"course_id=`{course_id}`",
            f"assignment_id=`{assignment_id}`",
            f"user_id=`{user_id}`",
            f"files={', '.join(str(path) for path in validated_paths)}",
        ]
        if submission_id:
            details.append(f"submission_id=`{submission_id}`")
        if student_field_name:
            details.append(f"student_field_name=`{student_field_name}`")
        return write_confirmation_required("upload_submission_for_student", details)

    try:
        conn = get_connection()
        page_url = _submission_page_url(
            conn, course_id, assignment_id, submission_id
        )
        page_resp = conn.session.get(page_url)
        if page_resp.status_code != 200:
            return (
                f"Error: cannot access upload form page (status "
                f"{page_resp.status_code}) at {page_url}."
            )

        soup = BeautifulSoup(page_resp.text, "html.parser")
        form = _find_staff_upload_form(
            soup,
            user_id=user_id,
            submission_id=submission_id,
            student_field_name=student_field_name,
        )
        if form is None:
            return (
                "Error: could not find a suitable staff upload form. "
                "Run `tool_inspect_submission_upload_form` for this assignment "
                "to see the available forms and field names."
            )

        post_url = _form_action(page_url, form)
        method = (form.get("method") or "get").lower()
        if method != "post":
            return f"Error: upload form uses unsupported method `{method}`."

        fields = _extract_non_file_fields(form)
        if submission_id is None:
            student_field = student_field_name or _choose_student_field(form, user_id)
            if not student_field:
                return (
                    "Error: could not identify a student field in the upload "
                    "form. Re-run with student_field_name from "
                    "`tool_inspect_submission_upload_form`."
                )
            fields = [(name, value) for name, value in fields if name != student_field]
            fields.append((student_field, user_id))

        file_input_names = _file_input_names(form)
        file_field = file_input_names[0] if file_input_names else "submission[files][]"
        post_resp = _post_file_form(
            conn.session,
            post_url=post_url,
            referer=page_url,
            fields=fields,
            file_field=file_field,
            file_paths=validated_paths,
        )
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error uploading submission for student `{user_id}`: {e}"

    if post_resp.status_code >= 400:
        return (
            f"Error: Gradescope upload request failed with status "
            f"{post_resp.status_code} at {post_url}."
        )

    filenames = [p.name for p in validated_paths]
    return (
        "✅ Staff upload request completed.\n"
        f"- **Student user ID:** {user_id}\n"
        f"- **Files:** {', '.join(filenames)}\n"
        f"- **Request URL:** {post_url}\n"
        f"- **Final URL:** {post_resp.url}"
    )


def _validate_upload_request(
    course_id: str,
    assignment_id: str,
    file_paths: list[str],
) -> tuple[str | None, list[pathlib.Path]]:
    if not course_id or not assignment_id:
        return "Error: both course_id and assignment_id are required.", []

    if not file_paths:
        return "Error: at least one file path is required.", []

    validated_paths = []
    for fp in file_paths:
        original_path = pathlib.Path(fp)
        if not original_path.is_absolute():
            return f"Error: file path must be absolute: {fp}", []

        path = original_path.resolve()
        if not path.exists():
            return f"Error: file not found: {fp}", []
        if not path.is_file():
            return f"Error: not a file: {fp}", []
        validated_paths.append(path)

    return None, validated_paths


def _submission_page_url(
    conn,
    course_id: str,
    assignment_id: str,
    submission_id: str | None = None,
) -> str:
    base = f"{conn.gradescope_base_url}/courses/{course_id}/assignments/{assignment_id}"
    if submission_id:
        return f"{base}/submissions/{submission_id}"
    return f"{base}/submissions"


def _file_upload_forms(soup: BeautifulSoup) -> list:
    return [
        form
        for form in soup.find_all("form")
        if form.find("input", attrs={"type": lambda v: (v or "").lower() == "file"})
    ]


def _file_input_names(form) -> list[str]:
    names = []
    for input_el in form.find_all("input"):
        if (input_el.get("type") or "").lower() == "file":
            names.append(input_el.get("name") or "submission[files][]")
    return names


def _extract_non_file_fields(form) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    for input_el in form.find_all("input"):
        name = input_el.get("name")
        if not name:
            continue
        type_ = (input_el.get("type") or "").lower()
        if type_ in {"file", "submit", "button", "image", "reset"}:
            continue
        fields.append((name, input_el.get("value", "")))

    for textarea in form.find_all("textarea"):
        name = textarea.get("name")
        if name:
            fields.append((name, textarea.get_text()))

    for select in form.find_all("select"):
        name = select.get("name")
        if not name:
            continue
        selected = select.find("option", selected=True) or select.find("option")
        fields.append((name, selected.get("value", "") if selected else ""))

    return fields


def _form_action(page_url: str, form) -> str:
    return urljoin(page_url, form.get("action") or page_url)


def _form_text(form) -> str:
    return " ".join(form.get_text(" ", strip=True).split())


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _student_field_candidates(form) -> list[str]:
    candidates = []
    pattern = re.compile(r"(user|owner|student|member|submitter)", re.I)
    for el in form.find_all(["input", "select"]):
        name = el.get("name")
        if name and pattern.search(name) and name not in candidates:
            candidates.append(name)
    return candidates


def _choose_student_field(form, user_id: str) -> str | None:
    for select in form.find_all("select"):
        name = select.get("name")
        if not name:
            continue
        for option in select.find_all("option"):
            if (option.get("value") or "").strip() == str(user_id):
                return name

    candidates = _student_field_candidates(form)
    return candidates[0] if candidates else None


def _find_staff_upload_form(
    soup: BeautifulSoup,
    user_id: str,
    submission_id: str | None,
    student_field_name: str | None,
):
    forms = _file_upload_forms(soup)
    if not forms:
        return None

    if submission_id:
        terms = ("replace", "resubmit", "re-upload", "upload", "submission")
        ranked = sorted(
            forms,
            key=lambda form: (
                not any(
                    term
                    in (_form_text(form) + " " + (form.get("action") or "")).lower()
                    for term in terms
                ),
                len(_form_text(form)),
            ),
        )
        return ranked[0]

    for form in forms:
        if student_field_name:
            field_names = {name for name, _value in _extract_non_file_fields(form)}
            if student_field_name in field_names:
                return form
        elif _choose_student_field(form, user_id):
            return form

    return None


def _post_file_form(
    session,
    post_url: str,
    referer: str,
    fields: list[tuple[str, str]],
    file_field: str,
    file_paths: list[pathlib.Path],
):
    with contextlib.ExitStack() as stack:
        multipart_fields = list(fields)
        for path in file_paths:
            handle = stack.enter_context(open(path, "rb"))
            multipart_fields.append(
                (
                    file_field,
                    (
                        path.name,
                        handle,
                        (
                            mimetypes.guess_type(path.name)[0]
                            or "application/octet-stream"
                        ),
                    ),
                )
            )

        multipart = MultipartEncoder(fields=multipart_fields)
        return session.post(
            post_url,
            data=multipart,
            headers={
                "Content-Type": multipart.content_type,
                "Referer": referer,
            },
        )


def get_assignment_submissions(course_id: str, assignment_id: str) -> str:
    """Get all submissions for an assignment (instructor/TA only).

    Works for all assignment types: scanned PDF, online, and code assignments.
    Returns submission IDs, graded status, and grading progress.

    Note: The returned IDs are **Global Submission IDs** (the whole assignment
    submission). For grading a specific question, you may need the per-question
    submission ID from `get_submission_grading_context`.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
    """
    if not course_id or not assignment_id:
        return "Error: both course_id and assignment_id are required."

    try:
        conn = get_connection()
        # Primary: submissions.json (works for scanned PDF/image assignments)
        resp = conn.session.get(
            f"{conn.gradescope_base_url}/courses/{course_id}"
            f"/assignments/{assignment_id}/submissions.json",
            headers={
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
        )

        if resp.status_code == 200:
            return _format_submissions_json(resp.json(), assignment_id, course_id)

        # Fallback: scrape review_grades HTML table (works for online assignments)
        return _get_submissions_from_review_grades(conn, course_id, assignment_id)

    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error fetching submissions: {e}"


def _format_submissions_json(data: dict, assignment_id: str, course_id: str) -> str:
    """Format submission data from the submissions.json endpoint."""
    detailed = data.get("detailed_submissions", {})
    basic = data.get("submissions", {})

    if not detailed and not basic:
        return f"No submissions found for assignment `{assignment_id}` in course `{course_id}`."

    subs = detailed or basic
    total = len(subs)
    graded = sum(1 for s in subs.values() if s.get("graded"))

    lines = [f"## Submissions for Assignment {assignment_id}\n"]
    lines.append(f"**Total submissions:** {total}")
    lines.append(f"**Graded:** {graded}/{total}\n")
    lines.append("| # | Global Submission ID | Graded | Progress | Late |")
    lines.append("|---|---------------|--------|----------|------|")

    for i, (sub_id, sub) in enumerate(sorted(subs.items(), key=lambda x: x[0]), 1):
        is_graded = "✅" if sub.get("graded") else "—"
        progress = sub.get("grading_progress")
        progress_str = f"{progress:.0f}%" if progress is not None else "—"
        late = "⚠️" if sub.get("late") else ""
        lines.append(f"| {i} | `{sub_id}` | {is_graded} | {progress_str} | {late} |")

    return "\n".join(lines)


_REVIEW_GRADES_PLACEHOLDERS = frozenset({"", "-", "--", "—", "–", "n/a"})
_REVIEW_GRADES_AFFIRMATIVE = frozenset({"yes", "y", "true", "graded", "done", "✓", "✅"})
_REVIEW_GRADES_SCORE_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?|Graded|✓|✅)\s*$"
)


def _get_submissions_from_review_grades(
    conn, course_id: str, assignment_id: str
) -> str:
    """Fallback: scrape submission list from the review_grades HTML table.

    Used for online assignments where submissions.json returns 404. Uses
    header-aware column resolution so that adding/removing the Sections column
    (or any other layout shift) doesn't pull score/graded data from the wrong
    cells, which used to silently corrupt the output.
    """
    from bs4 import BeautifulSoup

    url = (
        f"{conn.gradescope_base_url}/courses/{course_id}"
        f"/assignments/{assignment_id}/review_grades"
    )
    resp = conn.session.get(url)
    if resp.status_code != 200:
        return f"Error: Cannot access submissions or review_grades (status {resp.status_code})."

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    if not table:
        return (
            f"Error: No submission data found for assignment `{assignment_id}`. "
            "The submissions.json endpoint returned 404 and the review_grades page "
            "has no table. This assignment type may not be supported yet."
        )

    headers = []
    thead = table.find("thead")
    if thead is not None:
        header_row = thead.find("tr")
        if header_row is not None:
            headers = [th.get_text(strip=True) for th in header_row.find_all(["th", "td"])]
    if not headers:
        first_row = table.find("tr")
        if first_row is not None:
            ths = first_row.find_all("th")
            if ths:
                headers = [th.get_text(strip=True) for th in ths]

    def _col(*names: str) -> int | None:
        wanted = {n.lower() for n in names}
        for idx, h in enumerate(headers):
            if (h or "").strip().lower() in wanted:
                return idx
        return None

    score_idx = _col("score", "total score", "points")
    graded_idx = _col("graded?", "graded", "status")

    body = table.find("tbody") or table
    data_rows = [tr for tr in body.find_all("tr") if tr.find("td")]
    if not data_rows:
        return f"No submissions found for assignment `{assignment_id}` in course `{course_id}`."

    sub_id_pattern = re.compile(r"/submissions/(\d+)")
    submissions = []
    for row in data_rows:
        cells = row.find_all("td")
        cell_text = [c.get_text(strip=True) for c in cells]
        if not cell_text:
            continue

        sub_id = None
        for link in row.find_all("a", href=True):
            match = sub_id_pattern.search(link["href"])
            if match:
                sub_id = match.group(1)
                break
        if not sub_id:
            continue

        # Score: prefer the resolved column; fall back to the historical
        # cells[4] only when the header lookup fails.
        score_text = ""
        if score_idx is not None and score_idx < len(cell_text):
            score_text = cell_text[score_idx]
        elif len(cell_text) > 4:
            score_text = cell_text[4]

        # Graded: derived from the explicit column if present, otherwise from
        # the score column's content. ``--`` and other placeholders never
        # count, no matter which column they appear in.
        graded = False
        if graded_idx is not None and graded_idx < len(cell_text):
            flag = cell_text[graded_idx].strip().lower()
            if flag in _REVIEW_GRADES_AFFIRMATIVE:
                graded = True
        if not graded and score_text:
            cleaned = score_text.strip()
            if cleaned.lower() not in _REVIEW_GRADES_PLACEHOLDERS and _REVIEW_GRADES_SCORE_RE.match(cleaned):
                graded = True

        submissions.append({
            "id": sub_id,
            "score": score_text,
            "graded": graded,
        })

    total = len(submissions)
    graded = sum(1 for s in submissions if s["graded"])

    lines = [f"## Submissions for Assignment {assignment_id}\n"]
    lines.append(f"**Total submissions:** {total}")
    lines.append(f"**Graded:** {graded}/{total}")
    lines.append("_(Note: retrieved from review_grades fallback)_\n")
    lines.append("| # | Global Submission ID | Score | Graded |")
    lines.append("|---|---------------|-------|--------|")

    for i, sub in enumerate(submissions, 1):
        is_graded = "✅" if sub["graded"] else "—"
        lines.append(f"| {i} | `{sub['id']}` | {sub['score']} | {is_graded} |")

    return "\n".join(lines)


def get_student_submission(
    course_id: str, assignment_id: str, student_email: str
) -> str:
    """Get the full content of a specific student's submission.

    Requires instructor/TA access. Returns the student's text answers for each
    question, as well as direct URLs to any uploaded files or images.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        student_email: The student's email address.
    """
    if not course_id or not assignment_id or not student_email:
        return "Error: course_id, assignment_id, and student_email are required."

    return get_student_submission_content(course_id, assignment_id, student_email)


def get_assignment_graders(course_id: str, question_id: str) -> str:
    """Get the list of graders for a specific question (instructor/TA only).

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID within the assignment.
    """
    if not course_id or not question_id:
        return "Error: both course_id and question_id are required."

    try:
        conn = get_connection()
        graders = conn.account.get_assignment_graders(course_id, question_id)
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error fetching graders: {e}"

    if not graders:
        return f"No graders found for question `{question_id}` in course `{course_id}`."

    # Filter: some question types return raw user IDs or internal labels
    _DIRTY_GRADER_PATTERNS = {"(needs labeling)", "(none)", "(unassigned)"}
    named_graders = [
        g for g in graders
        if not str(g).isdigit()
        and str(g).lower().strip() not in _DIRTY_GRADER_PATTERNS
    ]
    id_only = [g for g in graders if str(g).isdigit()]
    dirty_labels = [
        g for g in graders
        if str(g).lower().strip() in _DIRTY_GRADER_PATTERNS
    ]

    lines = [
        f"## Graders for Question {question_id}\n",
        f"**Total graders:** {len(named_graders)}\n",
    ]
    for grader in sorted(named_graders):
        lines.append(f"- {grader}")

    if id_only or dirty_labels:
        extras_count = len(id_only) + len(dirty_labels)
        lines.append(
            f"\n⚠️ **Note:** {extras_count} system/auto-grader "
            f"{'entry was' if extras_count == 1 else 'entries were'} "
            "filtered from the list."
        )

    return "\n".join(lines)

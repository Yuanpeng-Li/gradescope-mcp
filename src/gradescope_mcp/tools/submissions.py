"""Submission-related MCP tools."""

import contextlib
import hashlib
import os
import pathlib
import re

from bs4 import BeautifulSoup
from gradescopeapi.classes.upload import upload_assignment

from gradescope_mcp.auth import get_connection, AuthError
from gradescope_mcp.tools.grading import get_student_submission_content
from gradescope_mcp.tools.safety import write_confirmation_required


# An upload posts local file bytes to Gradescope as the logged-in account,
# where course staff can read them, so the files an agent may name are
# limited: an optional allowlisted root, no hidden files or directories, no
# credential-looking names, no system directories, and a size cap.
UPLOAD_ROOT_ENV = "GRADESCOPE_MCP_UPLOAD_ROOT"
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024
_SECRET_FILE_NAMES = frozenset({
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "authorized_keys",
    "known_hosts", "credentials", "credentials.json", "client_secret.json",
    "service_account.json", "token.json", "secrets.json", "secrets.yaml",
    "secrets.yml", "secrets.toml", "passwd", "shadow",
})
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx", ".env")
_SYSTEM_DIRS = tuple(
    pathlib.Path(p)
    for p in (
        "/proc", "/sys", "/dev", "/etc", "/boot", "/root", "/run",
        "/var/run", "/var/lib", "/var/log", "/private/etc",
    )
)


def _upload_roots() -> list[pathlib.Path] | None:
    """Directories uploads are restricted to, or None when unrestricted."""
    raw = os.environ.get(UPLOAD_ROOT_ENV, "")
    if not raw.strip():
        return None
    roots = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        root = pathlib.Path(part).expanduser()
        if not root.is_absolute() or not root.is_dir():
            raise ValueError(
                f"{UPLOAD_ROOT_ENV} entry '{part}' is not an absolute path to "
                "an existing directory"
            )
        roots.append(root.resolve())
    if not roots:
        raise ValueError(f"{UPLOAD_ROOT_ENV} is set but names no directory")
    return roots


def _validate_upload_path(fp: str, roots: list[pathlib.Path] | None) -> pathlib.Path:
    """Resolve and vet one upload path; raise ValueError with the reason."""
    original = pathlib.Path(fp)
    if not original.is_absolute():
        raise ValueError(f"file path must be absolute: {fp}")

    path = original.resolve()
    if not path.exists():
        raise ValueError(f"file not found: {fp}")
    if not path.is_file():
        raise ValueError(f"not a file: {fp}")

    if roots is not None:
        root = next((r for r in roots if path.is_relative_to(r)), None)
        if root is None:
            raise ValueError(
                f"refusing to upload {fp}: it resolves to {path}, outside the "
                f"allowed upload directory ({UPLOAD_ROOT_ENV})"
            )
        checked_parts = path.relative_to(root).parts
    else:
        if original.is_symlink():
            raise ValueError(
                f"refusing to upload {fp}: it is a symbolic link (to {path}). "
                f"Pass the real file path, or set {UPLOAD_ROOT_ENV} to allow "
                "links that stay inside that directory"
            )
        for system_dir in _SYSTEM_DIRS:
            if path.is_relative_to(system_dir):
                raise ValueError(
                    f"refusing to upload {fp}: files under {system_dir} are not "
                    f"uploaded (set {UPLOAD_ROOT_ENV} to choose an upload "
                    "directory explicitly)"
                )
        checked_parts = path.parts[1:]

    hidden = next((part for part in checked_parts if part.startswith(".")), None)
    if hidden is not None:
        raise ValueError(
            f"refusing to upload {fp}: hidden files and directories ({hidden}) "
            "are not uploaded because they commonly hold credentials"
        )
    names = {path.name.lower(), original.name.lower()}
    if names & _SECRET_FILE_NAMES or any(n.endswith(_SECRET_SUFFIXES) for n in names):
        raise ValueError(
            f"refusing to upload {fp}: the file name looks like a credential or key file"
        )
    size = path.stat().st_size
    if size > _MAX_UPLOAD_BYTES:
        raise ValueError(
            f"{fp} is {size:,} bytes; the upload limit is {_MAX_UPLOAD_BYTES:,} bytes"
        )
    return path


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _describe_upload(path: pathlib.Path, size: int, digest: str) -> str:
    return f"`{path.name}` ({size:,} bytes, sha256 {digest})"


def upload_submission(
    course_id: str,
    assignment_id: str,
    file_paths: list[str],
    leaderboard_name: str | None = None,
    confirm_write: bool = False,
) -> str:
    """Upload files as a submission to a Gradescope assignment.

    The submission is made as the logged-in account. Each path must be an
    absolute path to a regular file of at most 100 MB. Hidden files or
    directories, credential-looking names (keys, ``.env``, ...) and system
    directories are refused. When ``GRADESCOPE_MCP_UPLOAD_ROOT`` is set
    (``os.pathsep``-separated directories), files must resolve inside it;
    otherwise symbolic links are refused. The preview lists each file's size
    and SHA-256.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        file_paths: List of absolute file paths to upload.
        leaderboard_name: Optional leaderboard display name.
        confirm_write: Must be True to perform the upload.
    """
    if not course_id or not assignment_id:
        return "Error: both course_id and assignment_id are required."

    if not file_paths:
        return "Error: at least one file path is required."

    try:
        roots = _upload_roots()
    except ValueError as e:
        return f"Error: {e}"

    validated = []
    for fp in file_paths:
        try:
            path = _validate_upload_path(fp, roots)
            validated.append((path, path.stat().st_size, _sha256(path)))
        except ValueError as e:
            return f"Error: {e}"
        except OSError as e:
            return f"Error: cannot read {fp}: {e}"
    validated_paths = [path for path, _size, _digest in validated]

    if not confirm_write:
        details = [
            f"course_id=`{course_id}`",
            f"assignment_id=`{assignment_id}`",
            f"files={', '.join(str(path) for path in validated_paths)}",
        ]
        details += [f"file {_describe_upload(*info)}" for info in validated]
        if leaderboard_name:
            details.append(f"leaderboard_name={leaderboard_name}")
        if roots is None:
            details.append(
                f"{UPLOAD_ROOT_ENV} is not set, so any non-hidden regular file "
                "outside system directories may be uploaded; set it to "
                "restrict uploads to one directory."
            )
        else:
            details.append(
                f"Uploads are restricted to {UPLOAD_ROOT_ENV}: "
                f"{', '.join(str(r) for r in roots)}"
            )
        details.append(
            "The submission is made as the logged-in Gradescope account and "
            "the files become readable by the course staff."
        )
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
        return (
            f"✅ Submission uploaded successfully!\n"
            f"- **Files:** {', '.join(_describe_upload(*info) for info in validated)}\n"
            f"- **Submission URL:** {result_url}"
        )
    else:
        return (
            "❌ Upload failed. Possible reasons:\n"
            "- Assignment is past the due date\n"
            "- You don't have permission to submit\n"
            "- Invalid course or assignment ID"
        )


def _json_body(resp):
    """Return the parsed JSON body, or None when the response isn't JSON.

    Gradescope can answer a JSON URL with an HTML page and status 200 (for
    example a login or error page); that must not be parsed as data.
    """
    headers = getattr(resp, "headers", None) or {}
    content_type = next(
        (str(v) for k, v in headers.items() if str(k).lower() == "content-type"), ""
    )
    if content_type and "json" not in content_type.lower():
        return None
    try:
        return resp.json()
    except ValueError:
        return None


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
            data = _json_body(resp)
            if isinstance(data, dict):
                return _format_submissions_json(data, assignment_id, course_id)

        # Fallback: scrape review_grades HTML table (works for online
        # assignments, and whenever submissions.json returned no JSON)
        return _get_submissions_from_review_grades(conn, course_id, assignment_id)

    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error fetching submissions: {e}"


def _submission_id_sort_key(item) -> tuple:
    """Sort numeric submission IDs numerically, anything else after them."""
    sub_id = str(item[0])
    return (0, int(sub_id), "") if sub_id.isdigit() else (1, 0, sub_id)


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

    for i, (sub_id, sub) in enumerate(sorted(subs.items(), key=_submission_id_sort_key), 1):
        is_graded = "✅" if sub.get("graded") else "—"
        progress = sub.get("grading_progress")
        progress_str = f"{progress:.0f}%" if progress is not None else "—"
        late = "⚠️" if sub.get("late") else ""
        lines.append(f"| {i} | `{sub_id}` | {is_graded} | {progress_str} | {late} |")

    return "\n".join(lines)


_REVIEW_GRADES_PLACEHOLDERS = frozenset({"", "-", "--", "—", "–", "n/a"})
_REVIEW_GRADES_AFFIRMATIVE = frozenset({"yes", "y", "true", "graded", "done", "✓", "✅"})
# An explicit "not graded" flag wins over whatever the Score cell holds.
_REVIEW_GRADES_NEGATIVE = frozenset(
    {"no", "n", "false", "ungraded", "not graded", "pending", "✗", "✘", "❌"}
)
# Signed and leading-dot decimals, "N / M" and a trailing "pt"/"pts" all count.
_REVIEW_GRADES_SCORE_RE = re.compile(
    r"^\s*(?:[-+−]?\d*\.?\d+(?:\s*/\s*\d*\.?\d+)?(?:\s*pts?)?|Graded|✓|✅)\s*$",
    re.IGNORECASE,
)


def _table_headers(table) -> list[str]:
    """Header texts of a table: its thead's first row, else a th-only first row."""
    thead = table.find("thead")
    header_row = thead.find("tr") if thead is not None else None
    if header_row is not None:
        return [c.get_text(strip=True) for c in header_row.find_all(["th", "td"], recursive=False)]
    first_row = table.find("tr")
    if first_row is not None and first_row.find("td", recursive=False) is None:
        return [c.get_text(strip=True) for c in first_row.find_all("th", recursive=False)]
    return []


def _row_cells(row) -> list:
    """A row's cells in column order. Row headers may be <th>, so both count,
    matching how header indices are computed."""
    return row.find_all(["td", "th"], recursive=False)


def _get_submissions_from_review_grades(
    conn, course_id: str, assignment_id: str
) -> str:
    """Fallback: scrape submission list from the review_grades HTML table.

    Used for online assignments where submissions.json returns 404. Uses
    header-aware column resolution so that adding/removing the Sections column
    (or any other layout shift) doesn't pull score/graded data from the wrong
    cells, which used to silently corrupt the output.
    """
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
            "The submissions.json endpoint returned no submission data and the "
            "review_grades page has no table. This assignment type may not be "
            "supported yet."
        )

    headers = _table_headers(table)

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
        cell_text = [c.get_text(strip=True) for c in _row_cells(row)]
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

        # Graded: an affirmative or negative Graded? flag decides; otherwise
        # (empty, placeholder or unrecognized flag) the Score cell decides.
        # ``--`` and other placeholders never count as a score.
        flag = ""
        if graded_idx is not None and graded_idx < len(cell_text):
            flag = cell_text[graded_idx].strip().lower()
        if flag in _REVIEW_GRADES_AFFIRMATIVE:
            graded = True
        elif flag in _REVIEW_GRADES_NEGATIVE:
            graded = False
        else:
            cleaned = score_text.strip()
            graded = (
                cleaned.lower() not in _REVIEW_GRADES_PLACEHOLDERS
                and bool(_REVIEW_GRADES_SCORE_RE.match(cleaned))
            )

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


_GRADER_HEADER_NAMES = frozenset(
    {"last graded by", "graded by", "grader", "graders", "last grader"}
)
_NO_GRADER_VALUES = frozenset(
    {"", "-", "--", "—", "–", "n/a", "(none)", "(unassigned)", "(needs labeling)"}
)


def get_assignment_graders(course_id: str, question_id: str) -> str:
    """List the staff who have graded submissions of a question (instructor/TA only).

    Reads the grader column ("Last Graded By" / "Grader") of the question's
    submissions table by header, row by row, and counts submissions per
    grader. This is who last graded each submission, not who is assigned to
    grade the question. If no grader column is found, nothing is guessed.

    Args:
        course_id: The Gradescope course ID.
        question_id: The question ID within the assignment.
    """
    if not course_id or not question_id:
        return "Error: both course_id and question_id are required."

    try:
        conn = get_connection()
        resp = conn.session.get(
            f"{conn.gradescope_base_url}/courses/{course_id}"
            f"/questions/{question_id}/submissions"
        )
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error fetching graders: {e}"

    if resp.status_code != 200:
        return (
            f"Error: cannot access the submissions page for question "
            f"`{question_id}` (status {resp.status_code})."
        )

    soup = BeautifulSoup(resp.text, "html.parser")
    counts: dict[str, int] = {}
    column_label = None
    rows_seen = 0
    without_grader = 0
    for table in soup.find_all("table"):
        headers = _table_headers(table)
        idx = next(
            (i for i, h in enumerate(headers) if h.strip().lower() in _GRADER_HEADER_NAMES),
            None,
        )
        if idx is None:
            continue
        column_label = column_label or headers[idx]
        for row in table.find_all("tr"):
            if row.find_parent("table") is not table or row.find("td", recursive=False) is None:
                continue
            rows_seen += 1
            cells = _row_cells(row)
            name = " ".join(cells[idx].get_text(" ", strip=True).split()) if idx < len(cells) else ""
            if name.lower() in _NO_GRADER_VALUES:
                without_grader += 1
                continue
            counts[name] = counts.get(name, 0) + 1

    if column_label is None:
        return (
            f"Error: the submissions page for question `{question_id}` has no "
            "'Last Graded By' / 'Grader' column, so graders can't be read "
            "reliably (unrecognized page layout)."
        )
    if not counts:
        return (
            f"No graders found for question `{question_id}` in course "
            f"`{course_id}`: none of its {rows_seen} listed submission(s) has a grader yet."
        )

    lines = [
        f"## Graders for Question {question_id}\n",
        f"Staff who last graded at least one submission (from the '{column_label}' "
        "column); this is not the list of graders assigned to the question.\n",
        f"**Total graders:** {len(counts)}\n",
    ]
    for name in sorted(counts, key=str.lower):
        count = counts[name]
        lines.append(f"- {name} ({count} submission{'' if count == 1 else 's'})")
    if without_grader:
        lines.append(
            f"\n**Submissions without a grader:** {without_grader} of {rows_seen}"
        )
    return "\n".join(lines)

"""Submission-related MCP tools."""

import hashlib
import io
import json
import mimetypes
import os
import pathlib
import re
from typing import NamedTuple
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from gradescopeapi.classes.upload import upload_assignment

from gradescope_mcp.auth import get_connection, AuthError
from gradescope_mcp.tools.common import element_classes, is_hidden_element, sanitize_inline
from gradescope_mcp.tools.courses import _parse_roster
from gradescope_mcp.tools.grading import _fetch_assignment_scores_csv, get_student_submission_content
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


_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _expected_hashes(expected_sha256, count: int) -> list[str] | None:
    """Normalize ``expected_sha256``: one hex digest per file, in order.

    Backticks, surrounding whitespace, letter case and a ``sha256:`` prefix
    are tolerated (agents copy the digests from the markdown preview).

    Raises:
        ValueError: the list is malformed or has the wrong length.
    """
    if expected_sha256 is None:
        return None
    if isinstance(expected_sha256, str) or not isinstance(expected_sha256, (list, tuple)):
        raise ValueError(
            "expected_sha256 must be a list of SHA-256 hex digests, one per file "
            "in file_paths and in the same order (the preview lists them)"
        )
    if len(expected_sha256) != count:
        raise ValueError(
            f"expected_sha256 has {len(expected_sha256)} digest(s) for {count} "
            "file(s); pass one SHA-256 per file in file_paths, in the same order "
            "(the preview lists them)"
        )
    digests = []
    for index, value in enumerate(expected_sha256, 1):
        digest = value.strip().strip("`").strip().lower() if isinstance(value, str) else ""
        digest = re.sub(r"^sha-?256[:\s]\s*", "", digest)
        if not _SHA256_RE.fullmatch(digest):
            raise ValueError(
                f"expected_sha256 entry {index} ({value!r}) is not a SHA-256 hex "
                "digest (64 hexadecimal characters)"
            )
        digests.append(digest)
    return digests


def _hash_mismatches(
    infos: list[tuple[pathlib.Path, int, str]], expected: list[str]
) -> list[str]:
    """One line per file whose SHA-256 is not the approved one."""
    return [
        f"- `{path.name}`: approved sha256 {want}, now sha256 {digest} ({size:,} bytes)"
        for (path, size, digest), want in zip(infos, expected)
        if digest != want
    ]


def _changed_since_preview(mismatches: list[str]) -> str:
    return (
        "Error: the file content differs from the approved preview "
        "(expected_sha256), so nothing was uploaded:\n"
        + "\n".join(mismatches)
        + "\nPreview the upload again and have the user approve the current content."
    )


class _UploadRecorder:
    """Session stand-in that keeps the final response of the upload POST.

    gradescopeapi's ``upload_assignment`` returns only a URL; the response is
    kept to see where Gradescope sent the browser in answer to the POST
    itself (its redirect history) and what the final page said.
    """

    def __init__(self, session):
        self._session = session
        self.post_response = None

    def get(self, *args, **kwargs):
        return self._session.get(*args, **kwargs)

    def post(self, *args, **kwargs):
        self.post_response = self._session.post(*args, **kwargs)
        return self.post_response


# Gradescope's flash messages and other alerts on the page after an upload.
_FLASH_SELECTORS = (
    ".alert, .flash, #flash, [role=alert], .alert-error, .alert-danger, "
    ".alert-alert, .flash-error, .flash-alert, .flash-danger"
)
# Classes that style a flash message as a failure, and as a success or notice.
# A message with neither (a warning, a bare [role=alert]) is not read as a
# failure: it is reported next to the result instead.
_ERROR_FLASH_CLASSES = frozenset({
    "alert-error", "alert-danger", "alert-alert", "flash-error", "flash-alert",
    "flash-danger",
})
_NOTICE_FLASH_CLASSES = frozenset({
    "alert-success", "alert-notice", "alert-info", "flash-success", "flash-notice",
})
# Elements whose content the browser does not show as part of the page.
_NOT_RENDERED_TAGS = frozenset({"noscript", "template", "script", "style"})


def _submission_id(url: str | None, course_id: str, assignment_id: str) -> str | None:
    """The submission ID if ``url`` is the page of one submission of this assignment.

    That is /courses/<cid>/assignments/<aid>/submissions/<id>, or a page
    below it such as the PDF page-selection step. Any other page (the
    assignment, the course, the home page after a lost session) gives None.
    """
    if not url:
        return None
    pattern = (
        rf"/courses/{re.escape(str(course_id))}/assignments/"
        rf"{re.escape(str(assignment_id))}/submissions/(\d+)(?:/[A-Za-z0-9_-]+)*/?"
    )
    match = re.fullmatch(pattern, urlsplit(str(url)).path)
    return match.group(1) if match else None


def _existing_submission_ids(conn, course_id: str, assignment_id: str) -> set[str] | None:
    """IDs of this assignment's submissions the account can see before an upload.

    Gradescope sends an account that has already submitted from the
    assignment page on to its latest submission, whose page links to the
    earlier ones; every submission of this assignment that the page or its
    redirects mention is collected. Returns None when the page can't be
    read. ``AuthError`` (an expired session) propagates, so nothing is
    uploaded.
    """
    url = f"{conn.gradescope_base_url}/courses/{course_id}/assignments/{assignment_id}"
    try:
        resp = conn.session.get(url)
    except AuthError:
        raise
    except Exception:
        return None
    status = getattr(resp, "status_code", None)
    if not isinstance(status, int) or not 200 <= status < 300:
        return None
    ids: set[str] = set()
    for hop in [*(getattr(resp, "history", None) or []), resp]:
        sub_id = _submission_id(getattr(hop, "url", None), course_id, assignment_id)
        if sub_id is not None:
            ids.add(sub_id)
    text = getattr(resp, "text", "")
    if isinstance(text, str):
        mention = re.compile(
            rf"/courses/{re.escape(str(course_id))}/assignments/"
            rf"{re.escape(str(assignment_id))}/submissions/(\d+)"
        )
        ids.update(mention.findall(text))
    return ids


def _redirect_target(response, fallback: str | None) -> str | None:
    """The page Gradescope sent the browser to in answer to the upload POST itself.

    requests follows every redirect, so the final URL can be a page reached
    later (e.g. the assignment page forwarding to an older submission).
    ``history[0]`` is the POST's own answer and its Location the target;
    without a redirect it is the response's own URL.
    """
    if response is None:
        return fallback
    history = list(getattr(response, "history", None) or [])
    if not history:
        return getattr(response, "url", None) or fallback
    first = history[0]
    location = (getattr(first, "headers", None) or {}).get("Location")
    if not location:
        return None
    return urljoin(str(getattr(first, "url", None) or ""), str(location))


def _is_rendered(element) -> bool:
    """Whether ``element`` is shown on the page: neither it nor an enclosing
    element is hidden (attribute, class or style) or a noscript/template."""
    for node in [element, *element.parents]:
        if node.name in _NOT_RENDERED_TAGS:
            return False
        if node.name != "[document]" and is_hidden_element(node):
            return False
    return True


def _flash_messages(response) -> dict[str, str | None]:
    """The visible flash messages on a response page, by kind.

    Returns ``{"error": ..., "notice": ..., "other": ...}``, each the joined
    text (at most 300 characters) or None. A message is an error when it or
    an enclosing alert is error-styled (``alert-danger``, ``flash-error``,
    ...), a notice when success/notice/info-styled, and "other" otherwise
    (e.g. ``alert-warning`` or a bare ``role=alert``). Hidden elements, JS
    templates and ``<noscript>`` content are ignored. A container holding
    other alerts (e.g. ``#flash``) is read through those alerts.
    """
    found: dict[str, list[str]] = {"error": [], "notice": [], "other": []}
    try:
        text = response.text
    except Exception:
        text = None
    if isinstance(text, str) and text:
        soup = BeautifulSoup(text, "html.parser")
        matched = [el for el in soup.select(_FLASH_SELECTORS) if _is_rendered(el)]
        ids = {id(el) for el in matched}
        for element in matched:
            if any(id(inner) in ids for inner in element.find_all(True)):
                continue
            message = " ".join(element.get_text(" ", strip=True).split())
            if not message:
                continue
            classes: set[str] = set()
            for node in [element, *element.parents]:
                if id(node) in ids:
                    classes.update(element_classes(node))
            if classes & _ERROR_FLASH_CLASSES:
                kind = "error"
            elif classes & _NOTICE_FLASH_CLASSES:
                kind = "notice"
            else:
                kind = "other"
            if message not in found[kind]:
                found[kind].append(message)
    return {kind: "; ".join(msgs)[:300] or None for kind, msgs in found.items()}


def upload_submission(
    course_id: str,
    assignment_id: str,
    file_paths: list[str],
    leaderboard_name: str | None = None,
    confirm_write: bool = False,
    expected_sha256: list[str] | None = None,
) -> str:
    """Upload files as a submission to a Gradescope assignment.

    The submission is made as the logged-in account. Each path must be an
    absolute path to a regular file of at most 100 MB. Hidden files or
    directories, credential-looking names (keys, ``.env``, ...) and system
    directories are refused. When ``GRADESCOPE_MCP_UPLOAD_ROOT`` is set
    (``os.pathsep``-separated directories), files must resolve inside it;
    otherwise symbolic links are refused.

    The preview lists each file's size and SHA-256. Passing those digests
    back as ``expected_sha256`` binds the upload to the approved content:
    if any file differs, nothing is uploaded. Each file is read once, and
    the bytes read are the bytes hashed and sent.

    Success is reported only when Gradescope answers the upload POST itself
    with a redirect to a submission of this assignment that was not among
    the account's submissions seen on the assignment page just before the
    upload, the final page is that submission's page (or one below it), and
    it shows no visible error-styled message (see ``_flash_messages``; a
    warning or unstyled message is quoted under a ⚠️ line of the success
    result). Anything else is reported as not confirmed, with the page
    Gradescope showed.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        file_paths: List of absolute file paths to upload.
        leaderboard_name: Optional leaderboard display name.
        confirm_write: Must be True to perform the upload.
        expected_sha256: Optional SHA-256 hex digests from the preview, one
            per file in ``file_paths`` and in the same order.
    """
    if not course_id or not assignment_id:
        return "Error: both course_id and assignment_id are required."

    if not file_paths:
        return "Error: at least one file path is required."

    try:
        expected = _expected_hashes(expected_sha256, len(file_paths))
    except ValueError as e:
        return f"Error: {e}"

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
        if expected is not None:
            mismatches = _hash_mismatches(validated, expected)
            if mismatches:
                return _changed_since_preview(mismatches)
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
        details.append(
            "To upload exactly the content shown here, pass "
            f"expected_sha256={json.dumps([digest for _p, _s, digest in validated])} "
            "together with confirm_write=True: the upload is then refused if "
            "any file has changed since this preview."
        )
        return write_confirmation_required("upload_submission", details)

    # Each file is read once; those bytes are hashed, compared with the
    # approved digests and sent, so a file rewritten in the meantime can't
    # put different content into the upload than the result reports.
    contents = []
    for path in validated_paths:
        try:
            with open(path, "rb") as fh:
                data = fh.read(_MAX_UPLOAD_BYTES + 1)
        except OSError as e:
            return (
                f"Error uploading submission: cannot read {path}: {e}. "
                "Nothing was uploaded."
            )
        if len(data) > _MAX_UPLOAD_BYTES:
            return (
                f"Error: {path} grew past the upload limit of "
                f"{_MAX_UPLOAD_BYTES:,} bytes. Nothing was uploaded."
            )
        contents.append(data)
    sent = [
        (path, len(data), hashlib.sha256(data).hexdigest())
        for path, data in zip(validated_paths, contents)
    ]
    if expected is not None:
        mismatches = _hash_mismatches(sent, expected)
        if mismatches:
            return _changed_since_preview(mismatches)

    files = []
    for path, data in zip(validated_paths, contents):
        buffer = io.BytesIO(data)
        # gradescopeapi takes the uploaded file's name and MIME type from .name.
        buffer.name = str(path)
        files.append(buffer)

    try:
        conn = get_connection()
        existing = _existing_submission_ids(conn, course_id, assignment_id)
        recorder = _UploadRecorder(conn.session)
        # Files are passed positionally: upload_assignment's signature is
        # (session, course_id, assignment_id, *files, leaderboard_name=...).
        result_url = upload_assignment(
            recorder,
            course_id,
            assignment_id,
            *files,
            leaderboard_name=leaderboard_name,
        )
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error uploading submission: {e}"

    return _upload_outcome(
        course_id, assignment_id, sent, expected is not None, existing,
        recorder.post_response, result_url,
    )


def _upload_outcome(
    course_id: str,
    assignment_id: str,
    sent: list[tuple[pathlib.Path, int, str]],
    hashes_checked: bool,
    existing: set[str] | None,
    response,
    result_url: str | None,
) -> str:
    """Report an upload: ✅ only with evidence that a new submission exists.

    ``response`` is the upload POST's final response (None if the upload
    function did not go through the recorder) and ``result_url`` what
    gradescopeapi returned (None for the course page and ".../submissions").
    """
    final_url = getattr(response, "url", None) or result_url
    target = _redirect_target(response, result_url)
    new_id = _submission_id(target, course_id, assignment_id)
    final_id = _submission_id(final_url, course_id, assignment_id)
    messages = (
        _flash_messages(response) if response is not None
        else {"error": None, "notice": None, "other": None}
    )
    error_flash = messages["error"]

    if new_id is None:
        problem = (
            f"Gradescope did not open a new submission of assignment "
            f"`{assignment_id}`, so the submission was most likely not created."
        )
    elif existing is not None and new_id in existing:
        problem = (
            f"Gradescope opened submission `{new_id}`, which already existed "
            "before this upload, so the upload most likely did not create a "
            "new submission."
        )
    elif final_id != new_id:
        problem = (
            f"Gradescope opened submission `{new_id}` but then went on to "
            "another page, so the upload may not have been accepted."
        )
    elif error_flash:
        problem = (
            f"Gradescope opened submission `{new_id}` but its page shows an "
            "error message, so the upload may not have been accepted."
        )
    else:
        lines = [
            "✅ Submission uploaded successfully!",
            f"- **Files:** {', '.join(_describe_upload(*info) for info in sent)}",
            f"- **Submission ID:** `{new_id}`",
            f"- **Submission URL:** {result_url or final_url}",
        ]
        if messages["notice"]:
            lines.append(f"- Gradescope said: {messages['notice']}")
        if messages["other"]:
            lines.append(
                "- ⚠️ The submission page also shows a message that is not "
                f"styled as an error: \"{messages['other']}\" The submission "
                "was created; check it on Gradescope if this reads like a problem."
            )
        if hashes_checked:
            lines.append("- Every file matched the approved expected_sha256.")
        else:
            lines.append(
                "- The content was not checked against an approved preview "
                "(expected_sha256 was not passed)."
            )
        if existing is None:
            lines.append(
                "- The account's existing submissions could not be read before "
                "the upload, so this ID was not compared with them."
            )
        return "\n".join(lines)

    lines = [f"❌ Upload not confirmed: {problem}"]
    if target and target != final_url:
        lines.append(f"- Gradescope answered the upload with a redirect to: {target}")
    if final_url:
        lines.append(f"- Final page: {final_url}")
    others = "; ".join(m for m in (messages["notice"], messages["other"]) if m)[:300]
    if error_flash:
        lines.append(f"- Gradescope's error message: {error_flash}")
        if others:
            lines.append(f"- Other messages on the page: {others}")
    elif others:
        lines.append(f"- Gradescope said: {others}")
    if new_id is None:
        lines.append(
            "- Possible reasons: the assignment is closed or past its due date, "
            "you don't have permission to submit, the files were rejected, the "
            "session was lost, or the course or assignment ID is wrong."
        )
    lines.append(
        "- Each upload creates a new submission: check the assignment on "
        "Gradescope before uploading again."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Staff uploads on behalf of a student (issue #13)
# ---------------------------------------------------------------------------
#
# Gradescope's staff upload forms live on Manage Submissions (a new
# submission for a roster student) and on a submission's page (replacing
# it), and differ by assignment type, so the form's action, hidden fields,
# file field and student field are read from the live page. Only a POST form
# whose action is on this Gradescope site and under this assignment is used.


def inspect_submission_upload_form(
    course_id: str,
    assignment_id: str,
    submission_id: str | None = None,
) -> str:
    """Summarize the file-upload forms of Manage Submissions or one submission page.

    Read-only. For each form: method, action, whether
    ``upload_submission_for_student`` would use it (a POST to this
    assignment on this site), file inputs, candidate student fields (with
    the number of choices of a student list) and the number of other fields.
    """
    if not course_id or not assignment_id:
        return "Error: both course_id and assignment_id are required."

    try:
        conn = get_connection()
        page_url = _submission_page_url(conn, course_id, assignment_id, submission_id)
        resp = conn.session.get(page_url, headers=_html_headers())
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
        usable = _usable_form(conn, form, page_url, course_id, assignment_id)
        file_names = _file_input_names(form)
        student_fields = [
            f"`{name}` (list of {choices} choices)" if choices is not None else f"`{name}`"
            for name, choices in _student_field_candidates(form)
        ]
        lines.extend([
            "",
            f"### Form {index}",
            f"- method: `{method}`",
            f"- action: `{sanitize_inline(action)}`",
            "- usable for tool_upload_submission_for_student: "
            + ("yes" if usable else "no (not a POST to this assignment on this site)"),
            f"- file inputs: {', '.join(f'`{n}`' for n in file_names) or 'none'}",
            f"- candidate student fields: {', '.join(student_fields) or 'none'}",
            f"- other submitted fields: {len(_form_fields(form))}",
            f"- text: {sanitize_inline(_clip(_form_text(form), 220)) or 'N/A'}",
        ])

    return "\n".join(lines)


def upload_submission_for_student(
    course_id: str,
    assignment_id: str,
    user_id: str,
    file_paths: list[str],
    submission_id: str | None = None,
    student_field_name: str | None = None,
    confirm_write: bool = False,
    expected_sha256: list[str] | None = None,
) -> str:
    """Upload files on behalf of one roster student through the staff upload form.

    The files are vetted, hashed and bound to the preview exactly as in
    ``upload_submission`` (absolute paths to regular files of at most
    100 MB; no hidden, credential-like or system files;
    ``GRADESCOPE_MCP_UPLOAD_ROOT``; ``expected_sha256``; each file read once).

    The student must be a Student on the course roster. Whether they already
    have a submission is read from the assignment's scores export, and both
    the preview and the write state it:

    - Without ``submission_id``, the upload creates a submission for a
      student who has none, through the Manage Submissions form (with the
      student field set to ``user_id``). If the student already has one,
      the call is refused and names it: replacing it takes an explicit
      ``submission_id``.
    - With ``submission_id``, it must be the student's current submission;
      the upload goes through that submission page's form and replaces it.

    If the scores export can't be read, nothing is uploaded, since it would
    be unknown whether a submission is replaced. Success is reported only
    when Gradescope answers the upload with a redirect to a submission of
    this assignment that did not exist before (none of the submissions the
    form's page links to, nor the student's current one) and lands on its
    page without an error message (as for ``upload_submission``); anything
    else is ``❌ Upload not confirmed``.

    Args:
        course_id: The Gradescope course ID.
        assignment_id: The assignment ID.
        user_id: The student's Gradescope user ID (from the course roster).
        file_paths: List of absolute file paths to upload.
        submission_id: The student's current assignment-level submission ID,
            to replace it; omit to create the student's first submission.
        student_field_name: Exact name of the form field that takes the
            student's user ID, if it is not detected (see
            ``inspect_submission_upload_form``).
        confirm_write: Must be True to perform the upload.
        expected_sha256: Optional SHA-256 hex digests from the preview, one
            per file in ``file_paths`` and in the same order.
    """
    if not course_id or not assignment_id:
        return "Error: both course_id and assignment_id are required."
    if not user_id:
        return "Error: user_id is required."
    if not file_paths:
        return "Error: at least one file path is required."

    try:
        expected = _expected_hashes(expected_sha256, len(file_paths))
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

    contents: list[bytes] = []
    if confirm_write:
        # As in upload_submission: the bytes read are the bytes hashed,
        # compared with the approved digests and sent.
        for path in validated_paths:
            try:
                with open(path, "rb") as fh:
                    data = fh.read(_MAX_UPLOAD_BYTES + 1)
            except OSError as e:
                return f"Error: cannot read {path}: {e}. Nothing was uploaded."
            if len(data) > _MAX_UPLOAD_BYTES:
                return (
                    f"Error: {path} grew past the upload limit of "
                    f"{_MAX_UPLOAD_BYTES:,} bytes. Nothing was uploaded."
                )
            contents.append(data)
        files_info = [
            (path, len(data), hashlib.sha256(data).hexdigest())
            for path, data in zip(validated_paths, contents)
        ]
    else:
        files_info = validated
    if expected is not None:
        mismatches = _hash_mismatches(files_info, expected)
        if mismatches:
            return _changed_since_preview(mismatches)

    try:
        conn = get_connection()
        student = _roster_student(conn, course_id, user_id)
        if isinstance(student, str):
            return student
        current = _current_submission(course_id, assignment_id, student)
        who = _student_label(student)
        if isinstance(current, str):
            return (
                f"Error: could not check whether {who} already has a submission "
                f"for assignment `{assignment_id}` ({current}), so it is unknown "
                "whether this upload would replace one. Nothing was uploaded."
            )
        current_id = current[0]
        if submission_id is None and current_id is not None:
            return (
                f"Error: {who} already has submission `{current_id}` for assignment "
                f"`{assignment_id}`. To replace it, pass submission_id=`{current_id}` "
                "(the upload then goes through that submission's page). Nothing "
                "was uploaded."
            )
        if submission_id is not None and current_id != submission_id:
            current_text = (
                f"their current submission is `{current_id}`" if current_id
                else "they have no submission yet; omit submission_id to create one"
            )
            return (
                f"Error: submission `{submission_id}` is not {who}'s current "
                f"submission for assignment `{assignment_id}` ({current_text}). "
                "Nothing was uploaded."
            )

        page_url = _submission_page_url(conn, course_id, assignment_id, submission_id)
        page_resp = conn.session.get(page_url, headers=_html_headers())
        if page_resp.status_code != 200:
            return (
                f"Error: cannot access upload form page (status "
                f"{page_resp.status_code}) at {page_url}."
            )
        soup = BeautifulSoup(page_resp.text, "html.parser")
        plan = _plan_staff_upload(
            conn, soup, page_url, course_id, assignment_id, user_id,
            submission_id, student_field_name,
        )
        if isinstance(plan, str):
            return plan

        if not confirm_write:
            return _staff_upload_preview(
                course_id, assignment_id, student, current, submission_id,
                plan, validated, roots,
            )

        existing = _linked_submission_ids(page_resp.text, course_id, assignment_id)
        if current_id is not None:
            existing.add(current_id)
        post_resp = conn.session.post(
            plan.post_url,
            data=plan.fields,
            files=[
                (plan.file_field, (path.name, data, _mime_type(path)))
                for path, data in zip(validated_paths, contents)
            ],
            headers={**_html_headers(), "Referer": page_url},
        )
    except AuthError as e:
        return f"Authentication error: {e}"
    except Exception as e:
        return f"Error uploading submission for student `{user_id}`: {e}"

    outcome = _upload_outcome(
        course_id, assignment_id, files_info, expected is not None, existing, post_resp, None,
    )
    head, _, rest = outcome.partition("\n")
    label = "Replaced" if head.startswith("✅") else "Was to replace"
    target = (
        f"- **{label}:** submission `{submission_id}`" if submission_id
        else f"- **{label}:** nothing (the student's first submission)"
    )
    return "\n".join([head, f"- **Student:** {who}", target, rest])


class _StaffUploadPlan(NamedTuple):
    """What ``upload_submission_for_student`` will post, read from the live form."""

    post_url: str
    fields: list[tuple[str, str]]  # the form's submitted fields, student field set
    file_field: str
    student_field: str | None  # None when replacing (the page names the owner)


def _roster_student(conn, course_id: str, user_id: str) -> dict | str:
    """The roster entry of the Student with ``user_id``, or an error string."""
    url = f"{conn.gradescope_base_url}/courses/{course_id}/memberships"
    resp = conn.session.get(url, headers=_html_headers())
    if resp.status_code != 200:
        return (
            f"Error: cannot read the roster of course `{course_id}` (status "
            f"{resp.status_code}) to check user `{user_id}`. Nothing was uploaded."
        )
    members = _parse_roster(BeautifulSoup(resp.text, "html.parser"), course_id)
    matches = [m for m in members if str(m.get("user_id") or "") == str(user_id)]
    if not matches:
        return (
            f"Error: user `{user_id}` is not on the roster of course `{course_id}` "
            "(see tool_get_course_roster for user IDs). Nothing was uploaded."
        )
    member = matches[0]
    if member.get("role") != "Student":
        return (
            f"Error: user `{user_id}` ({_student_label(member)}) is a "
            f"{sanitize_inline(member.get('role'))} in course `{course_id}`, not a "
            "student. Nothing was uploaded."
        )
    return member


def _student_label(member: dict) -> str:
    name = sanitize_inline(member.get("full_name")) or "(no name)"
    email = sanitize_inline(member.get("email"))
    label = f"{name} ({email})" if email else name
    return f"{label}, user ID `{member.get('user_id')}`"


def _current_submission(
    course_id: str, assignment_id: str, member: dict
) -> tuple[str | None, str] | str:
    """The student's current submission from the scores export.

    Returns ``(submission_id or None, status)``, or a string saying why it
    is unknown. ``AuthError`` propagates.
    """
    email = (member.get("email") or "").strip().casefold()
    if not email:
        return "the roster has no email for this student to find their row"
    try:
        rows, _fields = _fetch_assignment_scores_csv(course_id, assignment_id)
    except ValueError as e:
        return f"the scores export could not be read: {e}"
    matches = [r for r in rows if (r.get("Email") or "").strip().casefold() == email]
    if len(matches) != 1:
        return f"the scores export has {len(matches)} rows for this student's email"
    row = matches[0]
    status = (row.get("Status") or "").strip()
    sub_id = (row.get("Submission ID") or "").strip()
    if status == "Missing" or not sub_id:
        return None, status or "Missing"
    if not sub_id.isdigit():
        return f"the scores export lists an unexpected submission ID {sub_id!r}"
    return sub_id, status or "Submitted"


def _usable_form(conn, form, page_url: str, course_id: str, assignment_id: str) -> bool:
    """A POST form whose action is under this assignment on this Gradescope site."""
    if (form.get("method") or "get").strip().lower() != "post":
        return False
    action = urlsplit(_form_action(page_url, form))
    base = urlsplit(conn.gradescope_base_url)
    if action.scheme != base.scheme or action.netloc.lower() != base.netloc.lower():
        return False
    prefix = f"/courses/{course_id}/assignments/{assignment_id}/"
    return (action.path.rstrip("/") + "/").startswith(prefix)


def _plan_staff_upload(
    conn,
    soup: BeautifulSoup,
    page_url: str,
    course_id: str,
    assignment_id: str,
    user_id: str,
    submission_id: str | None,
    student_field_name: str | None,
) -> _StaffUploadPlan | str:
    """Pick the upload form and fill in the student, or explain why not."""
    forms = [
        f for f in _file_upload_forms(soup)
        if _usable_form(conn, f, page_url, course_id, assignment_id)
    ]
    inspect_hint = (
        "Run tool_inspect_submission_upload_form"
        + (f" with submission_id=`{submission_id}`" if submission_id else "")
        + " to see the forms on the page. Nothing was uploaded."
    )
    if not forms:
        return (
            f"Error: {page_url} has no staff upload form that posts to assignment "
            f"`{assignment_id}`. {inspect_hint}"
        )

    if submission_id is not None:
        form = sorted(forms, key=lambda f: _replace_form_rank(f, page_url, submission_id))[0]
        return _StaffUploadPlan(
            _form_action(page_url, form), _form_fields(form), _file_input_names(form)[0], None,
        )

    for form in forms:
        field = student_field_name or _choose_student_field(form, user_id)
        if not field or field not in _field_names(form):
            continue
        choices = _select_choices(form, field)
        if choices is not None and str(user_id) not in choices:
            return (
                f"Error: the upload form's student list (`{field}`) has no entry for "
                f"user `{user_id}`. {inspect_hint}"
            )
        fields = [(name, value) for name, value in _form_fields(form) if name != field]
        fields.append((field, str(user_id)))
        return _StaffUploadPlan(
            _form_action(page_url, form), fields, _file_input_names(form)[0], field,
        )

    if student_field_name:
        return (
            f"Error: no upload form on {page_url} has a field named "
            f"`{student_field_name}`. {inspect_hint}"
        )
    return (
        "Error: could not identify the student field of the upload form; pass "
        f"student_field_name. {inspect_hint}"
    )


def _replace_form_rank(form, page_url: str, submission_id: str) -> tuple:
    """Sort key: forms posting to this submission, then upload-sounding ones."""
    action = urlsplit(_form_action(page_url, form)).path
    text = (_form_text(form) + " " + action).lower()
    terms = ("replace", "resubmit", "re-upload", "upload", "submission")
    return (
        f"/submissions/{submission_id}" not in action + "/",
        not any(term in text for term in terms),
        len(_form_text(form)),
    )


def _staff_upload_preview(
    course_id: str,
    assignment_id: str,
    student: dict,
    current: tuple[str | None, str],
    submission_id: str | None,
    plan: _StaffUploadPlan,
    validated: list[tuple[pathlib.Path, int, str]],
    roots: list[pathlib.Path] | None,
) -> str:
    who = _student_label(student)
    details = [
        f"course_id=`{course_id}`",
        f"assignment_id=`{assignment_id}`",
        f"Student: {who}",
    ]
    if submission_id:
        details.append(
            f"Replaces the student's current submission `{submission_id}` "
            f"(status: {sanitize_inline(current[1])}) through its page's upload form."
        )
    else:
        details.append(
            "Creates the student's first submission (the scores export shows none "
            "for them); it replaces nothing."
        )
    details.append(
        f"Form: POST {plan.post_url}"
        + (f", student field `{plan.student_field}`=`{student.get('user_id')}`"
           if plan.student_field else "")
        + f", file field `{plan.file_field}`"
    )
    details += [f"file {_describe_upload(*info)}" for info in validated]
    if roots is None:
        details.append(
            f"{UPLOAD_ROOT_ENV} is not set, so any non-hidden regular file "
            "outside system directories may be uploaded; set it to restrict "
            "uploads to one directory."
        )
    else:
        details.append(
            f"Uploads are restricted to {UPLOAD_ROOT_ENV}: "
            f"{', '.join(str(r) for r in roots)}"
        )
    details.append(
        "The files become the student's submission, readable by the student "
        "and the course staff."
    )
    details.append(
        "To upload exactly the content shown here, pass "
        f"expected_sha256={json.dumps([digest for _p, _s, digest in validated])} "
        "together with confirm_write=True: the upload is then refused if any "
        "file has changed since this preview."
    )
    return write_confirmation_required("upload_submission_for_student", details)


def _linked_submission_ids(html: str, course_id: str, assignment_id: str) -> set[str]:
    """Submission IDs of this assignment that a page links to."""
    mention = re.compile(
        rf"/courses/{re.escape(str(course_id))}/assignments/"
        rf"{re.escape(str(assignment_id))}/submissions/(\d+)"
    )
    return set(mention.findall(html or ""))


def _mime_type(path: pathlib.Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


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


def _html_headers() -> dict[str, str]:
    return {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "*/*;q=0.8"
        ),
    }


def _file_upload_forms(soup: BeautifulSoup) -> list:
    return [form for form in soup.find_all("form") if _file_input_names(form)]


def _file_input_names(form) -> list[str]:
    """Names of the form's enabled file inputs."""
    return [
        input_el.get("name") or "submission[files][]"
        for input_el in form.find_all("input")
        if (input_el.get("type") or "").lower() == "file"
        and not input_el.has_attr("disabled")
    ]


def _form_fields(form) -> list[tuple[str, str]]:
    """The non-file fields a browser would submit with the form, in order.

    Disabled controls, buttons and unchecked checkboxes or radio buttons are
    left out, like a browser does; a select sends its selected options (the
    first enabled option of a single select when none is selected).
    """
    fields: list[tuple[str, str]] = []
    for el in form.find_all(["input", "textarea", "select"]):
        name = el.get("name")
        if not name or el.has_attr("disabled"):
            continue
        if el.name == "input":
            type_ = (el.get("type") or "text").lower()
            if type_ in {"file", "submit", "button", "image", "reset"}:
                continue
            if type_ in {"checkbox", "radio"}:
                if el.has_attr("checked"):
                    fields.append((name, el.get("value", "on")))
                continue
            fields.append((name, el.get("value", "")))
        elif el.name == "textarea":
            fields.append((name, el.get_text()))
        else:
            options = [o for o in el.find_all("option") if not o.has_attr("disabled")]
            selected = [o for o in options if o.has_attr("selected")]
            if not selected and options and not el.has_attr("multiple"):
                selected = options[:1]
            if not el.has_attr("multiple"):
                selected = selected[:1]
            for option in selected:
                value = option.get("value")
                fields.append((name, option.get_text() if value is None else value))
    return fields


def _field_names(form) -> set[str]:
    return {
        el.get("name") for el in form.find_all(["input", "textarea", "select"])
        if el.get("name") and not el.has_attr("disabled")
    }


def _select_choices(form, name: str) -> set[str] | None:
    """The option values of the select named ``name``, or None if it is no select."""
    select = form.find("select", attrs={"name": name})
    if select is None:
        return None
    return {(o.get("value") or "").strip() for o in select.find_all("option")}


def _form_action(page_url: str, form) -> str:
    return urljoin(page_url, form.get("action") or page_url)


def _form_text(form) -> str:
    return " ".join(form.get_text(" ", strip=True).split())


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


_STUDENT_FIELD_RE = re.compile(r"(user|owner|student|member|submitter)", re.I)


def _student_field_candidates(form) -> list[tuple[str, int | None]]:
    """Fields that look like the student owner: (name, choices of a select or None)."""
    candidates: list[tuple[str, int | None]] = []
    seen: set[str] = set()
    for el in form.find_all(["input", "select"]):
        name = el.get("name")
        if not name or name in seen or not _STUDENT_FIELD_RE.search(name):
            continue
        if el.name == "input" and (el.get("type") or "text").lower() in {
            "file", "submit", "button", "image", "reset", "checkbox", "radio",
        }:
            continue
        seen.add(name)
        choices = len(el.find_all("option")) if el.name == "select" else None
        candidates.append((name, choices))
    return candidates


def _choose_student_field(form, user_id: str) -> str | None:
    """The select offering ``user_id``, else the first student-looking field."""
    for select in form.find_all("select"):
        name = select.get("name")
        if not name:
            continue
        for option in select.find_all("option"):
            if (option.get("value") or "").strip() == str(user_id):
                return name
    candidates = _student_field_candidates(form)
    return candidates[0][0] if candidates else None


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


def _header_key(text: str) -> str:
    """Normalize a header for matching: lowercase, single spaces, without a
    trailing parenthetical or colon ("Score (out of 10)" -> "score")."""
    key = " ".join((text or "").lower().split())
    key = re.sub(r"\s*\([^()]*\)\s*$", "", key)
    return key.rstrip(":").strip()


def _get_submissions_from_review_grades(
    conn, course_id: str, assignment_id: str
) -> str:
    """Fallback: scrape submission list from the review_grades HTML table.

    Used for online assignments where submissions.json returns 404. Columns
    are resolved by header only ("Score (out of 10)" counts as Score), so
    adding/removing the Sections column (or any other layout shift) doesn't
    pull score/graded data from the wrong cells. Without a Score or Graded
    column, graded status is reported as unknown rather than guessed.
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
        for idx, h in enumerate(headers):
            if _header_key(h) in names:
                return idx
        return None

    score_idx = _col("score", "total score", "points", "total points")
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

        # Score: only from a column whose header says so.
        score_text = ""
        if score_idx is not None and score_idx < len(cell_text):
            score_text = cell_text[score_idx]

        # Graded: an affirmative or negative Graded? flag decides; otherwise
        # (empty, placeholder or unrecognized flag) the Score cell decides.
        # ``--`` and other placeholders never count as a score. With neither
        # column the status is unknown (None).
        flag = ""
        if graded_idx is not None and graded_idx < len(cell_text):
            flag = cell_text[graded_idx].strip().lower()
        if flag in _REVIEW_GRADES_AFFIRMATIVE:
            graded = True
        elif flag in _REVIEW_GRADES_NEGATIVE:
            graded = False
        elif score_idx is None:
            graded = None
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
    unknown = sum(1 for s in submissions if s["graded"] is None)

    lines = [f"## Submissions for Assignment {assignment_id}\n"]
    lines.append(f"**Total submissions:** {total}")
    if unknown:
        if graded_idx is None:
            reason = (
                "the review_grades table has no recognizable Score or Graded "
                f"column (headers: {', '.join(repr(h) for h in headers) or 'none'}; "
                "unrecognized page layout)"
            )
        else:
            reason = (
                f"{unknown} row(s) have no recognizable Graded value and the "
                "table has no Score column"
            )
        count = "unknown" if unknown == total else f"at least {graded}/{total} ({unknown} unknown)"
        lines.append(f"**Graded:** {count} — ⚠️ {reason}, so graded status is not guessed.")
    else:
        lines.append(f"**Graded:** {graded}/{total}")
    lines.append("_(Note: retrieved from review_grades fallback)_\n")
    lines.append("| # | Global Submission ID | Score | Graded |")
    lines.append("|---|---------------|-------|--------|")

    for i, sub in enumerate(submissions, 1):
        is_graded = "?" if sub["graded"] is None else ("✅" if sub["graded"] else "—")
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

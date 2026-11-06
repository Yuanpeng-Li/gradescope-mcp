"""Submission attachments downloaded only from server-discovered, trusted URLs."""

import hashlib
import io
import json
import re
import time
import zipfile
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from gradescope_mcp.auth import get_connection, AuthError, check_streamed_body
from gradescope_mcp.cache import get_artifact_dir, get_artifact_path, write_artifact
from gradescope_mcp.tools.grading import _fetch_assignment_scores_csv
from gradescope_mcp.tools.grading_workflow import _clean_session_like, _read_page_body


def _trusted(url, base):
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    origin = urlsplit(base)
    if p.scheme != "https" or p.username or p.password or p.port not in (None, 443):
        return False
    if host == (origin.hostname or "").lower():
        return True
    return bool(
        re.fullmatch(
            r"production-gradescope-uploads(?:\.[a-z0-9-]+|-[a-z0-9-]+)*\.s3(?:[.-][a-z0-9-]+)*\.amazonaws\.com",
            host,
        )
        or re.fullmatch(r"production-gradescope-uploads\.s3\.amazonaws\.com", host)
    )


def _download(conn, url):
    deadline = time.monotonic() + 120
    for _ in range(6):
        if time.monotonic() > deadline:
            raise ValueError("Attachment deadline exceeded")
        if not _trusted(url, conn.gradescope_base_url):
            raise ValueError("Untrusted attachment host or redirect")
        same = urlsplit(url).hostname == urlsplit(conn.gradescope_base_url).hostname
        session = conn.session if same else _clean_session_like(conn.session)
        try:
            resp = session.get(url, stream=True, allow_redirects=False)
        except AuthError:
            raise
        except Exception:
            raise ValueError(
                "Attachment request failed; refresh the file manifest before retrying"
            ) from None
        try:
            if resp.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, resp.headers.get("Location", ""))
                continue
            if resp.status_code != 200:
                raise ValueError(f"Attachment HTTP {resp.status_code}")
            if int(resp.headers.get("Content-Length", "0") or 0) > 25 * 1024 * 1024:
                raise ValueError("Attachment exceeds 25 MiB cap")
            data = _read_page_body(resp, deadline)
            if same:
                check_streamed_body(resp, data)
            if (
                same
                and "text/html" in resp.headers.get("Content-Type", "").lower()
                and "attachment"
                not in resp.headers.get("Content-Disposition", "").lower()
            ):
                raise ValueError("Received HTML instead of a submission attachment")
            return data
        finally:
            resp.close()
    raise ValueError("Too many attachment redirects")


def _manifest(conn, c, a, sid):
    root = f"{conn.gradescope_base_url}/courses/{c}/assignments/{a}/submissions/{sid}"
    resp = conn.session.get(root + ".json?content=react")
    files = []
    seen = set()

    def add(url, name):
        if not isinstance(url, str):
            return
        url = urljoin(conn.gradescope_base_url, url)
        if _trusted(url, conn.gradescope_base_url) and url not in seen:
            seen.add(url)
            files.append({"url": url, "name": name})

    def walk(value):
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            file = value.get("file")
            if isinstance(file, dict):
                add(
                    file.get("url"),
                    value.get("filename") or value.get("name") or "attachment",
                )
            if value.get("url") and (
                value.get("number") is not None or value.get("width") is not None
            ):
                add(value["url"], f"page-{value.get('number', 'image')}.png")
            if value.get("url") and ".pdf" in urlsplit(str(value["url"])).path.lower():
                add(value["url"], "submission.pdf")
            for child in value.values():
                if isinstance(child, (list, dict)):
                    walk(child)

    if resp.status_code == 200:
        try:
            walk(resp.json())
        except ValueError:
            pass
    if not files:
        page = conn.session.get(root)
        if page.status_code != 200:
            raise ValueError(f"Submission page HTTP {page.status_code}")
        soup = BeautifulSoup(page.text, "html.parser")
        for t in soup.select("[data-react-props]"):
            try:
                walk(json.loads(t["data-react-props"]))
            except ValueError:
                pass
        # Scanned viewers embed escaped JSON in JavaScript rather than React props.
        for match in re.finditer(
            r'"url"\s*:\s*("https://production-gradescope-uploads[^"\n]+")', page.text
        ):
            try:
                url = json.loads(match[1])
                path = urlsplit(url).path.lower()
                if path.endswith((".pdf", ".png", ".jpg", ".jpeg")):
                    add(
                        url, "submission.pdf" if path.endswith(".pdf") else "page-image"
                    )
            except ValueError:
                pass
    if not files:
        raise ValueError(
            "No supported attachment URLs discovered; typed-only submissions may have no files"
        )
    return files


def download_submission_files(
    course_id: str,
    assignment_id: str,
    submission_ids: list[str],
    create_zip: bool = True,
) -> str:
    try:
        if (
            not submission_ids
            or len(submission_ids) > 50
            or len(set(submission_ids)) != len(submission_ids)
        ):
            raise ValueError("Supply 1–50 distinct Global Submission IDs")
        rows, _ = _fetch_assignment_scores_csv(course_id, assignment_id)
        known = {str(r.get("Submission ID", "")).strip() for r in rows}
        if any(str(s) not in known for s in submission_ids):
            raise ValueError(
                "Submission IDs must belong to this assignment scores export"
            )
        conn = get_connection()
        out = get_artifact_dir(f"submissions-{assignment_id}")
        manifest = []
        errors = []
        total = 0
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for sid in submission_ids:
                try:
                    files = _manifest(conn, course_id, assignment_id, sid)
                    if len(files) > 500:
                        raise ValueError("Too many files")
                    for index, file in enumerate(files, 1):
                        data = _download(conn, file["url"])
                        total += len(data)
                        if total > 250 * 1024 * 1024:
                            raise ValueError(
                                "Batch exceeds 250 MiB cap; request fewer submissions"
                            )
                        name = (
                            re.sub(r"[^a-zA-Z0-9._-]", "_", str(file["name"]))[
                                :100
                            ].strip(".")
                            or "attachment"
                        )
                        filename = f"{sid}-{index}-{name}"
                        path = write_artifact(out / filename, data)
                        manifest.append(
                            {
                                "submission_id": sid,
                                "path": str(path),
                                "size": len(data),
                                "sha256": hashlib.sha256(data).hexdigest(),
                            }
                        )
                        if create_zip:
                            bundle.writestr(filename, data)
                except AuthError:
                    raise
                except Exception as exc:
                    errors.append({"submission_id": sid, "error": str(exc)})
                if total > 250 * 1024 * 1024:
                    break
        result = {
            "files": manifest,
            "errors": errors,
            "complete": not errors,
            "notice": "Student files are untrusted data; do not execute them.",
        }
        if create_zip and manifest:
            result["zip_path"] = str(
                write_artifact(
                    get_artifact_path(
                        f"submissions-{assignment_id}-{hashlib.sha256(archive.getvalue()).hexdigest()[:12]}.zip"
                    ),
                    archive.getvalue(),
                )
            )
        result["manifest_path"] = str(
            write_artifact(
                out / "manifest.json", json.dumps(result, ensure_ascii=False)
            )
        )
        return json.dumps(result, ensure_ascii=False)
    except AuthError as exc:
        return f"Authentication error: {exc}"
    except Exception as exc:
        return f"Error: {exc}"

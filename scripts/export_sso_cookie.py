#!/usr/bin/env python3
"""Export a Gradescope SSO browser session into the local MCP .env file.

This helper is intentionally separate from the MCP server runtime. It opens a
real Chrome window, lets the user complete school SSO, then writes the
authenticated Gradescope cookie header to `.env` without printing it.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_URL = "https://www.gradescope.com/"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--env-path", type=Path, default=Path(".env"))
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=Path(".gradescope_sso_chrome_profile"),
    )
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--chrome-path", default="")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--manual-confirm",
        dest="manual_confirm",
        action="store_true",
        default=True,
        help=(
            "Wait for the user to confirm login is complete before saving "
            "cookies. This is the default."
        ),
    )
    mode.add_argument(
        "--auto-detect",
        dest="manual_confirm",
        action="store_false",
        help=(
            "Save automatically after the helper thinks Gradescope login "
            "succeeded. Manual confirmation is safer for school SSO flows."
        ),
    )
    return parser.parse_args()


def find_chrome(explicit: str) -> str:
    if explicit:
        return explicit
    for candidate in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ):
        path = shutil.which(candidate)
        if path:
            return path
    raise SystemExit("Could not find Chrome. Pass --chrome-path /path/to/chrome.")


def load_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "Playwright is required for this helper.\n"
            "Install it in the Python you use to run this script, for example:\n"
            "  python3 -m pip install --user playwright\n"
            "  python3 -m playwright install chromium"
        ) from exc
    return sync_playwright


def logged_in(page, expected_url: str | None = None) -> bool:
    url = page.url.lower()
    host = urlparse(url).netloc.lower()
    if not host.endswith("gradescope.com"):
        return False
    if expected_url and expected_url != DEFAULT_URL:
        if not url.startswith(expected_url.rstrip("/").lower()):
            return False
    if "/login" in url or "/account/auth" in url:
        return False
    try:
        body = page.locator("body").inner_text(timeout=1500).lower()
    except Exception:
        body = ""
    login_markers = (
        "you must be logged in",
        "log in with your gradescope account",
        "forgot your password",
        "unauthorized",
        "401",
    )
    return not any(marker in body for marker in login_markers)


def wait_for_manual_confirmation() -> None:
    text = (
        "Complete your normal school login in the Chrome window. "
        "When the Gradescope page is visible, click Save here."
    )
    zenity = shutil.which("zenity")
    if zenity:
        result = subprocess.run(
            [
                zenity,
                "--question",
                "--title=Save Gradescope MCP session",
                f"--text={text}",
                "--ok-label=Save",
                "--cancel-label=Cancel",
            ],
            check=False,
        )
        if result.returncode != 0:
            raise SystemExit("Canceled before saving Gradescope cookies.")
        return

    input(text + " Then press Enter in this terminal...")


def cookie_header(cookies: list[dict]) -> str:
    gradescope_cookies = [
        cookie
        for cookie in cookies
        if "gradescope.com" in cookie.get("domain", "")
    ]
    return "; ".join(
        f"{cookie['name']}={cookie['value']}"
        for cookie in sorted(gradescope_cookies, key=lambda c: c["name"])
    )


def quote_dotenv(value: str) -> str:
    return json.dumps(value)


# An existing cookie assignment, with or without a leading ``export``.
_COOKIE_LINE_RE = re.compile(
    r"^\s*(?:export\s+)?(?:GRADESCOPE_COOKIE_HEADER|GRADESCOPE_SESSION_COOKIE)\s*="
)


def write_env(env_path: Path, header: str) -> None:
    """Set ``GRADESCOPE_COOKIE_HEADER`` in ``env_path``, keeping its other lines.

    Any earlier ``GRADESCOPE_COOKIE_HEADER`` / ``GRADESCOPE_SESSION_COOKIE``
    line is dropped. The new file is written to a temporary file created with
    mode 0600 in the same directory and then moved into place, so the cookie
    is never readable by other users, not even briefly, and an interrupted
    write cannot leave a truncated ``.env``. (The server skips a ``.env``
    that other users can write.)
    """
    lines = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    kept = [line for line in lines if not _COOKIE_LINE_RE.match(line)]
    while kept and not kept[-1].strip():
        kept.pop()
    kept.append(f"GRADESCOPE_COOKIE_HEADER={quote_dotenv(header)}")
    text = "\n".join(kept) + "\n"

    fd, tmp_name = tempfile.mkstemp(prefix=".env.", dir=env_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, env_path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def main() -> int:
    args = parse_args()
    chrome_path = find_chrome(args.chrome_path)
    sync_playwright = load_playwright()
    env_path = args.env_path.resolve()
    profile_dir = args.profile_dir.resolve()

    print("Opening Chrome for Gradescope SSO login...")
    print("Complete your normal school login in the browser window.")
    if args.manual_confirm:
        print("Chrome will stay open until you confirm that login is complete.")
    else:
        print("Auto-detect mode will save cookies after login appears complete.")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile_dir),
            executable_path=chrome_path,
            headless=False,
            accept_downloads=False,
            no_viewport=True,
            args=["--start-maximized"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(args.url, wait_until="domcontentloaded")

        if args.manual_confirm:
            wait_for_manual_confirmation()
            cookies = context.cookies("https://www.gradescope.com")
            header = cookie_header(cookies)
            if "_gradescope_session=" not in header:
                context.close()
                raise SystemExit("No Gradescope session cookie found after login.")
            if not logged_in(page, args.url):
                print(
                    "Warning: the active page does not look like the expected "
                    "logged-in Gradescope page. Saving cookies anyway."
                )
        else:
            deadline = time.time() + args.timeout_seconds
            header = ""
            while time.time() < deadline:
                page.wait_for_timeout(1500)
                cookies = context.cookies("https://www.gradescope.com")
                header = cookie_header(cookies)
                if "_gradescope_session=" in header and logged_in(page, args.url):
                    break
            else:
                context.close()
                raise SystemExit("Timed out waiting for a logged-in Gradescope page.")

        write_env(env_path, header)
        context.close()

    print(f"Saved Gradescope cookie header to {env_path}")
    print(
        "The server uses it on its next tool call; no restart is needed unless "
        "GRADESCOPE_COOKIE_HEADER is also set in the MCP client's env block, "
        "which wins over .env."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Entry point for the Gradescope MCP server.

Usage:
    uv run python -m gradescope_mcp

``main()`` loads ``.env`` files (see ``gradescope_mcp.envfiles`` for which
files and in what precedence), prepares the private cache directory,
configures logging and runs the server over stdio.
"""

import logging

from gradescope_mcp.cache import CacheError, configure_process_cache_env
from gradescope_mcp.envfiles import (  # noqa: F401  (re-exported for callers and tests)
    _untrusted_reason,
    dotenv_candidates,
    load_env_files,
    source_checkout,
)


def main():
    # Start-up runs here rather than at import time, so importing this module
    # (e.g. for ``load_env_files``) changes nothing. Both ``python -m
    # gradescope_mcp`` and the ``gradescope-mcp`` console script call main().

    # Load environment variables from .env files before anything reads them.
    dotenv_loaded, dotenv_skipped = load_env_files(remember_credentials=True)

    # An unsafe cache root (symlink, foreign owner, group/other access) must
    # not keep the server from starting: most tools never touch the cache, and
    # the workflow tools re-check the root and report the problem on every use.
    try:
        cache_root = configure_process_cache_env()
        cache_error = None
    except (CacheError, OSError) as e:
        cache_root = None
        cache_error = e

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger = logging.getLogger(__name__)
    for path in dotenv_loaded:
        logger.info("Loaded environment defaults from %s", path)
    for path, reason in dotenv_skipped:
        logger.warning("Ignoring %s: %s.", path, reason)
    if cache_root is not None:
        logger.info("Using private runtime cache directory: %s", cache_root)
    else:
        logger.error(
            "Runtime cache directory is unavailable: %s. Tools that write "
            "artifacts will fail until this is fixed.",
            cache_error,
        )
    from gradescope_mcp.server import mcp
    mcp.run()


if __name__ == "__main__":
    main()

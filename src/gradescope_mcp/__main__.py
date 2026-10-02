"""Entry point for the Gradescope MCP server.

Usage:
    uv run python -m gradescope_mcp
"""

import logging

from dotenv import load_dotenv

from gradescope_mcp.cache import CacheError, configure_process_cache_env

# Load environment variables from .env file
load_dotenv()

# An unsafe cache root (symlink, foreign owner, group/other access) must not
# keep the server from starting: most tools never touch the cache, and the
# workflow tools re-check the root and report the problem on every use.
try:
    cache_root = configure_process_cache_env()
    cache_error = None
except (CacheError, OSError) as e:
    cache_root = None
    cache_error = e

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


def main():
    logger = logging.getLogger(__name__)
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

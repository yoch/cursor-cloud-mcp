"""Test entry point equivalent to the binary, with the fictional transport.

The stdio tests launch ``python -m cursor_cloud_mcp`` and set
``CURSOR_MCP_FIXTURE=1`` in the subprocess environment. This module
remains available for a client that can only pass a Python command.
"""

import os

from cursor_cloud_mcp.__main__ import main


def run() -> None:
    os.environ["CURSOR_MCP_FIXTURE"] = "1"
    main()


if __name__ == "__main__":
    run()

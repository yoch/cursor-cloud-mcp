"""Entry point ``python -m cursor_cloud_mcp`` and console script ``cursor-cloud-mcp``."""

from cursor_cloud_mcp.server import run_stdio


def main() -> None:
    run_stdio()


if __name__ == "__main__":
    main()

"""Point d'entrée ``python -m cursor_cloud_mcp`` et console ``cursor-cloud-mcp``."""

from cursor_cloud_mcp.server import run_stdio


def main() -> None:
    run_stdio()


if __name__ == "__main__":
    main()

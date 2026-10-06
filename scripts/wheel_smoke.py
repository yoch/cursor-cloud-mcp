"""Échange MCP réel avec le binaire installé (wheel), en mode simulé. Lancer hors du dépôt."""

import asyncio
import shutil
import sys

from mcp import Client, StdioServerParameters

EXPECTED_TOOLS = 16


async def main() -> None:
    command = shutil.which("cursor-cloud-mcp", path=str(sys.exec_prefix) + "/bin")
    assert command, "cursor-cloud-mcp introuvable dans l'environnement du wheel"
    params = StdioServerParameters(command=command, env={"CURSOR_MCP_FIXTURE": "1", "PATH": "/usr/bin:/bin"})
    async with Client(params) as client:
        tools = await client.list_tools()
        assert len(tools.tools) == EXPECTED_TOOLS, [tool.name for tool in tools.tools]
        account = await client.call_tool("cursor_get_account", {})
        assert account.is_error is False, account
    print(f"ok: {len(tools.tools)} outils, cursor_get_account répond")


if __name__ == "__main__":
    asyncio.run(main())

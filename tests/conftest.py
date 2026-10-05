import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def isolated_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registre de secrets vide pour un test, restauré ensuite."""
    from collections import OrderedDict

    from cursor_cloud_mcp import redaction

    monkeypatch.setattr(redaction, "_permanent", set())
    monkeypatch.setattr(redaction, "_recent", OrderedDict())
    monkeypatch.setattr(redaction, "_pattern", None)

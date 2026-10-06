import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def isolated_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty secrets registry for a test, restored afterwards."""
    from collections import OrderedDict

    from cursor_cloud_mcp import redaction

    monkeypatch.setattr(redaction, "_permanent", set())
    monkeypatch.setattr(redaction, "_recent", OrderedDict())
    monkeypatch.setattr(redaction, "_pattern", None)

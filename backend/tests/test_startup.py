from unittest.mock import AsyncMock, Mock

import pytest

from navigator.api import main
from navigator.config import Settings


@pytest.mark.asyncio
async def test_startup_failure_closes_both_clients(monkeypatch):
    store = Mock()
    async_store = Mock(close=AsyncMock())
    monkeypatch.setattr(main, "make_store", lambda settings: store)
    monkeypatch.setattr(main, "AsyncStore", lambda *args: async_store)
    monkeypatch.setattr(main, "create_indexes", Mock(side_effect=RuntimeError("test startup failure")))
    app = main.create_app(Settings(_env_file=None, NAVIGATOR_MODE="production"))
    with pytest.raises(RuntimeError, match="test startup failure"):
        async with app.router.lifespan_context(app):
            pytest.fail("Startup must fail before accepting requests")
    async_store.close.assert_awaited_once()
    store.close.assert_called_once()

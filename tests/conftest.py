from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cryptowatcher.config import Infrastructure, OwnerSettings
from cryptowatcher.storage import Store


@pytest.fixture
def infrastructure(tmp_path: Path) -> Infrastructure:
    return Infrastructure.from_env(
        {
            "OWNER_USER_ID": "233382575",
            "EXPECTED_BOT_ID": "7523865123",
            "TELEGRAM_BOT_TOKEN": "7523865123:" + "test_only_not_a_secret" * 2,
            "DATABASE_FILE": str(tmp_path / "state.sqlite3"),
            "STATE_FILE": str(tmp_path / "legacy.json"),
        }
    )


@pytest.fixture
def store(infrastructure: Infrastructure):
    instance = Store(infrastructure.database, infrastructure.owner_id)
    instance.initialize(OwnerSettings())
    yield instance
    instance.close()


def update(text: str = "/terms", *, owner: bool = True, group: bool = False, callback: str | None = None):
    message = SimpleNamespace(text=text, reply_text=AsyncMock())
    query = (
        SimpleNamespace(data=callback, answer=AsyncMock(), edit_message_text=AsyncMock())
        if callback
        else None
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=233382575 if owner else 999),
        effective_chat=SimpleNamespace(id=-123 if group else 233382575, type="group" if group else "private"),
        effective_message=message,
        callback_query=query,
    )

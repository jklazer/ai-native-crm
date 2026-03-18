"""
Фикстуры pytest — fakeredis + MockAdapter.
Никаких внешних зависимостей (PostgreSQL, реального Redis, LLM API).
"""
import pytest
import fakeredis.aioredis
from fakeredis import FakeServer

from ai_native_crm.core.state_store import StateStore
from ai_native_crm.adapters.mock import MockAdapter


@pytest.fixture
def redis():
    """
    In-memory Redis через fakeredis с поддержкой Lua (нужен для get_due_reminders).
    decode_responses=True — StateStore разработан с этим флагом (строковые ключи и значения).
    """
    server = FakeServer()
    server.lua_modules = True  # включаем Lua для поддержки eval/evalsha
    return fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)


@pytest.fixture
def state_store(redis):
    """StateStore поверх fakeredis."""
    return StateStore(redis)


@pytest.fixture
def mock_adapter():
    """MockAdapter — изолированный экземпляр со свежим seed."""
    return MockAdapter()

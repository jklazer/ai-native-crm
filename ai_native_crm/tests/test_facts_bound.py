"""
Тесты ограничения размера списка critical_facts через LTRIM.
Использует fakeredis — никакого реального Redis, никакого SQL.
"""
import pytest
import fakeredis.aioredis
from fakeredis import FakeServer

from ai_native_crm.core.state_store import CriticalFact, StateStore


# ---------------------------------------------------------------------------
# Фикстура: StateStore с маленьким лимитом для удобства тестирования
# ---------------------------------------------------------------------------


@pytest.fixture
def redis_server():
    """Общий FakeServer с поддержкой Lua (нужен для Lua dedup-скрипта)."""
    server = FakeServer()
    server.lua_modules = True
    return server


@pytest.fixture
def bounded_store(redis_server):
    """StateStore с max_critical_facts=5 поверх fakeredis."""
    redis = fakeredis.aioredis.FakeRedis(server=redis_server, decode_responses=True)
    return StateStore(redis, max_critical_facts=5)


# ---------------------------------------------------------------------------
# test 1: после добавления 10 фактов остаётся ровно 5
# ---------------------------------------------------------------------------


async def test_ltrim_keeps_only_max_facts(bounded_store):
    """Добавление 10 фактов при лимите 5 → список содержит ровно 5 записей."""
    chat_id = 1001

    for i in range(10):
        fact = CriticalFact(fact_type="rejection", content=f"fact_{i}")
        await bounded_store.add_critical_fact(chat_id, fact)

    facts = await bounded_store.get_critical_facts(chat_id)
    assert len(facts) == 5


# ---------------------------------------------------------------------------
# test 2: сохраняются последние N фактов (самые новые), а не первые
# ---------------------------------------------------------------------------


async def test_ltrim_keeps_newest_facts(bounded_store):
    """После обрезки в списке остаются факты с индексами 5–9, а не 0–4."""
    chat_id = 1002

    for i in range(10):
        fact = CriticalFact(fact_type="budget_limit", content=f"fact_{i}")
        await bounded_store.add_critical_fact(chat_id, fact)

    facts = await bounded_store.get_critical_facts(chat_id)
    contents = [f.content for f in facts]

    # Старые факты (fact_0 … fact_4) должны быть отброшены
    for old in [f"fact_{i}" for i in range(5)]:
        assert old not in contents, f"{old} should have been trimmed"

    # Новые факты (fact_5 … fact_9) должны остаться
    for new in [f"fact_{i}" for i in range(5, 10)]:
        assert new in contents, f"{new} should be retained"


# ---------------------------------------------------------------------------
# test 3: порядок добавления сохраняется после обрезки
# ---------------------------------------------------------------------------


async def test_ltrim_preserves_order(bounded_store):
    """Оставшиеся факты возвращаются в порядке добавления (от старых к новым)."""
    chat_id = 1003

    for i in range(8):
        fact = CriticalFact(fact_type="deadline", content=f"ordered_{i}")
        await bounded_store.add_critical_fact(chat_id, fact)

    facts = await bounded_store.get_critical_facts(chat_id)
    contents = [f.content for f in facts]

    # Ожидаем факты 3..7 в хронологическом порядке
    assert contents == [f"ordered_{i}" for i in range(3, 8)]


# ---------------------------------------------------------------------------
# test 4: добавление меньше лимита — список не обрезается раньше времени
# ---------------------------------------------------------------------------


async def test_ltrim_no_trim_below_limit(bounded_store):
    """Если фактов меньше лимита — ни один не теряется."""
    chat_id = 1004

    for i in range(3):
        fact = CriticalFact(fact_type="hard_requirement", content=f"keep_{i}")
        await bounded_store.add_critical_fact(chat_id, fact)

    facts = await bounded_store.get_critical_facts(chat_id)
    assert len(facts) == 3
    assert [f.content for f in facts] == ["keep_0", "keep_1", "keep_2"]


# ---------------------------------------------------------------------------
# test 5: ровно на границе лимита — все факты сохраняются
# ---------------------------------------------------------------------------


async def test_ltrim_exactly_at_limit(bounded_store):
    """Ровно max_critical_facts фактов — обрезки нет, все сохраняются."""
    chat_id = 1005

    for i in range(5):
        fact = CriticalFact(fact_type="decision_maker", content=f"exact_{i}")
        await bounded_store.add_critical_fact(chat_id, fact)

    facts = await bounded_store.get_critical_facts(chat_id)
    assert len(facts) == 5
    assert [f.content for f in facts] == [f"exact_{i}" for i in range(5)]

"""
Тесты StateStore — 6 тестов.
Все операции через fakeredis: никакого реального Redis, никакого SQL.
"""
import time

import pytest

from ai_native_crm.core.state_store import AuditEntry, CriticalFact, SemanticState


# ---------------------------------------------------------------------------
# test 1: пустой chat_id → SemanticState(iteration=0)
# ---------------------------------------------------------------------------


async def test_load_empty(state_store):
    """Загрузка несуществующего chat_id возвращает пустой стейт с iteration=0."""
    state = await state_store.load(chat_id=99999)

    assert isinstance(state, SemanticState)
    assert state.chat_id == 99999
    assert state.iteration == 0
    assert state.working_memory == ""
    assert state.agent_assessment == ""
    assert state.conversation_summary == ""


# ---------------------------------------------------------------------------
# test 2: save → load → совпадает
# ---------------------------------------------------------------------------


async def test_save_load_roundtrip(state_store):
    """Сохранённый стейт точно восстанавливается из Redis."""
    original = SemanticState(
        chat_id=42,
        iteration=7,
        working_memory="Клиент заинтересован в сделке d1",
        agent_assessment="Высокий шанс закрытия",
        conversation_summary="Обсудили условия",
    )

    await state_store.save(42, original)
    loaded = await state_store.load(42)

    assert loaded.chat_id == 42
    assert loaded.iteration == 7
    assert loaded.working_memory == original.working_memory
    assert loaded.agent_assessment == original.agent_assessment
    assert loaded.conversation_summary == original.conversation_summary
    # last_updated должен быть проставлен save()
    assert loaded.last_updated != ""


# ---------------------------------------------------------------------------
# test 3: добавить 3 критических факта → получить 3
# ---------------------------------------------------------------------------


async def test_critical_facts_append(state_store):
    """Факты добавляются в конец списка и возвращаются в правильном порядке."""
    chat_id = 10

    facts_to_add = [
        CriticalFact(fact_type="rejection", content="Клиент отказался из-за цены"),
        CriticalFact(fact_type="budget_limit", content="Бюджет не более 500k", deal_id="d1"),
        CriticalFact(fact_type="deadline", content="Контракт нужен до 1 апреля"),
    ]

    for f in facts_to_add:
        await state_store.add_critical_fact(chat_id, f)

    retrieved = await state_store.get_critical_facts(chat_id)

    assert len(retrieved) == 3
    assert retrieved[0].fact_type == "rejection"
    assert retrieved[1].fact_type == "budget_limit"
    assert retrieved[1].deal_id == "d1"
    assert retrieved[2].fact_type == "deadline"


# ---------------------------------------------------------------------------
# test 4: log_audit → get_audit возвращает запись
# ---------------------------------------------------------------------------


async def test_audit_stream(state_store):
    """Запись аудита сохраняется в Redis Stream и доступна через get_audit."""
    chat_id = 20

    entry = AuditEntry(
        chat_id=chat_id,
        user_input="Покажи сделки",
        llm_response="Вот ваши сделки...",
        actions=[{"type": "noop", "success": True}],
        model_used="gpt-4o-mini",
        tokens_in=50,
        tokens_out=80,
        latency_ms=320,
    )

    await state_store.log_audit(chat_id, entry)
    records = await state_store.get_audit(chat_id, limit=10)

    assert len(records) == 1
    rec = records[0]
    assert rec["chat_id"] == chat_id
    assert rec["user_input"] == "Покажи сделки"
    assert rec["llm_response"] == "Вот ваши сделки..."
    assert rec["model_used"] == "gpt-4o-mini"
    assert rec["tokens_in"] == 50
    assert rec["tokens_out"] == 80
    assert len(rec["actions"]) == 1


# ---------------------------------------------------------------------------
# test 5: update_metrics → get_metrics совпадает
# ---------------------------------------------------------------------------


async def test_metrics(state_store):
    """Метрики записываются в Redis Hash и точно читаются обратно."""
    chat_id = 30

    metrics_in = {
        "total_turns": 15.0,
        "hallucinations": 1.0,
        "hallucination_rate": 1 / 15,
        "action_successes": 12.0,
        "action_failures": 2.0,
    }
    await state_store.update_metrics(chat_id, metrics_in)

    result = await state_store.get_metrics(chat_id)

    assert result["total_turns"] == pytest.approx(15.0)
    assert result["hallucinations"] == pytest.approx(1.0)
    assert result["action_successes"] == pytest.approx(12.0)
    assert result["action_failures"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# test 6: add_reminder → get_due (прошлый timestamp) → получить и удалить
# ---------------------------------------------------------------------------


async def test_reminders(state_store):
    """
    Просроченное напоминание возвращается get_due_reminders и атомарно удаляется.
    Повторный вызов возвращает пустой список.
    """
    chat_id = 40

    # Время в прошлом — напоминание уже «просрочено»
    past_ts = time.time() - 3600

    await state_store.add_reminder(
        chat_id=chat_id,
        text="Позвонить клиенту",
        fire_at=past_ts,
        deal_id="d2",
    )

    # Будущее напоминание — НЕ должно попасть в due
    future_ts = time.time() + 86400
    await state_store.add_reminder(
        chat_id=chat_id,
        text="Встреча завтра",
        fire_at=future_ts,
    )

    due = await state_store.get_due_reminders(chat_id)

    assert len(due) == 1
    assert due[0]["text"] == "Позвонить клиенту"
    assert due[0]["deal_id"] == "d2"

    # После get_due просроченное напоминание должно быть удалено
    due_again = await state_store.get_due_reminders(chat_id)
    assert len(due_again) == 0

    # Будущее напоминание должно оставаться в системе
    chat_keys = await state_store.get_all_reminder_keys()
    assert chat_id in chat_keys

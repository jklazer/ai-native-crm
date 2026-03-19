"""
Тесты повторной анонимизации PII в LLM-генерированных полях стейта (152-ФЗ).

Проверяем, что _apply_llm_updates маскирует ПДн в new_working_memory,
new_assessment и new_conversation_summary ПЕРЕД сохранением в Redis,
даже когда LLM дословно повторяет имена/телефоны/email из контекста.
"""
from __future__ import annotations

import pytest
import fakeredis.aioredis
from fakeredis import FakeServer

from ai_native_crm.core.state_store import SemanticState
from ai_native_crm.services.pii_anonymizer import PIIAnonymizer


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture
def redis():
    server = FakeServer()
    server.lua_modules = True
    return fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)


@pytest.fixture
def anonymizer(redis):
    return PIIAnonymizer(redis)


def _base_state(chat_id: int = 42) -> SemanticState:
    return SemanticState(chat_id=chat_id, iteration=0)


# ---------------------------------------------------------------------------
# Вспомогательная фабрика — импортируем AgentEngine здесь, чтобы не
# дублировать всю сборку из test_engine.py
# ---------------------------------------------------------------------------

from ai_native_crm.adapters.mock import MockAdapter
from ai_native_crm.core.engine import AgentEngine
from ai_native_crm.core.state_store import StateStore
from ai_native_crm.core.response_validator import ResponseValidator
from ai_native_crm.core.action_router import ActionRouter
from ai_native_crm.core.compressor import StateCompressor
from ai_native_crm.core.drift_detector import DriftDetector
from ai_native_crm.services.lock import DistributedLock
from ai_native_crm.services.metrics import MetricsService


class _FakeLLM:
    """Заглушка LLM с настраиваемым ответом."""

    def __init__(self, resp: dict) -> None:
        self._resp = resp

    async def call(self, messages: list[dict]) -> tuple[dict, dict]:
        return self._resp, {
            "model": "fake",
            "tokens_in": 10,
            "tokens_out": 20,
            "latency_ms": 5,
        }


def _make_engine(llm, redis) -> AgentEngine:
    adapter = MockAdapter()
    store = StateStore(redis)
    validator = ResponseValidator(adapter)
    action_router = ActionRouter(adapter, bot=None, state_store=store)
    compressor = StateCompressor(llm)
    drift = DriftDetector(adapter)
    metrics = MetricsService(store)
    lock = DistributedLock(redis)
    anon = PIIAnonymizer(redis)

    return AgentEngine(
        state_store=store,
        crm=adapter,
        llm=llm,
        validator=validator,
        action_router=action_router,
        compressor=compressor,
        drift=drift,
        anonymizer=anon,
        lock=lock,
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# Тест 1: _apply_llm_updates маскирует полное ФИО в working_memory
# ---------------------------------------------------------------------------


async def test_pii_in_working_memory_is_anonymized(redis, anonymizer):
    """
    Если LLM вписал полное ФИО в new_working_memory, оно должно быть
    заменено токеном [PERSON_N] до передачи в replace().
    """
    state = _base_state(chat_id=100)

    llm_response = {
        "new_working_memory": "Клиент Иванов Иван Иванович ждёт коммерческое предложение",
        "new_assessment": "Активные переговоры",
        "new_conversation_summary": "Обсуждается КП",
    }

    engine = _make_engine(_FakeLLM(llm_response), redis)
    new_state = await engine._apply_llm_updates(state, llm_response)

    assert "Иванов Иван Иванович" not in new_state.working_memory
    assert "[PERSON_" in new_state.working_memory


# ---------------------------------------------------------------------------
# Тест 2: _apply_llm_updates маскирует телефон в agent_assessment
# ---------------------------------------------------------------------------


async def test_pii_phone_in_assessment_is_anonymized(redis, anonymizer):
    """
    Если LLM вписал телефон в new_assessment, он должен быть
    заменён токеном [PHONE_N].

    Используем формат 8-999-123-45-67, который уверенно матчится регуляркой.
    Формат +7 (999) ... с пробелом после +7 — известное ограничение текущего
    паттерна (_RE_PHONE не захватывает пробел между +7 и открывающей скобкой).
    """
    state = _base_state(chat_id=101)

    llm_response = {
        "new_working_memory": "Обсуждаем условия",
        "new_assessment": "Контакт по номеру 8-999-123-45-67",
        "new_conversation_summary": "Переговоры",
    }

    engine = _make_engine(_FakeLLM(llm_response), redis)
    new_state = await engine._apply_llm_updates(state, llm_response)

    assert "8-999-123-45-67" not in new_state.agent_assessment
    assert "[PHONE_" in new_state.agent_assessment


# ---------------------------------------------------------------------------
# Тест 3: _apply_llm_updates маскирует email в conversation_summary
# ---------------------------------------------------------------------------


async def test_pii_email_in_summary_is_anonymized(redis, anonymizer):
    """
    Если LLM вписал email в new_conversation_summary, он должен быть
    заменён токеном [EMAIL_N].
    """
    state = _base_state(chat_id=102)

    llm_response = {
        "new_working_memory": "Обсуждаем условия",
        "new_assessment": "Активная стадия",
        "new_conversation_summary": "Менеджер отправил КП на client@example.com",
    }

    engine = _make_engine(_FakeLLM(llm_response), redis)
    new_state = await engine._apply_llm_updates(state, llm_response)

    assert "client@example.com" not in new_state.conversation_summary
    assert "[EMAIL_" in new_state.conversation_summary


# ---------------------------------------------------------------------------
# Тест 4: PII маскируется перед сохранением в Redis через полный pipeline
# ---------------------------------------------------------------------------


async def test_pii_not_persisted_to_redis_via_pipeline(redis):
    """
    Интеграционный тест: PII в LLM-ответе НЕ должна попасть в Redis-стейт.
    Проверяем значение working_memory из StateStore после полного прогона engine.
    """
    pii_name = "Петров Пётр Петрович"
    pii_phone = "89001234567"

    llm_resp = {
        "response": "Принято к сведению.",
        "actions": [],
        "new_working_memory": f"Клиент {pii_name}, тел. {pii_phone}",
        "new_assessment": "Ждём решения",
        "new_conversation_summary": "Первый контакт",
        "extracted_critical_facts": [],
    }

    engine = _make_engine(_FakeLLM(llm_resp), redis)
    chat_id = 200

    await engine.process("Привет", chat_id=chat_id)

    # Читаем сохранённый стейт напрямую из StateStore
    store = StateStore(redis)
    saved_state = await store.load(chat_id)

    assert pii_name not in saved_state.working_memory, (
        f"ФИО '{pii_name}' не должно храниться в Redis, "
        f"но найдено в working_memory: {saved_state.working_memory!r}"
    )
    assert pii_phone not in saved_state.working_memory, (
        f"Телефон '{pii_phone}' не должен храниться в Redis, "
        f"но найден в working_memory: {saved_state.working_memory!r}"
    )


# ---------------------------------------------------------------------------
# Тест 5: текст без PII проходит без изменений
# ---------------------------------------------------------------------------


async def test_no_pii_text_unchanged(redis):
    """
    Если LLM-ответ не содержит ПДн, поля стейта не должны изменяться
    (кроме обрезки по wm_max_chars, которая здесь не актуальна).
    """
    state = _base_state(chat_id=103)

    clean_wm = "Обсуждаем стратегию. Клиент сравнивает с конкурентом."
    clean_assessment = "Активные переговоры"
    clean_summary = "Менеджер спросил о скидке. Рекомендовано не давать."

    llm_response = {
        "new_working_memory": clean_wm,
        "new_assessment": clean_assessment,
        "new_conversation_summary": clean_summary,
    }

    engine = _make_engine(_FakeLLM(llm_response), redis)
    new_state = await engine._apply_llm_updates(state, llm_response)

    assert new_state.working_memory == clean_wm
    assert new_state.agent_assessment == clean_assessment
    assert new_state.conversation_summary == clean_summary

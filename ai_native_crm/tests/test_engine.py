"""
Тесты AgentEngine — 3 теста.
Используем FakeLLM вместо реального LLM; fakeredis вместо реального Redis.
"""
import fakeredis.aioredis
from fakeredis import FakeServer

from ai_native_crm.adapters.mock import MockAdapter
from ai_native_crm.core.state_store import StateStore
from ai_native_crm.core.engine import AgentEngine
from ai_native_crm.core.response_validator import ResponseValidator
from ai_native_crm.core.action_router import ActionRouter
from ai_native_crm.core.compressor import StateCompressor
from ai_native_crm.core.drift_detector import DriftDetector
from ai_native_crm.services.lock import DistributedLock
from ai_native_crm.services.metrics import MetricsService
from ai_native_crm.services.pii_anonymizer import PIIAnonymizer


# ---------------------------------------------------------------------------
# Вспомогательный LLM-заглушка
# ---------------------------------------------------------------------------


class FakeLLM:
    """Заглушка LLM — возвращает предопределённый ответ."""

    def __init__(self, resp: dict):
        self._resp = resp

    async def call(self, messages: list[dict]) -> tuple[dict, dict]:
        return self._resp, {
            "model": "fake",
            "tokens_in": 10,
            "tokens_out": 20,
            "latency_ms": 5,
        }


# ---------------------------------------------------------------------------
# Фабрика engine
# ---------------------------------------------------------------------------


def _make_engine(llm, adapter=None, redis=None) -> AgentEngine:
    """
    Собрать AgentEngine из заглушек для тестов.

    Конструктор AgentEngine: (state_store, crm, llm, validator,
                               action_router, compressor, drift,
                               anonymizer, lock, metrics)
    """
    if redis is None:
        server = FakeServer()
        server.lua_modules = True
        redis = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    if adapter is None:
        adapter = MockAdapter()

    store = StateStore(redis)
    validator = ResponseValidator(adapter)
    action_router = ActionRouter(adapter, bot=None, state_store=store)
    compressor = StateCompressor(llm)
    drift = DriftDetector(adapter)
    metrics = MetricsService(store)
    lock = DistributedLock(redis)
    # PIIAnonymizer требует redis-клиент; при pii_enabled=False ничего не маскирует
    anonymizer = PIIAnonymizer(redis)

    return AgentEngine(
        state_store=store,
        crm=adapter,
        llm=llm,
        validator=validator,
        action_router=action_router,
        compressor=compressor,
        drift=drift,
        anonymizer=anonymizer,
        lock=lock,
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# test 1: базовая обработка — FakeLLM simple response → engine returns text
# ---------------------------------------------------------------------------


async def test_process_basic():
    """Engine возвращает текст из поля 'response' ответа LLM."""
    llm = FakeLLM({
        "response": "Привет! Чем могу помочь?",
        "actions": [],
        "new_assessment": "Первый контакт",
        "new_working_memory": "Пользователь поздоровался",
    })
    engine = _make_engine(llm)

    result = await engine.process("Привет", chat_id=1)

    assert result == "Привет! Чем могу помочь?"


# ---------------------------------------------------------------------------
# test 2: action target=crm → adapter called
# ---------------------------------------------------------------------------


async def test_process_with_crm_action():
    """
    Если LLM вернул action update_deal с существующим deal_id,
    MockAdapter должен зафиксировать вызов update_deal.
    """
    adapter = MockAdapter()

    llm = FakeLLM({
        "response": "Обновил стадию сделки d1 на WON.",
        "actions": [
            {
                "type": "update_deal",
                "target": "crm",
                "params": {
                    "deal_id": "d1",
                    "fields": {"stage": "WON"},
                },
            }
        ],
        "new_assessment": "Сделка d1 закрыта",
        "new_working_memory": "d1 переведена в WON",
    })

    engine = _make_engine(llm, adapter=adapter)
    result = await engine.process("Закрой сделку d1", chat_id=2)

    assert "d1" in result or "Обновил" in result

    # Проверяем, что update_deal был вызван с правильным deal_id
    updated_deal = adapter._deals.get("d1")
    assert updated_deal is not None
    assert updated_deal.stage == "WON"


# ---------------------------------------------------------------------------
# test 3: несуществующий deal_id → validator убирает из actions
# ---------------------------------------------------------------------------


async def test_hallucinated_deal_removed():
    """
    Если LLM «придумал» deal_id d999, который не существует в MockAdapter,
    ResponseValidator должен убрать этот action из результата.
    Engine при этом НЕ должен падать и должен вернуть текст ответа.
    """
    adapter = MockAdapter()
    # d999 точно не существует в MockAdapter

    llm = FakeLLM({
        "response": "Обновил сделку d999.",
        "actions": [
            {
                "type": "update_deal",
                "target": "crm",
                "params": {
                    "deal_id": "d999",
                    "fields": {"stage": "WON"},
                },
            }
        ],
        "new_assessment": "",
        "new_working_memory": "",
    })

    engine = _make_engine(llm, adapter=adapter)
    result = await engine.process("Закрой сделку d999", chat_id=3)

    # Engine должен вернуть текст (не упасть)
    assert isinstance(result, str)
    assert len(result) > 0

    # d999 не должна появиться в MockAdapter
    assert "d999" not in adapter._deals

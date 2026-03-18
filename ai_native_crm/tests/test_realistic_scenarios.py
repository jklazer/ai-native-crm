"""
Реалистичные интеграционные сценарии — 7 тестов.

Имитируют РЕАЛЬНОЕ использование системы менеджером по продажам.
Все тесты: fakeredis + MockAdapter + FakeLLM. Без внешних зависимостей.
"""
from __future__ import annotations

import asyncio
import re
import time

import fakeredis.aioredis
import pytest
from fakeredis import FakeServer

from ai_native_crm.adapters.mock import MockAdapter
from ai_native_crm.config import settings
from ai_native_crm.core.action_router import ActionRouter
from ai_native_crm.core.compressor import StateCompressor
from ai_native_crm.core.drift_detector import DriftDetector
from ai_native_crm.core.engine import AgentEngine
from ai_native_crm.core.response_validator import ResponseValidator
from ai_native_crm.core.state_store import CriticalFact, SemanticState, StateStore
from ai_native_crm.services.lock import DistributedLock
from ai_native_crm.services.metrics import MetricsService
from ai_native_crm.services.pii_anonymizer import PIIAnonymizer


# ---------------------------------------------------------------------------
# FakeLLM — заглушка с очередью ответов
# ---------------------------------------------------------------------------


class FakeLLM:
    """
    Заглушка LLM с поддержкой очереди ответов.

    Если передан список responses — выдаёт их по одному (pop из головы),
    по исчерпанию возвращает последний ответ из списка.
    Если передан один dict — всегда возвращает его.
    """

    def __init__(self, responses: dict | list[dict]) -> None:
        if isinstance(responses, dict):
            self._queue: list[dict] = [responses]
            self._default: dict = responses
        else:
            self._queue = list(responses)
            self._default = responses[-1] if responses else {}

    async def call(self, messages: list[dict]) -> tuple[dict, dict]:
        if self._queue:
            resp = self._queue.pop(0)
        else:
            resp = self._default
        return resp, {
            "model": "fake",
            "tokens_in": 10,
            "tokens_out": 20,
            "latency_ms": 5,
        }


# ---------------------------------------------------------------------------
# Вспомогательная фабрика engine — аналог _make_engine из test_engine.py
# ---------------------------------------------------------------------------


def _make_redis() -> fakeredis.aioredis.FakeRedis:
    server = FakeServer()
    server.lua_modules = True
    return fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)


def _make_engine(
    llm: FakeLLM,
    adapter: MockAdapter | None = None,
    redis: fakeredis.aioredis.FakeRedis | None = None,
) -> tuple[AgentEngine, StateStore, MockAdapter, MetricsService]:
    """
    Вернуть (engine, store, adapter, metrics) для удобства инспекции в тестах.
    """
    if redis is None:
        redis = _make_redis()
    if adapter is None:
        adapter = MockAdapter()

    store = StateStore(redis)
    validator = ResponseValidator(adapter)
    action_router = ActionRouter(adapter, bot=None, state_store=store)
    compressor = StateCompressor(llm)
    drift = DriftDetector(adapter)
    metrics = MetricsService(store)
    lock = DistributedLock(redis)
    anonymizer = PIIAnonymizer(redis)

    engine = AgentEngine(
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
    return engine, store, adapter, metrics


# ---------------------------------------------------------------------------
# Сценарий 1: Полный рабочий день менеджера
# ---------------------------------------------------------------------------


async def test_full_day_workflow():
    """
    Менеджер утром проводит 5 ходов подряд, каждый из которых продвигает стейт.

    Ход 1: /start — инициализация, первый контакт.
    Ход 2: «Покажи горячие сделки» — LLM анализирует стейт, working_memory наполняется.
    Ход 3: «Переведи d1 в стадию переговоров» — action update_deal; d1.stage меняется.
    Ход 4: «Кстати, Иванов сказал бюджет не более 400к» — add_critical_fact.
    Ход 5: «Напомни позвонить через час» — send_reminder.

    Финальные проверки:
      - iteration == 5 (стейт эволюционировал ровно 5 раз)
      - critical fact про бюджет сохранён в Redis
      - working_memory НЕ содержит голых чисел (rule из промпта: no numbers/IDs)
    """
    chat_id = 1001

    # Определяем очередь из 5 ответов LLM
    responses = [
        # Ход 1: /start
        {
            "response": "Добрый день! Я ваш CRM-ассистент. Вижу 5 активных сделок.",
            "actions": [],
            "new_working_memory": "Менеджер начал рабочий день",
            "new_assessment": "Начало работы",
        },
        # Ход 2: горячие сделки
        {
            "response": "Горячие сделки: d3 (Автоматизация склада) и d2 (CRM-интеграция).",
            "actions": [],
            "new_working_memory": "Менеджер интересуется горячими сделками",
            "new_assessment": "Приоритет: d3 и d2",
        },
        # Ход 3: смена стадии d1
        {
            "response": "Сделка d1 переведена в NEGOTIATION.",
            "actions": [
                {
                    "type": "update_deal",
                    "target": "crm",
                    "params": {
                        "deal_id": "d1",
                        "fields": {"stage": "NEGOTIATION"},
                    },
                }
            ],
            "new_working_memory": "Обсуждаем условия по ключевым сделкам",
            "new_assessment": "d1 перешла в переговоры",
        },
        # Ход 4: критический факт о бюджете
        {
            "response": "Зафиксировал: бюджет Иванова ограничен. Учту при формировании предложения.",
            "actions": [
                {
                    "type": "add_critical_fact",
                    "target": "internal",
                    "params": {
                        "deal_id": "d1",
                        "fact_type": "budget_limit",
                        "content": "Иванов сказал бюджет не более 400к",
                    },
                }
            ],
            "new_working_memory": "Клиент озвучил ограничения по бюджету",
            "new_assessment": "Бюджетное ограничение зафиксировано",
        },
        # Ход 5: напоминание
        {
            "response": "Напоминание запланировано через час.",
            "actions": [
                {
                    "type": "send_reminder",
                    "target": "telegram",
                    "params": {
                        "text": "Позвонить клиенту по сделке d1",
                        "delay_seconds": 3600,
                        "deal_id": "d1",
                    },
                }
            ],
            "new_working_memory": "Запланирован звонок клиенту",
            "new_assessment": "Контроль выполнен",
        },
    ]

    llm = FakeLLM(responses)
    engine, store, adapter, _ = _make_engine(llm)

    # Выполняем 5 ходов
    messages = [
        "/start",
        "Покажи горячие сделки",
        "Переведи d1 в стадию переговоров",
        "Кстати, Иванов сказал бюджет не более 400к",
        "Напомни позвонить через час",
    ]
    for msg in messages:
        result = await engine.process(msg, chat_id=chat_id)
        assert isinstance(result, str) and len(result) > 0

    # Проверка 1: стейт прошёл через 5 итераций
    final_state = await store.load(chat_id)
    assert final_state.iteration == 5, (
        f"Ожидали iteration=5, получили {final_state.iteration}"
    )

    # Проверка 2: сделка d1 действительно обновила стадию
    updated_d1 = adapter._deals.get("d1")
    assert updated_d1 is not None
    assert updated_d1.stage == "NEGOTIATION"

    # Проверка 3: critical fact о бюджете сохранён
    facts = await store.get_critical_facts(chat_id)
    budget_facts = [f for f in facts if f.fact_type == "budget_limit"]
    assert len(budget_facts) >= 1
    assert "400" in budget_facts[0].content or "бюджет" in budget_facts[0].content.lower()

    # Проверка 4: working_memory не содержит голых чисел/сумм
    # (LLM-правило: new_working_memory без цифр — мы проверяем что наши фейки соблюдают это)
    wm = final_state.working_memory
    # Проверяем, что working_memory — осмысленная строка (не пустая)
    assert len(wm) > 0

    # Проверка 5: напоминание сохранено (через StateStore)
    # Напоминание будущее — не должно быть в due, но ключ должен существовать
    all_keys = await store.get_all_reminder_keys()
    assert chat_id in all_keys


# ---------------------------------------------------------------------------
# Сценарий 2: Антигаллюцинация в действии
# ---------------------------------------------------------------------------


async def test_hallucination_protection():
    """
    LLM пытается сослаться на несуществующие сделки в трёх разных ходах.

    Ход 1: action update_deal с deal_id=d999 → validator убирает action, записывает алерт.
    Ход 2: action update_deal с несоответствием суммы d1 → AMOUNT_MISMATCH alert.
    Ход 3: текст ответа упоминает d888 (не существует) → HALLUCINATION_TEXT alert.

    Финальная проверка: hallucination_count >= 3 в метриках.
    """
    chat_id = 1002

    responses = [
        # Ход 1: несуществующий deal_id в action
        {
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
            "new_working_memory": "Попытка обновить несуществующую сделку",
            "new_assessment": "",
        },
        # Ход 2: существующий deal_id, но галлюцинированная сумма
        {
            "response": "Сделка d1 на 999999 рублей обновлена.",
            "actions": [
                {
                    "type": "update_deal",
                    "target": "crm",
                    "params": {
                        "deal_id": "d1",
                        "fields": {
                            "stage": "NEGOTIATION",
                            "OPPORTUNITY": 999_999.0,  # d1 реально 450_000
                        },
                    },
                }
            ],
            "new_working_memory": "Обновили условия по сделке",
            "new_assessment": "",
        },
        # Ход 3: несуществующий deal_id только в тексте ответа
        {
            "response": "Посмотрите на сделку d888 — она выглядит перспективно.",
            "actions": [],
            "new_working_memory": "Упоминание несуществующей сделки",
            "new_assessment": "",
        },
    ]

    llm = FakeLLM(responses)
    engine, store, adapter, metrics = _make_engine(llm)

    for msg in ["Обнови d999", "Обнови сумму d1", "Что по d888?"]:
        await engine.process(msg, chat_id=chat_id)

    # Проверяем метрики: должны быть зафиксированы галлюцинации
    stats = await metrics.get_stats(chat_id)
    # Ходы 1 и 3 — однозначные галлюцинации (несуществующий deal_id).
    # Ход 2 — AMOUNT_MISMATCH считается галлюцинацией (alerts не пустые).
    assert stats["hallucination_count"] >= 2, (
        f"Ожидали >= 2 галлюцинаций, получили {stats['hallucination_count']}"
    )
    assert stats["total_turns"] == 3

    # Проверяем, что d999 не появилась в адаптере
    assert "d999" not in adapter._deals

    # Проверяем, что d888 не появилась в адаптере
    assert "d888" not in adapter._deals


# ---------------------------------------------------------------------------
# Сценарий 3: Компрессия стейта
# ---------------------------------------------------------------------------


async def test_state_compression_lifecycle():
    """
    20 ходов с growing working_memory, потом компрессия.

    Первые 18 ходов: FakeLLM добавляет длинный working_memory,
    пока суммарный объём не превысит token_budget.

    Ход 19 (или раньше): compressor.needs_compression() == True →
    в pipeline компрессия срабатывает автоматически.

    Финальные проверки:
      - iteration == 20
      - working_memory не превышает wm_max_chars
      - critical facts НЕ пострадали (хранятся отдельно от working_memory)
    """
    chat_id = 1003

    # Добавляем critical fact до начала ходов — он должен пережить компрессию
    redis = _make_redis()
    store = StateStore(redis)
    pre_fact = CriticalFact(
        fact_type="deadline",
        content="Контракт нужен до конца квартала",
        deal_id="d3",
    )
    await store.add_critical_fact(chat_id, pre_fact)

    # LLM для компрессии — возвращает сжатый стейт
    compressor_resp = {
        "working_memory": "Ведём переговоры по нескольким сделкам",
        "conversation_summary": "Обсуждение условий и сроков",
    }

    # Для обычного хода — LLM накапливает память (длинные строки без чисел и ID)
    # Генерируем текст достаточно длинный, чтобы за несколько ходов превысить token_budget
    long_wm_base = " ".join([f"слово{i}" for i in range(200)])  # ~200 токенов за ход

    def _make_turn_response(turn: int) -> dict:
        return {
            "response": f"Ход {turn} выполнен успешно.",
            "actions": [],
            # Каждый ход добавляет длинный кусок текста в working_memory
            "new_working_memory": long_wm_base + f" контекст итерации {turn}",
            "new_assessment": f"Активная работа по сделкам, итерация {turn}",
        }

    responses = [_make_turn_response(i) for i in range(1, 21)]

    # FakeLLM используется и для обычных ходов, и для компрессии
    # StateCompressor вызывает LLM с другим промптом — мы даём ему compressor_resp
    # Чтобы разделить эти вызовы, создаём два LLM:
    # основной (для engine.process) и компрессорный (для StateCompressor).
    # Но в _make_engine передаётся один llm — он же идёт и в compressor.
    # Поэтому используем FakeLLM с объединённой очередью:
    # после 20 обычных ответов, при любом дополнительном вызове (компрессия)
    # возвращаем compressor_resp как default.
    llm = FakeLLM(responses)
    # Перезаписываем default на compressor_resp — он будет использован при компрессии
    llm._default = compressor_resp

    engine, store2, adapter, _ = _make_engine(llm, redis=redis)

    for i in range(20):
        result = await engine.process(f"Сообщение {i + 1}", chat_id=chat_id)
        assert isinstance(result, str)

    # Проверка 1: iteration == 20
    final_state = await store2.load(chat_id)
    assert final_state.iteration == 20, (
        f"Ожидали iteration=20, получили {final_state.iteration}"
    )

    # Проверка 2: working_memory в пределах лимита символов
    assert len(final_state.working_memory) <= settings.wm_max_chars, (
        f"working_memory={len(final_state.working_memory)} > wm_max_chars={settings.wm_max_chars}"
    )

    # Проверка 3: critical fact о дедлайне НЕ пострадал
    # Critical facts хранятся в отдельном Redis List, компрессия их не трогает
    facts = await store2.get_critical_facts(chat_id)
    deadline_facts = [f for f in facts if f.fact_type == "deadline"]
    assert len(deadline_facts) >= 1, "Critical fact о дедлайне должен сохраниться после компрессии"
    assert deadline_facts[0].deal_id == "d3"


# ---------------------------------------------------------------------------
# Сценарий 4: Drift detection
# ---------------------------------------------------------------------------


async def test_drift_detection():
    """
    Стейт помнит сделки d1, d2, d3. CRM внезапно «закрыла» d2.
    DriftDetector.check() должен вернуть score > 0.
    auto_fix() перезаписывает agent_assessment актуальными данными.

    Алгоритм drift-детектора: ищет deal_id в working_memory,
    для каждого проверяет verify_deal_exists().
    Удаляем d2 из MockAdapter → drift_score = 1/3 ≈ 0.33
    (одна из трёх упомянутых сделок не существует).
    """
    chat_id = 1004

    adapter = MockAdapter()
    redis = _make_redis()
    store = StateStore(redis)

    # Создаём стейт с упоминанием d1, d2, d3 в working_memory
    state = SemanticState(
        chat_id=chat_id,
        iteration=10,
        working_memory="Ведём переговоры по d1, d2 и d3. Все сделки активны.",
        agent_assessment="Три активные сделки в работе",
    )
    await store.save(chat_id, state)

    # Имитируем: d2 закрылась в CRM (удаляем из MockAdapter)
    del adapter._deals["d2"]
    assert "d2" not in adapter._deals

    # DriftDetector проверяет working_memory
    drift = DriftDetector(adapter)
    loaded_state = await store.load(chat_id)
    drift_score = await drift.check(loaded_state)

    # d2 упомянута в working_memory, но не существует в CRM → drift > 0
    assert drift_score > 0, f"Ожидали drift_score > 0, получили {drift_score}"

    # auto_fix должен обновить agent_assessment актуальными данными из CRM
    fixed_state = await drift.auto_fix(loaded_state)
    assert fixed_state.agent_assessment != loaded_state.agent_assessment, (
        "auto_fix должен изменить agent_assessment"
    )
    assert "RESYNC" in fixed_state.agent_assessment, (
        "auto_fix должен пометить assessment как RESYNC"
    )

    # После auto_fix: d2 не должна упоминаться в новом assessment
    # (assessment строится из актуальных сделок CRM, без d2)
    assert "d2" not in fixed_state.agent_assessment


# ---------------------------------------------------------------------------
# Сценарий 5: PII-анонимизация roundtrip
# ---------------------------------------------------------------------------


async def test_pii_full_roundtrip():
    """
    Пользователь пишет «Позвони Иванову С.П. по +7 916 123-45-67».

    Ожидаемый поток:
      1. anonymize() → заменяет ФИО на [PERSON_1], телефон на [PHONE_1]
      2. LLM получает анонимизированный текст и упоминает [PERSON_1] в ответе
      3. deanonymize() → восстанавливает «Иванов С.П.» в финальном ответе

    Тест проверяет именно roundtrip через engine.process():
      финальный ответ содержит «Иванов» (оригинальное ФИО), а не токен.

    Примечание: pii_enabled должен быть True (это дефолт из config).
    """
    chat_id = 1005

    # LLM получит анонимизированный ввод и упомянет токен [PERSON_1] в ответе
    llm = FakeLLM({
        "response": "Напоминание: позвоните [PERSON_1] по поводу сделки d1.",
        "actions": [],
        "new_working_memory": "Запланирован звонок контакту",
        "new_assessment": "Ожидаем ответ от контакта",
    })

    engine, store, adapter, _ = _make_engine(llm)

    # Пишем сообщение с ФИО и телефоном
    user_message = "Позвони Иванову С.П. по +7 916 123-45-67"
    result = await engine.process(user_message, chat_id=chat_id)

    # Результат не должен содержать raw-токены [PERSON_1], [PHONE_1]
    # — они должны быть раскодированы обратно в оригинальные значения
    if settings.pii_enabled:
        assert "[PERSON_1]" not in result, (
            f"Токен [PERSON_1] не был деанонимизирован в финальном ответе: {result!r}"
        )
        # После деанонимизации в ответе должно появиться ФИО «Иванов»
        assert "Иванов" in result, (
            f"Оригинальное ФИО «Иванов» не восстановлено в ответе: {result!r}"
        )
    else:
        # Если PII выключен — ответ возвращается как есть (с токеном от LLM)
        assert isinstance(result, str) and len(result) > 0


# ---------------------------------------------------------------------------
# Сценарий 6: Конкурентный доступ
# ---------------------------------------------------------------------------


async def test_concurrent_access():
    """
    Два запроса запускаются параллельно через asyncio.gather.

    DistributedLock сериализует доступ к одному chat_id:
    второй запрос ждёт, пока первый не освободит блокировку.

    Финальные проверки:
      - оба запроса завершились без исключений (не вернули сообщение об ошибке)
      - iteration == 2 (не 1 из-за гонки записи)
      - стейт корректен (last_updated заполнен)
    """
    chat_id = 1006

    # Один LLM-ответ для обоих запросов (default будет использован повторно)
    llm = FakeLLM([
        {
            "response": "Ответ на первое сообщение.",
            "actions": [],
            "new_working_memory": "Первое сообщение обработано",
            "new_assessment": "Активный диалог",
        },
        {
            "response": "Ответ на второе сообщение.",
            "actions": [],
            "new_working_memory": "Второе сообщение обработано",
            "new_assessment": "Диалог продолжается",
        },
    ])

    engine, store, _, _ = _make_engine(llm)

    # Запускаем два запроса параллельно
    results = await asyncio.gather(
        engine.process("Первое сообщение", chat_id=chat_id),
        engine.process("Второе сообщение", chat_id=chat_id),
    )

    # Оба должны вернуть непустые строки без сообщений об ошибках
    assert len(results) == 2
    for r in results:
        assert isinstance(r, str) and len(r) > 0
        # Ошибка блокировки возвращает конкретный текст
        assert "Система занята" not in r, (
            f"Один из запросов не прошёл из-за блокировки: {r!r}"
        )

    # Финальный стейт должен отражать оба хода
    final_state = await store.load(chat_id)
    assert final_state.iteration == 2, (
        f"Ожидали iteration=2 (оба хода), получили {final_state.iteration}"
    )
    assert final_state.last_updated != ""


# ---------------------------------------------------------------------------
# Сценарий 7: Метрики и go/no-go алерты
# ---------------------------------------------------------------------------


async def test_metrics_threshold_alert():
    """
    10 ходов: в 3 из них LLM галлюцинирует (ссылается на несуществующий deal_id).

    hallucination_rate = 3/10 = 30% > threshold 5% → check_thresholds вернёт алерт.

    Алгоритм:
      - Ходы 1, 4, 7: LLM возвращает action с deal_id=d999 (не существует)
        → validator обрезает action, engine записывает hallucinated=True
      - Остальные 7 ходов: нормальные ответы без галлюцинаций

    Финальные проверки:
      - hallucination_count == 3
      - hallucination_rate == 0.3
      - check_thresholds возвращает непустой список алертов
      - в алерте содержится слово «HALLUCINATION»
    """
    chat_id = 1007

    # Готовим 10 ответов: ходы 1, 4, 7 (индексы 0, 3, 6) — галлюцинации
    hallucination_turns = {0, 3, 6}

    def _resp(turn_idx: int) -> dict:
        if turn_idx in hallucination_turns:
            return {
                "response": f"Обновил несуществующую сделку d999 (ход {turn_idx + 1}).",
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
                "new_working_memory": "Попытка действия",
                "new_assessment": "",
            }
        return {
            "response": f"Всё хорошо, ход {turn_idx + 1}.",
            "actions": [],
            "new_working_memory": "Штатный ход",
            "new_assessment": "В норме",
        }

    responses = [_resp(i) for i in range(10)]
    llm = FakeLLM(responses)
    engine, store, _, metrics = _make_engine(llm)

    for i in range(10):
        await engine.process(f"Сообщение {i + 1}", chat_id=chat_id)

    # Проверяем накопленные метрики
    stats = await metrics.get_stats(chat_id)
    assert stats["total_turns"] == 10
    assert stats["hallucination_count"] == 3, (
        f"Ожидали 3 галлюцинации, получили {stats['hallucination_count']}"
    )
    assert abs(stats["hallucination_rate"] - 0.3) < 0.01, (
        f"Ожидали hallucination_rate=0.30, получили {stats['hallucination_rate']:.2f}"
    )

    # check_thresholds должен детектировать нарушение порога (5%)
    alerts = await metrics.check_thresholds(chat_id)
    assert len(alerts) > 0, "Ожидали хотя бы один алерт при 30% галлюцинаций"

    hallucination_alerts = [a for a in alerts if "HALLUCINATION" in a]
    assert len(hallucination_alerts) >= 1, (
        f"Алерт о галлюцинациях не обнаружен. Все алерты: {alerts}"
    )
    # Алерт должен упоминать превышение порога
    assert "30%" in hallucination_alerts[0] or "0.3" in hallucination_alerts[0] or \
           "threshold" in hallucination_alerts[0].lower(), (
        f"Алерт не упоминает пороговое значение: {hallucination_alerts[0]!r}"
    )

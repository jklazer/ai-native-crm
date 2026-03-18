"""
AgentEngine — оркестратор одного хода диалога CRM-агента.

10 шагов pipeline:
  1.  DistributedLock.lock(chat_id) — защита от параллельных запросов
  2.  StateStore.load(chat_id) — загрузка стейта из Redis
  3.  CRM.get_deals() — актуальные сделки (source of truth)
  4.  StateStore.get_critical_facts() — важные бизнес-факты
  5.  PIIAnonymizer.anonymize() — маскирование ПДн перед LLM
  6.  LLMClient.call() — вызов LLM с полным промптом
  7.  ResponseValidator.validate() — проверка галлюцинаций
  8.  ActionRouter.execute_batch() — выполнение действий
  9.  StateStore.save() — сохранение обновлённого стейта
  10. StateStore.log_audit() + MetricsService — аудит и метрики

Компрессия и drift-check встраиваются между шагами 5 и 6.
Никакого PostgreSQL, никакого SQL.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import replace
from typing import Any

from redis.exceptions import RedisError

from ai_native_crm.adapters.base import CRMAdapter, DealInfo
from ai_native_crm.config import settings
from ai_native_crm.core.action_router import ActionRouter
from ai_native_crm.core.compressor import StateCompressor
from ai_native_crm.core.drift_detector import DriftDetector
from ai_native_crm.core.response_validator import ResponseValidator
from ai_native_crm.core.state_store import AuditEntry, CriticalFact, SemanticState, StateStore
from ai_native_crm.services.llm_client import LLMClient, LLMError
from ai_native_crm.services.lock import DistributedLock, LockAcquireError
from ai_native_crm.services.metrics import MetricsService
from ai_native_crm.services.pii_anonymizer import PIIAnonymizer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Системный промпт — определяет роль агента, формат ответа и anti-hallucination rules
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a CRM assistant for a sales manager. You analyze VERIFIED FACTS from the CRM system and SEMANTIC CONTEXT, then respond and suggest actions.

## RESPONSE FORMAT
Return ONLY valid JSON, no markdown:
{
  "response": "ответ менеджеру на русском, до 300 символов",
  "actions": [
    {"type": "update_deal", "target": "crm", "params": {"deal_id": "d1", "fields": {"STAGE_ID": "NEGOTIATION"}}},
    {"type": "send_reminder", "target": "telegram", "params": {"text": "Позвонить", "delay_seconds": 3600}},
    {"type": "add_critical_fact", "target": "internal", "params": {"deal_id": "d1", "fact_type": "budget_limit", "content": "Бюджет не более 500к"}}
  ],
  "new_working_memory": "контекст БЕЗ чисел, ID, сумм — только смысл",
  "new_assessment": "оценка ситуации",
  "new_conversation_summary": "краткое резюме всего диалога",
  "extracted_critical_facts": ["Клиент ограничен бюджетом 500к"]
}

## RULES
1. deal_id — ONLY from VERIFIED FACTS. NEVER invent.
2. Amounts — ONLY from VERIFIED FACTS. No rounding.
3. new_working_memory — NO numbers, IDs, amounts.
4. Missing data → "данных нет в системе".
5. actions can be [].
6. response in Russian.
7. Always extract critical facts.

## CORRECT EXAMPLE
Facts: Deal d1 "Внедрение 1С", 450000 RUB, PREPARATION
Q: "Стоит ли дать скидку?"
{"response": "Сделка d1 на 450 000 ₽. Не давайте скидку — бюджет подтверждён.", "actions": [], "new_working_memory": "Обсуждаем стратегию. Клиент сравнивает с конкурентом.", "new_assessment": "Активные переговоры", "new_conversation_summary": "Менеджер спросил о скидке по сделке d1. Рекомендовано не давать скидку.", "extracted_critical_facts": []}

## HALLUCINATION (FORBIDDEN!)
{"response": "Сделка d99..."} — d99 NOT in facts!
{"response": "Сделка d1 на 460 000 ₽..."} — Facts say 450000!
"""


class AgentEngine:
    """
    Оркестратор хода CRM-агента.

    Все зависимости передаются в конструктор (Dependency Injection) —
    нет скрытого состояния, нет обращений к global/singleton, нет SQL.

    Сигнатура конструктора совпадает с порядком из ТЗ:
    state_store, crm, llm, validator, action_router, compressor, drift,
    anonymizer, lock, metrics
    """

    def __init__(
        self,
        state_store: StateStore,
        crm: CRMAdapter,
        llm: LLMClient,
        validator: ResponseValidator,
        action_router: ActionRouter,
        compressor: StateCompressor,
        drift: DriftDetector,
        anonymizer: PIIAnonymizer,
        lock: DistributedLock,
        metrics: MetricsService,
    ) -> None:
        self._store = state_store
        self._crm = crm
        self._llm = llm
        self._validator = validator
        self._router = action_router
        self._compressor = compressor
        self._drift = drift
        self._anonymizer = anonymizer
        self._lock = lock
        self._metrics = metrics

    async def process(self, user_input: str, chat_id: int) -> str:
        """
        Обработать одно сообщение менеджера за 10 шагов.

        Захватывает distributed lock на chat_id, выполняет полный pipeline
        и возвращает текст ответа для отправки в Telegram.
        При критической ошибке возвращает user-friendly сообщение об ошибке.
        """
        try:
            async with self._lock.lock(chat_id):
                return await self._run_pipeline(user_input, chat_id)
        except LockAcquireError:
            logger.warning("Lock не получен для chat_id=%d", chat_id)
            return "Система занята, повторите через несколько секунд."
        except Exception as exc:
            logger.error(
                "AgentEngine: критическая ошибка chat_id=%d: %s",
                chat_id, exc, exc_info=True,
            )
            return "Произошла внутренняя ошибка. Попробуйте повторить запрос."

    def _build_prompt(
        self,
        deals: list[DealInfo],
        critical_facts: list[CriticalFact],
        state: SemanticState,
        user_input: str,
    ) -> list[dict[str, str]]:
        """
        Собрать messages для LLM.

        User-сообщение разбито на 4 блока:
          VERIFIED FACTS   — актуальные сделки из CRM (source of truth)
          CRITICAL FACTS   — бизнес-факты, накопленные за историю диалога
          SEMANTIC CONTEXT — working_memory и assessment из Redis-стейта
          MESSAGE          — вопрос/команда менеджера
        """
        # --- VERIFIED FACTS ---
        deals_data = [
            {
                "deal_id": d.id,
                "title": d.title,
                "stage": d.stage,
                "amount": d.amount,
                "currency": d.currency,
                "contact": d.contact_name,
            }
            for d in deals
        ]
        verified_facts = json.dumps(deals_data, ensure_ascii=False, indent=2)

        # --- CRITICAL FACTS ---
        if critical_facts:
            facts_lines = [
                f"- [{cf.fact_type}] deal={cf.deal_id or 'общий'}: {cf.content}"
                for cf in critical_facts
            ]
            critical_text = "\n".join(facts_lines)
        else:
            critical_text = "(нет)"

        # --- SEMANTIC CONTEXT ---
        semantic = (
            f"WORKING MEMORY:\n{state.working_memory or '(пусто)'}\n\n"
            f"ASSESSMENT:\n{state.agent_assessment or '(нет оценки)'}\n\n"
            f"CONVERSATION SUMMARY:\n{state.conversation_summary or '(нет резюме)'}\n\n"
            f"ITERATION: {state.iteration}"
        )

        user_content = (
            f"VERIFIED FACTS (CRM, source of truth):\n{verified_facts}\n\n"
            f"CRITICAL FACTS:\n{critical_text}\n\n"
            f"SEMANTIC CONTEXT:\n{semantic}\n\n"
            f"MESSAGE:\n{user_input}"
        )

        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

    # ------------------------------------------------------------------
    # Внутренний pipeline (вызывается внутри lock)
    # ------------------------------------------------------------------

    async def _run_pipeline(self, user_input: str, chat_id: int) -> str:
        t_start = time.monotonic()
        session_id = str(chat_id)

        # --- Шаг 2: загрузить SemanticState ---
        state = await self._store.load(chat_id)
        logger.info(
            "AgentEngine: chat_id=%d iteration=%d input=%d символов",
            chat_id, state.iteration, len(user_input),
        )

        # --- Шаг 3: загрузить сделки из CRM (source of truth) ---
        try:
            deals = await self._crm.get_deals()
        except Exception as exc:
            # Intentionally broad: CRM adapter catches specific errors internally
            logger.error("get_deals упало: %s", exc)
            deals = []

        # --- Шаг 4: загрузить critical facts ---
        try:
            critical_facts = await self._store.get_critical_facts(chat_id)
        except RedisError as exc:
            logger.error("get_critical_facts (Redis): %s", exc)
            critical_facts = []

        # --- Шаг 5: PII-анонимизация ---
        safe_input = await self._anonymizer.anonymize(user_input, session_id)

        # --- Между 5 и 6: компрессия стейта если нужна ---
        if self._compressor.needs_compression(state):
            state = await self._compressor.compress(state)

        # --- Между 5 и 6: drift-check каждые N итераций ---
        if self._drift.should_check(state.iteration):
            drift_score = await self._drift.check(state)
            if drift_score >= settings.drift_threshold:
                logger.warning(
                    "Drift %.2f >= threshold %.2f — auto_fix (chat_id=%d)",
                    drift_score, settings.drift_threshold, chat_id,
                )
                state = await self._drift.auto_fix(state)

        # --- Шаг 6: LLM вызов ---
        messages = self._build_prompt(deals, critical_facts, state, safe_input)
        llm_response, usage = await self._llm.call(messages)

        # Если LLM вернул невалидный JSON, _parse_json оборачивает сырой текст
        # в dict с ключом _parse_error. Обрабатываем как fallback-ответ.
        if "_parse_error" in llm_response:
            logger.warning(
                "LLM вернул невалидный JSON (chat_id=%d): %s",
                chat_id,
                llm_response.get("_parse_error"),
            )
            # Пытаемся использовать _raw как текстовый ответ, если он есть
            raw_text = llm_response.get("_raw", "")
            llm_response = {
                "response": raw_text[:500] if raw_text else "Ошибка обработки ответа LLM.",
                "actions": [],
                "new_working_memory": state.working_memory,
                "new_assessment": state.agent_assessment,
            }

        # --- Шаг 7: валидация (антигаллюцинационный фильтр) ---
        fixed_response, alerts = await self._validator.validate(llm_response, deals)
        has_hallucination = bool(alerts)

        # --- Шаг 8: выполнить actions ---
        actions: list[dict[str, Any]] = fixed_response.get("actions", [])
        action_results = await self._router.execute_batch(actions, chat_id)
        action_succeeded = all(r.success for r in action_results) if action_results else True

        # --- Process extracted_critical_facts from LLM ---
        extracted_facts = fixed_response.get("extracted_critical_facts", [])
        for fact_text in extracted_facts:
            if isinstance(fact_text, str) and fact_text.strip():
                fact = CriticalFact(
                    fact_type="extracted",
                    content=fact_text.strip(),
                )
                await self._store.add_critical_fact(chat_id, fact)

        # --- Шаг 9: обновить и сохранить стейт ---
        state = self._apply_llm_updates(state, fixed_response)
        await self._store.save(chat_id, state)

        # --- Шаг 10: аудит + метрики ---
        latency_ms = round((time.monotonic() - t_start) * 1000)
        response_text: str = fixed_response.get("response") or "Нет ответа"

        await self._write_audit(
            chat_id=chat_id,
            user_input=safe_input,
            response_text=response_text,
            action_results=action_results,
            usage=usage,
            alerts=alerts,
            latency_ms=latency_ms,
        )

        await self._metrics.record_turn(
            chat_id,
            hallucinated=has_hallucination,
            action_succeeded=action_succeeded,
            has_actions=bool(action_results),
        )

        # Деанонимизируем ответ перед показом менеджеру
        response_text = await self._anonymizer.deanonymize(response_text, session_id)

        logger.info(
            "AgentEngine: ход завершён chat_id=%d iteration=%d latency=%dмс "
            "actions=%d alerts=%d",
            chat_id, state.iteration, latency_ms, len(actions), len(alerts),
        )

        return response_text

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _apply_llm_updates(
        self, state: SemanticState, llm_response: dict[str, Any]
    ) -> SemanticState:
        """
        Применить изменения из ответа LLM к стейту.
        Не мутирует исходный state — возвращает новый объект через dataclasses.replace.
        """
        new_wm = str(llm_response.get("new_working_memory") or state.working_memory)
        new_assessment = str(llm_response.get("new_assessment") or state.agent_assessment)
        new_summary = str(llm_response.get("new_conversation_summary") or state.conversation_summary)

        # TODO [MEDIUM]: LLM-generated fields may contain PII that bypasses anonymization.
        # For production: run anonymizer on new_wm, new_assessment, new_summary before storing.
        # Current mitigation: system prompt instructs LLM not to include numbers/IDs in working_memory.

        # Ограничиваем размер полей памяти
        new_wm = new_wm[: settings.wm_max_chars]
        new_assessment = new_assessment[: settings.wm_max_chars]
        new_summary = new_summary[: settings.wm_max_chars]

        return replace(
            state,
            working_memory=new_wm,
            agent_assessment=new_assessment,
            conversation_summary=new_summary,
            iteration=state.iteration + 1,
        )

    async def _write_audit(
        self,
        chat_id: int,
        user_input: str,
        response_text: str,
        action_results: list,
        usage: dict[str, Any],
        alerts: list[str],
        latency_ms: int,
    ) -> None:
        """Записать AuditEntry в Redis Stream. Ошибка не прерывает основной поток."""
        actions_log = [
            {"type": r.action_type, "success": r.success, "details": r.details[:200]}
            for r in action_results
        ]
        if alerts:
            # Добавляем алерты как специальную запись для прозрачности
            actions_log.append({"type": "_validator_alerts", "alerts": alerts})

        entry = AuditEntry(
            chat_id=chat_id,
            user_input=user_input[:500],
            llm_response=response_text[:1000],
            actions=actions_log,
            model_used=usage.get("model", "unknown"),
            tokens_in=usage.get("tokens_in", 0),
            tokens_out=usage.get("tokens_out", 0),
            latency_ms=latency_ms,
        )
        try:
            await self._store.log_audit(chat_id, entry)
        except RedisError as exc:
            logger.error("Ошибка записи аудита (Redis): %s", exc)

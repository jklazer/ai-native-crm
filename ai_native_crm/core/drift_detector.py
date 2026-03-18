"""
Детектор дрейфа стейта — сравнивает стейт агента с реальными данными CRM.

Дрейф возникает когда агент «помнит» устаревшие данные о сделках
(например, неверный stage или сумму). Проверяется каждые N итераций
(settings.drift_check_interval) или по команде /drift.

Никакого SQL. Источник истины — CRM API.
"""
from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Any

from ai_native_crm.adapters.base import CRMAdapter, DealInfo
from ai_native_crm.config import settings
from ai_native_crm.core.state_store import SemanticState

logger = logging.getLogger(__name__)

# Паттерны для извлечения упомянутых deal_id из рабочей памяти
_RE_DEAL_ID = re.compile(r"\b(d\d+)\b", re.IGNORECASE)


class DriftDetector:
    """
    Детектирует расхождения между стейтом агента и CRM.

    check(state) → float [0, 1]
        0.0 — стейт актуален.
        1.0 — полная рассинхронизация.

    auto_fix(state) → SemanticState
        Перезаписывает agent_assessment свежими данными из CRM.

    Для обратной совместимости с внутренним использованием сохранён
    приватный метод _check_memory(working_memory) → (float, list[str]).
    """

    def __init__(self, crm: CRMAdapter) -> None:
        self._crm = crm

    def should_check(self, iteration: int) -> bool:
        """True если текущая итерация кратна drift_check_interval."""
        if settings.drift_check_interval <= 0:
            return False
        return iteration > 0 and (iteration % settings.drift_check_interval == 0)

    async def check(self, state: SemanticState) -> float:
        """
        Проверить стейт на дрейф. Возвращает float [0, 1].

        > settings.drift_threshold → вызовите auto_fix().
        """
        drift_score, issues = await self._check_memory(state.working_memory)

        if drift_score >= settings.drift_threshold:
            logger.error(
                "DriftDetector: drift=%.2f >= threshold=%.2f (chat_id=%d issues=%d)",
                drift_score, settings.drift_threshold, state.chat_id, len(issues),
            )
        elif issues:
            logger.warning(
                "DriftDetector: issues=%d drift=%.2f (chat_id=%d)",
                len(issues), drift_score, state.chat_id,
            )
        else:
            logger.debug(
                "DriftDetector: OK drift=%.2f (chat_id=%d)", drift_score, state.chat_id
            )

        return drift_score

    async def auto_fix(self, state: SemanticState) -> SemanticState:
        """
        Пересинхронизировать стейт с CRM.

        Загружает свежие сделки из CRM и обновляет agent_assessment.
        working_memory — не трогается: это задача LLM, не механического patching.
        Возвращает новый SemanticState (исходный не мутируется).
        """
        logger.info(
            "auto_fix: resync для chat_id=%d iteration=%d",
            state.chat_id, state.iteration,
        )

        try:
            crm_deals = await self._crm.get_deals()
        except Exception as exc:
            logger.error("auto_fix: ошибка загрузки сделок из CRM: %s", exc)
            return state

        summary = self._build_deals_summary(crm_deals)
        new_assessment = (
            f"[RESYNC итерация={state.iteration}] "
            f"Актуальные сделки: {summary}"
        )

        logger.info(
            "auto_fix: resync завершён, загружено %d сделок (chat_id=%d)",
            len(crm_deals), state.chat_id,
        )

        return replace(state, agent_assessment=new_assessment)

    # ------------------------------------------------------------------
    # Внутренний метод — сохранён для обратной совместимости
    # ------------------------------------------------------------------

    async def _check_memory(
        self, working_memory: str
    ) -> tuple[float, list[str]]:
        """
        Проверить рабочую память на дрейф.

        Алгоритм:
          1. Извлечь deal_id из working_memory.
          2. Для каждого deal_id проверить существование в CRM.
          3. Для найденных — проверить сумму рядом с упоминанием.
          4. Вернуть (drift_score [0,1], issues).
        """
        issues: list[str] = []
        deal_ids = list(set(_RE_DEAL_ID.findall(working_memory)))

        if not deal_ids:
            # Нет упоминаний сделок — дрейф не определить
            return 0.0, issues

        checked = 0
        for deal_id in deal_ids:
            checked += 1
            exists = await self._crm.verify_deal_exists(deal_id)
            if not exists:
                issues.append(
                    f"Сделка {deal_id} упоминается в памяти, но не найдена в CRM"
                )
                logger.warning("DriftDetector: deal_id=%s в памяти но не в CRM", deal_id)
                continue

            # Дополнительно проверяем сумму если она упомянута рядом с deal_id
            amount_in_memory = self._extract_amount_near(working_memory, deal_id)
            if amount_in_memory is not None:
                real_amount = await self._crm.get_deal_amount(deal_id)
                if real_amount is not None and real_amount > 0:
                    discrepancy = abs(amount_in_memory - real_amount) / real_amount
                    if discrepancy > 0.01:  # расхождение больше 1%
                        issues.append(
                            f"Сумма сделки {deal_id}: в памяти {amount_in_memory:.0f}, "
                            f"в CRM {real_amount:.0f} (расхождение {discrepancy:.1%})"
                        )
                        logger.warning(
                            "DriftDetector: amount drift deal_id=%s memory=%.0f crm=%.0f",
                            deal_id, amount_in_memory, real_amount,
                        )

        drift_score = len(issues) / checked if checked > 0 else 0.0
        return drift_score, issues

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _extract_amount_near(
        self, text: str, deal_id: str
    ) -> float | None:
        """
        Попытаться извлечь сумму из фрагмента текста рядом с deal_id.
        Ищем числа в радиусе 100 символов от упоминания deal_id.
        Returns None если сумму не нашли.
        """
        # Найдём позицию упоминания deal_id
        match = re.search(rf"\b{re.escape(deal_id)}\b", text, re.IGNORECASE)
        if not match:
            return None

        start = max(0, match.start() - 100)
        end = min(len(text), match.end() + 100)
        fragment = text[start:end]

        # Ищем числа с единицами валюты
        amount_match = re.search(
            r"(\d[\d\s]*\d|\d)\s*(?:руб(?:лей|ля)?|RUB)\b",
            fragment,
            re.IGNORECASE | re.UNICODE,
        )
        if amount_match:
            # Убираем пробелы из числа (1 200 000 → 1200000)
            raw_num = re.sub(r"\s", "", amount_match.group(1))
            try:
                return float(raw_num)
            except ValueError:
                pass

        return None

    @staticmethod
    def _build_deals_summary(deals: list[DealInfo]) -> str:
        """Компактная сводка сделок для записи в agent_assessment после resync."""
        if not deals:
            return "сделок нет"
        parts = [f"{d.id}({d.stage})" for d in deals[:10]]
        suffix = f" и ещё {len(deals) - 10}" if len(deals) > 10 else ""
        return ", ".join(parts) + suffix

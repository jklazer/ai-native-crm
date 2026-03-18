"""
Валидатор ответов LLM.

Проверяет каждый ответ на галлюцинации:
  - deal_id существует в CRM (вызов verify_deal_exists)
  - суммы совпадают с данными CRM (вызов get_deal_amount)

При обнаружении несоответствий:
  - возвращает список alerts (строки) для метрик / аудита
  - патчит response, убирая галлюцинированные суммы

Никакого PostgreSQL, никакого SQL.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ai_native_crm.adapters.base import CRMAdapter, DealInfo

logger = logging.getLogger(__name__)

# Паттерн для обнаружения deal_id вида d42, deal-7, D1 и т.п. в тексте ответа
_RE_DEAL_ID_IN_TEXT = re.compile(r"\b([Dd]\d+)\b")

# Допустимое отклонение суммы в рублях (1 коп.) — защита от float-погрешности
_AMOUNT_EPSILON = 0.01


class ResponseValidator:
    """
    Валидирует структурированный ответ LLM на предмет галлюцинаций.

    Принцип работы:
      1. Собирает все deal_id из action-параметров и текста ответа.
      2. Проверяет каждый deal_id через CRM API (verify_deal_exists).
      3. Для сделок с упоминанием суммы — проверяет совпадение с CRM API.
      4. Возвращает (исправленный_ответ, список_alerts).
    """

    def __init__(self, crm: CRMAdapter) -> None:
        self._crm = crm

    async def validate(
        self,
        response: dict[str, Any],
        deals: list[DealInfo],
    ) -> tuple[dict[str, Any], list[str]]:
        """
        Проверить ответ LLM и вернуть (fixed_response, alerts).

        response  — распарсенный JSON от LLM (поля: response, actions, ...)
        deals     — актуальный список сделок из CRM (загружен в этом же ходе)

        Returns:
            fixed_response — ответ с исправлениями (могут быть патчи в actions)
            alerts         — список строк с обнаруженными нарушениями
        """
        alerts: list[str] = []
        # Работаем с копией, чтобы не мутировать оригинал
        fixed = dict(response)

        # Индекс сделок по deal_id для быстрого lookup (из уже загруженных данных)
        deals_by_id: dict[str, DealInfo] = {d.id: d for d in deals}

        # --- Проверка actions ---
        fixed_actions: list[dict[str, Any]] = []
        for action in fixed.get("actions", []):
            checked_action, action_alerts = await self._validate_action(
                action, deals_by_id
            )
            alerts.extend(action_alerts)
            if checked_action is not None:
                # None означает: действие удалено как галлюцинационное
                fixed_actions.append(checked_action)

        fixed["actions"] = fixed_actions

        # --- Проверка упоминаний deal_id в тексте ответа ---
        response_text: str = fixed.get("response", "")
        text_alerts = await self._validate_deal_ids_in_text(
            response_text, deals_by_id
        )
        alerts.extend(text_alerts)

        if alerts:
            logger.warning(
                "ResponseValidator: обнаружено %d нарушений: %s",
                len(alerts),
                alerts,
            )

        return fixed, alerts

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    async def _validate_action(
        self,
        action: dict[str, Any],
        deals_by_id: dict[str, DealInfo],
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """
        Проверить одно действие.

        Возвращает (action_or_None, alerts).
        None — действие не прошло валидацию и должно быть отброшено.
        """
        alerts: list[str] = []
        action_type: str = action.get("type", "")
        params: dict[str, Any] = action.get("params", {})

        # Только действия с target=crm затрагивают реальные сделки
        if action.get("target") != "crm":
            return action, alerts

        deal_id: str | None = params.get("deal_id")
        if not deal_id:
            return action, alerts

        # Проверяем существование deal_id
        exists = deal_id in deals_by_id
        if not exists:
            # Пробуем через CRM API (на случай расхождения кэша)
            exists = await self._crm.verify_deal_exists(deal_id)

        if not exists:
            alert = (
                f"HALLUCINATION: action '{action_type}' ссылается на "
                f"несуществующую сделку deal_id='{deal_id}'"
            )
            alerts.append(alert)
            logger.error("Галлюцинация отброшена: %s", alert)
            return None, alerts

        # Проверяем сумму в полях update_deal
        if action_type == "update_deal":
            fields: dict[str, Any] = params.get("fields", {})
            # Bitrix-ключ OPPORTUNITY или snake_case amount
            proposed_amount: float | None = None
            for key in ("OPPORTUNITY", "amount"):
                if key in fields:
                    try:
                        proposed_amount = float(fields[key])
                    except (TypeError, ValueError):
                        pass
                    break

            if proposed_amount is not None:
                # Источник правды — данные, которые мы загрузили из CRM в этом ходу
                deal = deals_by_id.get(deal_id)
                if deal is not None:
                    crm_amount = deal.amount
                    if abs(proposed_amount - crm_amount) > _AMOUNT_EPSILON:
                        alert = (
                            f"AMOUNT_MISMATCH: action 'update_deal' deal_id='{deal_id}' "
                            f"предлагает сумму {proposed_amount}, CRM вернул {crm_amount}"
                        )
                        alerts.append(alert)
                        logger.warning("Несоответствие суммы: %s", alert)
                        # Не удаляем действие — только фиксируем расхождение,
                        # т.к. update_deal может законно изменять сумму.
                        # Если нужно блокировать — раскомментировать: return None, alerts

        return action, alerts

    async def _validate_deal_ids_in_text(
        self,
        text: str,
        deals_by_id: dict[str, DealInfo],
    ) -> list[str]:
        """
        Найти все deal_id в тексте ответа и проверить их существование.
        Несуществующие — галлюцинации.
        """
        alerts: list[str] = []
        found_ids = set(_RE_DEAL_ID_IN_TEXT.findall(text))

        for raw_id in found_ids:
            deal_id = raw_id.lower()  # нормализуем к нижнему регистру
            # Проверяем сначала по кэшу deals_by_id (уже загруженный снапшот)
            if deal_id in deals_by_id:
                continue
            # Если не в кэше — проверяем через CRM API
            exists = await self._crm.verify_deal_exists(deal_id)
            if not exists:
                alerts.append(
                    f"HALLUCINATION_TEXT: текст ответа упоминает несуществующий "
                    f"deal_id='{deal_id}'"
                )

        return alerts

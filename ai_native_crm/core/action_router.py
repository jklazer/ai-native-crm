"""
Роутер действий — исполняет actions из ответа LLM.

Три типа targets:
  crm      → CRM-адаптер (update_deal, create_deal)
  telegram → aiogram Bot (send_reminder, send_message)
  internal → StateStore (add_critical_fact)

Никакого PostgreSQL, никакого SQL.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ai_native_crm.adapters.base import CRMAdapter
from ai_native_crm.core.state_store import CriticalFact, StateStore

logger = logging.getLogger(__name__)


@dataclass
class ActionResult:
    """Результат выполнения одного action."""

    success: bool
    action_type: str
    details: str = ""


class ActionRouter:
    """
    Маршрутизирует и выполняет action-объекты из ответа LLM.

    Схема action (JSON):
        {
            "type":   "update_deal" | "send_reminder" | "add_critical_fact" | ...,
            "target": "crm" | "telegram" | "internal",
            "params": { ... }
        }

    Неизвестный target → ActionResult(success=False, details="unknown target").
    """

    def __init__(
        self,
        crm: CRMAdapter,
        bot: Any,          # aiogram Bot — Any чтобы не тянуть aiogram в core-импорты
        state_store: StateStore,
    ) -> None:
        self._crm = crm
        self._bot = bot
        self._state_store = state_store

    async def execute(self, action: dict[str, Any], chat_id: int) -> ActionResult:
        """
        Выполнить одно действие.

        action  — словарь с ключами type, target, params
        chat_id — идентификатор чата (нужен для telegram / internal)
        """
        action_type: str = action.get("type", "unknown")
        target: str = action.get("target", "")
        params: dict[str, Any] = action.get("params", {})

        logger.info(
            "ActionRouter.execute: type=%s target=%s chat_id=%d",
            action_type, target, chat_id,
        )

        try:
            if target == "crm":
                return await self._execute_crm(action_type, params)
            elif target == "telegram":
                return await self._execute_telegram(action_type, params, chat_id)
            elif target == "internal":
                return await self._execute_internal(action_type, params, chat_id)
            else:
                msg = f"Неизвестный target='{target}' для action type='{action_type}'"
                logger.warning(msg)
                return ActionResult(success=False, action_type=action_type, details=msg)

        except Exception as exc:
            # Intentionally broad: router must never crash the calling pipeline regardless
            # of what the underlying CRM, Telegram, or store raises.
            msg = f"Исключение при выполнении action type='{action_type}': {exc}"
            logger.error(msg, exc_info=True)
            return ActionResult(success=False, action_type=action_type, details=msg)

    async def execute_batch(
        self,
        actions: list[dict[str, Any]],
        chat_id: int,
    ) -> list[ActionResult]:
        """
        Выполнить список действий последовательно.
        Ошибка одного action не прерывает выполнение остальных.
        """
        results: list[ActionResult] = []
        for action in actions:
            result = await self.execute(action, chat_id)
            results.append(result)
            if not result.success:
                logger.warning(
                    "Action не выполнен: type=%s details=%s",
                    result.action_type,
                    result.details,
                )
        return results

    # ------------------------------------------------------------------
    # CRM-actions (target=crm)
    # ------------------------------------------------------------------

    async def _execute_crm(
        self, action_type: str, params: dict[str, Any]
    ) -> ActionResult:
        """Направить action в CRM-адаптер."""

        if action_type == "update_deal":
            deal_id: str = str(params.get("deal_id", ""))
            fields: dict = params.get("fields", {})
            if not deal_id:
                return ActionResult(
                    success=False,
                    action_type=action_type,
                    details="params.deal_id не указан",
                )
            if not fields:
                return ActionResult(
                    success=False,
                    action_type=action_type,
                    details="params.fields пустые — нечего обновлять",
                )
            ok = await self._crm.update_deal(deal_id, fields)
            return ActionResult(
                success=ok,
                action_type=action_type,
                details=f"deal_id={deal_id} fields={list(fields.keys())} ok={ok}",
            )

        elif action_type == "create_deal":
            data: dict = params.get("data", params)  # data может быть плоским params
            deal_id = await self._crm.create_deal(data)
            return ActionResult(
                success=bool(deal_id),
                action_type=action_type,
                details=f"создана сделка deal_id={deal_id}",
            )

        else:
            msg = f"Неизвестный CRM action_type='{action_type}'"
            logger.warning(msg)
            return ActionResult(success=False, action_type=action_type, details=msg)

    # ------------------------------------------------------------------
    # Telegram-actions (target=telegram)
    # ------------------------------------------------------------------

    async def _execute_telegram(
        self, action_type: str, params: dict[str, Any], chat_id: int
    ) -> ActionResult:
        """Отправить сообщение или запланировать напоминание."""

        if action_type == "send_message":
            text: str = params.get("text", "")
            if not text:
                return ActionResult(
                    success=False,
                    action_type=action_type,
                    details="params.text пустой",
                )
            await self._bot.send_message(chat_id, text)
            return ActionResult(
                success=True,
                action_type=action_type,
                details=f"отправлено {len(text)} символов",
            )

        elif action_type == "send_reminder":
            text = params.get("text", "Напоминание")
            # TODO [MEDIUM]: validate delay_seconds bounds (e.g., 60..2592000)
            delay_seconds: int = int(params.get("delay_seconds", 3600))
            deal_id: str | None = params.get("deal_id")
            fire_at = time.time() + delay_seconds

            await self._state_store.add_reminder(
                chat_id=chat_id,
                text=text,
                fire_at=fire_at,
                deal_id=deal_id,
            )
            return ActionResult(
                success=True,
                action_type=action_type,
                details=(
                    f"напоминание запланировано через {delay_seconds}с: '{text[:80]}'"
                ),
            )

        else:
            msg = f"Неизвестный Telegram action_type='{action_type}'"
            logger.warning(msg)
            return ActionResult(success=False, action_type=action_type, details=msg)

    # ------------------------------------------------------------------
    # Internal-actions (target=internal)
    # ------------------------------------------------------------------

    async def _execute_internal(
        self, action_type: str, params: dict[str, Any], chat_id: int
    ) -> ActionResult:
        """Записать данные во внутренний стейт (StateStore)."""

        if action_type == "add_critical_fact":
            fact_type: str = params.get("fact_type", "unknown")
            content: str = params.get("content", "")
            deal_id: str | None = params.get("deal_id")

            if not content:
                return ActionResult(
                    success=False,
                    action_type=action_type,
                    details="params.content пустой — факт не сохранён",
                )

            fact = CriticalFact(
                fact_type=fact_type,
                content=content,
                deal_id=deal_id or None,
            )
            await self._state_store.add_critical_fact(chat_id, fact)
            return ActionResult(
                success=True,
                action_type=action_type,
                details=(
                    f"факт сохранён: type={fact_type} deal_id={deal_id} "
                    f"content='{content[:80]}'"
                ),
            )

        else:
            msg = f"Неизвестный internal action_type='{action_type}'"
            logger.warning(msg)
            return ActionResult(success=False, action_type=action_type, details=msg)

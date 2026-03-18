"""
Сервис метрик качества CRM-бота.

Все данные хранятся в Redis Hash с ключом metrics:{chat_id}.
Поддерживает скользящие счётчики и проверку порогов для алертов.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ai_native_crm.config import settings

if TYPE_CHECKING:
    # Избегаем циклического импорта — state_store и bot используются только для типов
    from aiogram import Bot

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Поля Redis Hash
# ---------------------------------------------------------------------------
_F_TOTAL_TURNS = "total_turns"
_F_HALLUCINATION_COUNT = "hallucination_count"
_F_ACTION_TOTAL = "action_total"
_F_ACTION_SUCCESS = "action_success"


class MetricsService:
    """Метрики в Redis Hash. Алерты при пробитии порогов.

    Redis Hash metrics:{chat_id} содержит:
        total_turns           — всего диалоговых ходов
        hallucination_count   — количество детектированных галлюцинаций
        action_total          — всего попыток выполнить действие (CRM-вызов)
        action_success        — успешных действий

    Пороги берутся из settings:
        hallucination_threshold   — допустимая доля галлюцинаций (default 5%)
        action_success_threshold  — минимальная доля успеха (default 90%)
    """

    def __init__(
        self,
        state_store: Any,
        bot: "Bot | None" = None,
        alert_chat_id: int | None = None,
    ) -> None:
        # state_store должен предоставлять self._redis (Redis-клиент)
        self._state_store = state_store
        self._bot = bot
        self._alert_chat_id = alert_chat_id

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    async def record_turn(
        self,
        chat_id: int,
        *,
        hallucinated: bool,
        action_succeeded: bool,
        has_actions: bool = False,
    ) -> None:
        """Записать метрики одного диалогового хода.

        Args:
            chat_id: идентификатор чата/пользователя
            hallucinated: был ли обнаружен галлюцинационный ответ
            action_succeeded: успешно ли выполнено CRM-действие в этом ходу
            has_actions: были ли вообще действия в этом ходу; если False —
                         action_total и action_success не инкрементируются
        """
        redis = self._get_redis()
        key = self._metrics_key(chat_id)

        pipe = redis.pipeline()
        pipe.hincrby(key, _F_TOTAL_TURNS, 1)

        if hallucinated:
            pipe.hincrby(key, _F_HALLUCINATION_COUNT, 1)

        # Счётчики действий обновляем только если в этом ходу реально были действия
        if has_actions:
            pipe.hincrby(key, _F_ACTION_TOTAL, 1)
            if action_succeeded:
                pipe.hincrby(key, _F_ACTION_SUCCESS, 1)

        await pipe.execute()

        # Проверяем пороги и при необходимости отправляем алерт
        alerts = await self.check_thresholds(chat_id)
        if alerts and self._bot and self._alert_chat_id:
            await self._send_alert(alerts, chat_id)

    async def check_thresholds(self, chat_id: int) -> list[str]:
        """Проверить пороговые значения и вернуть список нарушений (строки).

        Возвращает пустой список, если всё в норме.
        """
        stats = await self.get_stats(chat_id)
        violations: list[str] = []

        total = stats["total_turns"]
        if total == 0:
            return violations

        # --- Галлюцинации ---
        hallucination_rate = stats["hallucination_count"] / total
        if hallucination_rate > settings.hallucination_threshold:
            violations.append(
                f"HALLUCINATION: {hallucination_rate:.1%} > "
                f"threshold {settings.hallucination_threshold:.1%} "
                f"(chat_id={chat_id}, total_turns={total})"
            )

        # --- Успешность действий ---
        action_total = stats["action_total"]
        if action_total > 0:
            action_rate = stats["action_success"] / action_total
            if action_rate < settings.action_success_threshold:
                violations.append(
                    f"ACTION_SUCCESS: {action_rate:.1%} < "
                    f"threshold {settings.action_success_threshold:.1%} "
                    f"(chat_id={chat_id}, action_total={action_total})"
                )

        return violations

    async def get_stats(self, chat_id: int) -> dict[str, Any]:
        """Вернуть все метрики для chat_id в виде словаря."""
        redis = self._get_redis()
        key = self._metrics_key(chat_id)

        raw: dict[str, str] = await redis.hgetall(key)

        def _int(field: str) -> int:
            val = raw.get(field)
            if val is None:
                return 0
            return int(val)

        total = _int(_F_TOTAL_TURNS)
        hallucination_count = _int(_F_HALLUCINATION_COUNT)
        action_total = _int(_F_ACTION_TOTAL)
        action_success = _int(_F_ACTION_SUCCESS)

        return {
            "total_turns": total,
            "hallucination_count": hallucination_count,
            "hallucination_rate": hallucination_count / total if total else 0.0,
            "action_total": action_total,
            "action_success": action_success,
            "action_success_rate": action_success / action_total if action_total else 0.0,
        }

    async def reset(self, chat_id: int) -> None:
        """Сбросить все метрики для chat_id (для тестов и ручного вмешательства)."""
        redis = self._get_redis()
        await redis.delete(self._metrics_key(chat_id))

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _get_redis(self) -> Any:
        """Получить Redis-клиент из state_store."""
        # TODO [MEDIUM]: accept Redis directly instead of extracting from state_store
        # state_store обязан иметь атрибут .redis
        return self._state_store.redis

    def _metrics_key(self, chat_id: int) -> str:
        return f"metrics:{chat_id}"

    async def _send_alert(self, violations: list[str], chat_id: int) -> None:
        """Отправить Telegram-алерт при нарушении порогов."""
        text = (
            f"⚠️ Метрики вышли за пороги (chat_id={chat_id}):\n"
            + "\n".join(f"• {v}" for v in violations)
        )
        try:
            await self._bot.send_message(self._alert_chat_id, text)
        except Exception as exc:
            logger.error("Не удалось отправить алерт: %s", exc)

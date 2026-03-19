"""
Точка входа Telegram-бота.

Инициализирует все компоненты системы и запускает aiogram polling.
Никакого PostgreSQL, никакого SQL — только Redis.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

import redis.asyncio as aioredis
from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import Message

from ai_native_crm.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Глобальные ссылки (заполняются в main() и используются в хендлерах)
# ---------------------------------------------------------------------------
_bot: Bot | None = None
_engine = None       # AgentEngine
_store = None        # StateStore
_crm = None          # CRMAdapter
_drift = None        # DriftDetector
_metrics = None      # MetricsService


# ---------------------------------------------------------------------------
# Аутентификация
# ---------------------------------------------------------------------------

def _is_authorized(chat_id: int) -> bool:
    """Вернуть True, если chat_id разрешён.

    Если allowed_chat_ids не задан (пустая строка) — все разрешены.
    Иначе — только те chat_id, которые перечислены в настройке.
    """
    allowed = settings.allowed_chat_ids_set
    if not allowed:
        return True
    return chat_id in allowed


_DENY_MESSAGE = "Доступ запрещён. Обратитесь к администратору."


# ---------------------------------------------------------------------------
# Rate limiter — скользящее окно (1 минута) на chat_id
# ---------------------------------------------------------------------------

class RateLimiter:
    """Простой in-memory rate limiter со скользящим окном."""

    def __init__(self, max_requests: int, window_sec: float = 60.0):
        self._max = max_requests
        self._window = window_sec
        self._hits: dict[int, list[float]] = defaultdict(list)

    def is_allowed(self, chat_id: int) -> bool:
        """Вернуть True, если запрос разрешён."""
        now = time.monotonic()
        bucket = self._hits[chat_id]
        # Удалить старые записи за пределами окна
        cutoff = now - self._window
        self._hits[chat_id] = bucket = [t for t in bucket if t > cutoff]
        if len(bucket) >= self._max:
            return False
        bucket.append(now)
        return True


_limiter = RateLimiter(
    max_requests=settings.rate_limit_per_minute,
    window_sec=60.0,
)

_RATE_LIMIT_MESSAGE = "Слишком много сообщений. Подождите минуту."


# ---------------------------------------------------------------------------
# Роутер хендлеров
# ---------------------------------------------------------------------------
router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Приветствие + кол-во активных сделок из CRM."""
    if message.chat is None:
        return
    chat_id = message.chat.id
    if not _is_authorized(chat_id):
        await message.answer(_DENY_MESSAGE)
        return

    deals = await _crm.get_deals()
    await message.answer(
        f"Привет! Я AI-ассистент CRM.\n"
        f"В CRM сейчас {len(deals)} сделок.\n\n"
        f"Команды:\n"
        f"/state — текущий стейт агента\n"
        f"/deals — список сделок из CRM\n"
        f"/facts — критические факты\n"
        f"/metrics — метрики качества\n"
        f"/drift — проверка дрейфа стейта\n\n"
        f"Или просто напишите вопрос — я отвечу."
    )


@router.message(Command("state"))
async def cmd_state(message: Message) -> None:
    """Показать текущий стейт агента из Redis."""
    chat_id = message.chat.id
    if not _is_authorized(chat_id):
        await message.answer(_DENY_MESSAGE)
        return
    state = await _store.load(chat_id)

    wm_preview = (state.working_memory[:200] + "…") if len(state.working_memory) > 200 else state.working_memory

    await message.answer(
        f"<b>Стейт агента</b>\n"
        f"Итерация: {state.iteration}\n"
        f"Обновлён: {state.last_updated or '(никогда)'}\n\n"
        f"<b>Рабочая память (первые 200 символов):</b>\n"
        f"<code>{wm_preview or '(пусто)'}</code>",
        parse_mode="HTML",
    )


@router.message(Command("deals"))
async def cmd_deals(message: Message) -> None:
    """Вывести сделки напрямую из CRM API."""
    chat_id = message.chat.id
    if not _is_authorized(chat_id):
        await message.answer(_DENY_MESSAGE)
        return
    deals = await _crm.get_deals()

    if not deals:
        await message.answer("Сделок не найдено.")
        return

    lines = ["<b>Сделки из CRM:</b>"]
    for d in deals[:10]:  # не более 10 штук в сообщение
        lines.append(
            f"• <b>{d.id}</b>: {d.title}\n"
            f"  Стадия: {d.stage} | Сумма: {d.amount:,.0f} {d.currency}"
        )

    if len(deals) > 10:
        lines.append(f"\n…и ещё {len(deals) - 10} сделок")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("facts"))
async def cmd_facts(message: Message) -> None:
    """Показать критические факты из Redis для данного чата."""
    chat_id = message.chat.id
    if not _is_authorized(chat_id):
        await message.answer(_DENY_MESSAGE)
        return
    facts = await _store.get_critical_facts(chat_id)

    if not facts:
        await message.answer("Критических фактов не зафиксировано.")
        return

    lines = ["<b>Критические факты:</b>"]
    for i, f in enumerate(facts[-10:], 1):  # последние 10
        lines.append(f"{i}. [{f.fact_type}] {f.content}")
        if f.deal_id:
            lines[-1] += f" (сделка {f.deal_id})"

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("metrics"))
async def cmd_metrics(message: Message) -> None:
    """Показать метрики качества агента из Redis."""
    chat_id = message.chat.id
    if not _is_authorized(chat_id):
        await message.answer(_DENY_MESSAGE)
        return
    stats = await _metrics.get_stats(chat_id)

    total = int(stats.get("total_turns", 0))
    hall_count = int(stats.get("hallucination_count", 0))
    hall_rate = stats.get("hallucination_rate", 0.0)
    act_total = int(stats.get("action_total", 0))
    act_success = int(stats.get("action_success", 0))
    act_rate = stats.get("action_success_rate", 0.0)

    await message.answer(
        f"<b>Метрики агента</b>\n"
        f"Всего ходов: {total}\n"
        f"Галлюцинаций: {hall_count} ({hall_rate:.1%})\n"
        f"Действий всего: {act_total}\n"
        f"Успешных действий: {act_success} ({act_rate:.1%})",
        parse_mode="HTML",
    )


@router.message(Command("drift"))
async def cmd_drift(message: Message) -> None:
    """Запустить проверку дрейфа стейта вручную."""
    chat_id = message.chat.id
    if not _is_authorized(chat_id):
        await message.answer(_DENY_MESSAGE)
        return
    state = await _store.load(chat_id)

    # DriftDetector.check() принимает SemanticState и возвращает float [0, 1]
    drift_score = await _drift.check(state)

    if drift_score < 0.1:
        await message.answer(
            f"Дрейф стейта не обнаружен (score={drift_score:.2f}). Всё актуально."
        )
    else:
        await message.answer(
            f"<b>Дрейф обнаружен (score={drift_score:.2f})</b>\n"
            f"Рекомендуется пересинхронизация. Отправьте любое сообщение — агент обновит стейт.",
            parse_mode="HTML",
        )


@router.message()
async def handle_text(message: Message) -> None:
    """Основной хендлер — передать текст в AgentEngine и ответить."""
    chat_id = message.chat.id
    if not _is_authorized(chat_id):
        await message.answer(_DENY_MESSAGE)
        return
    if not _limiter.is_allowed(chat_id):
        await message.answer(_RATE_LIMIT_MESSAGE)
        return
    user_text = message.text or ""

    if not user_text:
        await message.answer("Пожалуйста, введите текстовое сообщение.")
        return

    # Индикатор загрузки
    typing_task = asyncio.create_task(
        _send_typing_periodically(message.bot, chat_id)
    )

    try:
        response = await _engine.process(user_text, chat_id)
    finally:
        typing_task.cancel()

    await message.answer(response)


async def _send_typing_periodically(bot: Bot, chat_id: int) -> None:
    """Периодически отправлять 'typing...' пока агент думает."""
    try:
        while True:
            await bot.send_chat_action(chat_id, "typing")
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Фоновая задача: планировщик напоминаний
# ---------------------------------------------------------------------------

_REMINDER_BATCH_LIMIT = 20  # макс. напоминаний за один цикл (защита от 429)


async def reminder_scheduler() -> None:
    """
    Фоновая задача — проверяет просроченные напоминания каждые N секунд.
    При срабатывании отправляет сообщение в соответствующий чат.
    Batch limit + exponential backoff на Telegram 429.
    """
    interval = settings.reminder_check_interval
    logger.info("Планировщик напоминаний запущен, интервал=%ds", interval)
    backoff = 0.0  # текущая задержка при 429

    while True:
        if backoff > 0:
            logger.warning("Telegram 429 backoff: ждём %.1f с", backoff)
            await asyncio.sleep(backoff)

        sent = 0
        try:
            chat_ids = await _store.get_all_reminder_keys()
            for chat_id in chat_ids:
                if sent >= _REMINDER_BATCH_LIMIT:
                    break
                reminders = await _store.get_due_reminders(chat_id)
                for reminder in reminders:
                    if sent >= _REMINDER_BATCH_LIMIT:
                        break
                    text = reminder.get("text", "Напоминание")
                    deal_id = reminder.get("deal_id")

                    msg = f"Напоминание: {text}"
                    if deal_id:
                        msg += f" (сделка {deal_id})"

                    try:
                        await _bot.send_message(chat_id, msg)
                        sent += 1
                        backoff = 0.0  # сброс при успехе
                        logger.info(
                            "Напоминание отправлено: chat_id=%d deal_id=%s",
                            chat_id, deal_id,
                        )
                    except TelegramRetryAfter as exc:
                        # Telegram requires waiting — respect retry_after before continuing
                        backoff = max(backoff * 2, float(exc.retry_after))
                        backoff = min(backoff, 300.0)  # cap at 5 minutes
                        logger.warning(
                            "Telegram 429 RetryAfter=%ds, backoff=%.1fs",
                            exc.retry_after, backoff,
                        )
                        break
                    except Exception as exc:
                        logger.error(
                            "Ошибка отправки напоминания chat_id=%d: %s",
                            chat_id, exc,
                        )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Ошибка в reminder_scheduler: %s", exc)

        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Инициализация и запуск
# ---------------------------------------------------------------------------

async def main() -> None:
    """Инициализировать все компоненты и запустить бота."""
    global _bot, _engine, _store, _crm, _drift, _metrics

    logger.info("Запуск AI-Native CRM бота...")

    # --- 1. Redis — единственный persistence, никакого SQL ---
    redis = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=settings.redis_socket_timeout,
        socket_connect_timeout=settings.redis_connect_timeout,
    )
    await redis.ping()
    logger.info("Redis подключён: %s", settings.redis_url)

    # --- 2. StateStore ---
    from ai_native_crm.core.state_store import StateStore
    _store = StateStore(
        redis,
        audit_ttl_days=settings.audit_ttl_days,
        max_critical_facts=settings.max_critical_facts,
    )

    # --- 3. CRM-адаптер ---
    from ai_native_crm.adapters import get_adapter
    _crm = get_adapter()
    logger.info("CRM-адаптер: %s", settings.crm_adapter)

    # --- 4. LLM-клиент ---
    from ai_native_crm.services.llm_client import LLMClient
    llm = LLMClient()

    # --- 5. PIIAnonymizer ---
    # Anonymizer обязателен для AgentEngine — при pii_enabled=False создаём инстанс,
    # который внутри ничего не маскирует (settings.pii_enabled проверяется внутри)
    from ai_native_crm.services.pii_anonymizer import PIIAnonymizer
    pii = PIIAnonymizer(redis)
    if settings.pii_enabled:
        logger.info("PII-анонимизация включена (TTL=%ds)", settings.pii_ttl_sec)
    else:
        logger.info("PII-анонимизация отключена")

    # --- 6. DistributedLock ---
    from ai_native_crm.services.lock import DistributedLock
    lock = DistributedLock(redis)

    # --- 7. Bot + Dispatcher ---
    _bot = Bot(token=settings.telegram_token)
    dp = Dispatcher()
    dp.include_router(router)

    # --- 8. ResponseValidator ---
    from ai_native_crm.core.response_validator import ResponseValidator
    validator = ResponseValidator(_crm)

    # --- 9. ActionRouter ---
    from ai_native_crm.core.action_router import ActionRouter
    action_router = ActionRouter(_crm, _bot, _store)

    # --- 10. StateCompressor ---
    from ai_native_crm.core.compressor import StateCompressor
    compressor = StateCompressor(llm)

    # --- 11. DriftDetector ---
    from ai_native_crm.core.drift_detector import DriftDetector
    _drift = DriftDetector(_crm)

    # --- 12. MetricsService ---
    from ai_native_crm.services.metrics import MetricsService
    _metrics = MetricsService(_store, bot=_bot)

    # --- 13. AgentEngine ---
    # Порядок аргументов: state_store, crm, llm, validator, action_router,
    #                      compressor, drift, anonymizer, lock, metrics
    from ai_native_crm.core.engine import AgentEngine
    _engine = AgentEngine(
        state_store=_store,
        crm=_crm,
        llm=llm,
        validator=validator,
        action_router=action_router,
        compressor=compressor,
        drift=_drift,
        anonymizer=pii,
        lock=lock,
        metrics=_metrics,
    )

    logger.info("Все компоненты инициализированы.")

    # --- Фоновая задача напоминаний ---
    reminder_task = asyncio.create_task(reminder_scheduler())

    # --- Запуск polling ---
    try:
        logger.info("Бот запущен, polling...")
        await dp.start_polling(_bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        reminder_task.cancel()
        await asyncio.gather(reminder_task, return_exceptions=True)
        if hasattr(_crm, "close"):
            await _crm.close()
        await _bot.session.close()
        await redis.aclose()
        logger.info("Бот остановлен, ресурсы освобождены.")


if __name__ == "__main__":
    asyncio.run(main())

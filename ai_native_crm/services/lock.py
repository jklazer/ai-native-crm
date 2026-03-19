"""
Распределённая блокировка на Redis (алгоритм RedLock — упрощённый, один узел).

Захват: SET key uuid NX PX timeout
Освобождение: Lua-скрипт — удаляем ключ только если uuid совпадает,
              чтобы не снять чужую блокировку после истечения TTL.
Продление TTL: фоновая задача обновляет PX каждые timeout/3 мс,
               пока блок не завершится — защищает от истечения при долгих LLM-вызовах.
"""
from __future__ import annotations

import asyncio
import logging
import uuid as uuid_lib
from contextlib import asynccontextmanager
from typing import AsyncIterator

from redis.asyncio import Redis

from ai_native_crm.config import settings

logger = logging.getLogger(__name__)

# Lua-скрипт: атомарно проверяем владельца и удаляем ключ.
# Возвращает 1 при успехе, 0 если ключ уже не наш (истёк или перехвачен).
_LUA_RELEASE = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""

# Lua-скрипт: атомарно проверяем владельца и продлеваем TTL.
# KEYS[1] — ключ блокировки, ARGV[1] — owner uuid, ARGV[2] — новый TTL в мс.
# Возвращает 1 при успехе, 0 если блокировка уже не наша (истекла или перехвачена).
_LUA_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


class LockAcquireError(Exception):
    """Не удалось захватить блокировку в отведённое время."""


class DistributedLock:
    """Redis distributed lock с Lua-скриптом для безопасного освобождения.

    Каждому вызову lock() выдаётся уникальный UUID — владелец блокировки.
    Это гарантирует, что истёкшая блокировка не будет снята «чужим» корутином.

    Фоновая задача продлевает TTL каждые timeout/3 секунд, чтобы операции,
    длящиеся дольше исходного TTL (например, долгие LLM-вызовы), не теряли
    блокировку преждевременно.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        # Регистрируем Lua-скрипты один раз при старте
        self._release_script = self._redis.register_script(_LUA_RELEASE)
        self._renew_script = self._redis.register_script(_LUA_RENEW)

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def lock(self, chat_id: int) -> AsyncIterator[None]:
        """Захватить блокировку для chat_id на время выполнения блока.

        Повторяет попытки захвата каждые 100 мс до lock_timeout_sec.
        При исчерпании таймаута бросает LockAcquireError.

        После захвата запускает фоновую задачу, которая продлевает TTL
        каждые timeout/3 секунд — защита от истечения при долгих операциях.
        """
        key = self._lock_key(chat_id)
        owner = str(uuid_lib.uuid4())
        timeout_ms = settings.lock_timeout_sec * 1000

        acquired = await self._acquire(key, owner, timeout_ms)
        if not acquired:
            raise LockAcquireError(
                f"Не удалось захватить блокировку для chat_id={chat_id} "
                f"за {settings.lock_timeout_sec}с"
            )

        logger.debug("Блокировка захвачена: chat_id=%d owner=%s", chat_id, owner)

        # Запускаем фоновое продление TTL сразу после захвата.
        # Интервал — треть от полного TTL, чтобы было достаточно попыток
        # даже при временной недоступности Redis.
        renewal_interval_sec = (timeout_ms / 3) / 1000
        renewal_task = asyncio.ensure_future(
            self._renewal_loop(key, owner, timeout_ms, renewal_interval_sec)
        )

        try:
            yield
        finally:
            # Отменяем продление до освобождения — порядок важен:
            # renewal_loop не должен гоняться с _release за ключом.
            renewal_task.cancel()
            try:
                await renewal_task
            except asyncio.CancelledError:
                pass

            released = await self._release(key, owner)
            if not released:
                # Блокировка истекла сама — это не критично, просто логируем
                logger.warning(
                    "Блокировка уже истекла к моменту освобождения: chat_id=%d owner=%s",
                    chat_id,
                    owner,
                )
            else:
                logger.debug("Блокировка освобождена: chat_id=%d owner=%s", chat_id, owner)

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    async def _renewal_loop(
        self,
        key: str,
        owner: str,
        timeout_ms: int,
        interval_sec: float,
    ) -> None:
        """Периодически продлевает TTL блокировки, пока задача не отменена.

        Использует Lua-скрипт для атомарной проверки владельца перед продлением.
        Если блокировка украдена или истекла, логирует предупреждение и выходит —
        дальнейшее продление бессмысленно и опасно (мы уже не владелец).
        """
        try:
            while True:
                await asyncio.sleep(interval_sec)
                try:
                    result = await self._renew_script(
                        keys=[key], args=[owner, str(int(timeout_ms))]
                    )
                except Exception as exc:
                    logger.warning(
                        "Ошибка при продлении блокировки key=%s: %s", key, exc
                    )
                    return

                if not result:
                    # Блокировка уже не наша — кто-то перехватил или она истекла
                    logger.warning(
                        "Не удалось продлить блокировку key=%s owner=%s — "
                        "блокировка потеряна",
                        key,
                        owner,
                    )
                    return

                logger.debug(
                    "TTL блокировки продлён: key=%s owner=%s timeout_ms=%d",
                    key,
                    owner,
                    timeout_ms,
                )
        except asyncio.CancelledError:
            # Штатная остановка при выходе из контекстного менеджера
            raise

    async def _acquire(self, key: str, owner: str, timeout_ms: int) -> bool:
        """Попытаться захватить блокировку с повторами каждые 100 мс."""
        retry_interval = 0.1  # секунд
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settings.lock_timeout_sec

        while True:
            # SET key owner NX PX timeout_ms
            ok = await self._redis.set(key, owner, nx=True, px=timeout_ms)
            if ok:
                return True

            remaining = deadline - loop.time()
            if remaining <= 0:
                return False

            await asyncio.sleep(min(retry_interval, remaining))

    async def _release(self, key: str, owner: str) -> bool:
        """Атомарно освободить блокировку через Lua. Возвращает True при успехе."""
        try:
            result = await self._release_script(keys=[key], args=[owner])
            return bool(result)
        except Exception as exc:
            logger.error("Ошибка при освобождении блокировки key=%s: %s", key, exc)
            return False

    @staticmethod
    def _lock_key(chat_id: int) -> str:
        return f"lock:chat:{chat_id}"

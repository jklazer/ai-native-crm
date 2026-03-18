"""
Распределённая блокировка на Redis (алгоритм RedLock — упрощённый, один узел).

Захват: SET key uuid NX PX timeout
Освобождение: Lua-скрипт — удаляем ключ только если uuid совпадает,
              чтобы не снять чужую блокировку после истечения TTL.
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


class LockAcquireError(Exception):
    """Не удалось захватить блокировку в отведённое время."""


class DistributedLock:
    """Redis distributed lock с Lua-скриптом для безопасного освобождения.

    Каждому вызову lock() выдаётся уникальный UUID — владелец блокировки.
    Это гарантирует, что истёкшая блокировка не будет снята «чужим» корутином.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        # Регистрируем Lua-скрипт один раз при старте
        self._release_script = self._redis.register_script(_LUA_RELEASE)

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def lock(self, chat_id: int) -> AsyncIterator[None]:
        """Захватить блокировку для chat_id на время выполнения блока.

        Повторяет попытки захвата каждые 100 мс до lock_timeout_sec.
        При исчерпании таймаута бросает LockAcquireError.
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
        try:
            yield
        finally:
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

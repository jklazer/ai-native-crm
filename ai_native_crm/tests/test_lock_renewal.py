"""
Тесты продления TTL распределённой блокировки.

Проверяем, что фоновая задача renewal_loop не даёт блокировке истечь
во время долгой операции (дольше исходного TTL).
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import fakeredis.aioredis
import pytest
from fakeredis import FakeServer

from ai_native_crm.services.lock import DistributedLock, LockAcquireError


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture
def redis():
    """In-memory Redis с поддержкой Lua — идентично conftest.py."""
    server = FakeServer()
    server.lua_modules = True
    return fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)


@pytest.fixture
def lock(redis):
    """DistributedLock поверх fakeredis."""
    return DistributedLock(redis)


@pytest.fixture
def redis2(redis):
    """Второй клиент к тому же fakeredis-серверу — имитирует конкурирующий процесс."""
    # Берём FakeServer из первого клиента, чтобы оба видели одни данные
    server = redis.connection_pool.connection_class._server  # type: ignore[attr-defined]
    return fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)


# ---------------------------------------------------------------------------
# test 1: renewal удерживает блокировку дольше исходного TTL
# ---------------------------------------------------------------------------


async def test_renewal_keeps_lock_alive(lock, redis):
    """Блокировка с TTL=2с должна пережить 3-секундный hold благодаря renewal."""
    chat_id = 1001

    # lock_timeout_sec=2 → renewal каждые ~0.67 с
    with patch("ai_native_crm.services.lock.settings") as mock_settings:
        mock_settings.lock_timeout_sec = 2

        async with lock.lock(chat_id):
            # Ждём дольше исходного TTL
            await asyncio.sleep(3)

            # Блокировка должна всё ещё существовать в Redis
            key = f"lock:chat:{chat_id}"
            value = await redis.get(key)
            assert value is not None, (
                "Блокировка истекла раньше времени — renewal не сработал"
            )

    # После выхода из контекстного менеджера ключ должен быть удалён
    key = f"lock:chat:{chat_id}"
    value = await redis.get(key)
    assert value is None, "Блокировка не была освобождена после выхода из контекста"


# ---------------------------------------------------------------------------
# test 2: после освобождения другой клиент может захватить блокировку
# ---------------------------------------------------------------------------


async def test_lock_released_after_context(redis):
    """После выхода из lock() другой DistributedLock должен захватить ключ."""
    chat_id = 1002
    lock_a = DistributedLock(redis)
    lock_b = DistributedLock(redis)

    with patch("ai_native_crm.services.lock.settings") as mock_settings:
        mock_settings.lock_timeout_sec = 2

        async with lock_a.lock(chat_id):
            await asyncio.sleep(0.1)  # убеждаемся, что renewal запустился

    # lock_a освобождён — lock_b должен захватить немедленно
    acquired = False
    with patch("ai_native_crm.services.lock.settings") as mock_settings:
        mock_settings.lock_timeout_sec = 2
        async with lock_b.lock(chat_id):
            acquired = True

    assert acquired, "Второй клиент не смог захватить блокировку после освобождения первым"


# ---------------------------------------------------------------------------
# test 3: renewal task отменяется даже при исключении в теле блока
# ---------------------------------------------------------------------------


async def test_renewal_cancelled_on_exception(lock, redis):
    """Фоновая задача renewal должна отменяться даже если тело блока бросает исключение."""
    chat_id = 1003

    with patch("ai_native_crm.services.lock.settings") as mock_settings:
        mock_settings.lock_timeout_sec = 2

        with pytest.raises(RuntimeError, match="намеренная ошибка"):
            async with lock.lock(chat_id):
                raise RuntimeError("намеренная ошибка")

    # После исключения ключ должен быть освобождён (finally отработал)
    key = f"lock:chat:{chat_id}"
    value = await redis.get(key)
    assert value is None, (
        "Блокировка не освобождена после исключения — finally не сработал корректно"
    )


# ---------------------------------------------------------------------------
# test 4: renewal прекращается, если блокировка была перехвачена извне
# ---------------------------------------------------------------------------


async def test_renewal_stops_if_lock_stolen(redis, caplog):
    """Если ключ удалён извне, renewal_loop должен залогировать warning и остановиться."""
    import logging

    chat_id = 1004
    lock = DistributedLock(redis)

    with patch("ai_native_crm.services.lock.settings") as mock_settings:
        mock_settings.lock_timeout_sec = 2

        with caplog.at_level(logging.WARNING, logger="ai_native_crm.services.lock"):
            async with lock.lock(chat_id):
                # Форсированно удаляем ключ, имитируя кражу блокировки
                key = f"lock:chat:{chat_id}"
                await redis.delete(key)

                # Ждём, пока renewal_loop обнаружит пропажу (> одного интервала)
                await asyncio.sleep(1.5)

    # Должно быть предупреждение о потере блокировки
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("потеряна" in str(w) or "истекла" in str(w) for w in warnings), (
        f"Ожидалось предупреждение о потере блокировки, получено: {warnings}"
    )

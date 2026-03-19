"""
Тесты RateLimiter — 3 теста.
Проверяем in-memory rate limiter с скользящим окном в 60 секунд.
Никакого Redis, никакого Telegram — чистая unit-логика.
"""
from unittest.mock import patch

import pytest

from ai_native_crm.main import RateLimiter


# ---------------------------------------------------------------------------
# test 1: 10 запросов за минуту — все разрешены
# ---------------------------------------------------------------------------

def test_rate_limiter_allows_up_to_limit():
    """Первые max_per_minute запросов должны быть разрешены."""
    limiter = RateLimiter(max_per_minute=10)
    chat_id = 12345

    for _ in range(10):
        assert limiter.is_allowed(chat_id) is True


# ---------------------------------------------------------------------------
# test 2: 11-й запрос в ту же минуту — заблокирован
# ---------------------------------------------------------------------------

def test_rate_limiter_blocks_exceeding_requests():
    """Запрос сверх лимита в скользящем окне должен быть отклонён."""
    limiter = RateLimiter(max_per_minute=10)
    chat_id = 99999

    for _ in range(10):
        limiter.is_allowed(chat_id)  # заполняем окно

    # 11-й — должен быть заблокирован
    assert limiter.is_allowed(chat_id) is False


# ---------------------------------------------------------------------------
# test 3: после истечения 60 секунд запросы снова разрешены
# ---------------------------------------------------------------------------

def test_rate_limiter_resets_after_window():
    """Через 60 секунд скользящее окно освобождается и запросы снова проходят."""
    limiter = RateLimiter(max_per_minute=10)
    chat_id = 77777

    # Имитируем: 10 запросов были сделаны 61 секунду назад
    import time
    old_ts = time.time() - 61
    limiter._timestamps[chat_id] = [old_ts] * 10

    # Теперь запрос должен быть разрешён — старые метки вне окна
    assert limiter.is_allowed(chat_id) is True


# ---------------------------------------------------------------------------
# test 4: разные chat_id не влияют друг на друга
# ---------------------------------------------------------------------------

def test_rate_limiter_isolated_per_chat():
    """Лимиты у разных chat_id независимы."""
    limiter = RateLimiter(max_per_minute=3)

    for _ in range(3):
        limiter.is_allowed(1)

    # chat_id=1 исчерпан, chat_id=2 — нет
    assert limiter.is_allowed(1) is False
    assert limiter.is_allowed(2) is True

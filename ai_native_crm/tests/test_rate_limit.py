"""Тесты для rate limiter — скользящее окно по chat_id."""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from ai_native_crm.main import RateLimiter


class TestRateLimiter:
    """Проверяем RateLimiter в изоляции — без бота и Redis."""

    def test_allows_within_limit(self):
        limiter = RateLimiter(max_requests=3, window_sec=60.0)
        assert limiter.is_allowed(1) is True
        assert limiter.is_allowed(1) is True
        assert limiter.is_allowed(1) is True

    def test_blocks_over_limit(self):
        limiter = RateLimiter(max_requests=3, window_sec=60.0)
        for _ in range(3):
            limiter.is_allowed(1)
        assert limiter.is_allowed(1) is False

    def test_different_users_independent(self):
        limiter = RateLimiter(max_requests=2, window_sec=60.0)
        limiter.is_allowed(1)
        limiter.is_allowed(1)
        # user 1 is blocked
        assert limiter.is_allowed(1) is False
        # user 2 is fine
        assert limiter.is_allowed(2) is True

    def test_window_expiry_allows_again(self):
        limiter = RateLimiter(max_requests=2, window_sec=1.0)
        limiter.is_allowed(1)
        limiter.is_allowed(1)
        assert limiter.is_allowed(1) is False
        # Simulate time passing
        with patch("ai_native_crm.main.time") as mock_time:
            # monotonic returns future time
            future = time.monotonic() + 2.0
            mock_time.monotonic.return_value = future
            # Need to re-check — but the stored timestamps are real
            # So we need to actually wait or mock properly
        # Simpler: just sleep a bit with a tiny window
        import time as _time
        _time.sleep(1.1)
        assert limiter.is_allowed(1) is True

    def test_zero_limit_blocks_all(self):
        limiter = RateLimiter(max_requests=0, window_sec=60.0)
        assert limiter.is_allowed(1) is False

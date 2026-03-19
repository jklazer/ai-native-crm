"""
Тесты аутентификации по chat_id.

Проверяем логику Settings.allowed_chat_ids_set и _is_authorized из main.py.
Никаких реальных зависимостей (Redis, Telegram) не нужно.
"""
import pytest

from ai_native_crm.config import Settings


# ---------------------------------------------------------------------------
# Вспомогательная функция — изолированный _is_authorized без импорта main.py,
# чтобы не тянуть за собой aiogram / Redis инициализацию.
# Логика полностью дублирует main._is_authorized — тест покрывает
# именно Settings.allowed_chat_ids_set, на которой она строится.
# ---------------------------------------------------------------------------

def is_authorized(settings: Settings, chat_id: int) -> bool:
    """Копия логики main._is_authorized, изолированная для теста."""
    allowed = settings.allowed_chat_ids_set
    if not allowed:
        return True
    return chat_id in allowed


# ---------------------------------------------------------------------------
# Вспомогательный конструктор Settings без .env-файла
# ---------------------------------------------------------------------------

def make_settings(allowed_chat_ids: str = "") -> Settings:
    """Создать Settings с минимальными полями, не читая .env."""
    return Settings(
        telegram_token="test-token",
        allowed_chat_ids=allowed_chat_ids,
        _env_file=None,  # не читать .env при тестировании
    )


# ---------------------------------------------------------------------------
# test 1: пустой allowed_chat_ids → все разрешены
# ---------------------------------------------------------------------------

def test_empty_allows_all():
    """Когда allowed_chat_ids не задан, любой chat_id авторизован."""
    s = make_settings(allowed_chat_ids="")
    assert s.allowed_chat_ids_set == set()
    assert is_authorized(s, 0) is True
    assert is_authorized(s, 123) is True
    assert is_authorized(s, 999999999) is True


# ---------------------------------------------------------------------------
# test 2: allowed_chat_ids="123,456" → только эти два разрешены
# ---------------------------------------------------------------------------

def test_whitelist_allows_listed_ids():
    """Когда задан whitelist, разрешены только перечисленные chat_id."""
    s = make_settings(allowed_chat_ids="123,456")
    assert s.allowed_chat_ids_set == {123, 456}
    assert is_authorized(s, 123) is True
    assert is_authorized(s, 456) is True


# ---------------------------------------------------------------------------
# test 3: другие chat_id возвращают False
# ---------------------------------------------------------------------------

def test_whitelist_denies_unlisted_ids():
    """chat_id вне whitelist должны быть отклонены."""
    s = make_settings(allowed_chat_ids="123,456")
    assert is_authorized(s, 789) is False
    assert is_authorized(s, 0) is False
    assert is_authorized(s, 124) is False


# ---------------------------------------------------------------------------
# test 4: пробелы вокруг ID игнорируются
# ---------------------------------------------------------------------------

def test_whitelist_strips_whitespace():
    """Пробелы вокруг chat_id не должны ломать разбор."""
    s = make_settings(allowed_chat_ids="  123 , 456  ")
    assert s.allowed_chat_ids_set == {123, 456}
    assert is_authorized(s, 123) is True
    assert is_authorized(s, 456) is True
    assert is_authorized(s, 789) is False


# ---------------------------------------------------------------------------
# test 5: один ID в whitelist
# ---------------------------------------------------------------------------

def test_single_id_whitelist():
    """Whitelist из одного ID: только он разрешён."""
    s = make_settings(allowed_chat_ids="42")
    assert s.allowed_chat_ids_set == {42}
    assert is_authorized(s, 42) is True
    assert is_authorized(s, 43) is False

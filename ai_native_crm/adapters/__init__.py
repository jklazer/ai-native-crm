"""
Пакет CRM-адаптеров.

Публичный API пакета:
  DealInfo, ContactInfo, CRMAdapter — модели и абстрактный интерфейс
  BitrixAdapter                     — адаптер Bitrix24
  AmoAdapter                        — адаптер AmoCRM
  MockAdapter                       — in-memory адаптер для тестов
  get_adapter()                     — фабрика: читает settings.crm_adapter
"""
from ai_native_crm.adapters.base import ContactInfo, CRMAdapter, DealInfo
from ai_native_crm.adapters.bitrix import BitrixAdapter
from ai_native_crm.adapters.amo import AmoAdapter
from ai_native_crm.adapters.mock import MockAdapter

__all__ = [
    "CRMAdapter",
    "DealInfo",
    "ContactInfo",
    "BitrixAdapter",
    "AmoAdapter",
    "MockAdapter",
    "get_adapter",
]


def get_adapter() -> CRMAdapter:
    """
    Фабрика адаптеров.

    Читает settings.crm_adapter и возвращает нужную реализацию:
      "bitrix" → BitrixAdapter(settings.bitrix_webhook)
      "amo"    → AmoAdapter(settings.amo_subdomain, ...)
      "mock"   → MockAdapter()

    Raises:
        ValueError: если значение settings.crm_adapter не поддерживается.
    """
    # Импортируем здесь, а не на уровне модуля: избегаем проблем с порядком
    # инициализации при тестах, которые могут подменять settings до импорта.
    from ai_native_crm.config import settings

    if settings.crm_adapter == "bitrix":
        return BitrixAdapter(settings.bitrix_webhook)

    if settings.crm_adapter == "amo":
        return AmoAdapter(
            subdomain=settings.amo_subdomain,
            access_token=settings.amo_access_token,
            refresh_token=settings.amo_refresh_token,
            client_id=settings.amo_client_id,
            client_secret=settings.amo_client_secret,
            redirect_uri=settings.amo_redirect_uri,
        )

    if settings.crm_adapter == "mock":
        return MockAdapter()

    raise ValueError(
        f"Неизвестный адаптер: '{settings.crm_adapter}'. "
        f"Допустимые значения: 'bitrix', 'amo', 'mock'."
    )

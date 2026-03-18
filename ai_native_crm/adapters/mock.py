"""
Mock-адаптер для тестов и локальной разработки.

Хранит все данные in-memory. Не обращается к внешним API.
При каждом создании нового экземпляра MockAdapter seed-данные
инициализируются заново — каждый тест получает чистое состояние.
"""
from __future__ import annotations

import logging
from copy import deepcopy

from ai_native_crm.adapters.base import ContactInfo, CRMAdapter, DealInfo

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Seed-данные — 5 сделок и 3 контакта
# ---------------------------------------------------------------------------

_SEED_DEALS: list[DealInfo] = [
    DealInfo(
        id="d1",
        title="Внедрение 1С для ООО Альфа",
        stage="PREPARATION",
        amount=450_000,
        contact_name="Иванов С.П.",
        contact_id="c1",
    ),
    DealInfo(
        id="d2",
        title="CRM-интеграция для Бета Групп",
        stage="NEGOTIATION",
        amount=280_000,
        contact_name="Петрова Е.А.",
        contact_id="c2",
    ),
    DealInfo(
        id="d3",
        title="Автоматизация склада Гамма",
        stage="PROPOSAL",
        amount=1_200_000,
        contact_name="Сидоров А.В.",
        contact_id="c3",
    ),
    DealInfo(
        id="d4",
        title="Техподдержка ИП Козлов",
        stage="NEW",
        amount=85_000,
        contact_name="Козлов Д.М.",
        contact_id="c1",
    ),
    DealInfo(
        id="d5",
        title="Аудит ИТ-инфраструктуры Дельта",
        stage="QUALIFIED",
        amount=350_000,
        contact_name="Николаева О.С.",
        contact_id="c2",
    ),
]

_SEED_CONTACTS: list[ContactInfo] = [
    ContactInfo(
        id="c1",
        name="Иванов Сергей Петрович",
        phone="+7-916-100-10-01",
        email="ivanov@alfa.ru",
        company="ООО Альфа",
    ),
    ContactInfo(
        id="c2",
        name="Петрова Елена Александровна",
        phone="+7-916-200-20-02",
        email="petrova@beta.ru",
        company="Бета Групп",
    ),
    ContactInfo(
        id="c3",
        name="Сидоров Андрей Васильевич",
        phone="+7-916-300-30-03",
        email="sidorov@gamma.ru",
        company="Гамма",
    ),
]


class MockAdapter(CRMAdapter):
    """
    In-memory реализация CRMAdapter.

    При инициализации создаётся глубокая копия seed-данных,
    поэтому операции мутирующие состояние (update, create)
    не влияют на другие экземпляры.
    """

    def __init__(self) -> None:
        # Глубокая копия: каждый экземпляр — изолированное хранилище
        self._deals: dict[str, DealInfo] = {
            d.id: d.model_copy() for d in _SEED_DEALS
        }
        self._contacts: dict[str, ContactInfo] = {
            c.id: c.model_copy() for c in _SEED_CONTACTS
        }
        # Счётчик для генерации ID новых сделок
        self._next_deal_index: int = len(_SEED_DEALS) + 1
        log.debug(
            "MockAdapter инициализирован: %d сделок, %d контактов",
            len(self._deals),
            len(self._contacts),
        )

    # ------------------------------------------------------------------
    # CRMAdapter: сделки
    # ------------------------------------------------------------------

    async def get_deals(self, filters: dict | None = None) -> list[DealInfo]:
        """
        Вернуть все сделки (или отфильтрованные).

        Поддерживаемые ключи filters:
          stage  — точное совпадение по полю stage
          min_amount — минимальная сумма (включительно)
          max_amount — максимальная сумма (включительно)
        """
        deals = list(self._deals.values())

        if filters:
            if "stage" in filters:
                deals = [d for d in deals if d.stage == filters["stage"]]
            if "min_amount" in filters:
                deals = [d for d in deals if d.amount >= float(filters["min_amount"])]
            if "max_amount" in filters:
                deals = [d for d in deals if d.amount <= float(filters["max_amount"])]

        log.debug("MockAdapter get_deals: возвращено %d сделок", len(deals))
        return deals

    async def update_deal(self, deal_id: str, fields: dict) -> bool:
        """
        Обновить поля сделки по deal_id.

        Поддерживаемые поля: stage, amount, title, contact_name, contact_id, currency.
        Неизвестные поля игнорируются (имитация поведения Bitrix24).
        """
        deal = self._deals.get(deal_id)
        if deal is None:
            log.warning("MockAdapter update_deal: сделка %s не найдена", deal_id)
            return False

        # Применяем известные поля; model_copy(update=...) возвращает новый объект
        update: dict = {}
        field_map = {
            "STAGE_ID": "stage",
            "stage": "stage",
            "OPPORTUNITY": "amount",
            "amount": "amount",
            "TITLE": "title",
            "title": "title",
            "CONTACT_ID": "contact_id",
            "contact_id": "contact_id",
            "contact_name": "contact_name",
            "CURRENCY_ID": "currency",
            "currency": "currency",
        }
        for bitrix_key, model_key in field_map.items():
            if bitrix_key in fields:
                update[model_key] = fields[bitrix_key]

        self._deals[deal_id] = deal.model_copy(update=update)
        log.debug("MockAdapter update_deal: сделка %s обновлена полями %s", deal_id, list(update.keys()))
        return True

    async def create_deal(self, data: dict) -> str:
        """
        Создать новую сделку.

        Обязательное поле: title (или TITLE).
        Остальные поля опциональны.
        """
        deal_id = f"d{self._next_deal_index}"
        self._next_deal_index += 1

        # Нормализуем как Bitrix-ключи, так и snake_case
        title = data.get("title") or data.get("TITLE", f"Сделка {deal_id}")
        stage = data.get("stage") or data.get("STAGE_ID", "NEW")
        amount = float(data.get("amount") or data.get("OPPORTUNITY") or 0)
        currency = data.get("currency") or data.get("CURRENCY_ID", "RUB")
        contact_id = str(data.get("contact_id") or data.get("CONTACT_ID", ""))
        contact_name = data.get("contact_name", "")

        deal = DealInfo(
            id=deal_id,
            title=title,
            stage=stage,
            amount=amount,
            currency=currency,
            contact_id=contact_id,
            contact_name=contact_name,
        )
        self._deals[deal_id] = deal
        log.debug("MockAdapter create_deal: создана сделка ID=%s title='%s'", deal_id, title)
        return deal_id

    # ------------------------------------------------------------------
    # CRMAdapter: контакты
    # ------------------------------------------------------------------

    async def get_contacts(self, filters: dict | None = None) -> list[ContactInfo]:
        """
        Вернуть все контакты (или отфильтрованные).

        Поддерживаемый ключ filters:
          company — точное совпадение по полю company
        """
        contacts = list(self._contacts.values())

        if filters:
            if "company" in filters:
                contacts = [c for c in contacts if c.company == filters["company"]]

        log.debug("MockAdapter get_contacts: возвращено %d контактов", len(contacts))
        return contacts

    # ------------------------------------------------------------------
    # CRMAdapter: вспомогательные методы
    # ------------------------------------------------------------------

    async def verify_deal_exists(self, deal_id: str) -> bool:
        """Проверить наличие сделки в хранилище."""
        exists = deal_id in self._deals
        log.debug("MockAdapter verify_deal_exists deal_id=%s: %s", deal_id, exists)
        return exists

    async def get_deal_amount(self, deal_id: str) -> float | None:
        """
        Вернуть сумму сделки.
        None, если сделка не найдена.
        """
        deal = self._deals.get(deal_id)
        if deal is None:
            log.warning("MockAdapter get_deal_amount: сделка %s не найдена", deal_id)
            return None
        log.debug("MockAdapter get_deal_amount deal_id=%s: %.2f", deal_id, deal.amount)
        return deal.amount

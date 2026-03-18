"""
Базовые модели данных и абстрактный интерфейс CRM-адаптера.

Pydantic-модели описывают канонические сущности CRM (сделка, контакт).
Все конкретные адаптеры (Bitrix, Mock, ...) обязаны реализовать CRMAdapter.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel


class DealInfo(BaseModel):
    """Сделка — базовая единица CRM."""
    id: str
    title: str
    stage: str
    amount: float
    currency: str = "RUB"
    contact_name: str = ""
    contact_id: str = ""


class ContactInfo(BaseModel):
    """Контакт (физлицо или представитель компании)."""
    id: str
    name: str
    phone: str = ""
    email: str = ""
    company: str = ""


class CRMAdapter(ABC):
    """
    Абстрактный CRM-адаптер.

    Все операции асинхронны. Реализации не должны хранить состояние
    в базе данных — CRM API является единственным источником правды.
    """

    @abstractmethod
    async def get_deals(self, filters: dict | None = None) -> list[DealInfo]:
        """Вернуть список сделок, опционально отфильтрованных по filters."""
        ...

    @abstractmethod
    async def update_deal(self, deal_id: str, fields: dict) -> bool:
        """
        Обновить поля сделки.
        Возвращает True при успехе, False при ошибке CRM.
        """
        ...

    @abstractmethod
    async def create_deal(self, data: dict) -> str:
        """
        Создать новую сделку.
        Возвращает ID созданной сделки в виде строки.
        """
        ...

    @abstractmethod
    async def get_contacts(self, filters: dict | None = None) -> list[ContactInfo]:
        """Вернуть список контактов, опционально отфильтрованных по filters."""
        ...

    @abstractmethod
    async def verify_deal_exists(self, deal_id: str) -> bool:
        """Проверить, существует ли сделка с данным ID."""
        ...

    @abstractmethod
    async def get_deal_amount(self, deal_id: str) -> float | None:
        """
        Вернуть сумму сделки.
        None — если сделка не найдена или сумма не задана.
        """
        ...

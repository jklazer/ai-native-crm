"""
Адаптер для Bitrix24 REST API.

Все запросы идут через webhook-URL (параметр bitrix_webhook).
Формат: https://<domain>/rest/<user_id>/<token>/

Документация Bitrix24 REST:
  crm.deal.list   — https://dev.1c-bitrix.ru/rest_help/crm/deals/crm_deal_list.php
  crm.deal.get    — https://dev.1c-bitrix.ru/rest_help/crm/deals/crm_deal_get.php
  crm.deal.update — https://dev.1c-bitrix.ru/rest_help/crm/deals/crm_deal_update.php
  crm.deal.add    — https://dev.1c-bitrix.ru/rest_help/crm/deals/crm_deal_add.php
  crm.contact.list — https://dev.1c-bitrix.ru/rest_help/crm/contacts/crm_contact_list.php
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from ai_native_crm.adapters.base import ContactInfo, CRMAdapter, DealInfo

log = logging.getLogger(__name__)

# Единый тайм-аут для всех запросов к Bitrix24
_TIMEOUT = aiohttp.ClientTimeout(total=15)

# Семантические статусы, которые считаются «открытыми» сделками:
#   P — в процессе (In progress)
#   F — успешно завершена (Won)
_OPEN_STAGES = ["P", "F"]


class BitrixAdapter(CRMAdapter):
    """
    Реализация CRMAdapter для Bitrix24.

    Args:
        webhook: базовый URL webhook, например
                 'https://b24-xxx.bitrix24.ru/rest/1/token/'
    """

    def __init__(self, webhook: str) -> None:
        # Убеждаемся, что URL заканчивается на /
        self._webhook = webhook.rstrip("/") + "/"
        # Единая сессия на весь жизненный цикл адаптера — избегаем
        # накладных расходов на создание TCP-соединений для каждого запроса.
        # Ленивая инициализация: создаётся при первом запросе,
        # чтобы не требовать наличия event loop при __init__.
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Получить или создать aiohttp.ClientSession."""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=10)
            self._session = aiohttp.ClientSession(
                timeout=_TIMEOUT,
                connector=connector,
            )
        return self._session

    async def close(self) -> None:
        """Закрыть HTTP-сессию. Вызывать при shutdown."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "BitrixAdapter":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _url(self, method: str) -> str:
        """Собрать полный URL для метода Bitrix24 REST API."""
        return f"{self._webhook}{method}"

    async def _get(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Выполнить GET-запрос к Bitrix24 REST API.
        Возвращает распарсенный JSON-ответ.
        """
        url = self._url(method)
        try:
            session = await self._get_session()
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                return await resp.json()
        except asyncio.TimeoutError:
            log.error("Bitrix24 GET %s: таймаут (%ss)", method, _TIMEOUT.total)
            raise
        except aiohttp.ClientError as exc:
            log.error("Bitrix24 GET %s ошибка: %s", method, exc)
            raise

    async def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Выполнить POST-запрос к Bitrix24 REST API.
        Возвращает распарсенный JSON-ответ.
        """
        url = self._url(method)
        try:
            session = await self._get_session()
            async with session.post(url, json=payload) as resp:
                resp.raise_for_status()
                return await resp.json()
        except asyncio.TimeoutError:
            log.error("Bitrix24 POST %s: таймаут (%ss)", method, _TIMEOUT.total)
            raise
        except aiohttp.ClientError as exc:
            log.error("Bitrix24 POST %s ошибка: %s", method, exc)
            raise

    # ------------------------------------------------------------------
    # CRMAdapter: сделки
    # ------------------------------------------------------------------

    async def get_deals(self, filters: dict | None = None) -> list[DealInfo]:
        """
        Загрузить открытые сделки из Bitrix24.

        По умолчанию фильтрует по STAGE_SEMANTIC_ID = P (в работе) и F (выиграна).
        Если filters переданы явно — используются они.
        """
        # Формируем параметры запроса
        params: dict[str, Any] = {
            "select[]": ["ID", "TITLE", "STAGE_ID", "OPPORTUNITY", "CURRENCY_ID", "CONTACT_ID"],
        }

        if filters:
            for key, value in filters.items():
                params[key] = value
        else:
            # Только открытые и успешно завершённые сделки
            params["filter[STAGE_SEMANTIC_ID][]"] = _OPEN_STAGES

        try:
            data = await self._get("crm.deal.list", params=params)
            result: list[dict] = data.get("result", [])

            deals = [
                DealInfo(
                    id=str(d.get("ID", "")),
                    title=d.get("TITLE", ""),
                    stage=d.get("STAGE_ID", ""),
                    amount=float(d.get("OPPORTUNITY") or 0),
                    currency=d.get("CURRENCY_ID", "RUB"),
                    contact_id=str(d.get("CONTACT_ID", "") or ""),
                    # Имя контакта в crm.deal.list не возвращается — оставляем пустым
                    contact_name="",
                )
                for d in result
            ]
            log.info("Bitrix24 crm.deal.list: загружено %d сделок", len(deals))
            return deals

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.error("Ошибка get_deals (сеть/таймаут): %s", exc)
            return []
        except (KeyError, ValueError, TypeError) as exc:
            log.error("Ошибка get_deals (разбор ответа): %s", exc)
            return []

    async def update_deal(self, deal_id: str, fields: dict) -> bool:
        """
        Обновить поля сделки через crm.deal.update.

        Args:
            deal_id: числовой ID сделки в виде строки
            fields:  словарь полей Bitrix24, например {"STAGE_ID": "NEGOTIATION"}
        """
        payload = {"id": deal_id, "fields": fields}
        try:
            data = await self._post("crm.deal.update", payload)
            ok = bool(data.get("result", False))
            log.info("Bitrix24 crm.deal.update deal_id=%s: %s", deal_id, "OK" if ok else "FAIL")
            return ok
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.error("Ошибка update_deal deal_id=%s (сеть/таймаут): %s", deal_id, exc)
            return False
        except (KeyError, ValueError, TypeError) as exc:
            log.error("Ошибка update_deal deal_id=%s (разбор ответа): %s", deal_id, exc)
            return False

    async def create_deal(self, data: dict) -> str:
        """
        Создать новую сделку через crm.deal.add.

        Args:
            data: словарь полей Bitrix24 (TITLE, STAGE_ID, OPPORTUNITY, ...)

        Returns:
            ID созданной сделки в виде строки, или "" при ошибке.
        """
        payload = {"fields": data}
        try:
            resp = await self._post("crm.deal.add", payload)
            deal_id = str(resp.get("result", ""))
            log.info("Bitrix24 crm.deal.add: создана сделка ID=%s", deal_id)
            return deal_id
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.error("Ошибка create_deal (сеть/таймаут): %s", exc)
            return ""
        except (KeyError, ValueError, TypeError) as exc:
            log.error("Ошибка create_deal (разбор ответа): %s", exc)
            return ""

    # ------------------------------------------------------------------
    # CRMAdapter: контакты
    # ------------------------------------------------------------------

    async def get_contacts(self, filters: dict | None = None) -> list[ContactInfo]:
        """
        Загрузить контакты из Bitrix24 через crm.contact.list.

        Args:
            filters: опциональные фильтры в формате Bitrix24
        """
        params: dict[str, Any] = {
            "select[]": ["ID", "NAME", "LAST_NAME", "PHONE", "EMAIL", "COMPANY_ID"],
        }
        if filters:
            for key, value in filters.items():
                params[key] = value

        try:
            data = await self._get("crm.contact.list", params=params)
            result: list[dict] = data.get("result", [])

            contacts = [
                ContactInfo(
                    id=str(c.get("ID", "")),
                    name=f"{c.get('NAME', '')} {c.get('LAST_NAME', '')}".strip(),
                    # PHONE и EMAIL — списки объектов [{"VALUE": "...", "VALUE_TYPE": "WORK"}, ...]
                    phone=_extract_first_value(c.get("PHONE")),
                    email=_extract_first_value(c.get("EMAIL")),
                    company=str(c.get("COMPANY_ID", "") or ""),
                )
                for c in result
            ]
            log.info("Bitrix24 crm.contact.list: загружено %d контактов", len(contacts))
            return contacts

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.error("Ошибка get_contacts (сеть/таймаут): %s", exc)
            return []
        except (KeyError, ValueError, TypeError) as exc:
            log.error("Ошибка get_contacts (разбор ответа): %s", exc)
            return []

    # ------------------------------------------------------------------
    # CRMAdapter: вспомогательные методы
    # ------------------------------------------------------------------

    async def verify_deal_exists(self, deal_id: str) -> bool:
        """
        Проверить существование сделки через crm.deal.get.
        Возвращает True, если сделка найдена.
        """
        params = {"id": deal_id}
        try:
            data = await self._get("crm.deal.get", params=params)
            exists = bool(data.get("result"))
            log.debug("Bitrix24 verify_deal_exists deal_id=%s: %s", deal_id, exists)
            return exists
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.error("Ошибка verify_deal_exists deal_id=%s (сеть/таймаут): %s", deal_id, exc)
            return False
        except (KeyError, ValueError, TypeError) as exc:
            log.error("Ошибка verify_deal_exists deal_id=%s (разбор ответа): %s", deal_id, exc)
            return False

    async def get_deal_amount(self, deal_id: str) -> float | None:
        """
        Получить сумму сделки через crm.deal.get → поле OPPORTUNITY.
        Возвращает None, если сделка не найдена.
        """
        params = {"id": deal_id}
        try:
            data = await self._get("crm.deal.get", params=params)
            result = data.get("result")
            if not result:
                log.warning("Bitrix24 get_deal_amount: сделка %s не найдена", deal_id)
                return None
            raw = result.get("OPPORTUNITY")
            if raw is None:
                return None
            amount = float(raw)
            log.debug("Bitrix24 get_deal_amount deal_id=%s: %.2f", deal_id, amount)
            return amount
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.error("Ошибка get_deal_amount deal_id=%s (сеть/таймаут): %s", deal_id, exc)
            return None
        except (KeyError, ValueError, TypeError) as exc:
            log.error("Ошибка get_deal_amount deal_id=%s (разбор ответа): %s", deal_id, exc)
            return None


# ---------------------------------------------------------------------------
# Вспомогательная функция
# ---------------------------------------------------------------------------

def _extract_first_value(field: Any) -> str:
    """
    Извлечь первое значение из мультиполя Bitrix24 (PHONE / EMAIL).

    Bitrix возвращает такие поля как список dict-ов:
      [{"VALUE": "+7-999-123-45-67", "VALUE_TYPE": "WORK"}, ...]
    Если поле пустое или не является списком — возвращаем "".
    """
    if not field or not isinstance(field, list):
        return ""
    first = field[0]
    if isinstance(first, dict):
        return str(first.get("VALUE", ""))
    return ""

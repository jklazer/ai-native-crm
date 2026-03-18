"""
AmoCRM API v4 adapter.
https://www.amocrm.ru/developers/content/crm_platform/leads-api

OAuth2: access_token + refresh_token, auto-refresh on 401.
Pipeline stages → universal stages mapping.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from ai_native_crm.adapters.base import ContactInfo, CRMAdapter, DealInfo

log = logging.getLogger(__name__)

# Единый тайм-аут для всех запросов к AmoCRM
_TIMEOUT = aiohttp.ClientTimeout(total=15)

# Маппинг человекочитаемых названий статусов AmoCRM → универсальные стадии.
# AmoCRM хранит название статуса в поле name внутри _embedded.statuses.
# Если название не совпало — возвращаем "UNKNOWN".
_STAGE_MAP: dict[str, str] = {
    "Первичный контакт": "NEW",
    "Переговоры": "NEGOTIATION",
    "Принятие решения": "DECISION",
    "Согласование договора": "PROPOSAL",
    "Успешно реализовано": "WON",
    "Закрыто и не реализовано": "LOST",
}

_DEFAULT_STAGE = "UNKNOWN"


class AmoAdapter(CRMAdapter):
    """
    Реализация CRMAdapter для AmoCRM API v4.

    Args:
        subdomain:     поддомен аккаунта, например 'mycompany'
                       (итоговый хост: mycompany.amocrm.ru)
        access_token:  Bearer-токен для авторизации запросов
        refresh_token: токен обновления (OAuth2); используется при 401
        client_id:     ID интеграции OAuth2
        client_secret: секрет интеграции OAuth2
        redirect_uri:  URI перенаправления OAuth2
    """

    def __init__(
        self,
        subdomain: str,
        access_token: str,
        refresh_token: str = "",
        client_id: str = "",
        client_secret: str = "",
        redirect_uri: str = "",
    ) -> None:
        self._base_url = f"https://{subdomain}.amocrm.ru"
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        # Ленивая инициализация сессии — не требует event loop при __init__
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    # Управление сессией
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        """Получить или создать aiohttp.ClientSession."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_TIMEOUT)
        return self._session

    async def close(self) -> None:
        """Закрыть HTTP-сессию. Вызывать при shutdown."""
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # OAuth2: обновление токена
    # ------------------------------------------------------------------

    async def _refresh_access_token(self) -> bool:
        """
        Обновить access_token через refresh_token (OAuth2).

        Вызывается автоматически при получении 401 от API.
        Возвращает True при успехе, False — если обновление невозможно
        (не заданы client_id / client_secret / refresh_token).
        """
        if not all([self._refresh_token, self._client_id, self._client_secret]):
            log.warning(
                "AmoCRM: невозможно обновить токен — "
                "не заданы refresh_token / client_id / client_secret"
            )
            return False

        url = f"{self._base_url}/oauth2/access_token"
        payload = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "redirect_uri": self._redirect_uri,
        }
        try:
            session = await self._get_session()
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    log.error(
                        "AmoCRM: обновление токена вернуло %d: %s", resp.status, text
                    )
                    return False
                data = await resp.json()
                self._access_token = data["access_token"]
                self._refresh_token = data.get("refresh_token", self._refresh_token)
                log.info("AmoCRM: access_token успешно обновлён")
                return True
        except Exception as exc:
            log.error("AmoCRM: ошибка при обновлении токена: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Базовые HTTP-методы с автоматическим повтором при 401
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    async def _get(
        self, path: str, params: dict[str, Any] | None = None, _retry: bool = True
    ) -> dict[str, Any]:
        """
        GET-запрос к AmoCRM API v4.

        При статусе 401 однократно пробует обновить токен и повторить запрос.
        """
        url = f"{self._base_url}{path}"
        try:
            session = await self._get_session()
            async with session.get(url, params=params, headers=self._auth_headers()) as resp:
                if resp.status == 401 and _retry:
                    log.info("AmoCRM GET %s: 401, пробуем обновить токен", path)
                    if await self._refresh_access_token():
                        return await self._get(path, params=params, _retry=False)
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientError as exc:
            log.error("AmoCRM GET %s ошибка: %s", path, exc)
            raise

    async def _patch(
        self, path: str, payload: Any, _retry: bool = True
    ) -> dict[str, Any]:
        """
        PATCH-запрос к AmoCRM API v4.

        При статусе 401 однократно пробует обновить токен и повторить запрос.
        """
        url = f"{self._base_url}{path}"
        try:
            session = await self._get_session()
            async with session.patch(url, json=payload, headers=self._auth_headers()) as resp:
                if resp.status == 401 and _retry:
                    log.info("AmoCRM PATCH %s: 401, пробуем обновить токен", path)
                    if await self._refresh_access_token():
                        return await self._patch(path, payload, _retry=False)
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientError as exc:
            log.error("AmoCRM PATCH %s ошибка: %s", path, exc)
            raise

    async def _post(
        self, path: str, payload: Any, _retry: bool = True
    ) -> dict[str, Any]:
        """
        POST-запрос к AmoCRM API v4.

        При статусе 401 однократно пробует обновить токен и повторить запрос.
        """
        url = f"{self._base_url}{path}"
        try:
            session = await self._get_session()
            async with session.post(url, json=payload, headers=self._auth_headers()) as resp:
                if resp.status == 401 and _retry:
                    log.info("AmoCRM POST %s: 401, пробуем обновить токен", path)
                    if await self._refresh_access_token():
                        return await self._post(path, payload, _retry=False)
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientError as exc:
            log.error("AmoCRM POST %s ошибка: %s", path, exc)
            raise

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    @staticmethod
    def _map_stage(status_name: str) -> str:
        """Перевести название статуса AmoCRM в универсальную стадию."""
        return _STAGE_MAP.get(status_name, _DEFAULT_STAGE)

    @staticmethod
    def _extract_contact_name(lead: dict[str, Any]) -> tuple[str, str]:
        """
        Извлечь имя и ID первого связанного контакта из сделки.

        AmoCRM возвращает контакты внутри _embedded.contacts при запросе
        с параметром with=contacts.
        """
        embedded = lead.get("_embedded") or {}
        contacts = embedded.get("contacts") or []
        if contacts:
            first = contacts[0]
            return str(first.get("id", "")), first.get("name", "")
        return "", ""

    @staticmethod
    def _fields_to_amo(fields: dict) -> dict:
        """
        Преобразовать универсальный словарь полей в формат AmoCRM.

        Поддерживаемые маппинги:
          STAGE_ID  → status_id  (числовой ID статуса в AmoCRM)
          TITLE     → name
          AMOUNT    → price
        Остальные ключи передаются как есть — это позволяет передавать
        нативные поля AmoCRM напрямую, если нужно.
        """
        mapping = {
            "STAGE_ID": "status_id",
            "TITLE": "name",
            "AMOUNT": "price",
        }
        result: dict[str, Any] = {}
        for key, value in fields.items():
            result[mapping.get(key, key)] = value
        return result

    # ------------------------------------------------------------------
    # CRMAdapter: сделки
    # ------------------------------------------------------------------

    async def get_deals(self, filters: dict | None = None) -> list[DealInfo]:
        """
        Загрузить сделки из AmoCRM.

        Запрашивает до 250 лидов за раз, включая связанные контакты.
        Параметр filters игнорируется в текущей реализации — AmoCRM
        поддерживает фильтрацию через query-параметры, которые можно
        расширить при необходимости.
        """
        params: dict[str, Any] = {"with": "contacts", "limit": 250}

        try:
            data = await self._get("/api/v4/leads", params=params)
            embedded = data.get("_embedded") or {}
            leads: list[dict] = embedded.get("leads") or []

            deals: list[DealInfo] = []
            for lead in leads:
                # Статус хранится в поле _embedded.statuses внутри самого лида
                # (при запросе без with=pipeline), либо в status_id + pipeline_id.
                # Название статуса доступно только при дополнительном запросе к
                # /api/v4/leads/pipelines/{id}/statuses/{id}.
                # Для простоты используем числовой status_id как stage,
                # если нет явного маппинга по имени.
                status_id = lead.get("status_id")
                # AmoCRM не возвращает имя статуса в списке лидов — используем
                # числовой id в виде строки как fallback
                stage = str(status_id) if status_id is not None else _DEFAULT_STAGE

                contact_id, contact_name = self._extract_contact_name(lead)

                deals.append(
                    DealInfo(
                        id=str(lead.get("id", "")),
                        title=lead.get("name", ""),
                        stage=stage,
                        amount=float(lead.get("price") or 0),
                        currency="RUB",
                        contact_id=contact_id,
                        contact_name=contact_name,
                    )
                )

            log.info("AmoCRM GET /api/v4/leads: загружено %d сделок", len(deals))
            return deals

        except Exception as exc:
            log.error("Ошибка get_deals: %s", exc)
            return []

    async def update_deal(self, deal_id: str, fields: dict) -> bool:
        """
        Обновить поля сделки через PATCH /api/v4/leads/{deal_id}.

        Args:
            deal_id: числовой ID сделки в виде строки
            fields:  универсальный словарь полей (STAGE_ID, TITLE, AMOUNT)
                     или нативные поля AmoCRM (status_id, name, price)
        """
        payload = self._fields_to_amo(fields)
        try:
            await self._patch(f"/api/v4/leads/{deal_id}", payload)
            log.info("AmoCRM PATCH /api/v4/leads/%s: OK", deal_id)
            return True
        except Exception as exc:
            log.error("Ошибка update_deal deal_id=%s: %s", deal_id, exc)
            return False

    async def create_deal(self, data: dict) -> str:
        """
        Создать новую сделку через POST /api/v4/leads.

        Args:
            data: словарь полей (поддерживаются TITLE/name, AMOUNT/price,
                  STAGE_ID/status_id и любые нативные поля AmoCRM)

        Returns:
            ID созданной сделки в виде строки, или "" при ошибке.
        """
        payload = [self._fields_to_amo(data)]
        try:
            resp = await self._post("/api/v4/leads", payload)
            embedded = resp.get("_embedded") or {}
            leads: list[dict] = embedded.get("leads") or []
            if not leads:
                log.error("AmoCRM create_deal: пустой _embedded.leads в ответе")
                return ""
            deal_id = str(leads[0].get("id", ""))
            log.info("AmoCRM POST /api/v4/leads: создана сделка ID=%s", deal_id)
            return deal_id
        except Exception as exc:
            log.error("Ошибка create_deal: %s", exc)
            return ""

    # ------------------------------------------------------------------
    # CRMAdapter: контакты
    # ------------------------------------------------------------------

    async def get_contacts(self, filters: dict | None = None) -> list[ContactInfo]:
        """
        Загрузить контакты из AmoCRM через GET /api/v4/contacts.

        Args:
            filters: не используется в текущей реализации
        """
        params: dict[str, Any] = {"limit": 250}

        try:
            data = await self._get("/api/v4/contacts", params=params)
            embedded = data.get("_embedded") or {}
            raw_contacts: list[dict] = embedded.get("contacts") or []

            contacts: list[ContactInfo] = []
            for c in raw_contacts:
                # Телефон и email хранятся в custom_fields_values как массив
                phone = _extract_custom_field(c, "PHONE")
                email = _extract_custom_field(c, "EMAIL")

                contacts.append(
                    ContactInfo(
                        id=str(c.get("id", "")),
                        name=c.get("name", ""),
                        phone=phone,
                        email=email,
                        company=str(c.get("company_id") or ""),
                    )
                )

            log.info("AmoCRM GET /api/v4/contacts: загружено %d контактов", len(contacts))
            return contacts

        except Exception as exc:
            log.error("Ошибка get_contacts: %s", exc)
            return []

    # ------------------------------------------------------------------
    # CRMAdapter: вспомогательные методы
    # ------------------------------------------------------------------

    async def verify_deal_exists(self, deal_id: str) -> bool:
        """
        Проверить существование сделки через GET /api/v4/leads/{deal_id}.

        Возвращает True при статусе 200, False при 404 или любой ошибке.
        """
        url = f"{self._base_url}/api/v4/leads/{deal_id}"
        try:
            session = await self._get_session()
            async with session.get(
                url, headers=self._auth_headers(), timeout=_TIMEOUT
            ) as resp:
                if resp.status == 401:
                    if await self._refresh_access_token():
                        async with session.get(
                            url, headers=self._auth_headers(), timeout=_TIMEOUT
                        ) as retry_resp:
                            exists = retry_resp.status == 200
                            log.debug(
                                "AmoCRM verify_deal_exists deal_id=%s: %s", deal_id, exists
                            )
                            return exists
                    return False
                exists = resp.status == 200
                log.debug("AmoCRM verify_deal_exists deal_id=%s: %s", deal_id, exists)
                return exists
        except Exception as exc:
            log.error("Ошибка verify_deal_exists deal_id=%s: %s", deal_id, exc)
            return False

    async def get_deal_amount(self, deal_id: str) -> float | None:
        """
        Получить сумму сделки через GET /api/v4/leads/{deal_id} → поле price.

        Возвращает None, если сделка не найдена или price не задан.
        """
        try:
            data = await self._get(f"/api/v4/leads/{deal_id}")
            raw = data.get("price")
            if raw is None:
                log.warning("AmoCRM get_deal_amount: поле price отсутствует в сделке %s", deal_id)
                return None
            amount = float(raw)
            log.debug("AmoCRM get_deal_amount deal_id=%s: %.2f", deal_id, amount)
            return amount
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                log.warning("AmoCRM get_deal_amount: сделка %s не найдена", deal_id)
                return None
            log.error("Ошибка get_deal_amount deal_id=%s: %s", deal_id, exc)
            return None
        except Exception as exc:
            log.error("Ошибка get_deal_amount deal_id=%s: %s", deal_id, exc)
            return None


# ---------------------------------------------------------------------------
# Вспомогательная функция
# ---------------------------------------------------------------------------

def _extract_custom_field(contact: dict[str, Any], field_code: str) -> str:
    """
    Извлечь первое значение кастомного поля из контакта AmoCRM.

    AmoCRM хранит телефоны и email в custom_fields_values:
      [{"field_code": "PHONE", "values": [{"value": "+7-999-..."}]}, ...]
    """
    fields: list[dict] = contact.get("custom_fields_values") or []
    for field in fields:
        if field.get("field_code") == field_code:
            values: list[dict] = field.get("values") or []
            if values:
                return str(values[0].get("value", ""))
    return ""

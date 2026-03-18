"""
PII-анонимизатор в соответствии с 152-ФЗ «О персональных данных».

Маскирует: ФИО, телефоны, email-адреса.
Маппинг токен → оригинал хранится в Redis с TTL.
"""
from __future__ import annotations

import re
import json
import logging
from typing import Any

from redis.asyncio import Redis

from ai_native_crm.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Регулярные выражения
# ---------------------------------------------------------------------------

# ФИО — полное: Иванов Иван Иванович (кириллица, заглавная первая буква)
_RE_FULL_NAME = re.compile(
    r"\b([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)\s+"   # Фамилия
    r"([А-ЯЁ][а-яё]+)\s+"                            # Имя
    r"([А-ЯЁ][а-яё]+(?:ич|на|вна|евна|овна))\b",    # Отчество
    re.UNICODE,
)

# ФИО — сокращённое: Иванов С.П. или Иванов С. П.
_RE_SHORT_NAME = re.compile(
    r"\b([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)\s+"     # Фамилия
    r"([А-ЯЁ])\.\s*([А-ЯЁ])\.",                      # И.О.
    re.UNICODE,
)

# Телефоны: +7/8 с различными разделителями
# Примеры: +7 (999) 123-45-67, 8-999-123-45-67, 89991234567
_RE_PHONE = re.compile(
    r"(?<!\d)"                          # не предшествует цифра
    r"(\+?[78])"                        # +7 или 8
    r"[\s\-\(]?"
    r"(\d{3})"                          # код
    r"[\s\-\)]?"
    r"(\d{3})"
    r"[\s\-]?"
    r"(\d{2})"
    r"[\s\-]?"
    r"(\d{2})"
    r"(?!\d)",                          # не следует цифра
)

# Email
_RE_EMAIL = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)

# Порядок применения важен: сначала полные ФИО, потом сокращённые
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("PERSON", _RE_FULL_NAME),
    ("PERSON", _RE_SHORT_NAME),
    ("PHONE", _RE_PHONE),
    ("EMAIL", _RE_EMAIL),
]


class PIIAnonymizer:
    """152-ФЗ: заменяет ПДн на обратимые токены [PERSON_1], [PHONE_1] и т.д.

    Маппинг токен → оригинальная строка персистируется в Redis Hash
    с ключом pii:{session_id} и TTL = settings.pii_ttl_sec.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    async def anonymize(self, text: str, session_id: str) -> str:
        """Заменить ПДн на токены и сохранить маппинг в Redis."""
        if not settings.pii_enabled:
            return text

        # TODO [MEDIUM]: if _load_mapping fails, deanonymize will silently fail for this session
        mapping = await self._load_mapping(session_id)

        # Счётчики по типам — берём максимальный существующий номер
        counters: dict[str, int] = {}
        for token in mapping.keys():
            match = re.match(r"\[([A-Z]+)_(\d+)\]", token)
            if match:
                kind = match.group(1)
                num = int(match.group(2))
                counters[kind] = max(counters.get(kind, 0), num)

        result = text

        for kind, pattern in _PATTERNS:
            result = self._replace_matches(result, pattern, kind, counters, mapping)

        if mapping:
            await self._save_mapping(session_id, mapping)

        return result

    async def deanonymize(self, text: str, session_id: str) -> str:
        """Восстановить оригинальные ПДн из токенов."""
        if not settings.pii_enabled:
            return text

        mapping = await self._load_mapping(session_id)
        if not mapping:
            return text

        result = text
        # Заменяем все токены обратно (от длинных к коротким — на всякий случай)
        for token, original in sorted(mapping.items(), key=lambda x: -len(x[0])):
            result = result.replace(token, original)

        return result

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _replace_matches(
        self,
        text: str,
        pattern: re.Pattern,
        kind: str,
        counters: dict[str, int],
        mapping: dict[str, str],
    ) -> str:
        """Найти совпадения, назначить/переиспользовать токены, подставить."""

        # Обратный маппинг: оригинал → токен (для дедупликации)
        reverse: dict[str, str] = {v: k for k, v in mapping.items()}

        def replace_fn(m: re.Match) -> str:
            original = m.group(0)

            # Если уже замаппировано — переиспользуем токен
            if original in reverse:
                return reverse[original]

            counters[kind] = counters.get(kind, 0) + 1
            token = f"[{kind}_{counters[kind]}]"

            mapping[token] = original
            reverse[original] = token
            return token

        return pattern.sub(replace_fn, text)

    def _redis_key(self, session_id: str) -> str:
        return f"pii:{session_id}"

    async def _load_mapping(self, session_id: str) -> dict[str, str]:
        """Загрузить маппинг из Redis Hash. Вернуть пустой dict при отсутствии.

        Redis-клиент инициализирован с decode_responses=True,
        поэтому hgetall возвращает dict[str, str] (не bytes).
        """
        key = self._redis_key(session_id)
        try:
            raw: dict[str, str] = await self._redis.hgetall(key)
            return dict(raw)
        except Exception as exc:
            logger.error("Ошибка чтения PII-маппинга из Redis: %s", exc)
            return {}

    async def _save_mapping(self, session_id: str, mapping: dict[str, str]) -> None:
        """Сохранить маппинг в Redis Hash с обновлением TTL."""
        key = self._redis_key(session_id)
        try:
            pipe = self._redis.pipeline()
            pipe.hset(key, mapping=mapping)
            pipe.expire(key, settings.pii_ttl_sec)
            await pipe.execute()
        except Exception as exc:
            logger.error("Ошибка записи PII-маппинга в Redis: %s", exc)

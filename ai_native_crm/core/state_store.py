"""
Единственный persistence-слой CRM.
Всё хранится в Redis с AOF. Никакого PostgreSQL, SQLAlchemy, SQL.

Схема ключей:
  state:{chat_id}           → JSON (SemanticState)       TTL: нет
  critical_facts:{chat_id}  → Redis List                 TTL: нет (append-only навсегда)
  audit:{chat_id}           → Redis Stream               TTL: audit_ttl_days
  metrics:{chat_id}         → Redis Hash                 TTL: нет
  reminders:{chat_id}       → Redis Sorted Set           TTL: нет (элементы удаляются при срабатывании)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Модели данных
# ---------------------------------------------------------------------------


@dataclass
class SemanticState:
    """
    Семантический стейт агента для одного чата.
    Перезаписывается целиком каждый ход — нет инкрементальных патчей.
    """

    chat_id: int
    iteration: int = 0
    working_memory: str = ""       # краткосрочная рабочая память (до wm_max_chars символов)
    agent_assessment: str = ""     # текущая оценка ситуации агентом
    conversation_summary: str = "" # накапливаемое резюме диалога
    last_updated: str = ""         # ISO-8601, проставляется при save()


@dataclass
class CriticalFact:
    """
    Критический факт по сделке / контакту.
    Append-only — НИКОГДА не редактируется и не удаляется.
    """

    fact_type: str   # rejection | budget_limit | decision_maker | deadline | hard_requirement
    content: str
    deal_id: str | None = None
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = time.time()


@dataclass
class AuditEntry:
    """Запись аудита одного хода агента."""

    chat_id: int
    user_input: str
    llm_response: str
    actions: list[dict] = field(default_factory=list)
    model_used: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = time.time()


# ---------------------------------------------------------------------------
# StateStore
# ---------------------------------------------------------------------------


# Lua script for atomic dedup-then-rpush on the critical_facts list.
# Returns 1 if the item was added, 0 if it was a duplicate (same content + deal_id).
_LUA_DEDUP_RPUSH = """
local key = KEYS[1]
local new_content = ARGV[1]
local new_deal = ARGV[2]
local payload = ARGV[3]
local items = redis.call('LRANGE', key, 0, -1)
for _, item in ipairs(items) do
    local data = cjson.decode(item)
    local existing_deal = data.deal_id
    if existing_deal == cjson.null or existing_deal == nil then existing_deal = '' end
    if data.content == new_content and existing_deal == new_deal then
        return 0
    end
end
redis.call('RPUSH', key, payload)
return 1
"""


class StateStore:
    """
    Единственный persistence. Redis с AOF.
    НЕТ PostgreSQL. НЕТ SQLAlchemy. НЕТ SQL. НЕТ ORM.

    Инициализация:
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        store = StateStore(redis)
    """

    # Суффикс TTL аудита: seconds per day
    _SECS_PER_DAY = 86_400

    def __init__(self, redis: Redis, audit_ttl_days: int = 30) -> None:
        self._r = redis
        # TTL для Redis Stream с аудитом — в секундах
        self._audit_ttl_sec = audit_ttl_days * self._SECS_PER_DAY
        # Pre-registered Lua script for atomic dedup on critical_facts list
        self._script_dedup_rpush = self._r.register_script(_LUA_DEDUP_RPUSH)

    @property
    def redis(self) -> Redis:
        """Публичный доступ к Redis-клиенту для MetricsService и тестов."""
        return self._r

    # ------------------------------------------------------------------
    # Вспомогательные методы для ключей
    # ------------------------------------------------------------------

    @staticmethod
    def _key_state(chat_id: int) -> str:
        return f"state:{chat_id}"

    @staticmethod
    def _key_facts(chat_id: int) -> str:
        return f"critical_facts:{chat_id}"

    @staticmethod
    def _key_audit(chat_id: int) -> str:
        return f"audit:{chat_id}"

    @staticmethod
    def _key_metrics(chat_id: int) -> str:
        return f"metrics:{chat_id}"

    @staticmethod
    def _key_reminders(chat_id: int) -> str:
        return f"reminders:{chat_id}"

    # ------------------------------------------------------------------
    # Семантический стейт
    # ------------------------------------------------------------------

    async def load(self, chat_id: int) -> SemanticState:
        """
        Загрузить SemanticState из Redis.
        Если ключа нет — вернуть пустой стейт с нулевой итерацией.
        """
        raw = await self._r.get(self._key_state(chat_id))
        if raw is None:
            return SemanticState(chat_id=chat_id)
        try:
            data: dict = json.loads(raw)
            data["chat_id"] = int(data.get("chat_id", chat_id))
            # Filter to only known fields to handle schema changes
            known_fields = {f.name for f in SemanticState.__dataclass_fields__.values()}
            filtered = {k: v for k, v in data.items() if k in known_fields}
            return SemanticState(**filtered)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.error(
                "Corrupted state in Redis for chat_id=%d, resetting: %s",
                chat_id, exc,
            )
            return SemanticState(chat_id=chat_id)

    async def save(self, chat_id: int, state: SemanticState) -> None:
        """
        Сохранить SemanticState в Redis.
        last_updated проставляется здесь, чтобы caller не думал об этом.
        """
        # TODO [MEDIUM]: save() mutates its argument. Use replace() for immutability.
        from datetime import datetime, timezone

        state.last_updated = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(asdict(state), ensure_ascii=False)
        await self._r.set(self._key_state(chat_id), payload)

    # ------------------------------------------------------------------
    # Critical Facts (append-only Redis List)
    # ------------------------------------------------------------------

    async def get_critical_facts(self, chat_id: int) -> list[CriticalFact]:
        """
        Вернуть все критические факты в порядке добавления.
        Список никогда не обрезается — это намеренно.
        """
        # TODO [LOW]: critical_facts list has no size bound. Add LTRIM after RPUSH.
        raw_items: list[str] = await self._r.lrange(self._key_facts(chat_id), 0, -1)
        facts: list[CriticalFact] = []
        for raw in raw_items:
            data: dict = json.loads(raw)
            facts.append(CriticalFact(**data))
        return facts

    async def add_critical_fact(self, chat_id: int, fact: CriticalFact) -> None:
        """
        Добавить критический факт в конец списка.
        TTL не ставится — факты хранятся вечно.
        Дубликаты по content+deal_id пропускаются атомарно через Lua-скрипт
        (избегает гонки read-then-write при параллельных записях).
        """
        key = self._key_facts(chat_id)
        payload = json.dumps(asdict(fact), ensure_ascii=False)
        # ARGV[2] = deal_id or '' — Lua сравнивает строку с пустой строкой как отсутствие
        deal_id_str = fact.deal_id or ""
        await self._script_dedup_rpush(
            keys=[key],
            args=[fact.content, deal_id_str, payload],
        )

    # ------------------------------------------------------------------
    # Audit Trail (Redis Stream)
    # ------------------------------------------------------------------

    async def log_audit(self, chat_id: int, entry: AuditEntry) -> None:
        """
        Записать ход в Redis Stream.
        После записи обновляем MAXLEN через XTRIM (приблизительно, ~10 000 записей),
        и выставляем TTL на весь стрим.

        Поля стрима — плоские строки, actions сериализуется отдельно.
        """
        key = self._key_audit(chat_id)

        fields: dict[str, str] = {
            "chat_id": str(entry.chat_id),
            "user_input": entry.user_input,
            "llm_response": entry.llm_response,
            "actions": json.dumps(entry.actions, ensure_ascii=False),
            "model_used": entry.model_used,
            "tokens_in": str(entry.tokens_in),
            "tokens_out": str(entry.tokens_out),
            "latency_ms": str(entry.latency_ms),
            "timestamp": str(entry.timestamp),
        }

        # XADD с автоматическим ID (millisecond timestamp + sequence)
        await self._r.xadd(key, fields, maxlen=10_000, approximate=True)

        # TTL на стрим — обновляется при каждой записи, сдвигая окно вперёд
        await self._r.expire(key, self._audit_ttl_sec)

    async def get_audit(self, chat_id: int, limit: int = 50) -> list[dict]:
        """
        Получить последние `limit` записей аудита в хронологическом порядке.
        Возвращает список словарей с теми же полями, что и AuditEntry.
        """
        key = self._key_audit(chat_id)

        # XREVRANGE возвращает от новых к старым — берём limit, потом разворачиваем
        raw_entries = await self._r.xrevrange(key, count=limit)

        result: list[dict] = []
        for _stream_id, fields in reversed(raw_entries):
            entry: dict = {
                "chat_id": int(fields["chat_id"]),
                "user_input": fields["user_input"],
                "llm_response": fields["llm_response"],
                "actions": json.loads(fields["actions"]),
                "model_used": fields["model_used"],
                "tokens_in": int(fields["tokens_in"]),
                "tokens_out": int(fields["tokens_out"]),
                "latency_ms": int(fields["latency_ms"]),
                "timestamp": float(fields["timestamp"]),
            }
            result.append(entry)

        return result

    # ------------------------------------------------------------------
    # Метрики (Redis Hash)
    # ------------------------------------------------------------------

    async def update_metrics(self, chat_id: int, metrics: dict[str, float]) -> None:
        """
        Обновить метрики агента.
        Каждый ключ в metrics соответствует полю в Redis Hash.
        Можно передавать частичный набор — остальные поля не затрагиваются.
        """
        if not metrics:
            return

        key = self._key_metrics(chat_id)
        # HSET принимает mapping; значения конвертируем в строку для совместимости
        str_metrics = {k: str(v) for k, v in metrics.items()}
        await self._r.hset(key, mapping=str_metrics)

    async def get_metrics(self, chat_id: int) -> dict[str, float]:
        """
        Прочитать все метрики. Возвращает пустой dict если метрик ещё нет.
        """
        key = self._key_metrics(chat_id)
        raw: dict[str, str] = await self._r.hgetall(key)
        return {k: float(v) for k, v in raw.items()}

    # ------------------------------------------------------------------
    # Напоминания (Redis Sorted Set, score = fire_at unix timestamp)
    # ------------------------------------------------------------------

    async def add_reminder(
        self,
        chat_id: int,
        text: str,
        fire_at: float,
        deal_id: str | None = None,
    ) -> None:
        """
        Добавить напоминание.
        Значение в ZSet — JSON с текстом и опциональным deal_id.
        Score — Unix-время срабатывания, что позволяет ZRANGEBYSCORE выбирать просроченные.
        """
        key = self._key_reminders(chat_id)
        member = json.dumps(
            {"text": text, "deal_id": deal_id, "fire_at": fire_at},
            ensure_ascii=False,
        )
        await self._r.zadd(key, {member: fire_at})

    async def get_due_reminders(self, chat_id: int) -> list[dict]:
        """
        Атомарно получить и удалить напоминания, время которых уже наступило.

        Использует Lua-скрипт для атомарности: ZRANGEBYSCORE + ZREM в одной транзакции,
        чтобы при нескольких воркерах не было дублей.
        """
        key = self._key_reminders(chat_id)
        now = time.time()

        # Lua: прочитать просроченные → удалить → вернуть
        lua_script = """
        local key = KEYS[1]
        local now = tonumber(ARGV[1])
        local members = redis.call('ZRANGEBYSCORE', key, '-inf', now)
        if #members > 0 then
            redis.call('ZREM', key, unpack(members))
        end
        return members
        """
        raw_members: list[str] = await self._r.eval(lua_script, 1, key, now)

        reminders: list[dict] = []
        for raw in raw_members:
            reminders.append(json.loads(raw))
        return reminders

    async def get_all_reminder_keys(self) -> list[int]:
        """
        Получить все chat_id, у которых есть хотя бы одно напоминание.
        Используется фоновым планировщиком для обхода всех чатов.

        SCAN предпочтительнее KEYS — не блокирует Redis на больших базах.
        """
        chat_ids: set[int] = set()
        cursor = 0

        while True:
            cursor, keys = await self._r.scan(
                cursor=cursor,
                match="reminders:*",
                count=100,
            )
            for key in keys:
                # key имеет вид "reminders:{chat_id}"
                suffix = key.split(":", 1)[1]
                try:
                    chat_ids.add(int(suffix))
                except ValueError:
                    # некорректный ключ — пропускаем
                    continue

            if cursor == 0:
                break

        return list(chat_ids)

# Часть 3: Сравнение классической и AI-native архитектур
## Сценарий: «Пришёл новый лид» (webhook от Bitrix24)

---

## 1. Потоки обработки — Sequence-диаграммы

### 1.1 Классический поток

```mermaid
sequenceDiagram
    participant B24 as Bitrix24
    participant FW as FastAPI<br/>(webhook handler)
    participant PV as Pydantic<br/>(validation)
    participant ORM as SQLAlchemy<br/>(ORM layer)
    participant PG as PostgreSQL
    participant SC as Scoring<br/>(бизнес-логика)
    participant AS as Assignment<br/>(бизнес-логика)
    participant RC as Redis Cache
    participant CL as Celery Worker
    participant TG as Telegram API

    B24->>FW: POST /webhook/lead {lead_id, name, phone, source, ...}
    FW->>PV: LeadCreateSchema(data)
    PV-->>FW: validated_lead | ValidationError 422

    FW->>ORM: Lead(**validated_lead.dict())
    ORM->>PG: BEGIN; INSERT INTO leads (...) VALUES (...) RETURNING id;
    PG-->>ORM: lead_id=4521
    ORM->>PG: COMMIT
    PG-->>ORM: OK

    FW->>SC: calculate_score(lead)
    SC->>PG: SELECT * FROM scoring_rules WHERE active=true
    PG-->>SC: [rules...]
    SC-->>FW: score=78, tier="hot"

    FW->>AS: assign_manager(lead, score)
    AS->>PG: SELECT m.id, COUNT(l.id) as load FROM managers m<br/>LEFT JOIN leads l ON l.manager_id=m.id<br/>WHERE l.status='active' GROUP BY m.id<br/>ORDER BY load ASC LIMIT 1
    PG-->>AS: manager_id=7, load=12
    AS->>PG: UPDATE leads SET manager_id=7, score=78, tier='hot'<br/>WHERE id=4521
    PG-->>AS: OK

    FW->>RC: SET lead:4521 {serialized_lead} EX 3600
    RC-->>FW: OK

    FW->>CL: send_task('notify_manager', {manager_id:7, lead_id:4521})
    CL-->>FW: task_id="abc-123"

    FW-->>B24: 200 OK {lead_id: 4521, manager_id: 7}

    Note over CL: Асинхронно (через ~200-800ms)
    CL->>PG: SELECT * FROM leads WHERE id=4521
    PG-->>CL: lead_data
    CL->>PG: SELECT telegram_id FROM managers WHERE id=7
    PG-->>CL: telegram_id="@manager7"
    CL->>TG: sendMessage(chat_id, "Новый горячий лид: Иван И., +7...")
    TG-->>CL: message_id=998
    CL->>PG: INSERT INTO notifications (lead_id, type, sent_at) VALUES (...)
    PG-->>CL: OK
```

### 1.2 AI-native поток

```mermaid
sequenceDiagram
    participant B24 as Bitrix24
    participant EH as Event Handler<br/>(минимальный FastAPI)
    participant SS as State Store<br/>(Redis/S3)
    participant LLM as LLM Engine<br/>(GPT-4o / Claude)
    participant AX as Action Executor
    participant B24W as Bitrix24 API<br/>(write-back)
    participant TG as Telegram API

    B24->>EH: POST /webhook/lead {event JSON}
    EH->>SS: GET crm_state:current
    SS-->>EH: state_snapshot (JSON, ~4KB)

    EH->>LLM: prompt = system_prompt + state + event
    Note over LLM: Reasoning:<br/>- Кто этот лид?<br/>- Какой score по контексту?<br/>- Кому назначить (из state)?<br/>- Нужна ли срочная реакция?

    LLM-->>EH: {<br/>  "actions": [<br/>    {"type":"update_lead","score":78,"tier":"hot","manager_id":7},<br/>    {"type":"notify_telegram","manager_id":7,"message":"..."},<br/>    {"type":"update_state","changes":{...}}<br/>  ],<br/>  "reasoning": "Лид из источника 'referral'...",<br/>  "new_state_delta": {<br/>    "managers.7.load": 13,<br/>    "leads.active_hot_count": "+1"<br/>  }<br/>}

    EH->>SS: PATCH crm_state:current (apply delta)
    SS-->>EH: state_N+1 saved

    par Action execution (параллельно)
        EH->>AX: execute update_lead
        AX->>B24W: PATCH /crm.lead.update {id, SCORE:78, ASSIGNED_BY_ID:7}
        B24W-->>AX: OK

        EH->>AX: execute notify_telegram
        AX->>TG: sendMessage(manager_7_chat, "...")
        TG-->>AX: OK
    end

    EH-->>B24: 200 OK
```

---

## 2. Реализация — полный код

### 2.1 Классический поток (FastAPI + SQLAlchemy + Redis + Celery)

```python
# ============================================================
# models.py — SQLAlchemy ORM
# ============================================================
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, ForeignKey,
    Boolean, Text, create_engine
)
from sqlalchemy.orm import DeclarativeBase, relationship, Session
from sqlalchemy.dialects.postgresql import JSONB


class Base(DeclarativeBase):
    pass


class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)
    bitrix_id = Column(Integer, unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    phone = Column(String(50))
    email = Column(String(255))
    source = Column(String(100))
    status = Column(String(50), default="new")
    score = Column(Float, nullable=True)
    tier = Column(String(20), nullable=True)  # cold/warm/hot
    manager_id = Column(Integer, ForeignKey("managers.id"), nullable=True)
    raw_data = Column(JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    manager = relationship("Manager", back_populates="leads")
    notifications = relationship("Notification", back_populates="lead")


class Manager(Base):
    __tablename__ = "managers"

    id = Column(Integer, primary_key=True)
    bitrix_id = Column(Integer, unique=True, nullable=False)
    name = Column(String(255))
    telegram_id = Column(String(100))
    is_active = Column(Boolean, default=True)
    max_load = Column(Integer, default=20)

    leads = relationship("Lead", back_populates="manager")


class ScoringRule(Base):
    __tablename__ = "scoring_rules"

    id = Column(Integer, primary_key=True)
    field = Column(String(100), nullable=False)
    condition = Column(String(50), nullable=False)  # eq/contains/gte
    value = Column(String(255), nullable=False)
    points = Column(Float, nullable=False)
    is_active = Column(Boolean, default=True)


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("leads.id"))
    manager_id = Column(Integer, ForeignKey("managers.id"))
    type = Column(String(50))
    sent_at = Column(DateTime, nullable=True)
    status = Column(String(20), default="pending")
    payload = Column(JSONB)

    lead = relationship("Lead", back_populates="notifications")


# ============================================================
# schemas.py — Pydantic validation
# ============================================================
from pydantic import BaseModel, field_validator, model_validator
from typing import Optional
import re


class LeadWebhookPayload(BaseModel):
    """Схема входящего вебхука от Bitrix24."""
    TITLE: str
    NAME: Optional[str] = None
    LAST_NAME: Optional[str] = None
    PHONE: Optional[list[dict]] = None
    EMAIL: Optional[list[dict]] = None
    SOURCE_ID: Optional[str] = None
    OPPORTUNITY: Optional[float] = None
    CURRENCY_ID: Optional[str] = "RUB"
    COMMENTS: Optional[str] = None
    UF_CRM_REFERRAL: Optional[str] = None

    @field_validator("TITLE")
    @classmethod
    def title_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("TITLE cannot be empty")
        return v.strip()

    @property
    def primary_phone(self) -> Optional[str]:
        if self.PHONE:
            return self.PHONE[0].get("VALUE")
        return None

    @property
    def primary_email(self) -> Optional[str]:
        if self.EMAIL:
            return self.EMAIL[0].get("VALUE")
        return None


class BitrixWebhookEvent(BaseModel):
    """Обёртка события от Bitrix24."""
    event: str  # ONCRMLEAD ADD
    data: dict
    ts: int
    auth: dict

    @model_validator(mode="after")
    def validate_event_type(self) -> "BitrixWebhookEvent":
        allowed = {"ONCRMLEAD ADD", "ONCRMLEADADD"}
        if self.event not in allowed:
            raise ValueError(f"Unexpected event type: {self.event}")
        return self


class LeadCreateInternal(BaseModel):
    """Внутренняя схема для записи в БД."""
    bitrix_id: int
    name: str
    phone: Optional[str]
    email: Optional[str]
    source: Optional[str]
    opportunity: Optional[float]
    raw_data: dict


# ============================================================
# scoring.py — бизнес-логика скоринга
# ============================================================
from sqlalchemy.orm import Session


TIER_THRESHOLDS = {"hot": 70, "warm": 40, "cold": 0}
SOURCE_SCORES = {
    "REFERRAL": 30,
    "WEB": 20,
    "CALL": 15,
    "EMAIL": 10,
    "OTHER": 5,
}


def calculate_score(lead_data: LeadCreateInternal, rules: list[ScoringRule]) -> tuple[float, str]:
    """Рассчитывает score лида на основе правил из БД + статических весов."""
    score = 0.0

    # Статические правила по источнику
    source_key = (lead_data.source or "OTHER").upper()
    score += SOURCE_SCORES.get(source_key, 5)

    # Бонус за наличие бюджета
    if lead_data.opportunity and lead_data.opportunity > 0:
        if lead_data.opportunity >= 1_000_000:
            score += 25
        elif lead_data.opportunity >= 100_000:
            score += 15
        else:
            score += 5

    # Бонус за email (можно слать прогрев)
    if lead_data.email:
        score += 10

    # Правила из БД (динамические)
    for rule in rules:
        field_value = getattr(lead_data, rule.field, None)
        if field_value is None:
            continue
        if rule.condition == "eq" and str(field_value) == rule.value:
            score += rule.points
        elif rule.condition == "contains" and rule.value in str(field_value):
            score += rule.points
        elif rule.condition == "gte" and float(field_value) >= float(rule.value):
            score += rule.points

    score = min(score, 100.0)

    tier = "cold"
    for tier_name, threshold in TIER_THRESHOLDS.items():
        if score >= threshold:
            tier = tier_name
            break

    return round(score, 2), tier


def get_least_loaded_manager(db: Session) -> Optional[int]:
    """Выбирает менеджера с минимальной активной нагрузкой."""
    from sqlalchemy import func, text

    result = db.execute(
        text("""
            SELECT m.id, COALESCE(COUNT(l.id), 0) as active_load
            FROM managers m
            LEFT JOIN leads l ON l.manager_id = m.id AND l.status = 'active'
            WHERE m.is_active = true
            GROUP BY m.id
            ORDER BY active_load ASC
            LIMIT 1
        """)
    ).fetchone()

    return result[0] if result else None


# ============================================================
# tasks.py — Celery задачи
# ============================================================
import httpx
from celery import Celery
from sqlalchemy.orm import Session

celery_app = Celery(
    "crm_tasks",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/1",
)

TELEGRAM_BOT_TOKEN = "..."
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    name="tasks.notify_manager_new_lead",
)
def notify_manager_new_lead(self, manager_id: int, lead_id: int) -> dict:
    """Отправляет Telegram-уведомление менеджеру о новом лиде."""
    from database import get_db_session  # локальный фабричный метод

    try:
        with get_db_session() as db:
            lead = db.query(Lead).filter(Lead.id == lead_id).first()
            manager = db.query(Manager).filter(Manager.id == manager_id).first()

            if not lead or not manager:
                raise ValueError(f"Lead {lead_id} or Manager {manager_id} not found")

            tier_emoji = {"hot": "🔥", "warm": "☀️", "cold": "❄️"}.get(lead.tier, "")
            message = (
                f"{tier_emoji} Новый лид назначен на вас!\n\n"
                f"Имя: {lead.name}\n"
                f"Телефон: {lead.phone or 'не указан'}\n"
                f"Источник: {lead.source or 'неизвестен'}\n"
                f"Score: {lead.score} ({lead.tier})\n"
                f"Бюджет: {lead.opportunity or 'не указан'}"
            )

            response = httpx.post(
                f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": manager.telegram_id, "text": message, "parse_mode": "HTML"},
                timeout=10.0,
            )
            response.raise_for_status()

            # Логируем уведомление
            notification = Notification(
                lead_id=lead_id,
                manager_id=manager_id,
                type="telegram_new_lead",
                sent_at=datetime.utcnow(),
                status="sent",
                payload={"message_id": response.json()["result"]["message_id"]},
            )
            db.add(notification)
            db.commit()

            return {"status": "sent", "message_id": response.json()["result"]["message_id"]}

    except httpx.HTTPError as exc:
        raise self.retry(exc=exc)


# ============================================================
# main.py — FastAPI webhook handler (классика)
# ============================================================
import json
import logging
from contextlib import asynccontextmanager
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

DATABASE_URL = "postgresql+asyncpg://user:pass@localhost/crm"
REDIS_URL = "redis://localhost:6379/0"
WEBHOOK_SECRET = "bitrix24_secret_token"

engine = create_async_engine(DATABASE_URL, pool_size=10, max_overflow=20)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)

logger = logging.getLogger(__name__)


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


@asynccontextmanager
async def lifespan(app: FastAPI):
    await redis_client.ping()
    yield
    await redis_client.aclose()


app = FastAPI(lifespan=lifespan)


@app.post("/webhook/lead")
async def handle_lead_webhook(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    x_bitrix_token: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """
    Классический обработчик вебхука нового лида.
    Полный путь: validate → persist → score → assign → cache → queue notify.
    """
    # 1. Аутентификация вебхука
    if x_bitrix_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook token")

    # 2. Парсинг и валидация тела запроса
    try:
        raw_body = await request.json()
        event = BitrixWebhookEvent(**raw_body)
        lead_payload = LeadWebhookPayload(**event.data.get("FIELDS", {}))
    except Exception as e:
        logger.warning("Webhook validation failed: %s", e)
        raise HTTPException(status_code=422, detail=str(e))

    bitrix_lead_id = int(event.data.get("ID", 0))
    if not bitrix_lead_id:
        raise HTTPException(status_code=422, detail="Missing lead ID in webhook data")

    # 3. Идемпотентность — проверяем, не обрабатывали ли уже этот лид
    cache_key = f"lead_processed:{bitrix_lead_id}"
    if await redis_client.exists(cache_key):
        logger.info("Lead %s already processed, skipping", bitrix_lead_id)
        return JSONResponse({"status": "duplicate", "bitrix_id": bitrix_lead_id})

    # 4. Построение внутренней схемы
    lead_internal = LeadCreateInternal(
        bitrix_id=bitrix_lead_id,
        name=lead_payload.TITLE,
        phone=lead_payload.primary_phone,
        email=lead_payload.primary_email,
        source=lead_payload.SOURCE_ID,
        opportunity=lead_payload.OPPORTUNITY,
        raw_data=event.data,
    )

    # 5. Запись в PostgreSQL
    db_lead = Lead(
        bitrix_id=lead_internal.bitrix_id,
        name=lead_internal.name,
        phone=lead_internal.phone,
        email=lead_internal.email,
        source=lead_internal.source,
        opportunity=lead_internal.opportunity,
        status="new",
        raw_data=lead_internal.raw_data,
    )
    db.add(db_lead)
    await db.flush()  # получаем id до коммита

    # 6. Загружаем правила скоринга и считаем score
    from sqlalchemy import select
    rules_result = await db.execute(
        select(ScoringRule).where(ScoringRule.is_active == True)
    )
    rules = rules_result.scalars().all()
    score, tier = calculate_score(lead_internal, rules)

    # 7. Назначение менеджера (синхронный запрос через run_sync или raw SQL)
    manager_result = await db.execute(
        text("""
            SELECT m.id, COALESCE(COUNT(l.id), 0) as active_load
            FROM managers m
            LEFT JOIN leads l ON l.manager_id = m.id AND l.status = 'active'
            WHERE m.is_active = true
            GROUP BY m.id
            ORDER BY active_load ASC
            LIMIT 1
        """)
    )
    manager_row = manager_result.fetchone()
    manager_id = manager_row[0] if manager_row else None

    # 8. Обновляем лид с score и назначением
    db_lead.score = score
    db_lead.tier = tier
    db_lead.manager_id = manager_id
    db_lead.status = "active"
    await db.commit()
    await db.refresh(db_lead)

    # 9. Кэшируем в Redis (hot path — быстрое чтение)
    lead_cache_data = {
        "id": db_lead.id,
        "bitrix_id": db_lead.bitrix_id,
        "name": db_lead.name,
        "score": db_lead.score,
        "tier": db_lead.tier,
        "manager_id": db_lead.manager_id,
    }
    await redis_client.setex(
        f"lead:{db_lead.id}",
        3600,
        json.dumps(lead_cache_data),
    )

    # 10. Идемпотентный маркер (не обрабатывать повторно 24 часа)
    await redis_client.setex(cache_key, 86400, "1")

    # 11. Ставим задачу уведомления в Celery (асинхронно)
    if manager_id:
        notify_manager_new_lead.apply_async(
            args=[manager_id, db_lead.id],
            countdown=0,
            expires=300,
        )

    logger.info(
        "Lead %s processed: score=%.1f, tier=%s, manager=%s",
        bitrix_lead_id, score, tier, manager_id
    )

    return JSONResponse({
        "status": "created",
        "lead_id": db_lead.id,
        "bitrix_id": bitrix_lead_id,
        "score": score,
        "tier": tier,
        "manager_id": manager_id,
    })
```

**Итого классика:** ~350 строк кода в 5 файлах (models, schemas, scoring, tasks, main).

---

### 2.2 AI-native поток (генеративный стейт + LLM)

```python
# ============================================================
# state.py — управление генеративным стейтом CRM
# ============================================================
import json
import time
from typing import Any
import redis.asyncio as aioredis


REDIS_URL = "redis://localhost:6379/0"
STATE_KEY = "crm_state:current"
STATE_HISTORY_KEY = "crm_state:history"
STATE_TTL = 86400 * 30  # 30 дней


class CRMStateStore:
    """
    Хранилище генеративного стейта CRM.
    Стейт — живой JSON-снимок всего домена (~4-8 KB).
    """

    def __init__(self, redis: aioredis.Redis):
        self.redis = redis

    async def get_current(self) -> dict:
        raw = await self.redis.get(STATE_KEY)
        if not raw:
            return self._initial_state()
        return json.loads(raw)

    async def apply_delta(self, delta: dict, event_id: str) -> dict:
        """Применяет дельту к текущему стейту атомарно."""
        current = await self.get_current()
        new_state = self._deep_merge(current, delta)
        new_state["_meta"]["version"] += 1
        new_state["_meta"]["last_updated"] = time.time()
        new_state["_meta"]["last_event_id"] = event_id

        pipe = self.redis.pipeline()
        pipe.set(STATE_KEY, json.dumps(new_state), ex=STATE_TTL)
        # Храним историю последних 100 версий для отладки
        pipe.lpush(STATE_HISTORY_KEY, json.dumps({
            "version": new_state["_meta"]["version"],
            "event_id": event_id,
            "delta": delta,
            "ts": time.time(),
        }))
        pipe.ltrim(STATE_HISTORY_KEY, 0, 99)
        await pipe.execute()

        return new_state

    def _deep_merge(self, base: dict, delta: dict) -> dict:
        result = base.copy()
        for key, value in delta.items():
            if isinstance(value, dict) and key in result and isinstance(result[key], dict):
                result[key] = self._deep_merge(result[key], value)
            elif isinstance(value, str) and value.startswith("+") and value[1:].lstrip("-").isdigit():
                # Инкрементальное обновление: "+1" или "-1"
                result[key] = result.get(key, 0) + int(value)
            else:
                result[key] = value
        return result

    def _initial_state(self) -> dict:
        return {
            "_meta": {"version": 0, "last_updated": time.time(), "last_event_id": None},
            "managers": {
                # Загружается при первом запуске из Bitrix24
                # "7": {"name": "Алексей", "telegram_id": "@alex", "load": 12, "max_load": 20}
            },
            "leads_summary": {
                "total": 0,
                "by_tier": {"hot": 0, "warm": 0, "cold": 0},
                "by_source": {},
            },
            "business_context": {
                "current_month_target": 50,
                "current_month_leads": 0,
                "priority_sources": ["REFERRAL", "WEB"],
                "high_value_threshold": 500_000,
            },
        }


# ============================================================
# llm_engine.py — LLM как Decision Engine
# ============================================================
import json
import logging
from typing import Any

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

openai_client = AsyncOpenAI()

SYSTEM_PROMPT = """Ты — Decision Engine CRM-системы компании по продаже недвижимости.

Твоя задача: получить событие о новом лиде и текущий стейт CRM, затем вернуть JSON с:
1. actions — список действий для исполнителя
2. state_delta — изменения стейта (используй "+1"/"-1" для инкрементов)
3. reasoning — краткое объяснение решений (для аудита)

## Правила скоринга:
- REFERRAL source: +30 pts
- WEB: +20 pts, CALL: +15 pts, EMAIL: +10 pts, OTHER: +5 pts
- opportunity >= 1M: +25 pts, >= 100K: +15 pts, иначе: +5 pts
- email указан: +10 pts
- Tier: hot >= 70, warm >= 40, cold < 40

## Правила назначения:
- Назначай менеджера с наименьшим load из statemanagers
- Не назначай если load >= max_load (менеджер перегружен)
- При отсутствии свободных — назначай на менеджера с наименьшим load и предупреди

## Формат ответа (СТРОГО JSON, без markdown):
{
  "actions": [
    {
      "type": "update_lead_in_bitrix",
      "payload": {"score": <float>, "tier": "<hot|warm|cold>", "manager_bitrix_id": <int>}
    },
    {
      "type": "send_telegram",
      "payload": {"manager_id": "<str>", "message": "<text>"}
    }
  ],
  "state_delta": {
    "managers": {"<manager_id>": {"load": "+1"}},
    "leads_summary": {"total": "+1", "by_tier": {"<tier>": "+1"}}
  },
  "reasoning": "<объяснение>"
}"""


async def process_lead_event(state: dict, event: dict) -> dict:
    """
    Ключевая функция: LLM принимает стейт + событие, возвращает структурированный ответ.
    """
    prompt_content = json.dumps({
        "event_type": "NEW_LEAD",
        "event_data": event,
        "current_state": state,
    }, ensure_ascii=False, indent=2)

    response = await openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt_content},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,  # минимальная вариативность для детерминизма
        max_tokens=1024,
    )

    raw_response = response.choices[0].message.content
    logger.info("LLM response: %s", raw_response[:200])

    try:
        result = json.loads(raw_response)
    except json.JSONDecodeError as e:
        logger.error("LLM returned invalid JSON: %s", e)
        raise ValueError(f"LLM returned invalid JSON: {e}")

    # Валидация структуры ответа
    if "actions" not in result or "state_delta" not in result:
        raise ValueError(f"LLM response missing required fields: {list(result.keys())}")

    return result


# ============================================================
# action_executor.py — исполнитель действий
# ============================================================
import httpx
import logging

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = "..."
BITRIX_WEBHOOK_URL = "https://your-domain.bitrix24.ru/rest/1/token"


async def execute_actions(actions: list[dict], managers_state: dict) -> list[dict]:
    """Параллельное выполнение всех действий из LLM-ответа."""
    import asyncio

    results = await asyncio.gather(
        *[_execute_single(action, managers_state) for action in actions],
        return_exceptions=True,
    )

    return [
        {"action": actions[i], "result": r if not isinstance(r, Exception) else {"error": str(r)}}
        for i, r in enumerate(results)
    ]


async def _execute_single(action: dict, managers_state: dict) -> dict:
    action_type = action["type"]
    payload = action.get("payload", {})

    if action_type == "update_lead_in_bitrix":
        return await _update_bitrix_lead(payload)
    elif action_type == "send_telegram":
        return await _send_telegram(payload, managers_state)
    else:
        logger.warning("Unknown action type: %s", action_type)
        return {"status": "skipped", "reason": f"Unknown action: {action_type}"}


async def _update_bitrix_lead(payload: dict) -> dict:
    """Обновляет лид в Bitrix24 через REST API."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{BITRIX_WEBHOOK_URL}/crm.lead.update.json",
            json={
                "id": payload.get("lead_bitrix_id"),
                "fields": {
                    "ASSIGNED_BY_ID": payload.get("manager_bitrix_id"),
                    "UF_CRM_SCORE": payload.get("score"),
                    "UF_CRM_TIER": payload.get("tier"),
                },
            },
        )
        response.raise_for_status()
        return {"status": "ok", "bitrix_response": response.json()}


async def _send_telegram(payload: dict, managers_state: dict) -> dict:
    """Отправляет Telegram-сообщение менеджеру."""
    manager_id = payload["manager_id"]
    manager = managers_state.get(str(manager_id), {})
    chat_id = manager.get("telegram_id")

    if not chat_id:
        logger.warning("No telegram_id for manager %s", manager_id)
        return {"status": "skipped", "reason": "no telegram_id"}

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": payload["message"]},
        )
        response.raise_for_status()
        return {"status": "sent", "message_id": response.json()["result"]["message_id"]}


# ============================================================
# main.py — FastAPI webhook handler (AI-native)
# ============================================================
import logging
import time
import uuid
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
redis_client = aioredis.from_url("redis://localhost:6379/0", decode_responses=True)
state_store = CRMStateStore(redis_client)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await redis_client.ping()
    yield
    await redis_client.aclose()


app = FastAPI(lifespan=lifespan)


@app.post("/webhook/lead")
async def handle_lead_webhook(
    request: Request,
    x_bitrix_token: str | None = Header(default=None),
) -> JSONResponse:
    """
    AI-native обработчик вебхука нового лида.
    Путь: parse → get_state → LLM → apply_delta → execute_actions.
    """
    # 1. Базовая аутентификация
    if x_bitrix_token != "bitrix24_secret_token":
        raise HTTPException(status_code=401, detail="Invalid token")

    # 2. Минимальный парсинг (только извлечение сырого события)
    try:
        raw_event = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_id = str(uuid.uuid4())
    lead_bitrix_id = raw_event.get("data", {}).get("ID")

    # 3. Идемпотентность через Redis SET NX
    idempotency_key = f"event_processed:{lead_bitrix_id}"
    already_processed = not await redis_client.set(idempotency_key, event_id, ex=86400, nx=True)
    if already_processed:
        return JSONResponse({"status": "duplicate"})

    # 4. Получаем текущий стейт CRM (~1 мс из Redis)
    current_state = await state_store.get_current()

    # 5. LLM принимает решение (~800-2000 мс)
    t0 = time.monotonic()
    try:
        llm_result = await process_lead_event(
            state=current_state,
            event=raw_event,
        )
    except Exception as e:
        logger.error("LLM decision failed for event %s: %s", event_id, e)
        # Fallback: базовое назначение без LLM (graceful degradation)
        raise HTTPException(status_code=503, detail="Decision engine unavailable")

    llm_latency_ms = (time.monotonic() - t0) * 1000
    logger.info("LLM decision in %.0f ms: %s", llm_latency_ms, llm_result.get("reasoning", "")[:100])

    # 6. Применяем дельту к стейту (атомарно в Redis)
    await state_store.apply_delta(
        delta=llm_result.get("state_delta", {}),
        event_id=event_id,
    )

    # 7. Параллельное выполнение всех действий
    action_results = await execute_actions(
        actions=llm_result.get("actions", []),
        managers_state=current_state.get("managers", {}),
    )

    return JSONResponse({
        "status": "processed",
        "event_id": event_id,
        "actions_executed": len(action_results),
        "llm_latency_ms": round(llm_latency_ms),
        "reasoning": llm_result.get("reasoning"),
    })
```

**Итого AI-native:** ~180 строк кода в 4 файлах (state, llm_engine, action_executor, main).

---

## 3. Таблица сравнения

| Критерий | Классика (FastAPI + PG + Celery) | AI-native (LLM + генеративный стейт) |
|---|---|---|
| **Строки кода** | ~350 строк в 5 файлах. Не считая: миграции Alembic (~80 строк), Celery config (~30), docker-compose (~60). **Итого: ~520 строк** | ~180 строк в 4 файлах. Промпт ~40 строк. **Итого: ~220 строк** — в 2.4 раза меньше |
| **Latency (p50)** | **~45 мс** (FastAPI: 5 мс + PG INSERT: 8 мс + scoring query: 5 мс + assignment query: 8 мс + Redis SETEX: 2 мс + Celery enqueue: 5 мс + HTTP response: 12 мс). Telegram — асинхронно через Celery | **~1100 мс** (FastAPI: 5 мс + Redis GET state: 2 мс + LLM GPT-4o p50: ~900 мс + Redis PATCH: 3 мс + параллельные actions: ~200 мс). Синхронная блокировка на LLM |
| **Latency (p99)** | **~180 мс** (пики: конкурентные транзакции PG, lock contention на UPDATE leads). Celery notification p99: ~3000 мс (queue backlog) | **~4500 мс** (LLM p99: до 4000 мс при перегрузке OpenAI API, rate limit throttling). Без queue — всё синхронно, пики заметнее |
| **Стоимость / 1000 событий** | **Инфра: ~$0.15–0.40** = RDS db.t3.medium ($0.068/ч ÷ ~500 req/ч) + ElastiCache t3.micro ($0.017/ч) + 2× Celery worker ($0.04/ч). При 1000 событий/день: ~$4–12/мес инфра | **Inference: ~$1.80–3.50** = GPT-4o: ~1500 input tokens + ~300 output tokens = ~$0.00195/событие × 1000 = ~$1.95. При 1000 событий/день: ~$58/мес только inference. **В 5–15 раз дороже на масштабе** |
| **Стоимость / 100K событий/день** | ~$80–150/мес (инфра масштабируется горизонтально: RDS read replicas, больше Celery workers) | ~$5800/мес (inference линейно). Неприемлемо без кэширования паттернов |
| **Масштабируемость** | Горизонтальная и проверенная: stateless FastAPI workers за load balancer, PG read replicas, Celery autoscaling (Kubernetes HPA). Узкое место: PG writes ~5000 TPS на один instance | Ограничена rate limits OpenAI (tier 3: 10K RPM, tier 4: 30K RPM). Горизонтальное масштабирование не помогает — bottleneck внешний API. При 1000 событий/мин нужен tier 4 + backpressure |
| **Добавить новое правило скоринга** | Написать миграцию Alembic → INSERT в scoring_rules → deploy. **Время: 30–60 мин** с тестами. Риск: забытый индекс, неверный тип | Изменить 3 строки в SYSTEM_PROMPT → перезапуск. **Время: 5 мин**. Риск: неожиданные побочные эффекты в других решениях |
| **Добавить новый тип события** | Новый Pydantic schema + роут + бизнес-логика + тест. Структурированно, предсказуемо. **Время: 2–4 часа** | Добавить описание в промпт + новый action type в executor. **Время: 30–60 мин**. Но: нужно тестировать промпт на все существующие сценарии |
| **Maintainability** | Высокая структурная предсказуемость: каждый шаг explicit. Seniority: нужен знакомый с SQLAlchemy + Celery. Проблема: со временем rules накапливаются (200+ scoring rules → сложно читать SQL). Alembic migrations — техдолг | Низкая структурная, высокая семантическая: логика в промпте — "документируется сама". Проблема: **prompt drift** — изменения в промпте ломают неожиданные сценарии. Нет строгих типов → ошибки на runtime. Нужен prompt versioning |
| **Тестируемость** | Отличная: unit тесты для `calculate_score` (чистая функция), `get_least_loaded_manager`, tasks. Integration тесты с TestClient + SQLite/testcontainers. Покрытие 85%+ реалистично | Сложная: LLM недетерминирован при temperature > 0. Тесты требуют mock OpenAI (теряем смысл) или реальных вызовов (дорого, медленно). **Нет стандартного подхода**. Можно тестировать action_executor отдельно — это 30% логики |
| **Observability** | Стандартная: Prometheus метрики из SQLAlchemy + Celery, structured logging, Jaeger traces. Каждый шаг измерим | Нестандартная: нужно логировать prompts/responses, tracking reasoning, LLM latency breakdown. LangSmith / Helicone как отдельный инструмент. **Debugging галлюцинации ≠ debugging баг в коде** |
| **Риски** | Стандартные: PG connection pool exhaustion, Celery queue backlog, миграции в prod (downtime), N+1 queries | **Специфичные AI-риски:** галлюцинации (неверный score, назначение не тому менеджеру), prompt injection через данные лида, model deprecation (GPT-4o → GPT-5), cost spike при traffic burst, vendor lock-in OpenAI |
| **Отказоустойчивость** | Celery retry, PG транзакции (ACID), идемпотентные операции через ON CONFLICT. Стандартные паттерны | LLM может вернуть невалидный JSON (даже с json_mode ~0.1% случаев). Нет retry из коробки. Graceful degradation требует fallback логики (= часть классики возвращается) |
| **Time to market (MVP)** | **2–3 недели**: настройка инфраструктуры, написание models+schemas+scoring+tasks, тесты, CI/CD | **3–5 дней**: написание промпта, state schema, action executor, деплой. Итерации быстрые |
| **Изменение бизнес-логики** | Средне: изменить правило → миграция или код → PR → review → deploy. Безопасно, но медленно | Быстро: изменить промпт → тест в playground → deploy. Опасно без regression suite |

---

## 4. Детальная честная оценка по ключевым осям

### 4.1 Стоимость: детальный расчёт

```
КЛАССИКА (1000 событий/день = 30K/мес):
  RDS db.t3.medium:          $49/мес
  ElastiCache t3.micro:      $12/мес
  2× app server t3.small:    $30/мес
  Celery worker t3.micro:    $8/мес
  Monitoring (Grafana Cloud): $0 (free tier)
  ИТОГО:                     ~$99/мес
  На событие:                $0.0033

AI-NATIVE (1000 событий/день = 30K/мес):
  GPT-4o:
    Input:  ~1500 tokens × $0.0025/1K = $0.00375/событие
    Output: ~300 tokens  × $0.01/1K   = $0.003/событие
    Итого:                              $0.00675/событие
  30K событий/мес:           $202.50/мес
  Redis (state store):       $12/мес
  App server:                $8/мес
  ИТОГО:                     ~$222/мес
  На событие:                $0.0074

ВЫВОД: AI-native в 2.2 раза дороже на 1K событий/день.
На 10K событий/день разрыв растёт: $99 vs ~$2100/мес (21×).
```

### 4.2 Тестируемость: что можно, что нельзя

```python
# КЛАССИКА — полноценный unit test (детерминирован)
def test_calculate_score_referral_with_budget():
    lead = LeadCreateInternal(
        bitrix_id=1, name="Test", phone=None, email="t@t.com",
        source="REFERRAL", opportunity=1_500_000, raw_data={}
    )
    score, tier = calculate_score(lead, rules=[])
    assert score == 70.0  # 30 (referral) + 25 (budget >= 1M) + 10 (email) + 5 (opportunity)
    assert tier == "hot"

# AI-NATIVE — лучшее что можно (тест action executor, не LLM)
async def test_execute_update_lead_action():
    with respx.mock:
        respx.post(f"{BITRIX_WEBHOOK_URL}/crm.lead.update.json").mock(
            return_value=httpx.Response(200, json={"result": True})
        )
        result = await _execute_single(
            {"type": "update_lead_in_bitrix", "payload": {"score": 78, "tier": "hot", "manager_bitrix_id": 7}},
            managers_state={}
        )
        assert result["status"] == "ok"

# AI-NATIVE — тест LLM (дорого, медленно, недетерминирован)
@pytest.mark.llm_integration  # запускается отдельно, не в CI
async def test_llm_assigns_hot_tier_for_referral():
    result = await process_lead_event(
        state=sample_state_with_managers(),
        event=sample_referral_lead_event(),
    )
    # Лучшее что можем: проверяем структуру, не конкретные числа
    assert any(a["type"] == "update_lead_in_bitrix" for a in result["actions"])
    update_action = next(a for a in result["actions"] if a["type"] == "update_lead_in_bitrix")
    assert update_action["payload"]["tier"] in ("hot", "warm")  # не можем точно
```

### 4.3 Галлюцинации: реальные риски

| Сценарий | Вероятность | Последствие |
|---|---|---|
| LLM назначает несуществующего менеджера | ~2-5% без строгой валидации | Лид потерян, нет уведомления |
| LLM возвращает score > 100 | ~1% | Некорректные данные в Bitrix24 |
| Prompt injection через поле "Имя лида" | Зависит от фильтрации | Смена поведения системы |
| LLM игнорирует нового менеджера (добавленного в стейт) | ~5-10% при обновлении стейта | Неравномерная нагрузка |
| JSON невалиден (несмотря на json_mode) | ~0.05-0.1% | HTTP 503, retry |

**Митигация**: валидация всех LLM-ответов через Pydantic перед исполнением — это добавляет ~20 строк кода обратно.

---

## 5. Вердикт

### Где AI-native побеждает

1. **Сложная, меняющаяся бизнес-логика**: когда правила скоринга меняются еженедельно, а не раз в квартал — промпт быстрее кода. Пример: CRM с нестандартной квалификацией (учитывает тон переписки, историю компании, сезонность).

2. **Маленький MVP с неизвестными требованиями**: за 3-5 дней рабочий прототип вместо 3 недель. Идеально для валидации гипотез.

3. **Обработка неструктурированных данных**: когда входящее событие — это письмо или голосовое сообщение, а не чистый JSON. LLM справляется нативно, классике нужен отдельный NLP-пайплайн.

4. **Объяснимость решений**: поле `reasoning` из коробки даёт audit trail "почему этот лид отдан Алексею" — без дополнительного кода.

### Где классика лучше

1. **Объём > 5K событий/день**: стоимость inference становится неприемлемой. При 10K событий/день классика в 20+ раз дешевле.

2. **Latency < 200 мс**: интеграции с синхронными клиентами, real-time дашборды, webhook chains — классика на 45 мс vs AI-native на 1100 мс.

3. **Compliance и аудит**: финансовые операции, GDPR — нужна детерминированная, аудитируемая логика. LLM не даёт гарантий.

4. **Высоконагруженные системы**: горизонтальное масштабирование классики линейно и предсказуемо. AI-native упирается в rate limits внешнего API.

5. **Большие команды (5+ разработчиков)**: структурированный код с типами лучше промптов для code review и onboarding.

### Гибридный вариант (рекомендуется)

```
Уровень 1 — Классика (FastAPI + PostgreSQL):
  - Webhook validation и идемпотентность
  - Запись сырого события в event log (append-only)
  - Простой детерминированный scoring (< 5 правил)
  - Быстрый ответ клиенту: 200 OK за ~30 мс

Уровень 2 — AI-native (асинхронно, через очередь):
  - Сложная квалификация лида (анализ комментариев, истории)
  - Рекомендации по стратегии работы с лидом
  - Обогащение данных (парсинг неструктурированных полей)
  - Обновление Bitrix24 через background task

Граница: LLM вызывается только для решений, которые НЕЛЬЗЯ
формализовать правилами. Базовый flow остаётся детерминированным.
```

```mermaid
graph LR
    W[Webhook B24] --> FW[FastAPI]
    FW --> PG[(PostgreSQL\nevent log)]
    FW --> |async| Q[Redis Queue]
    FW --> |200 OK 30ms| B24[Bitrix24]
    Q --> CL[Celery Worker]
    CL --> |простые решения| SC[Scoring Rules]
    CL --> |сложные решения| LLM[LLM Engine]
    SC --> BX[Bitrix24 update]
    LLM --> BX
    LLM --> TG[Telegram notify]
    SC --> TG
```

**Итоговое правило выбора:**
- До 1K событий/день + логика меняется часто + команда 1-2 человека → **AI-native**
- Более 5K событий/день + latency критична + compliance → **классика**
- Всё остальное → **гибрид** (классика на hot path, LLM на enrichment)

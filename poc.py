"""
AI-Native CRM — Proof of Concept
Архитектура: Telegram → LLM(стейт + input) → response + actions + стейт_N+1
Bitrix24 = source of truth. Стейт агента = эволюционный JSON-кэш.

Запуск: python poc.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, List, Dict

import aiohttp
import tiktoken
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Конфиг
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN: str = os.environ["TELEGRAM_TOKEN"]
BITRIX_WEBHOOK: str = os.environ.get("BITRIX_WEBHOOK", "")
OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")

STATE_FILE: Path = Path("crm_state.json")
TOKEN_BUDGET: int = 3000          # макс токенов на историю
HISTORY_KEEP: int = 5             # сколько записей оставлять при компрессии
WM_MAX_CHARS: int = 2000          # лимит working_memory в символах
REMINDER_INTERVAL: int = 60       # секунды между проверками напоминаний

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("crm-poc")

# ---------------------------------------------------------------------------
# Модели данных
# ---------------------------------------------------------------------------

@dataclass
class Deal:
    """Сделка из Bitrix24 (кэш)."""
    id: str
    title: str
    stage: str
    amount: float
    contact_name: str


@dataclass
class Reminder:
    """Запланированное напоминание."""
    text: str
    fire_at: float          # unix timestamp
    deal_id: Optional[str] = None


@dataclass
class AgentState:
    """Эволюционный стейт CRM-агента."""
    chat_id: int
    iteration: int = 0
    deals: list[Deal] = field(default_factory=list)
    working_memory: str = ""
    reminders: list[Reminder] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    last_updated: str = ""

    def to_dict(self) -> dict:
        """Сериализация в dict для JSON."""
        return {
            "chat_id": self.chat_id,
            "iteration": self.iteration,
            "deals": [asdict(d) for d in self.deals],
            "working_memory": self.working_memory,
            "reminders": [asdict(r) for r in self.reminders],
            "history": self.history,
            "last_updated": self.last_updated,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AgentState":
        """Десериализация из dict."""
        return cls(
            chat_id=data["chat_id"],
            iteration=data.get("iteration", 0),
            deals=[Deal(**d) for d in data.get("deals", [])],
            working_memory=data.get("working_memory", ""),
            reminders=[Reminder(**r) for r in data.get("reminders", [])],
            history=data.get("history", []),
            last_updated=data.get("last_updated", ""),
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_state(state: AgentState) -> None:
    """Сохранить стейт в JSON-файл."""
    state.last_updated = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Стейт сохранён (iteration=%d)", state.iteration)


def load_state() -> AgentState | None:
    """Загрузить стейт из файла (None если файла нет)."""
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return AgentState.from_dict(data)
    except Exception as exc:
        log.error("Ошибка загрузки стейта: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Tiktoken — подсчёт токенов
# ---------------------------------------------------------------------------
_enc = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """Количество токенов в тексте."""
    return len(_enc.encode(text))


# ---------------------------------------------------------------------------
# Компрессия стейта
# ---------------------------------------------------------------------------

def compress_state(state: AgentState) -> None:
    """Сжать историю и working_memory при переполнении."""
    history_text = json.dumps(state.history, ensure_ascii=False)
    tokens = count_tokens(history_text)

    if tokens > TOKEN_BUDGET:
        removed = len(state.history) - HISTORY_KEEP
        state.history = state.history[-HISTORY_KEEP:]
        log.info("Компрессия: удалено %d записей из истории (было %d токенов)", removed, tokens)

    if len(state.working_memory) > WM_MAX_CHARS:
        state.working_memory = state.working_memory[:WM_MAX_CHARS]
        log.info("Working memory обрезана до %d символов", WM_MAX_CHARS)


# ---------------------------------------------------------------------------
# Bitrix24 API
# ---------------------------------------------------------------------------

async def bitrix_get_deals() -> list[Deal]:
    """Получить открытые сделки из Bitrix24."""
    if not BITRIX_WEBHOOK:
        log.warning("BITRIX_WEBHOOK не задан — возвращаю пустой список сделок")
        return []

    url = (
        f"{BITRIX_WEBHOOK}crm.deal.list?"
        "filter[STAGE_SEMANTIC_ID][]=P&filter[STAGE_SEMANTIC_ID][]=F"
        "&select[]=ID&select[]=TITLE&select[]=STAGE_ID"
        "&select[]=OPPORTUNITY&select[]=CONTACT_ID"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
                result = data.get("result", [])
                deals = [
                    Deal(
                        id=str(d.get("ID", "")),
                        title=d.get("TITLE", ""),
                        stage=d.get("STAGE_ID", ""),
                        amount=float(d.get("OPPORTUNITY", 0) or 0),
                        contact_name=str(d.get("CONTACT_ID", "")),
                    )
                    for d in result
                ]
                log.info("Bitrix24: загружено %d сделок", len(deals))
                return deals
    except Exception as exc:
        log.error("Bitrix24 crm.deal.list ошибка: %s", exc)
        return []


async def bitrix_update_deal(deal_id: str, fields: dict[str, Any]) -> bool:
    """Обновить поля сделки в Bitrix24."""
    if not BITRIX_WEBHOOK:
        log.warning("BITRIX_WEBHOOK не задан — пропускаю update deal %s", deal_id)
        return False

    url = f"{BITRIX_WEBHOOK}crm.deal.update"
    payload = {"id": deal_id, "fields": fields}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
                ok = data.get("result", False)
                log.info("Bitrix24 update deal %s: %s", deal_id, "OK" if ok else "FAIL")
                return bool(ok)
    except Exception as exc:
        log.error("Bitrix24 crm.deal.update ошибка: %s", exc)
        return False


# ---------------------------------------------------------------------------
# LLM — вызов с fallback
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
Ты — CRM-ассистент менеджера по продажам. Анализируешь стейт и сообщения.

СТРОГО: Возвращай ТОЛЬКО валидный JSON без markdown:
{
  "response": "ответ менеджеру на русском, до 300 символов",
  "actions": [
    {"type": "update_bitrix", "deal_id": "42", "fields": {"STAGE_ID": "NEGOTIATION"}},
    {"type": "schedule_reminder", "text": "Позвонить", "delay_seconds": 3600},
    {"type": "refresh_bitrix", "reason": "нужны свежие данные"}
  ],
  "new_working_memory": "обновлённый контекст работы менеджера",
  "state_summary": "краткое резюме текущей ситуации"
}

Правила:
- deal_id берёшь ТОЛЬКО из стейта, НИКОГДА не придумываешь
- Если данных нет — честно скажи "данных нет"
- actions может быть пустым []
- response всегда на русском
- Не оборачивай JSON в ```json блоки — только чистый JSON
"""


def _build_prompt(state: AgentState, user_input: str) -> list[dict[str, str]]:
    """Собрать messages для LLM: system + стейт-снапшот + история + ввод."""
    # Снапшот стейта (без полной истории)
    snapshot = {
        "iteration": state.iteration,
        "deals": [asdict(d) for d in state.deals],
        "working_memory": state.working_memory,
        "reminders_count": len(state.reminders),
    }
    state_text = json.dumps(snapshot, ensure_ascii=False)

    # Последние 3 записи истории
    recent = state.history[-3:] if state.history else []
    history_text = json.dumps(recent, ensure_ascii=False) if recent else "[]"

    user_content = (
        f"ТЕКУЩИЙ СТЕЙТ:\n{state_text}\n\n"
        f"ИСТОРИЯ (последние ходы):\n{history_text}\n\n"
        f"СООБЩЕНИЕ МЕНЕДЖЕРА:\n{user_input}"
    )

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


async def _call_openai(messages: list[dict[str, str]]) -> dict[str, Any]:
    """Вызов OpenAI gpt-4o-mini (json_object mode)."""
    import openai

    client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
    resp = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.3,
        max_tokens=1024,
    )
    text = resp.choices[0].message.content or "{}"
    return json.loads(text)


async def _call_anthropic(messages: list[dict[str, str]]) -> dict[str, Any]:
    """Вызов Anthropic Claude Haiku (fallback)."""
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    # Anthropic API: system отдельно, messages без system role
    system_text = ""
    api_messages = []
    for m in messages:
        if m["role"] == "system":
            system_text = m["content"]
        else:
            api_messages.append(m)

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system_text,
        messages=api_messages,
    )
    text = resp.content[0].text
    # Убираем возможные markdown-обёртки
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    text = text.strip()
    return json.loads(text)


async def call_llm(messages: list[dict[str, str]]) -> dict[str, Any]:
    """Вызов LLM с fallback: OpenAI → Anthropic."""
    # Попытка OpenAI
    if OPENAI_API_KEY:
        try:
            result = await _call_openai(messages)
            log.info("LLM ответ от OpenAI gpt-4o-mini")
            return result
        except Exception as exc:
            log.warning("OpenAI ошибка: %s — пробую fallback", exc)

    # Fallback: Anthropic
    if ANTHROPIC_API_KEY:
        try:
            result = await _call_anthropic(messages)
            log.info("LLM ответ от Anthropic Claude Haiku")
            return result
        except Exception as exc:
            log.error("Anthropic ошибка: %s", exc)
            raise

    raise RuntimeError("Нет доступных LLM — задайте OPENAI_API_KEY или ANTHROPIC_API_KEY")


# ---------------------------------------------------------------------------
# Action Executor
# ---------------------------------------------------------------------------

async def execute_actions(actions: list[dict[str, Any]], state: AgentState, bot: Bot) -> None:
    """Выполнить действия, предложенные LLM."""
    for action in actions:
        action_type = action.get("type", "")
        try:
            if action_type == "update_bitrix":
                deal_id = str(action.get("deal_id", ""))
                fields = action.get("fields", {})
                if deal_id and fields:
                    ok = await bitrix_update_deal(deal_id, fields)
                    if ok:
                        # Обновить кэш в стейте
                        for d in state.deals:
                            if d.id == deal_id:
                                if "STAGE_ID" in fields:
                                    d.stage = fields["STAGE_ID"]
                                if "OPPORTUNITY" in fields:
                                    d.amount = float(fields["OPPORTUNITY"])
                                break

            elif action_type == "schedule_reminder":
                text = action.get("text", "Напоминание")
                delay = int(action.get("delay_seconds", 3600))
                deal_id = action.get("deal_id")
                reminder = Reminder(
                    text=text,
                    fire_at=time.time() + delay,
                    deal_id=str(deal_id) if deal_id else None,
                )
                state.reminders.append(reminder)
                log.info("Напоминание запланировано: '%s' через %d сек", text, delay)

            elif action_type == "refresh_bitrix":
                deals = await bitrix_get_deals()
                if deals:
                    state.deals = deals
                    log.info("Сделки обновлены из Bitrix24 (%d шт.)", len(deals))

            else:
                log.warning("Неизвестный тип action: %s", action_type)

        except Exception as exc:
            log.error("Ошибка выполнения action %s: %s", action_type, exc)


# ---------------------------------------------------------------------------
# Фоновый планировщик напоминаний
# ---------------------------------------------------------------------------

async def reminder_scheduler(bot: Bot) -> None:
    """Фоновая задача: проверяет напоминания каждые REMINDER_INTERVAL секунд."""
    log.info("Планировщик напоминаний запущен (интервал %d сек)", REMINDER_INTERVAL)
    while True:
        await asyncio.sleep(REMINDER_INTERVAL)
        try:
            state = load_state()
            if state is None or not state.reminders:
                continue

            now = time.time()
            fired: list[Reminder] = []
            remaining: list[Reminder] = []

            for r in state.reminders:
                if r.fire_at <= now:
                    fired.append(r)
                else:
                    remaining.append(r)

            if not fired:
                continue

            state.reminders = remaining

            for r in fired:
                text = f"Напоминание: {r.text}"
                if r.deal_id:
                    # Найти название сделки
                    deal_title = next(
                        (d.title for d in state.deals if d.id == r.deal_id), None
                    )
                    if deal_title:
                        text += f" — {deal_title}"
                try:
                    await bot.send_message(state.chat_id, text)
                    log.info("Напоминание отправлено: %s", r.text)
                except Exception as exc:
                    log.error("Ошибка отправки напоминания: %s", exc)

            save_state(state)

        except Exception as exc:
            log.error("Ошибка в планировщике напоминаний: %s", exc)


# ---------------------------------------------------------------------------
# Главный ход агента
# ---------------------------------------------------------------------------

async def agent_turn(user_input: str, state: AgentState, bot: Bot) -> str:
    """
    Главный ход агента:
    стейт_N → LLM(стейт + input) → стейт_N+1 + actions → response
    """
    # 1. Компрессия при переполнении
    compress_state(state)

    # 2. Построить промпт
    messages = _build_prompt(state, user_input)

    # 3. Вызвать LLM
    llm_response = await call_llm(messages)

    # 4. Распарсить ответ
    response_text: str = llm_response.get("response", "Нет ответа от агента")
    actions: list[dict] = llm_response.get("actions", [])
    new_wm: str = llm_response.get("new_working_memory", state.working_memory)
    summary: str = llm_response.get("state_summary", "")

    # 5. Выполнить actions
    await execute_actions(actions, state, bot)

    # 6. Обновить стейт
    state.working_memory = new_wm
    state.iteration += 1
    state.history.append({
        "iteration": state.iteration,
        "user": user_input[:500],
        "agent": response_text[:500],
        "actions": [a.get("type", "") for a in actions],
    })

    # 7. Сохранить
    save_state(state)

    log.info(
        "Ход %d завершён: actions=%s, wm=%d символов",
        state.iteration,
        [a.get("type") for a in actions],
        len(state.working_memory),
    )

    return response_text


# ---------------------------------------------------------------------------
# Telegram-бот (aiogram 3.x)
# ---------------------------------------------------------------------------
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """/start — инициализация: pull сделок из Bitrix24, создание стейта."""
    chat_id = message.chat.id
    await message.answer("Инициализирую агента, загружаю данные из Bitrix24...")

    deals = await bitrix_get_deals()
    state = AgentState(chat_id=chat_id, deals=deals)
    save_state(state)

    await message.answer(
        f"Готово! Загружено сделок: {len(deals)}.\n"
        f"Итерация стейта: {state.iteration}\n"
        f"Напишите любой вопрос или задачу..."
    )


@router.message(Command("state"))
async def cmd_state(message: Message) -> None:
    """/state — показать текущий стейт."""
    state = load_state()
    if state is None:
        await message.answer("Стейт не найден. Используйте /start для инициализации.")
        return

    text = (
        f"Iteration: {state.iteration}\n"
        f"Deals: {len(state.deals)}\n"
        f"Reminders: {len(state.reminders)}\n"
        f"History entries: {len(state.history)}\n"
        f"Working memory:\n{state.working_memory[:500] or '(пусто)'}\n"
        f"Last updated: {state.last_updated}"
    )
    await message.answer(text)


@router.message(Command("deals"))
async def cmd_deals(message: Message) -> None:
    """/deals — показать список сделок из стейта."""
    state = load_state()
    if state is None:
        await message.answer("Стейт не найден. Используйте /start для инициализации.")
        return

    if not state.deals:
        await message.answer("Сделок нет. Используйте /start для загрузки из Bitrix24.")
        return

    lines: list[str] = []
    for i, d in enumerate(state.deals, 1):
        lines.append(f"{i}. [{d.id}] {d.title} — {d.amount:,.0f} ₽ ({d.stage})")

    await message.answer("\n".join(lines))


@router.message()
async def handle_message(message: Message) -> None:
    """Любое текстовое сообщение → agent_turn()."""
    if not message.text:
        return

    state = load_state()
    if state is None:
        await message.answer("Стейт не инициализирован. Нажмите /start")
        return

    try:
        response = await agent_turn(message.text, state, bot)
        await message.answer(response)
    except Exception as exc:
        log.error("Ошибка agent_turn: %s", exc)
        await message.answer(f"Ошибка обработки: {exc}")


dp.include_router(router)


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

async def main() -> None:
    """Точка входа: запуск бота и планировщика напоминаний."""
    log.info("AI-Native CRM PoC запускается...")
    log.info("Telegram: polling mode")
    log.info("Bitrix24: %s", "подключён" if BITRIX_WEBHOOK else "не настроен")
    log.info("OpenAI: %s", "есть ключ" if OPENAI_API_KEY else "нет ключа")
    log.info("Anthropic: %s", "есть ключ" if ANTHROPIC_API_KEY else "нет ключа")

    # Запустить планировщик напоминаний как фоновую задачу
    reminder_task = asyncio.create_task(reminder_scheduler(bot))

    try:
        await dp.start_polling(bot)
    finally:
        reminder_task.cancel()
        await bot.session.close()
        log.info("Бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())

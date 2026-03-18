"""
Компрессор семантического стейта.

Когда суммарный размер working_memory превышает token_budget,
агент вызывает LLM для суммаризации, чтобы сохранить смысл в меньшем объёме.
Fallback — жёсткая обрезка по символам без вызова LLM.

Никакого PostgreSQL, никакого SQL.
"""

from __future__ import annotations

import logging
from typing import Any

import tiktoken

from ai_native_crm.config import settings
from ai_native_crm.core.state_store import SemanticState
from ai_native_crm.services.llm_client import LLMClient

logger = logging.getLogger(__name__)

# Кодировка токенизатора — та же, что использует gpt-4o и большинство GPT-4 моделей
_ENCODING_NAME = "cl100k_base"

# Системный промпт для суммаризации — не трогать deal_id и числа
_COMPRESSION_SYSTEM = """\
You are a memory compressor for a CRM agent.
Summarize the given working_memory and conversation_summary into a compact version.

RULES:
1. Preserve ALL deal IDs (d1, d2, ...) and their stages exactly as-is.
2. Do NOT include any amounts, phone numbers, or IDs except deal IDs.
3. Output ONLY valid JSON:
   {"working_memory": "...", "conversation_summary": "..."}
4. Keep each field under 800 characters.
5. Write in Russian.
"""


class StateCompressor:
    """
    Проверяет бюджет токенов и сжимает стейт при необходимости.

    Логика:
      1. needs_compression() подсчитывает токены tiktoken.
      2. compress() вызывает LLM для суммаризации.
      3. При ошибке LLM — fallback: жёсткая обрезка символов.
    """

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm
        # Ленивая инициализация — tiktoken может быть медленным при первом вызове
        self._enc: tiktoken.Encoding | None = None

    def needs_compression(self, state: SemanticState) -> bool:
        """
        Вернуть True, если суммарный размер памяти превышает token_budget.

        Подсчитываем токены working_memory + conversation_summary + agent_assessment.
        """
        total_text = " ".join([
            state.working_memory,
            state.conversation_summary,
            state.agent_assessment,
        ])
        token_count = self._count_tokens(total_text)
        over_budget = token_count > settings.token_budget

        if over_budget:
            logger.info(
                "Компрессия нужна: %d токенов > budget %d (chat_id=%d, iteration=%d)",
                token_count,
                settings.token_budget,
                state.chat_id,
                state.iteration,
            )
        return over_budget

    async def compress(self, state: SemanticState) -> SemanticState:
        """
        Сжать стейт через LLM-суммаризацию.

        Если компрессия не нужна (needs_compression=False) — возвращает
        исходный стейт без изменений.
        При ошибке LLM применяет fallback-обрезку.
        Итерация и chat_id не меняются.
        """
        # Быстрый выход — компрессия не нужна
        if not self.needs_compression(state):
            return state

        logger.info(
            "Запуск компрессии: chat_id=%d iteration=%d wm=%d символов",
            state.chat_id,
            state.iteration,
            len(state.working_memory),
        )

        try:
            compressed = await self._compress_via_llm(state)
            logger.info(
                "Компрессия LLM успешна: wm %d→%d символов",
                len(state.working_memory),
                len(compressed.working_memory),
            )
            return compressed
        except Exception as exc:
            logger.warning(
                "Компрессия LLM не удалась (%s) — применяем fallback обрезку", exc
            )
            return self._compress_fallback(state)

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    async def _compress_via_llm(self, state: SemanticState) -> SemanticState:
        """Вызвать LLM для суммаризации working_memory и conversation_summary."""
        user_content = (
            f"WORKING_MEMORY:\n{state.working_memory}\n\n"
            f"CONVERSATION_SUMMARY:\n{state.conversation_summary}\n\n"
            f"AGENT_ASSESSMENT:\n{state.agent_assessment}\n\n"
            "Compress into JSON with keys: working_memory, conversation_summary."
        )

        messages = [
            {"role": "system", "content": _COMPRESSION_SYSTEM},
            {"role": "user", "content": user_content},
        ]

        parsed, usage = await self._llm.call(messages)

        # Извлекаем поля (с fallback на исходные значения при отсутствии)
        new_wm: str = parsed.get("working_memory", state.working_memory)
        new_summary: str = parsed.get("conversation_summary", state.conversation_summary)

        # Гарантируем лимит символов даже после LLM
        new_wm = new_wm[: settings.wm_max_chars]
        new_summary = new_summary[: settings.wm_max_chars]

        logger.debug(
            "LLM-компрессия: tokens_in=%d tokens_out=%d model=%s",
            usage.get("tokens_in", 0),
            usage.get("tokens_out", 0),
            usage.get("model", "unknown"),
        )

        # Возвращаем новый объект — не мутируем исходный
        from dataclasses import replace
        return replace(
            state,
            working_memory=new_wm,
            conversation_summary=new_summary,
        )

    def _compress_fallback(self, state: SemanticState) -> SemanticState:
        """
        Fallback: жёсткая обрезка по символам.
        Сохраняет конец строки (свежие данные важнее старых).
        """
        max_chars = settings.wm_max_chars

        new_wm = state.working_memory
        new_summary = state.conversation_summary

        if len(new_wm) > max_chars:
            # Оставляем последние max_chars символов — это свежий контекст
            new_wm = new_wm[-max_chars:]
            logger.info(
                "Fallback обрезка working_memory: %d→%d символов",
                len(state.working_memory),
                len(new_wm),
            )

        if len(new_summary) > max_chars:
            new_summary = new_summary[-max_chars:]
            logger.info(
                "Fallback обрезка conversation_summary: %d→%d символов",
                len(state.conversation_summary),
                len(new_summary),
            )

        from dataclasses import replace
        return replace(
            state,
            working_memory=new_wm,
            conversation_summary=new_summary,
        )

    def _count_tokens(self, text: str) -> int:
        """Подсчитать токены через tiktoken (ленивая инициализация кодировщика)."""
        if self._enc is None:
            self._enc = tiktoken.get_encoding(_ENCODING_NAME)
        return len(self._enc.encode(text))

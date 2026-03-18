"""
LLM-клиент с первичным OpenAI и fallback на Anthropic.
Возвращает распарсенный JSON и метаданные об использовании.
"""
from __future__ import annotations

import json
import re
import time
import logging
from typing import Any

from ai_native_crm.config import settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Оба провайдера вернули ошибку."""


class LLMClient:
    """OpenAI primary → Anthropic fallback.

    Оба провайдера обязаны вернуть валидный JSON-объект.
    При сбое OpenAI автоматически пробуем Anthropic.
    """

    def __init__(self) -> None:
        self._openai = None
        self._anthropic = None

        if settings.openai_api_key:
            import openai  # lazy import — не ломать старт, если пакет не установлен
            self._openai = openai.AsyncOpenAI(api_key=settings.openai_api_key)

        if settings.anthropic_api_key:
            import anthropic
            self._anthropic = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

        if self._openai is None and self._anthropic is None:
            logger.warning(
                "Ни OpenAI, ни Anthropic API-ключ не настроен. "
                "LLM-вызовы будут завершаться ошибкой."
            )

    # ------------------------------------------------------------------
    # Публичный метод
    # ------------------------------------------------------------------

    async def call(
        self,
        messages: list[dict[str, str]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Вызвать LLM и вернуть (parsed_json, usage_info).

        usage_info содержит:
            model       — имя модели, которая ответила
            tokens_in   — входящие токены
            tokens_out  — исходящие токены
            latency_ms  — время ответа в миллисекундах
        """
        last_error: Exception | None = None

        # --- OpenAI ---
        if self._openai is not None:
            try:
                return await self._call_openai(messages)
            except LLMError:
                raise
            except Exception as exc:
                # Intentionally broad: catches API errors, timeouts, network issues
                logger.warning("OpenAI вернул ошибку, переключаемся на Anthropic: %s", exc)
                last_error = exc

        # --- Anthropic fallback ---
        if self._anthropic is not None:
            try:
                return await self._call_anthropic(messages)
            except LLMError:
                raise
            except Exception as exc:
                # Intentionally broad: catches API errors, timeouts, network issues
                logger.error("Anthropic тоже вернул ошибку: %s", exc)
                last_error = exc

        raise LLMError(
            "Оба LLM-провайдера недоступны"
        ) from last_error

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    async def _call_openai(
        self,
        messages: list[dict[str, str]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Вызов OpenAI Chat Completions с обязательным JSON-форматом."""
        t_start = time.monotonic()

        response = await self._openai.chat.completions.create(
            model=settings.llm_model,
            messages=messages,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            response_format={"type": "json_object"},
            timeout=30,
        )

        latency_ms = round((time.monotonic() - t_start) * 1000)
        raw_text = response.choices[0].message.content or "{}"

        parsed = self._parse_json(raw_text)
        usage = {
            "model": response.model,
            "tokens_in": response.usage.prompt_tokens if response.usage else 0,
            "tokens_out": response.usage.completion_tokens if response.usage else 0,
            "latency_ms": latency_ms,
        }
        return parsed, usage

    async def _call_anthropic(
        self,
        messages: list[dict[str, str]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Вызов Anthropic Messages API.

        Системное сообщение передаётся отдельным параметром system=,
        остальные сообщения — в messages (только user/assistant роли).
        """
        t_start = time.monotonic()

        # Anthropic требует system отдельно
        system_parts: list[str] = []
        user_messages: list[dict[str, str]] = []

        for msg in messages:
            if msg.get("role") == "system":
                system_parts.append(msg["content"])
            else:
                user_messages.append(msg)

        system_text = "\n\n".join(system_parts) if system_parts else None

        # Убеждаемся, что ответ будет JSON
        json_instruction = (
            "\n\nВАЖНО: отвечай строго валидным JSON-объектом без markdown-обёртки."
        )
        if system_text:
            system_text += json_instruction
        else:
            system_text = json_instruction.strip()

        kwargs: dict[str, Any] = {
            "model": settings.llm_fallback_model,
            "max_tokens": settings.llm_max_tokens,
            "messages": user_messages,
            "system": system_text,
        }
        # temperature у Anthropic не обязателен, но поддерживается
        if settings.llm_temperature is not None:
            kwargs["temperature"] = settings.llm_temperature

        response = await self._anthropic.messages.create(**kwargs, timeout=30)

        latency_ms = round((time.monotonic() - t_start) * 1000)

        # Извлекаем текст из первого content-блока
        raw_text = ""
        if response.content:
            raw_text = response.content[0].text

        # Убираем ```json ... ``` если модель всё равно обернула ответ
        raw_text = self._strip_markdown_json(raw_text)

        parsed = self._parse_json(raw_text)
        usage = {
            "model": response.model,
            "tokens_in": response.usage.input_tokens if response.usage else 0,
            "tokens_out": response.usage.output_tokens if response.usage else 0,
            "latency_ms": latency_ms,
        }
        return parsed, usage

    # ------------------------------------------------------------------
    # Утилиты
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_markdown_json(text: str) -> str:
        """Удалить обёртку ```json ... ``` или ``` ... ``` из ответа Anthropic."""
        # Паттерн: опциональный язык после ```, пробелы/переводы строк
        pattern = r"^```(?:json)?\s*\n?([\s\S]*?)\n?```$"
        match = re.match(pattern, text.strip(), re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return text.strip()

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        """Распарсить JSON. При ошибке вернуть словарь с сырым текстом."""
        try:
            result = json.loads(text)
            if not isinstance(result, dict):
                # Иногда модель возвращает список — оборачиваем
                return {"data": result}
            return result
        except json.JSONDecodeError as exc:
            logger.error("Не удалось распарсить JSON от LLM: %s | raw=%r", exc, text[:200])
            return {"_raw": text, "_parse_error": str(exc)}

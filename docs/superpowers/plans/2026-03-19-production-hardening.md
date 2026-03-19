# Production Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 8 critical production-readiness issues to bring AI-Native CRM from prototype to shippable MVP.

**Architecture:** All fixes are independent and can be implemented in parallel. Each fix touches 1-2 files max. Tests use fakeredis + MockAdapter + FakeLLM fixtures from conftest.py.

**Tech Stack:** Python 3.9+, aiogram 3, redis-py async, OpenAI/Anthropic, pytest-asyncio, fakeredis[lua]

---

## File Map

| Task | Files Modified | Files Created |
|------|---------------|---------------|
| 1. PII re-anonymization | `ai_native_crm/core/engine.py` | `ai_native_crm/tests/test_pii_reanon.py` |
| 2. Block AMOUNT_MISMATCH | `ai_native_crm/core/response_validator.py` | `ai_native_crm/tests/test_amount_block.py` |
| 3. Auth whitelist | `ai_native_crm/config.py`, `ai_native_crm/main.py` | `ai_native_crm/tests/test_auth.py` |
| 4. Rate limiting | `ai_native_crm/main.py` | `ai_native_crm/tests/test_rate_limit.py` |
| 5. Lock renewal | `ai_native_crm/services/lock.py` | `ai_native_crm/tests/test_lock_renewal.py` |
| 6. create_deal prompt | `ai_native_crm/core/engine.py` | - |
| 7. Bound critical_facts | `ai_native_crm/core/state_store.py`, `ai_native_crm/config.py` | `ai_native_crm/tests/test_facts_bound.py` |
| 8. CI/CD | - | `.github/workflows/ci.yml` |

---

### Task 1: PII Re-Anonymization on LLM Output

**Files:**
- Modify: `ai_native_crm/core/engine.py:329-355` (_apply_llm_updates)
- Test: `ai_native_crm/tests/test_pii_reanon.py`

**Problem:** LLM-generated working_memory, assessment, summary may contain PII (names, phones) that bypasses anonymization before being saved to Redis.

**Fix:** After extracting LLM fields but before saving state, run PIIAnonymizer.anonymize() on each text field.

- [ ] **Step 1:** Write test that LLM output containing PII gets re-anonymized before save
- [ ] **Step 2:** Run test, verify it fails
- [ ] **Step 3:** In `_apply_llm_updates`, add `await self._pii.anonymize(field, session_id)` for working_memory, assessment, summary
- [ ] **Step 4:** Run test, verify it passes
- [ ] **Step 5:** Commit

---

### Task 2: Block Actions on AMOUNT_MISMATCH

**Files:**
- Modify: `ai_native_crm/core/response_validator.py:140-168` (_validate_action)
- Test: `ai_native_crm/tests/test_amount_block.py`

**Problem:** AMOUNT_MISMATCH detected but action still executed (only logged).

**Fix:** When AMOUNT_MISMATCH detected, remove the action (return None) instead of passing it through.

- [ ] **Step 1:** Write test that action with mismatched amount is removed from actions list
- [ ] **Step 2:** Run test, verify it fails
- [ ] **Step 3:** In `_validate_action`, return `(None, alerts)` when AMOUNT_MISMATCH detected
- [ ] **Step 4:** Run test, verify it passes
- [ ] **Step 5:** Commit

---

### Task 3: Chat ID Authentication Whitelist

**Files:**
- Modify: `ai_native_crm/config.py` (add allowed_chat_ids setting)
- Modify: `ai_native_crm/main.py:166-186` (handle_text) and command handlers
- Test: `ai_native_crm/tests/test_auth.py`

**Problem:** Any Telegram user can access the bot and all CRM data.

**Fix:** Add `allowed_chat_ids` to config (comma-separated). If non-empty, reject messages from unlisted chat_ids.

- [ ] **Step 1:** Write test for auth rejection
- [ ] **Step 2:** Run test, verify it fails
- [ ] **Step 3:** Add `allowed_chat_ids: str = ""` to config.py, parse as set of ints
- [ ] **Step 4:** Add middleware/check in main.py that rejects unauthorized users with "Access denied"
- [ ] **Step 5:** Run test, verify it passes
- [ ] **Step 6:** Commit

---

### Task 4: Rate Limiting + Telegram 429 Backoff

**Files:**
- Modify: `ai_native_crm/main.py` (handle_text + reminder_scheduler)
- Test: `ai_native_crm/tests/test_rate_limit.py`

**Problem:** No per-user rate limiting; reminder scheduler can trigger Telegram 429 ban.

**Fix:** Add simple in-memory rate limiter (max N requests per minute per chat_id). Add exponential backoff on TelegramRetryAfter errors.

- [ ] **Step 1:** Write test for rate limiter rejecting excess requests
- [ ] **Step 2:** Run test, verify it fails
- [ ] **Step 3:** Implement `RateLimiter` class with sliding window
- [ ] **Step 4:** Add backoff on `TelegramRetryAfter` in reminder_scheduler
- [ ] **Step 5:** Run test, verify it passes
- [ ] **Step 6:** Commit

---

### Task 5: Lock TTL Renewal

**Files:**
- Modify: `ai_native_crm/services/lock.py:53-84` (lock context manager)
- Test: `ai_native_crm/tests/test_lock_renewal.py`

**Problem:** Lock TTL=30s but LLM calls can take 35-45s. Lock expires mid-operation, causing race conditions.

**Fix:** Spawn background asyncio task that renews lock TTL every TTL/3 seconds while held.

- [ ] **Step 1:** Write test that lock renewal keeps lock alive beyond initial TTL
- [ ] **Step 2:** Run test, verify it fails
- [ ] **Step 3:** Add `_renewal_task` in lock context manager that calls `SET key owner XX PX ttl` every TTL/3
- [ ] **Step 4:** Cancel renewal task on lock release
- [ ] **Step 5:** Run test, verify it passes
- [ ] **Step 6:** Commit

---

### Task 6: Fix System Prompt for create_deal

**Files:**
- Modify: `ai_native_crm/core/engine.py:47-82` (_SYSTEM_PROMPT)

**Problem:** LLM almost never generates create_deal action when asked to create a deal.

**Fix:** Add explicit create_deal example to system prompt actions section.

- [ ] **Step 1:** Add create_deal example to _SYSTEM_PROMPT actions array
- [ ] **Step 2:** Verify existing tests still pass
- [ ] **Step 3:** Commit

---

### Task 7: Bound critical_facts with LTRIM

**Files:**
- Modify: `ai_native_crm/core/state_store.py:212-226` (add_critical_fact)
- Modify: `ai_native_crm/config.py` (add max_critical_facts setting)
- Test: `ai_native_crm/tests/test_facts_bound.py`

**Problem:** critical_facts Redis List grows unbounded. After 10K+ facts, LRANGE loads them all every turn.

**Fix:** After RPUSH, call LTRIM to keep only last N facts (default 500).

- [ ] **Step 1:** Write test that list is trimmed after exceeding max
- [ ] **Step 2:** Run test, verify it fails
- [ ] **Step 3:** Add `max_critical_facts: int = 500` to config.py
- [ ] **Step 4:** Add `await self._r.ltrim(key, -max, -1)` after rpush in add_critical_fact
- [ ] **Step 5:** Run test, verify it passes
- [ ] **Step 6:** Commit

---

### Task 8: CI/CD Pipeline

**Files:**
- Create: `.github/workflows/ci.yml`

**Problem:** No automated testing on push/PR.

**Fix:** GitHub Actions workflow: lint (ruff) + test (pytest) on push to main and PRs.

- [ ] **Step 1:** Create `.github/workflows/ci.yml` with Python 3.11, Redis service, pytest
- [ ] **Step 2:** Commit and push
- [ ] **Step 3:** Verify workflow runs on GitHub

---

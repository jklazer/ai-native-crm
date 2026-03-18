# Changelog

## [0.1.0] — 2026-03-19

### Added
- Core AgentEngine with 10-step pipeline
- Bitrix24 CRM adapter
- AmoCRM adapter (API v4 + OAuth2)
- Semantic state management (Redis-only, no SQL)
- Critical facts (append-only, deduplication via Lua)
- State compression (LLM-based + fallback)
- Drift detection (CRM sync validation)
- Anti-hallucination validator
- PII anonymization (152-ФЗ: ФИО, телефоны, email)
- Distributed lock (Redis SET NX PX + Lua release)
- Audit trail (Redis Stream, 30-day TTL)
- Quality metrics (hallucination rate, drift, action success)
- Telegram bot interface (aiogram 3.x)
- MockAdapter for testing
- Demo script
- 30-turn stress tests

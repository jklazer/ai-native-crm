"""
setup_test_bitrix.py
====================
Creates 10 test contacts + 20 test deals in Bitrix24 via REST API for e2e testing.
Saves created IDs to test_deal_ids.json for teardown.

Usage:
    python setup_test_bitrix.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import aiohttp

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WEBHOOK = "https://b24-sz8hxp.bitrix24.ru/rest/1/j34kkubjf0dtl0qg/"
OUTPUT_FILE = Path(__file__).parent / "test_deal_ids.json"

# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

CONTACTS = [
    {
        "NAME": "Алексей",
        "LAST_NAME": "Петров",
        "PHONE": [{"VALUE": "+7 (916) 100-10-01", "VALUE_TYPE": "WORK"}],
        "EMAIL": [{"VALUE": "petrov@alphatech.ru", "VALUE_TYPE": "WORK"}],
    },
    {
        "NAME": "Марина",
        "LAST_NAME": "Козлова",
        "PHONE": [{"VALUE": "+7 (925) 200-20-02", "VALUE_TYPE": "WORK"}],
        "EMAIL": [{"VALUE": "kozlova@betasoft.ru", "VALUE_TYPE": "WORK"}],
    },
    {
        "NAME": "Дмитрий",
        "LAST_NAME": "Сидоров",
        "PHONE": [{"VALUE": "+7 (903) 300-30-03", "VALUE_TYPE": "WORK"}],
        "EMAIL": [{"VALUE": "sidorov@gamma.ru", "VALUE_TYPE": "WORK"}],
    },
    {
        "NAME": "Елена",
        "LAST_NAME": "Васильева",
        "PHONE": [{"VALUE": "+7 (977) 400-40-04", "VALUE_TYPE": "WORK"}],
        "EMAIL": [{"VALUE": "vasileva@delta.ru", "VALUE_TYPE": "WORK"}],
    },
    {
        "NAME": "Иван",
        "LAST_NAME": "Новиков",
        "PHONE": [{"VALUE": "+7 (495) 500-50-05", "VALUE_TYPE": "WORK"}],
        "EMAIL": [{"VALUE": "novikov@epsilon.ru", "VALUE_TYPE": "WORK"}],
    },
    {
        "NAME": "Ольга",
        "LAST_NAME": "Морозова",
        "PHONE": [{"VALUE": "+7 (916) 600-60-06", "VALUE_TYPE": "WORK"}],
        "EMAIL": [{"VALUE": "morozova@zeta.ru", "VALUE_TYPE": "WORK"}],
    },
    {
        "NAME": "Сергей",
        "LAST_NAME": "Волков",
        "PHONE": [{"VALUE": "+7 (925) 700-70-07", "VALUE_TYPE": "WORK"}],
        "EMAIL": [{"VALUE": "volkov@eta.ru", "VALUE_TYPE": "WORK"}],
    },
    {
        "NAME": "Наталья",
        "LAST_NAME": "Лебедева",
        "PHONE": [{"VALUE": "+7 (903) 800-80-08", "VALUE_TYPE": "WORK"}],
        "EMAIL": [{"VALUE": "lebedeva@theta.ru", "VALUE_TYPE": "WORK"}],
    },
    {
        "NAME": "Андрей",
        "LAST_NAME": "Соколов",
        "PHONE": [{"VALUE": "+7 (977) 900-90-09", "VALUE_TYPE": "WORK"}],
        "EMAIL": [{"VALUE": "sokolov@iota.ru", "VALUE_TYPE": "WORK"}],
    },
    {
        "NAME": "Татьяна",
        "LAST_NAME": "Кузнецова",
        "PHONE": [{"VALUE": "+7 (495) 111-11-11", "VALUE_TYPE": "WORK"}],
        "EMAIL": [{"VALUE": "kuznetsova@kappa.ru", "VALUE_TYPE": "WORK"}],
    },
]

# contact_idx refers to position in CONTACTS list above
DEALS = [
    # 5 × NEW (50k–500k)
    {"TITLE": "[TEST] АльфаТех — внедрение CRM",        "STAGE_ID": "NEW",          "OPPORTUNITY": 150000,  "CURRENCY_ID": "RUB", "contact_idx": 0},
    {"TITLE": "[TEST] БетаСофт — автоматизация склада",  "STAGE_ID": "NEW",          "OPPORTUNITY": 280000,  "CURRENCY_ID": "RUB", "contact_idx": 1},
    {"TITLE": "[TEST] ГаммаПро — разработка сайта",      "STAGE_ID": "NEW",          "OPPORTUNITY": 95000,   "CURRENCY_ID": "RUB", "contact_idx": 2},
    {"TITLE": "[TEST] ДельтаИнж — техподдержка",         "STAGE_ID": "NEW",          "OPPORTUNITY": 50000,   "CURRENCY_ID": "RUB", "contact_idx": 3},
    {"TITLE": "[TEST] ЭпсилонГруп — интеграция 1С",     "STAGE_ID": "NEW",          "OPPORTUNITY": 420000,  "CURRENCY_ID": "RUB", "contact_idx": 4},

    # 5 × UC_QUALIFIED (Negotiation)
    {"TITLE": "[TEST] ЗетаЛогистик — TMS система",      "STAGE_ID": "UC_QUALIFIED", "OPPORTUNITY": 350000,  "CURRENCY_ID": "RUB", "contact_idx": 5},
    {"TITLE": "[TEST] ЭтаФинанс — биллинг",             "STAGE_ID": "UC_QUALIFIED", "OPPORTUNITY": 500000,  "CURRENCY_ID": "RUB", "contact_idx": 6},
    {"TITLE": "[TEST] ТетаМедиа — видеоплатформа",       "STAGE_ID": "UC_QUALIFIED", "OPPORTUNITY": 180000,  "CURRENCY_ID": "RUB", "contact_idx": 7},
    {"TITLE": "[TEST] ЙотаТех — мобильное приложение",   "STAGE_ID": "UC_QUALIFIED", "OPPORTUNITY": 620000,  "CURRENCY_ID": "RUB", "contact_idx": 8},
    {"TITLE": "[TEST] КаппаРитейл — POS интеграция",     "STAGE_ID": "UC_QUALIFIED", "OPPORTUNITY": 230000,  "CURRENCY_ID": "RUB", "contact_idx": 9},

    # 5 × UC_INVOICE (Proposal)
    {"TITLE": "[TEST] ЛямбдаСтрой — проектирование",    "STAGE_ID": "UC_INVOICE",   "OPPORTUNITY": 750000,  "CURRENCY_ID": "RUB", "contact_idx": 0},
    {"TITLE": "[TEST] МюКонсалт — аудит процессов",      "STAGE_ID": "UC_INVOICE",   "OPPORTUNITY": 120000,  "CURRENCY_ID": "RUB", "contact_idx": 1},
    {"TITLE": "[TEST] НюФарма — CRM для аптек",          "STAGE_ID": "UC_INVOICE",   "OPPORTUNITY": 340000,  "CURRENCY_ID": "RUB", "contact_idx": 2},
    {"TITLE": "[TEST] КсиЭнерго — мониторинг",           "STAGE_ID": "UC_INVOICE",   "OPPORTUNITY": 190000,  "CURRENCY_ID": "RUB", "contact_idx": 3},
    {"TITLE": "[TEST] ОмикронАвто — fleet management",   "STAGE_ID": "UC_INVOICE",   "OPPORTUNITY": 880000,  "CURRENCY_ID": "RUB", "contact_idx": 4},

    # 3 × WON
    {"TITLE": "[TEST] ПиДизайн — ребрендинг",            "STAGE_ID": "WON",          "OPPORTUNITY": 200000,  "CURRENCY_ID": "RUB", "contact_idx": 5},
    {"TITLE": "[TEST] РоТрейд — маркетплейс",            "STAGE_ID": "WON",          "OPPORTUNITY": 1500000, "CURRENCY_ID": "RUB", "contact_idx": 6},
    {"TITLE": "[TEST] СигмаБанк — онлайн-банкинг",       "STAGE_ID": "WON",          "OPPORTUNITY": 950000,  "CURRENCY_ID": "RUB", "contact_idx": 7},

    # 2 × LOSE
    {"TITLE": "[TEST] ТауЛогистик — провал бюджета",     "STAGE_ID": "LOSE",         "OPPORTUNITY": 300000,  "CURRENCY_ID": "RUB", "contact_idx": 8},
    {"TITLE": "[TEST] ИпсилонСервис — ушли к конкуренту","STAGE_ID": "LOSE",         "OPPORTUNITY": 175000,  "CURRENCY_ID": "RUB", "contact_idx": 9},
]

# Redis keys to flush for test chat_id 80001
REDIS_TEST_CHAT_ID = 80001
REDIS_KEY_PATTERNS = [
    f"state:{REDIS_TEST_CHAT_ID}",
    f"critical_facts:{REDIS_TEST_CHAT_ID}",
    f"audit:{REDIS_TEST_CHAT_ID}",
    f"metrics:{REDIS_TEST_CHAT_ID}",
    f"reminders:{REDIS_TEST_CHAT_ID}",
    f"pii:{REDIS_TEST_CHAT_ID}",
    f"pii:{REDIS_TEST_CHAT_ID}:*",  # wildcard — see note in flush_redis_test_state
]


# ---------------------------------------------------------------------------
# Bitrix24 helpers
# ---------------------------------------------------------------------------

async def bitrix_call(
    session: aiohttp.ClientSession,
    method: str,
    params: dict,
) -> dict:
    """Call a single Bitrix24 REST method and return the parsed JSON response."""
    url = f"{WEBHOOK}{method}"
    async with session.post(url, json=params) as resp:
        resp.raise_for_status()
        data = await resp.json()
    if "error" in data:
        raise RuntimeError(
            f"Bitrix24 error [{method}]: {data.get('error')} — {data.get('error_description', '')}"
        )
    return data


async def create_contact(session: aiohttp.ClientSession, contact: dict) -> int:
    """Create a single contact; returns the new contact ID."""
    fields = {k: v for k, v in contact.items()}  # shallow copy
    result = await bitrix_call(session, "crm.contact.add", {"fields": fields})
    return int(result["result"])


async def create_deal(
    session: aiohttp.ClientSession,
    deal: dict,
    contact_id: int,
) -> int:
    """Create a single deal linked to contact_id; returns the new deal ID."""
    fields = {k: v for k, v in deal.items() if k != "contact_idx"}
    fields["CONTACT_ID"] = contact_id
    result = await bitrix_call(session, "crm.deal.add", {"fields": fields})
    return int(result["result"])


# ---------------------------------------------------------------------------
# Redis cleanup (best-effort — no crash if Redis unavailable)
# ---------------------------------------------------------------------------

async def flush_redis_test_state() -> None:
    """Delete all Redis keys associated with test chat_id 80001."""
    try:
        import redis.asyncio as aioredis  # type: ignore
    except ImportError:
        print("  [redis] redis-py not importable — skipping Redis cleanup")
        return

    # Respect REDIS_URL env var if set, otherwise fall back to local default
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    try:
        client = aioredis.from_url(redis_url, decode_responses=True)
        await client.ping()
    except Exception as exc:
        print(f"  [redis] Cannot connect to {redis_url}: {exc} — skipping Redis cleanup")
        return

    deleted = 0
    # Exact keys first
    exact_keys = [
        f"state:{REDIS_TEST_CHAT_ID}",
        f"critical_facts:{REDIS_TEST_CHAT_ID}",
        f"audit:{REDIS_TEST_CHAT_ID}",
        f"metrics:{REDIS_TEST_CHAT_ID}",
        f"reminders:{REDIS_TEST_CHAT_ID}",
        f"pii:{REDIS_TEST_CHAT_ID}",
    ]
    # Wildcard scan for any pii sub-keys (pii:80001:*)
    cursor = 0
    wildcard_keys: list[str] = []
    while True:
        cursor, keys = await client.scan(cursor, match=f"pii:{REDIS_TEST_CHAT_ID}:*", count=100)
        wildcard_keys.extend(keys)
        if cursor == 0:
            break

    all_keys = exact_keys + wildcard_keys
    if all_keys:
        deleted = await client.delete(*all_keys)

    await client.aclose()
    print(f"  [redis] Deleted {deleted} key(s) for chat_id {REDIS_TEST_CHAT_ID}")


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def print_summary(
    deals: list[dict],
    contact_ids: list[int],
    deal_ids: list[int],
) -> None:
    col_title = 48
    col_stage = 14
    col_amount = 12
    col_contact = 24
    col_id = 8

    header = (
        f"{'Deal title':<{col_title}} "
        f"{'Stage':<{col_stage}} "
        f"{'Amount (RUB)':>{col_amount}} "
        f"{'Contact':<{col_contact}} "
        f"{'DealID':>{col_id}}"
    )
    sep = "-" * len(header)

    print()
    print(sep)
    print(header)
    print(sep)

    for i, (deal, deal_id) in enumerate(zip(deals, deal_ids)):
        c_idx = deal["contact_idx"]
        c = CONTACTS[c_idx]
        contact_name = f"{c['NAME']} {c['LAST_NAME']}"
        contact_id = contact_ids[c_idx]
        print(
            f"{deal['TITLE']:<{col_title}} "
            f"{deal['STAGE_ID']:<{col_stage}} "
            f"{deal['OPPORTUNITY']:>{col_amount},} "
            f"{contact_name + ' #' + str(contact_id):<{col_contact}} "
            f"{deal_id:>{col_id}}"
        )

    print(sep)
    print(f"  Total deals : {len(deal_ids)}")
    print(f"  Total contacts : {len(contact_ids)}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print("=" * 60)
    print("  Bitrix24 test fixture setup")
    print("=" * 60)

    # -- Step 0: flush Redis state for test chat_id --------------------------
    print(f"\n[0/3] Flushing Redis state for chat_id {REDIS_TEST_CHAT_ID}...")
    await flush_redis_test_state()

    connector = aiohttp.TCPConnector(limit=5)
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        # -- Step 1: create contacts -----------------------------------------
        print(f"\n[1/3] Creating {len(CONTACTS)} contacts...")
        contact_ids: list[int] = []
        for i, contact in enumerate(CONTACTS):
            try:
                cid = await create_contact(session, contact)
                contact_ids.append(cid)
                full_name = f"{contact['NAME']} {contact['LAST_NAME']}"
                print(f"  [{i+1:02d}/{len(CONTACTS)}] {full_name:<30} -> contact_id={cid}")
            except Exception as exc:
                print(f"  [ERROR] Failed to create contact {contact['NAME']} {contact['LAST_NAME']}: {exc}")
                sys.exit(1)

        # -- Step 2: create deals --------------------------------------------
        print(f"\n[2/3] Creating {len(DEALS)} deals...")
        deal_ids: list[int] = []
        for i, deal in enumerate(DEALS):
            c_idx = deal["contact_idx"]
            contact_id = contact_ids[c_idx]
            try:
                did = await create_deal(session, deal, contact_id)
                deal_ids.append(did)
                print(
                    f"  [{i+1:02d}/{len(DEALS)}] {deal['TITLE']:<50} "
                    f"stage={deal['STAGE_ID']:<14} -> deal_id={did}"
                )
            except Exception as exc:
                print(f"  [ERROR] Failed to create deal '{deal['TITLE']}': {exc}")
                sys.exit(1)

    # -- Step 3: persist IDs for teardown ------------------------------------
    print(f"\n[3/3] Saving IDs to {OUTPUT_FILE}...")

    payload = {
        "contact_ids": contact_ids,
        "deal_ids": deal_ids,
        "contacts": {
            str(cid): f"{CONTACTS[i]['NAME']} {CONTACTS[i]['LAST_NAME']}"
            for i, cid in enumerate(contact_ids)
        },
        "deals": {
            str(did): DEALS[i]["TITLE"]
            for i, did in enumerate(deal_ids)
        },
        "redis_test_chat_id": REDIS_TEST_CHAT_ID,
    }

    OUTPUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Saved {len(deal_ids)} deal IDs + {len(contact_ids)} contact IDs")

    # -- Summary table -------------------------------------------------------
    print_summary(DEALS, contact_ids, deal_ids)
    print("Done. Run teardown_test_bitrix.py to clean up.")


if __name__ == "__main__":
    asyncio.run(main())

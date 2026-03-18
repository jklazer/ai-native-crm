"""
teardown_test_bitrix.py
=======================
Reads test_deal_ids.json and deletes all test deals + contacts from Bitrix24.
Also cleans Redis state keys for test chat_id 80001.
Removes test_deal_ids.json when done.

Usage:
    python teardown_test_bitrix.py
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
    # Bitrix24 returns {"result": true} on successful delete; "error" key on failure
    if "error" in data:
        raise RuntimeError(
            f"Bitrix24 error [{method}]: {data.get('error')} — {data.get('error_description', '')}"
        )
    return data


async def delete_entity(
    session: aiohttp.ClientSession,
    method: str,
    entity_id: int,
    label: str,
    name: str,
) -> bool:
    """Delete a single entity; returns True on success, False on non-fatal error."""
    try:
        await bitrix_call(session, method, {"id": entity_id})
        print(f"  [OK] Deleted {label} id={entity_id}  ({name})")
        return True
    except Exception as exc:
        print(f"  [WARN] Could not delete {label} id={entity_id} ({name}): {exc}")
        return False


# ---------------------------------------------------------------------------
# Redis cleanup
# ---------------------------------------------------------------------------

async def flush_redis_test_state(chat_id: int) -> None:
    """Delete all Redis keys associated with the given test chat_id."""
    try:
        import redis.asyncio as aioredis  # type: ignore
    except ImportError:
        print("  [redis] redis-py not importable — skipping Redis cleanup")
        return

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    try:
        client = aioredis.from_url(redis_url, decode_responses=True)
        await client.ping()
    except Exception as exc:
        print(f"  [redis] Cannot connect to {redis_url}: {exc} — skipping Redis cleanup")
        return

    # Exact keys
    exact_keys = [
        f"state:{chat_id}",
        f"critical_facts:{chat_id}",
        f"audit:{chat_id}",
        f"metrics:{chat_id}",
        f"reminders:{chat_id}",
        f"pii:{chat_id}",
    ]

    # Wildcard scan for sub-keys  e.g. pii:80001:field
    wildcard_keys: list[str] = []
    cursor = 0
    while True:
        cursor, keys = await client.scan(cursor, match=f"pii:{chat_id}:*", count=100)
        wildcard_keys.extend(keys)
        if cursor == 0:
            break

    all_keys = exact_keys + wildcard_keys
    deleted = 0
    if all_keys:
        deleted = await client.delete(*all_keys)

    await client.aclose()
    print(f"  [redis] Deleted {deleted} key(s) for chat_id {chat_id}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print("=" * 60)
    print("  Bitrix24 test fixture teardown")
    print("=" * 60)

    # -- Load IDs from JSON --------------------------------------------------
    if not OUTPUT_FILE.exists():
        print(f"\n[ERROR] {OUTPUT_FILE} not found. Nothing to tear down.")
        sys.exit(1)

    payload = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))

    deal_ids: list[int] = payload.get("deal_ids", [])
    contact_ids: list[int] = payload.get("contact_ids", [])
    deals_map: dict[str, str] = payload.get("deals", {})
    contacts_map: dict[str, str] = payload.get("contacts", {})
    redis_test_chat_id: int = payload.get("redis_test_chat_id", 80001)

    print(f"\nLoaded {len(deal_ids)} deal(s) and {len(contact_ids)} contact(s) from {OUTPUT_FILE.name}")

    connector = aiohttp.TCPConnector(limit=5)
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        # -- Step 1: delete deals --------------------------------------------
        print(f"\n[1/3] Deleting {len(deal_ids)} deal(s)...")
        deal_ok = 0
        deal_fail = 0
        for did in deal_ids:
            name = deals_map.get(str(did), "?")
            ok = await delete_entity(session, "crm.deal.delete", did, "deal", name)
            if ok:
                deal_ok += 1
            else:
                deal_fail += 1

        print(f"  Deals deleted: {deal_ok}  /  failed: {deal_fail}")

        # -- Step 2: delete contacts -----------------------------------------
        print(f"\n[2/3] Deleting {len(contact_ids)} contact(s)...")
        contact_ok = 0
        contact_fail = 0
        for cid in contact_ids:
            name = contacts_map.get(str(cid), "?")
            ok = await delete_entity(session, "crm.contact.delete", cid, "contact", name)
            if ok:
                contact_ok += 1
            else:
                contact_fail += 1

        print(f"  Contacts deleted: {contact_ok}  /  failed: {contact_fail}")

    # -- Step 3: clean Redis -------------------------------------------------
    print(f"\n[3/4] Flushing Redis state for chat_id {redis_test_chat_id}...")
    await flush_redis_test_state(redis_test_chat_id)

    # -- Step 4: remove JSON file --------------------------------------------
    print(f"\n[4/4] Removing {OUTPUT_FILE.name}...")
    try:
        OUTPUT_FILE.unlink()
        print(f"  Removed {OUTPUT_FILE}")
    except Exception as exc:
        print(f"  [WARN] Could not remove {OUTPUT_FILE}: {exc}")

    # -- Final report --------------------------------------------------------
    total_errors = deal_fail + contact_fail
    print()
    print("=" * 60)
    if total_errors == 0:
        print("  Teardown complete — all test data removed.")
    else:
        print(f"  Teardown finished with {total_errors} warning(s). Check output above.")
    print("=" * 60)
    print()


if __name__ == "__main__":
    asyncio.run(main())

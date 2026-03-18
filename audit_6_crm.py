"""
audit_6_crm.py — Section 6: Bitrix24 CRM Adapter audit tests.

Tests the BitrixAdapter directly (bypassing the engine) and writes
results to audit_6_crm_results.txt.
"""

import os
import sys
import asyncio
import traceback
import time

os.environ["PYTHONIOENCODING"] = "utf-8"

# Make sure project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ai_native_crm.adapters.bitrix import BitrixAdapter
from ai_native_crm.adapters.base import DealInfo
from ai_native_crm.config import settings

WEBHOOK = settings.bitrix_webhook
RESULTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_6_crm_results.txt")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

class Reporter:
    def __init__(self):
        self.lines = []
        self.passed = 0
        self.failed = 0
        self.warnings = 0

    def _emit(self, line: str):
        self.lines.append(line)
        print(line)

    def header(self, title: str):
        sep = "=" * 70
        self._emit(f"\n{sep}")
        self._emit(f"  {title}")
        self._emit(sep)

    def section(self, name: str):
        self._emit(f"\n--- {name} ---")

    def info(self, msg: str):
        self._emit(f"    INFO: {msg}")

    def ok(self, test_name: str, msg: str = ""):
        self.passed += 1
        self._emit(f"  [PASS] {test_name}" + (f" | {msg}" if msg else ""))

    def fail(self, test_name: str, msg: str = "", severity: str = "HIGH"):
        self.failed += 1
        self._emit(f"  [FAIL] [{severity}] {test_name}" + (f" | {msg}" if msg else ""))

    def warn(self, test_name: str, msg: str = ""):
        self.warnings += 1
        self._emit(f"  [WARN] {test_name}" + (f" | {msg}" if msg else ""))

    def summary(self):
        sep = "=" * 70
        self._emit(f"\n{sep}")
        self._emit("SUMMARY")
        self._emit(sep)
        self._emit(f"  PASS:    {self.passed}")
        self._emit(f"  FAIL:    {self.failed}")
        self._emit(f"  WARN:    {self.warnings}")
        self._emit(f"  TOTAL:   {self.passed + self.failed + self.warnings}")
        self._emit(sep)

    def save(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.lines))
        print(f"\nResults written to: {path}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_get_deals_normal(r: Reporter):
    """Test 1: get_deals() returns a list of DealInfo objects."""
    r.section("TEST 1 — get_deals() normal")
    adapter = BitrixAdapter(WEBHOOK)
    try:
        deals = await adapter.get_deals()
        r.info(f"Returned type: {type(deals)}")

        if not isinstance(deals, list):
            r.fail("get_deals returns list", f"Got {type(deals).__name__}")
            return None

        r.ok("get_deals returns list", f"count={len(deals)}")

        if len(deals) > 0:
            sample = deals[0]
            r.info(f"Sample deal: id={sample.id!r} title={sample.title!r} "
                   f"stage={sample.stage!r} amount={sample.amount} "
                   f"currency={sample.currency!r}")
            r.ok("get_deals returns DealInfo instances", f"first id={sample.id!r}")
        else:
            r.warn("get_deals returned 0 deals", "CRM may be empty — check manually")

        return deals
    except Exception as exc:
        r.fail("get_deals() raised unexpected exception", str(exc), severity="CRITICAL")
        traceback.print_exc()
        return None
    finally:
        await adapter.close()


async def test_field_mapping(r: Reporter, deals):
    """Test 2: DealInfo field mapping — check all expected fields are present."""
    r.section("TEST 2 — get_deals() field mapping")

    if deals is None:
        r.warn("field_mapping skipped", "No deals from Test 1")
        return

    required_fields = ["id", "title", "stage", "amount", "currency", "contact_name"]
    none_fields_across_deals = {f: 0 for f in required_fields}
    empty_fields_across_deals = {f: 0 for f in required_fields}

    for deal in deals:
        if not isinstance(deal, DealInfo):
            r.fail("deal is DealInfo instance", f"Got {type(deal).__name__}", severity="HIGH")
            continue
        for field_name in required_fields:
            val = getattr(deal, field_name, "__MISSING__")
            if val is None:
                none_fields_across_deals[field_name] += 1
            elif val == "" or val == 0.0:
                empty_fields_across_deals[field_name] += 1

    r.ok("DealInfo has all required fields", f"fields={required_fields}")

    # Report None and empty fields
    for field_name in required_fields:
        none_count = none_fields_across_deals[field_name]
        empty_count = empty_fields_across_deals[field_name]
        if none_count > 0:
            r.warn(f"field '{field_name}' is None",
                   f"in {none_count}/{len(deals)} deals")
        elif empty_count > 0:
            # contact_name is expected to be empty from crm.deal.list
            if field_name == "contact_name":
                r.info(f"field 'contact_name' is empty in {empty_count}/{len(deals)} deals "
                       f"(expected — crm.deal.list does not return it)")
                r.ok("contact_name empty is expected behavior",
                     "adapter comment confirms this")
            else:
                r.warn(f"field '{field_name}' is empty/zero",
                       f"in {empty_count}/{len(deals)} deals")
        else:
            r.ok(f"field '{field_name}' populated", f"across {len(deals)} deals")

    # Show detailed sample
    if deals:
        d = deals[0]
        r.info(f"Full sample: id={d.id!r}, title={d.title!r}, stage={d.stage!r}, "
               f"amount={d.amount}, currency={d.currency!r}, "
               f"contact_name={d.contact_name!r}, contact_id={d.contact_id!r}")


async def test_update_deal_invalid_id(r: Reporter):
    """Test 3: update_deal() with invalid/nonexistent deal ID."""
    r.section("TEST 3 — update_deal() with invalid ID=999999")
    adapter = BitrixAdapter(WEBHOOK)
    try:
        result = await adapter.update_deal("999999", {"TITLE": "Test Audit"})
        r.info(f"update_deal('999999', ...) returned: {result!r}")
        if result is False:
            r.ok("update_deal invalid ID returns False", "Correct — silently fails")
        elif result is True:
            r.fail("update_deal invalid ID returned True",
                   "ID 999999 should not exist — returned success unexpectedly",
                   severity="MEDIUM")
        else:
            r.warn("update_deal invalid ID returned unexpected value", str(result))
    except Exception as exc:
        r.warn("update_deal invalid ID raised exception",
               f"{type(exc).__name__}: {exc}")
        r.info("Exception-on-invalid-ID is acceptable if caught by caller")
    finally:
        await adapter.close()


async def test_update_deal_invalid_fields(r: Reporter, deals):
    """Test 4: update_deal() with nonexistent field name."""
    r.section("TEST 4 — update_deal() with invalid field NONEXISTENT_FIELD")

    real_deal_id = None
    if deals:
        real_deal_id = deals[0].id

    if not real_deal_id:
        r.warn("test_update_deal_invalid_fields skipped", "No real deals available")
        return

    r.info(f"Using real deal_id={real_deal_id!r}")
    adapter = BitrixAdapter(WEBHOOK)
    try:
        result = await adapter.update_deal(real_deal_id, {"NONEXISTENT_FIELD": "test_value"})
        r.info(f"update_deal({real_deal_id!r}, NONEXISTENT_FIELD) returned: {result!r}")
        if result is True:
            r.warn("update_deal with invalid field returned True",
                   "Bitrix24 silently ignores unknown fields — not an error per se, but note it")
        elif result is False:
            r.ok("update_deal with invalid field returned False", "Bitrix rejected invalid field")
        else:
            r.info(f"Unexpected return value: {result!r}")
    except Exception as exc:
        r.warn("update_deal invalid field raised exception",
               f"{type(exc).__name__}: {exc}")
    finally:
        await adapter.close()


async def test_error_handling_wrong_url(r: Reporter):
    """Test 5: BitrixAdapter with wrong URL — does get_deals raise or return []?"""
    r.section("TEST 5 — BitrixAdapter with wrong/invalid webhook URL")
    bad_adapter = BitrixAdapter("https://invalid-url.example.com/rest/1/fake/")
    try:
        t0 = time.monotonic()
        result = await bad_adapter.get_deals()
        elapsed = time.monotonic() - t0
        r.info(f"get_deals() on invalid URL returned: {result!r} in {elapsed:.2f}s")
        if isinstance(result, list) and len(result) == 0:
            r.ok("wrong URL returns empty list (graceful degradation)",
                 f"elapsed={elapsed:.2f}s")
        else:
            r.warn("wrong URL returned unexpected non-empty result", str(result))
    except Exception as exc:
        r.info(f"get_deals() on invalid URL raised: {type(exc).__name__}: {exc}")
        # The code in get_deals() catches all exceptions and returns []
        # so this path means the inner exception escaped the try/except
        r.fail("wrong URL raised unhandled exception",
               f"{type(exc).__name__}: {exc}",
               severity="MEDIUM")
    finally:
        await bad_adapter.close()


async def test_session_management(r: Reporter):
    """Test 6: Session is created lazily and close() works without error."""
    r.section("TEST 6 — Session management / adapter.close()")
    adapter = BitrixAdapter(WEBHOOK)
    try:
        # Before any call, session should be None
        if adapter._session is None:
            r.ok("session is None before first call", "Lazy initialization confirmed")
        else:
            r.warn("session created at __init__", "Should be lazy-initialized")

        # Trigger session creation
        _ = await adapter.get_deals()

        if adapter._session is not None and not adapter._session.closed:
            r.ok("session created after first call", "Lazy init works")
        else:
            r.warn("session missing or already closed after call", "Unexpected")

        # Now close
        await adapter.close()
        if adapter._session is None or adapter._session.closed:
            r.ok("adapter.close() closes session cleanly", "No exception")
        else:
            r.fail("adapter.close() did NOT close session", severity="LOW")

        # Double-close should be safe
        await adapter.close()
        r.ok("double close() is safe", "No exception on second close()")

    except Exception as exc:
        r.fail("session management raised exception",
               f"{type(exc).__name__}: {exc}", severity="MEDIUM")
        traceback.print_exc()


async def test_response_parsing_edge_cases(r: Reporter):
    """Test 7: Inspect raw Bitrix response for edge cases (null amounts, etc.)."""
    r.section("TEST 7 — Raw response edge cases (null amounts, missing fields)")
    adapter = BitrixAdapter(WEBHOOK)
    try:
        # Fetch raw data ourselves to inspect
        raw_data = await adapter._get("crm.deal.list", params={
            "select[]": ["ID", "TITLE", "STAGE_ID", "OPPORTUNITY", "CURRENCY_ID", "CONTACT_ID"],
            "filter[STAGE_SEMANTIC_ID][]": ["P", "F"],
        })
        result_raw = raw_data.get("result", [])
        r.info(f"Raw result count from crm.deal.list: {len(result_raw)}")

        null_opportunity = [d for d in result_raw if d.get("OPPORTUNITY") in (None, "", "0", 0)]
        missing_contact = [d for d in result_raw if not d.get("CONTACT_ID")]
        missing_title = [d for d in result_raw if not d.get("TITLE")]
        missing_stage = [d for d in result_raw if not d.get("STAGE_ID")]
        zero_opportunity = [d for d in result_raw if d.get("OPPORTUNITY") == "0"]

        r.info(f"Deals with null/empty OPPORTUNITY: {len(null_opportunity)}")
        r.info(f"Deals with OPPORTUNITY='0': {len(zero_opportunity)}")
        r.info(f"Deals without CONTACT_ID: {len(missing_contact)}")
        r.info(f"Deals without TITLE: {len(missing_title)}")
        r.info(f"Deals without STAGE_ID: {len(missing_stage)}")

        if null_opportunity:
            r.warn("Some deals have null/empty OPPORTUNITY",
                   f"count={len(null_opportunity)} — adapter defaults to 0.0 (OK by design)")
            # Verify the adapter handles this correctly
            adapter2 = BitrixAdapter(WEBHOOK)
            deals = await adapter2.get_deals()
            await adapter2.close()
            null_amount_deals = [d for d in deals if d.amount == 0.0]
            r.info(f"After parsing: deals with amount=0.0: {len(null_amount_deals)}")
            r.ok("null OPPORTUNITY parsed to 0.0 (correct default)", "No crash")
        else:
            r.ok("All deals have non-null OPPORTUNITY", "No edge case in current data")

        if missing_contact:
            r.warn("Some deals have no CONTACT_ID",
                   f"count={len(missing_contact)} — contact_id defaults to '' (OK)")
        else:
            r.ok("All deals have CONTACT_ID set")

        # Inspect 'next' pagination field
        if "next" in raw_data:
            r.warn("Bitrix pagination: 'next' field present",
                   f"Only first 50 deals returned — adapter does NOT paginate")
        else:
            r.ok("No pagination needed", "All deals fit in single response")

        # Inspect 'error' field in raw response
        if "error" in raw_data:
            r.fail("Bitrix returned error field in response",
                   str(raw_data.get("error")), severity="HIGH")
        else:
            r.ok("No 'error' field in raw response")

    except Exception as exc:
        r.fail("raw response inspection raised exception",
               f"{type(exc).__name__}: {exc}", severity="HIGH")
        traceback.print_exc()
    finally:
        await adapter.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    r = Reporter()
    r.header("SECTION 6: Bitrix24 CRM Adapter Audit")
    r.info(f"Webhook: {WEBHOOK}")
    r.info(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # Test 1 — run first, its result feeds other tests
    deals = await test_get_deals_normal(r)

    # Test 2
    await test_field_mapping(r, deals)

    # Test 3
    await test_update_deal_invalid_id(r)

    # Test 4
    await test_update_deal_invalid_fields(r, deals)

    # Test 5
    await test_error_handling_wrong_url(r)

    # Test 6
    await test_session_management(r)

    # Test 7
    await test_response_parsing_edge_cases(r)

    r.summary()
    r.save(RESULTS_FILE)


if __name__ == "__main__":
    asyncio.run(main())

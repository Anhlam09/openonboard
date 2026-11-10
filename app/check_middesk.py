"""Check that your Middesk connection works, end to end.

    python -m app.check_middesk

It creates one test business in the Middesk SANDBOX (name "Corporation", 223 Grand St, New York),
waits for results, and prints every review task so you can confirm the rules in app/decision.py
match what your account returns. It refuses to run with a live key unless you pass --live.
"""

from __future__ import annotations

import sys
import time

from . import config
from .decision import RULES, evaluate
from .middesk import MiddeskClient, MiddeskError

HINTS = {
    "401": "Middesk rejected the key. Check MIDDESK_API_KEY, or try MIDDESK_AUTH=basic in .env.",
    "403": "The key works but this product may not be enabled. Try MIDDESK_ORDER_PRODUCT= (blank) in .env, or ask Middesk.",
    "404": "Wrong URL. Sandbox is https://api-sandbox.middesk.com/v1, live is https://api.middesk.com/v1.",
    "422": "Middesk didn't accept the request format. Copy the error above into an issue.",
}


def fail(msg: str) -> None:
    print(f"\nFAILED: {msg}")
    sys.exit(1)


def main() -> None:
    if config.MOCK_MODE:
        fail("MIDDESK_API_KEY is empty, so the app is in mock mode. Add your sandbox key to .env first.")
    live = config.MIDDESK_API_KEY.startswith("mk_live_") or "sandbox" not in config.MIDDESK_BASE_URL
    if live and "--live" not in sys.argv:
        fail("This looks like a LIVE key or URL. Live checks may be billed. Re-run with --live if you really mean it.")

    print(f"Middesk URL:  {config.MIDDESK_BASE_URL}")
    print(f"Auth style:   {config.MIDDESK_AUTH}")
    print(f"Order:        {config.MIDDESK_ORDER_PRODUCT or '(account default)'}")

    client = MiddeskClient(config.MIDDESK_API_KEY, config.MIDDESK_BASE_URL, config.MIDDESK_AUTH)
    payload = {
        "name": "Corporation",
        "addresses": [{"address_line1": "223 Grand St.", "city": "New York", "state": "NY", "postal_code": "10013"}],
        "external_id": f"openonboard-check-{int(time.time())}",
    }
    if config.MIDDESK_ORDER_PRODUCT:
        payload["orders"] = [{"product": config.MIDDESK_ORDER_PRODUCT}]

    print("\n1. Creating a test business...")
    try:
        biz = client.create_business(payload)
    except MiddeskError as e:
        code = next((c for c in HINTS if f"Middesk {c}" in str(e)), None)
        fail(f"{e}\n{HINTS.get(code, '')}")
    print(f"   OK. Middesk business id: {biz['id']}  status: {biz.get('status')}")

    print("2. Waiting for results (up to 90 seconds)...")
    for _ in range(18):
        biz = client.get_business(biz["id"])
        if biz.get("status") not in {"open", "pending"}:
            break
        time.sleep(5)
    print(f"   Status: {biz.get('status')}")

    tasks = (biz.get("review") or {}).get("tasks") or []
    if not tasks:
        fail("No review tasks came back yet. Wait a minute and check the business in the Middesk Dashboard.")

    print("3. Review tasks returned:\n")
    known = RULES["hard_stop_keys"] | RULES["escalate_keys"]
    print(f"   {'key':<26}{'category':<20}{'status':<10}message")
    for t in tasks:
        mark = "*" if t.get("key") in known else " "
        print(
            f" {mark} {t.get('key', ''):<26}{t.get('category', ''):<20}{t.get('status', ''):<10}{t.get('message', '')}"
        )
    print("\n   * = a key the rules in app/decision.py act on")

    ev = evaluate(biz, tin_submitted=False)
    print(f"\n4. OpenOnboard would show: {ev['recommendation']} (priority {ev['priority']})")
    print("   (This test sends no EIN, so 'needs_review' is expected.)")
    print("\nSUCCESS: your Middesk connection works.")
    print("If a key you care about has no *, add it to RULES in app/decision.py.")


if __name__ == "__main__":
    main()

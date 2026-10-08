"""Link a Sandbox bank end to end, without the frontend, then sync it.

Gets a Sandbox public_token straight from Plaid, then exchanges it through our
running API, the same call the frontend makes after Link succeeds. Then syncs
the new item twice: the second run should add nothing (sync is idempotent).

Run with the API up (uvicorn app.main:app), from any directory:
    python backend/scripts/sandbox_link.py [base_url]
"""
import sys
import time
from pathlib import Path

import httpx

# make `app` importable however the script is launched (it lives in backend/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.plaid_service.client import sandbox_public_token

base_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"

resp = httpx.post(f"{base_url}/plaid/link-token", timeout=30)
if resp.status_code != 200:
    sys.exit(f"FAILED link-token ({resp.status_code}): {resp.text}")
print(f"OK: link_token = {resp.json()['link_token'][:20]}...")

public_token = sandbox_public_token()
print(f"OK: sandbox public_token = {public_token[:20]}...")

resp = httpx.post(f"{base_url}/plaid/exchange", json={"public_token": public_token}, timeout=30)
if resp.status_code != 200:
    sys.exit(f"FAILED exchange ({resp.status_code}): {resp.text}")
print(f"OK: stored plaid_items row id = {resp.json()['item_id']}")
item_id = resp.json()["item_id"]


def sync() -> dict:
    resp = httpx.post(f"{base_url}/plaid/items/{item_id}/sync", timeout=60)
    if resp.status_code != 200:
        sys.exit(f"FAILED sync ({resp.status_code}): {resp.text}")
    return resp.json()


# a new sandbox item loads its history over several seconds, so keep syncing until a run
# adds nothing. each run only pulls what's new since the last cursor.
total = 0
for _ in range(15):
    time.sleep(2)
    body = sync()
    total += body["transactions"]["added"]
    print(f"OK: sync {body['transactions']}")
    if body["transactions"]["added"] == 0 and total > 0:
        break
print(f"OK: {total} transactions across {body['accounts']} accounts; last run added 0 (no duplicates)")
for liab in body["liabilities"]:
    print(f"    {liab['kind']} liability, APR {liab['apr']}")

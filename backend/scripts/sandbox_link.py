"""Link a Sandbox bank end to end, without the frontend.

Gets a Sandbox public_token straight from Plaid, then exchanges it through our
running API, the same call the frontend makes after Link succeeds.

Run with the API up (uvicorn app.main:app), from any directory:
    python backend/scripts/sandbox_link.py [base_url]
"""
import sys
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

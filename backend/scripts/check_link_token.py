import os
import sys

import httpx
from dotenv import load_dotenv
from pathlib import Path


load_dotenv(Path(__file__).parent.parent.parent / ".env")

client_id = os.getenv("PLAID_CLIENT_ID")
secret = os.getenv("PLAID_SECRET")
env = os.getenv("PLAID_ENV", "sandbox")

if not client_id or not secret:
    sys.exit("Missing PLAID_CLIENT_ID or PLAID_SECRET in backend/.env")
resp = httpx.post(
    f"https://{env}.plaid.com/link/token/create",
    json={
        "client_id": client_id,
        "secret": secret,
        "client_name": "Plaid Agent",
        "language": "en",
        "country_codes": ["US"],
        "user": {"client_user_id": "test-user-1"},
        "products": ["transactions"],
    },
    timeout=30,
)
data = resp.json()

if resp.status_code == 200:
    print(f"OK: link_token = {data['link_token'][:20]}...")
    print(f"expiration = {data['expiration']}")
else:
    sys.exit(f"FAILED ({resp.status_code}): {data.get('error_code')} - {data.get('error_message')}")

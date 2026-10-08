import os
from pathlib import Path
from dotenv import load_dotenv

# single place that loads the repo-root .env; import settings from here
# so they're set no matter which module (app, alembic, script) runs first
load_dotenv(Path(__file__).parent.parent.parent / ".env")

PLAID_CLIENT_ID = os.getenv("PLAID_CLIENT_ID")
PLAID_SECRET = os.getenv("PLAID_SECRET")
PLAID_ENV = os.getenv("PLAID_ENV", "sandbox")

# public URL plaid POSTs webhooks to (e.g. an ngrok tunnel to /plaid/webhook); unset = no webhooks
PLAID_WEBHOOK_URL = os.getenv("PLAID_WEBHOOK_URL")

# encrypts plaid access tokens at rest; losing it means every item must be re-linked
FERNET_KEY = os.getenv("FERNET_KEY")

# relative sqlite path, so run the app and alembic from backend/
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./dev.db")

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

# local LLM (architecture section 3); agents/llm.py is the only reader
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
# 0 keeps eval runs repeatable; chat can pass its own temperature
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0"))

# every prompt the privacy gateway builds is appended here (git-ignored)
# `or`, not a getenv default: .env.example leaves it blank, and Path("") would be the cwd
PROMPT_AUDIT_PATH = Path(os.getenv("PROMPT_AUDIT_PATH") or Path(__file__).parent.parent / "logs" / "prompt_audit.jsonl")

# embedding model for the knowledge base (architecture section 8); agents/llm.py is the only reader
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
# knowledge base root: sources.json, sources/ (committed), sources/local/ and chroma/ (git-ignored)
KB_DIR = Path(os.getenv("KB_DIR") or Path(__file__).parent.parent.parent / "kb")

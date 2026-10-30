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
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")
# 0 keeps eval runs repeatable; chat can pass its own temperature
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE") or "0")

# every prompt the privacy gateway builds is appended here (git-ignored)
# `or`, not a getenv default: .env.example leaves it blank, and Path("") would be the cwd
PROMPT_AUDIT_PATH = Path(os.getenv("PROMPT_AUDIT_PATH") or Path(__file__).parent.parent / "logs" / "prompt_audit.jsonl")
# Keep the current log and one rotated backup, both owner-readable/writable only.
PROMPT_AUDIT_MAX_BYTES = int(os.getenv("PROMPT_AUDIT_MAX_BYTES") or str(8 * 1024 * 1024))

# embedding model for the knowledge base (architecture section 8); agents/llm.py is the only reader
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
# knowledge base root: sources.json, sources/ (committed), sources/local/ and chroma/ (git-ignored)
KB_DIR = Path(os.getenv("KB_DIR") or Path(__file__).parent.parent.parent / "kb")
# context window for the chat model; ollama's smaller default would silently cut long chats (search_kb adds ~2k tokens)
LLM_NUM_CTX = int(os.getenv("LLM_NUM_CTX") or "8192")
# langgraph checkpointer file: chat threads survive a backend restart (git-ignored by *.db)
CHAT_CHECKPOINT_PATH = Path(os.getenv("CHAT_CHECKPOINT_PATH") or Path(__file__).parent.parent / "data" / "checkpoints.db")
# cap on tokens per model reply: a repetition loop would otherwise run (and hold the user's chat lock) forever
LLM_NUM_PREDICT = int(os.getenv("LLM_NUM_PREDICT") or "1024")
# seconds the ollama client waits on a response; a cold 14b model on a busy machine can take minutes
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS") or "300")

# the api's single user until real auth. point it at a seeded user's email (e.g. overspend-one-time@eval.local)
# to run the ui as that user; only the default is created when missing
DEV_USER_EMAIL = os.getenv("DEV_USER_EMAIL", "dev@localhost")

# langgraph checkpointer for budget runs waiting on the user's approval (git-ignored by *.db)
BUDGET_CHECKPOINT_PATH = Path(os.getenv("BUDGET_CHECKPOINT_PATH") or Path(__file__).parent.parent / "data" / "budget_checkpoints.db")

import logging
from typing import Optional
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import ToolException
from langchain_ollama import ChatOllama, OllamaEmbeddings
from pydantic import ValidationError
from .. import config

logger = logging.getLogger(__name__)

# the only place the app creates an LLM or embedding client (architecture section 3), so swapping
# llama3.1 for mistral, a hosted model or another embedding model in benchmarks is a change to this file only


def get_chat_model(temperature: Optional[float] = None, model: Optional[str] = None,
                   num_ctx: Optional[int] = None) -> BaseChatModel:
    # config is read at call time so tests (and later, per-run eval settings) can change it
    return ChatOllama(
        model=model or config.OLLAMA_MODEL,
        base_url=config.OLLAMA_BASE_URL,
        temperature=config.LLM_TEMPERATURE if temperature is None else temperature,
        num_ctx=config.LLM_NUM_CTX if num_ctx is None else num_ctx,
        num_predict=config.LLM_NUM_PREDICT,
        client_kwargs={"timeout": config.LLM_TIMEOUT_SECONDS},
    )


# the knowledge base's embedding model (nomic-embed-text by default); rag/store.py adds its task prefixes
def get_embeddings(model: Optional[str] = None) -> Embeddings:
    return OllamaEmbeddings(model=model or config.OLLAMA_EMBED_MODEL, base_url=config.OLLAMA_BASE_URL)


# errors go back to the model as text so it can recover; small models do invent tool names.
# only messages meant for the model are forwarded: any other exception text can carry user
# data (sqlalchemy prints bound parameters) and this path bypasses the privacy gateway
def _run_tool(by_name: dict, tc: dict) -> str:
    name = tc["name"]
    t = by_name.get(name)
    if t is None:
        return f"error: unknown tool {name}"
    try:
        return str(t.invoke(tc["args"]))
    except ToolException as exc:
        return f"error: {exc}"
    except ValidationError:
        # Validation may happen inside a tool using DB values, not just on model arguments.
        return "error: tool validation failed; check the tool's argument schema"
    except Exception as exc:
        logger.warning("tool %s failed", name, exc_info=True)   # full detail stays in local logs
        return f"error: {name} failed ({type(exc).__name__})"

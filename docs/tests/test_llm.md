# Tests: `backend/tests/test_llm.py`

These tests cover `app/agents/llm.py`, the only place the app creates an LLM or embedding client. They check that `get_chat_model` and `get_embeddings` build their Ollama clients from config. `_run_tool`, the function that decides what tool-error text reaches the model, also lives in `llm.py`. Since TPC-15 it's tested through the chat graph, in [`test_chat_graph.md`](test_chat_graph.md). Run these from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_llm.py -v
```

---

## `test_get_chat_model_uses_config`

**What it does:** Sets `OLLAMA_MODEL`, `OLLAMA_BASE_URL` and `LLM_TEMPERATURE` in `config` and checks that the model `get_chat_model()` returns uses all three.

**Why it's there:** The whole point of the wrapper is that config decides which model runs, including the eval runs that swap in `mistral`. Settings that were read once at import time, or hard-coded, would make that swap quietly do nothing.

## `test_get_chat_model_overrides`

**What it does:** Calls `get_chat_model(temperature=0.7, model="mistral")` and checks that both arguments win over the config defaults.

**Why it's there:** The default temperature is 0 so evals are repeatable, but chat may want some variety, and benchmarks need to pick the model per run. Both paths need to work.

## `test_get_chat_model_passes_num_ctx`

**What it does:** Checks that `get_chat_model()` passes `num_ctx` from `LLM_NUM_CTX` (8192), and that a `num_ctx` argument overrides it.

**Why it's there:** Ollama's default context window is smaller than a chat that has called `search_kb` (four chunks are about 2k tokens). Ollama cuts an over-long conversation without any error, so the model would quietly lose the start of the chat, or the system prompt with it.

## `test_get_chat_model_caps_generation_and_waits`

**What it does:** Checks that `get_chat_model()` passes `num_predict` from `LLM_NUM_PREDICT` (1024) and gives the Ollama client a timeout from `LLM_TIMEOUT_SECONDS` (300).

**Why it's there:** Without a cap, a model stuck repeating itself at temperature 0 keeps generating. Without a timeout, a wedged Ollama never answers. Either way, the chat turn holds the user's one-reply lock forever, and every send says "Still answering…" until a restart.

## `test_get_embeddings_uses_config`

**What it does:** Sets `OLLAMA_EMBED_MODEL` and `OLLAMA_BASE_URL` with monkeypatch, then checks that `get_embeddings()` builds an Ollama embeddings client with exactly that model and server.

**Why it's there:** The knowledge base (TPC-14) embeds with whatever this returns. If it ignored config, an M5 embedding-model comparison would quietly keep using nomic-embed-text.


from app import config
from app.agents.llm import get_chat_model, get_embeddings

# the tool loop and _run_tool's error rules are tested through the chat graph (test_chat_graph.py)


def test_get_chat_model_uses_config(monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_MODEL", "m1")
    monkeypatch.setattr(config, "OLLAMA_BASE_URL", "http://h:1")
    monkeypatch.setattr(config, "LLM_TEMPERATURE", 0.0)

    m = get_chat_model()

    assert (m.model, m.base_url, m.temperature) == ("m1", "http://h:1", 0.0)


def test_get_chat_model_overrides():
    m = get_chat_model(temperature=0.7, model="mistral")

    assert (m.model, m.temperature) == ("mistral", 0.7)


# ollama's default context is smaller than a chat with four kb chunks; the default must be explicit
def test_get_chat_model_passes_num_ctx(monkeypatch):
    monkeypatch.setattr(config, "LLM_NUM_CTX", 8192)

    assert get_chat_model().num_ctx == 8192
    assert get_chat_model(num_ctx=4096).num_ctx == 4096


# a repetition loop or a wedged ollama would otherwise hold the user's chat lock indefinitely
def test_get_chat_model_caps_generation_and_waits(monkeypatch):
    monkeypatch.setattr(config, "LLM_NUM_PREDICT", 1024)
    monkeypatch.setattr(config, "LLM_TIMEOUT_SECONDS", 300.0)

    m = get_chat_model()

    assert m.num_predict == 1024
    assert m.client_kwargs["timeout"] == 300.0


def test_get_embeddings_uses_config(monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_EMBED_MODEL", "e1")
    monkeypatch.setattr(config, "OLLAMA_BASE_URL", "http://h:1")

    e = get_embeddings()

    assert (e.model, e.base_url) == ("e1", "http://h:1")

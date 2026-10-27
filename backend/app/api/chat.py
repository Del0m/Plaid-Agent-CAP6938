import json
import logging
import sqlite3
import threading
from functools import lru_cache
from typing import Annotated
import httpx
from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel, Field, StringConstraints
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool
from .. import config
from ..agents.chat_graph import build_chat_graph
from ..agents.tools import make_tools
from ..db.models import User
from ..db.session import SessionLocal
from ..privacy.gateway import PrivacyGateway
from ..rag.store import KnowledgeBase
from .deps import get_current_user

# the chat agent over http (architecture section 9). POST /chat streams server-sent events, which
# ride the next.js /api rewrite like any other response; websockets wouldn't get through it

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])

THREAD_ID = r"^[A-Za-z0-9-]{1,64}$"
MESSAGES = {
    "ollama": "The local model isn't reachable. Is Ollama running?",
    "empty": "The model returned an empty answer. Try rephrasing.",
    "other": "Something went wrong answering that. Try again.",
}

# hooks the tests replace: where db sessions, checkpoints and the kb come from
session_factory = SessionLocal
make_kb = KnowledgeBase


@lru_cache(maxsize=1)
def _sqlite_saver() -> SqliteSaver:
    config.CHAT_CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(str(config.CHAT_CHECKPOINT_PATH), check_same_thread=False))


def get_checkpointer() -> BaseCheckpointSaver:
    return _sqlite_saver()


# one reply in flight per user: each turn holds the local model for seconds, and a refresh loop
# or a second tab would otherwise stack runs. in-process, so it assumes one uvicorn worker
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _user_lock(user_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(user_id, threading.Lock())


class ChatRequest(BaseModel):
    thread_id: str = Field(pattern=THREAD_ID)
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


def format_event(event: str, data: dict) -> str:
    # json escapes newlines, so answer text can never end an event early
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# the user id prefix keeps one user from loading another's thread by guessing its id
def _config(user: User, thread_id: str) -> dict:
    return {"configurable": {"thread_id": f"{user.id}:{thread_id}"}}


# what the browser shows under an answer; the text stays server-side
def _sources(found: list[dict]) -> list[dict]:
    return [{"title": r["title"], "section": r["section"], "url": r["source_url"]} for r in found]


def _error_key(exc: Exception) -> str:
    return "ollama" if isinstance(exc, (httpx.ConnectError, ConnectionError)) else "other"


async def _events(user: User, req: ChatRequest, lock: threading.Lock):
    session, stream, tools_seen, streamed = None, None, [], False
    try:
        session = session_factory()
        cfg = _config(user, req.thread_id)

        def setup():
            checkpointer = get_checkpointer()
            state = build_chat_graph([], checkpointer=checkpointer).get_state(cfg).values
            gateway, kb = PrivacyGateway(session, user.id, pseudonym=state.get("pseudonym")), make_kb()
            graph = build_chat_graph(make_tools(gateway, kb), retrieve=kb.search, checkpointer=checkpointer)
            turn = {"messages": [HumanMessage(req.message)], "tool_rounds": 0, "pseudonym": gateway.pseudonym}
            return graph, graph.stream(turn, cfg, stream_mode=["messages", "updates"])

        graph, stream = await run_in_threadpool(setup)
        async for mode, payload in iterate_in_threadpool(stream):
            if mode == "messages":
                chunk, meta = payload
                if meta.get("langgraph_node") == "agent" and isinstance(chunk.content, str) and chunk.content:
                    streamed = True
                    yield format_event("token", {"text": chunk.content})
            elif "retrieve" in payload:
                if payload["retrieve"]["sources"]:
                    yield format_event("sources", {"sources": _sources(payload["retrieve"]["sources"])})
            elif "agent" in payload:
                for tc in payload["agent"]["messages"][-1].tool_calls:
                    tools_seen.append(tc["name"])
                    yield format_event("tool_call", {"id": tc["id"], "name": tc["name"], "args": tc["args"]})
            elif "tools" in payload:
                for m in payload["tools"]["messages"]:
                    yield format_event("tool_result", {"id": m.tool_call_id, "name": m.name,
                                                       "ok": not str(m.content).startswith("error:")})
        if not streamed:
            last = (await run_in_threadpool(graph.get_state, cfg)).values["messages"][-1]
            # a model that doesn't stream still answers; one with nothing to say gets told so
            if isinstance(last, AIMessage) and isinstance(last.content, str) and last.content:
                yield format_event("token", {"text": last.content})
            else:
                yield format_event("error", {"message": MESSAGES["empty"]})
                return
        yield format_event("done", {})
    except Exception as exc:
        logger.warning("chat turn failed", exc_info=True)   # full detail stays in local logs
        yield format_event("error", {"message": MESSAGES[_error_key(exc)]})
    finally:
        if stream is not None:
            stream.close()
        if session is not None:
            session.close()
        lock.release()
        logger.info("chat turn ended: tools=%s", tools_seen)   # names only, never message text


@router.post("/chat")
def post_chat(req: ChatRequest, user: User = Depends(get_current_user)):
    lock = _user_lock(user.id)
    if not lock.acquire(blocking=False):
        return JSONResponse({"detail": "A reply is already in progress."}, status_code=409)
    # an async body, so starlette's close on disconnect reaches the finally that frees the lock
    return StreamingResponse(_events(user, req, lock), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@router.get("/chat/{thread_id}")
def get_history(thread_id: str = Path(pattern=THREAD_ID), user: User = Depends(get_current_user)) -> list[dict]:
    values = build_chat_graph([], checkpointer=get_checkpointer()).get_state(_config(user, thread_id)).values
    by_turn = values.get("sources_by_turn", {})
    turns, current, by_id, sources = [], None, {}, []
    for m in values.get("messages", []):
        if isinstance(m, HumanMessage):
            current, sources = None, _sources(by_turn.get(m.id, []))
            turns.append({"role": "user", "text": m.content, "tools": []})
        elif isinstance(m, AIMessage):
            if current is None:
                current = {"role": "assistant", "text": "", "tools": [], "sources": sources}
                turns.append(current)
            for tc in m.tool_calls:
                by_id[tc["id"]] = {"name": tc["name"], "args": tc["args"], "ok": False}
                current["tools"].append(by_id[tc["id"]])
            # text written next to a tool call is not the answer (qwen does this); the answer has no calls
            if m.content and not m.tool_calls:
                current["text"] = m.content
        elif isinstance(m, ToolMessage) and m.tool_call_id in by_id:
            by_id[m.tool_call_id]["ok"] = not str(m.content).startswith("error:")
    return turns

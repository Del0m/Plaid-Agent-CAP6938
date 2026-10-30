"use client";

import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import {
  ApiError,
  errorMessage,
  getChatHistory,
  streamChat,
  type ChatEvent,
  type ChatSource,
  type ChatTool,
} from "@/lib/api";

// the m2 chat: streamed answers from POST /api/chat, with the tools each answer used and the
// guidance the app retrieved for it. one conversation per tab, kept across a refresh

export const THREAD_KEY = "chatThreadId";
const BUSY = "Still answering the last message.";
const DROPPED = "Lost the connection to the server. Try again.";

// randomUUID only exists in secure contexts (https, localhost); the phone demo is plain http on the lan
function randomId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
}

type Tool = ChatTool & { id?: string; pending?: boolean };
type Turn = { role: "user" | "assistant"; text: string; tools: Tool[]; sources: ChatSource[]; error?: boolean };

// storage can throw (private mode, blocked site data); chat still works for the life of the page
function newThreadId(): string {
  const id = randomId();
  try {
    sessionStorage.setItem(THREAD_KEY, id);
  } catch {
    // keep the id in memory only
  }
  return id;
}

function storedThreadId(): string {
  try {
    const id = sessionStorage.getItem(THREAD_KEY);
    if (id) return id;
  } catch {
    // fall through to a fresh id
  }
  return newThreadId();
}

// folds one server event into the assistant turn being streamed (always the last turn)
function applyEvent(turns: Turn[], e: ChatEvent): Turn[] {
  const last = { ...turns[turns.length - 1] };
  switch (e.event) {
    case "sources":
      last.sources = e.data.sources;
      break;
    case "tool_call":
      // text the model wrote next to a tool call isn't the answer (qwen does this); the answer follows
      last.text = "";
      last.tools = [...last.tools, { id: e.data.id, name: e.data.name, args: e.data.args, ok: false, pending: true }];
      break;
    case "tool_result":
      last.tools = last.tools.map((t) => (t.id === e.data.id ? { ...t, ok: e.data.ok, pending: false } : t));
      break;
    case "token":
      last.text += e.data.text;
      break;
    case "error":
      last.text = e.data.message;
      last.error = true;
      break;
    case "done":
      break;
  }
  return [...turns.slice(0, -1), last];
}

function ToolChips({ tools }: { tools: Tool[] }) {
  if (tools.length === 0) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      {tools.map((t, i) => (
        <span
          key={t.id ?? i}
          title={JSON.stringify(t.args)}
          className="rounded-full border border-black/10 px-2 py-0.5 font-mono text-xs text-neutral-600 dark:border-white/15 dark:text-neutral-300"
        >
          {`${t.name} ${t.pending ? "…" : t.ok ? "✓" : "✗"}`}
        </span>
      ))}
    </div>
  );
}

function Sources({ sources }: { sources: ChatSource[] }) {
  if (sources.length === 0) return null;
  return (
    <div className="mt-3 text-xs">
      <p className="font-medium text-neutral-500">Sources</p>
      <ul className="mt-1 space-y-0.5">
        {sources.map((s, i) => (
          <li key={i}>
            <a href={s.url} target="_blank" rel="noopener noreferrer" className="underline underline-offset-2">
              {s.title}
            </a>
            <span className="text-neutral-500"> · {s.section}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function ChatPanel() {
  // never rendered, so a ref: picking it doesn't need a re-render
  const threadId = useRef<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [streaming, setStreaming] = useState(false);

  // storage only exists in the browser, so the thread is picked after mount
  useEffect(() => {
    const id = storedThreadId();
    threadId.current = id;
    let cancelled = false;
    getChatHistory(id)
      .then((history) => {
        // New chat may have picked another thread while this was loading
        if (cancelled || threadId.current !== id) return;
        // a message sent before history arrived wins; history only fills an empty conversation
        setTurns((current) => (current.length > 0 ? current : history.map((t) => ({ ...t, sources: t.sources ?? [] }))));
      })
      .catch(() => {
        // no history to show; the conversation simply starts empty
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function send(e?: FormEvent) {
    e?.preventDefault();
    const text = draft.trim();
    const thread = threadId.current;
    if (!text || streaming || !thread) return;
    setDraft("");
    setStreaming(true);
    setTurns((t) => [
      ...t,
      { role: "user", text, tools: [], sources: [] },
      { role: "assistant", text: "", tools: [], sources: [] },
    ]);
    try {
      await streamChat(thread, text, (ev) => setTurns((t) => applyEvent(t, ev)));
    } catch (err) {
      // anything that isn't an http status is the stream itself breaking ("terminated", "Load failed")
      const message = err instanceof ApiError ? (err.status === 409 ? BUSY : errorMessage(err)) : DROPPED;
      setTurns((t) => applyEvent(t, { event: "error", data: { message } }));
    } finally {
      setStreaming(false);
    }
  }

  // enter sends, shift+enter adds a line
  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  }

  function newChat() {
    threadId.current = newThreadId();
    setTurns([]);
  }

  return (
    <section className="flex flex-col gap-4">
      <div className="flex justify-end">
        <button
          type="button"
          onClick={newChat}
          disabled={streaming}
          className="rounded-md border border-black/10 px-3 py-1.5 text-sm disabled:opacity-50 dark:border-white/15"
        >
          New chat
        </button>
      </div>

      <ol className="flex flex-col gap-3">
        {turns.length === 0 && (
          <li className="text-sm text-neutral-500">Ask about your accounts, debts or spending.</li>
        )}
        {turns.map((t, i) => (
          <li
            key={i}
            className={
              t.role === "user"
                ? "self-end max-w-[85%] rounded-lg bg-black/5 px-3 py-2 dark:bg-white/10"
                : "max-w-[85%] rounded-lg border border-black/10 px-3 py-2 dark:border-white/15"
            }
          >
            <p className={`whitespace-pre-wrap ${t.error ? "text-red-600 dark:text-red-400" : ""}`}>
              {t.text || (t.role === "assistant" && streaming && i === turns.length - 1 ? "…" : t.text)}
            </p>
            {t.role === "assistant" && <ToolChips tools={t.tools} />}
            {t.role === "assistant" && <Sources sources={t.sources} />}
          </li>
        ))}
      </ol>

      <form onSubmit={send} className="flex gap-2">
        <textarea
          aria-label="Message"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={streaming}
          rows={2}
          maxLength={4000}
          placeholder="Which debt should I pay first?"
          className="flex-1 resize-none rounded-md border border-black/10 bg-transparent px-3 py-2 disabled:opacity-50 dark:border-white/15"
        />
        <button
          type="submit"
          disabled={streaming || !draft.trim()}
          className="rounded-md border border-black/10 px-4 py-2 disabled:opacity-50 dark:border-white/15"
        >
          Send
        </button>
      </form>
    </section>
  );
}

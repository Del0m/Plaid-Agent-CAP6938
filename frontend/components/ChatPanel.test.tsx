import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import ChatPanel, { THREAD_KEY } from "@/components/ChatPanel";
import { ApiError, getChatHistory, streamChat, type ChatEvent, type ChatTurn } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  streamChat: vi.fn(),
  getChatHistory: vi.fn(),
}));

const streamMock = vi.mocked(streamChat);
const historyMock = vi.mocked(getChatHistory);

// a reply the test drives by hand: push events one at a time, then finish
function heldReply() {
  let onEvent: (e: ChatEvent) => void = () => {};
  let finish: () => void = () => {};
  streamMock.mockImplementation((_thread, _message, cb) => {
    onEvent = cb;
    return new Promise<void>((resolve) => (finish = resolve));
  });
  return {
    push: (e: ChatEvent) => act(() => onEvent(e)),
    finish: () => act(async () => finish()),
  };
}

function replyWith(events: ChatEvent[]) {
  streamMock.mockImplementation(async (_thread, _message, cb) => events.forEach(cb));
}

async function send(text = "Which debt should I pay first?") {
  fireEvent.change(screen.getByRole("textbox", { name: /message/i }), { target: { value: text } });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Send" })));
}

beforeEach(() => {
  sessionStorage.clear();
  historyMock.mockResolvedValue([]);
  vi.spyOn(crypto, "randomUUID").mockReturnValue("00000000-0000-4000-8000-000000000001");
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  streamMock.mockReset();
  historyMock.mockReset();
});

describe("ChatPanel", () => {
  test("renders tokens as they arrive", async () => {
    render(<ChatPanel />);
    const reply = heldReply();
    await send();

    await reply.push({ event: "token", data: { text: "Pay " } });
    expect(screen.getByText("Pay")).toBeDefined();
    await reply.push({ event: "token", data: { text: "the card." } });
    expect(screen.getByText("Pay the card.")).toBeDefined();
    await reply.finish();
  });

  test("shows tool chips with ✓ and ✗", async () => {
    replyWith([
      { event: "tool_call", data: { id: "c1", name: "get_debts", args: {} } },
      { event: "tool_result", data: { id: "c1", name: "get_debts", ok: true } },
      { event: "tool_call", data: { id: "c2", name: "search_kb", args: { query: "debt" } } },
      { event: "tool_result", data: { id: "c2", name: "search_kb", ok: false } },
      { event: "token", data: { text: "Pay the card." } },
      { event: "done", data: {} },
    ]);
    render(<ChatPanel />);
    await send();

    expect(await screen.findByText("get_debts ✓")).toBeDefined();
    expect(screen.getByText("search_kb ✗")).toBeDefined();
  });

  test("lists sources as links", async () => {
    replyWith([
      { event: "sources", data: { sources: [{ title: "How to reduce your debt", section: "Snowball method", url: "https://www.consumerfinance.gov/x/" }] } },
      { event: "token", data: { text: "Pay the card." } },
      { event: "done", data: {} },
    ]);
    render(<ChatPanel />);
    await send();

    const link = await screen.findByRole("link", { name: /How to reduce your debt/ });
    expect(link.getAttribute("href")).toBe("https://www.consumerfinance.gov/x/");
  });

  // qwen sometimes writes text next to a tool call; only the text after the last call is the answer
  test("drops text written before a tool call", async () => {
    replyWith([
      { event: "token", data: { text: "CallCheck_debts()" } },
      { event: "tool_call", data: { id: "c1", name: "get_debts", args: {} } },
      { event: "tool_result", data: { id: "c1", name: "get_debts", ok: true } },
      { event: "token", data: { text: "Pay the card." } },
      { event: "done", data: {} },
    ]);
    render(<ChatPanel />);
    await send();

    expect(await screen.findByText("Pay the card.")).toBeDefined();
    expect(screen.queryByText(/CallCheck/)).toBeNull();
  });

  test("disables input while streaming and re-enables after done", async () => {
    render(<ChatPanel />);
    const reply = heldReply();
    await send();

    expect((screen.getByRole("textbox", { name: /message/i }) as HTMLTextAreaElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Send" }) as HTMLButtonElement).disabled).toBe(true);
    await reply.push({ event: "done", data: {} });
    await reply.finish();
    expect((screen.getByRole("textbox", { name: /message/i }) as HTMLTextAreaElement).disabled).toBe(false);
  });

  test("shows the error event message", async () => {
    replyWith([{ event: "error", data: { message: "The local model isn't reachable. Is Ollama running?" } }]);
    render(<ChatPanel />);
    await send();

    expect(await screen.findByText("The local model isn't reachable. Is Ollama running?")).toBeDefined();
  });

  test("shows the busy note on 409", async () => {
    streamMock.mockRejectedValue(new ApiError(409, { detail: "A reply is already in progress." }));
    render(<ChatPanel />);
    await send();

    expect(await screen.findByText("Still answering the last message.")).toBeDefined();
  });

  test("loads history for the stored thread id", async () => {
    sessionStorage.setItem(THREAD_KEY, "abc");
    const turns: ChatTurn[] = [
      { role: "user", text: "Earlier question", tools: [] },
      { role: "assistant", text: "Earlier answer", tools: [{ name: "get_debts", args: {}, ok: true }], sources: [] },
    ];
    historyMock.mockResolvedValue(turns);

    render(<ChatPanel />);

    expect(await screen.findByText("Earlier answer")).toBeDefined();
    expect(screen.getByText("Earlier question")).toBeDefined();
    expect(historyMock).toHaveBeenCalledWith("abc");
  });

  test("new chat clears messages and stores a new id", async () => {
    sessionStorage.setItem(THREAD_KEY, "abc");
    historyMock.mockResolvedValue([{ role: "user", text: "Earlier question", tools: [] }]);
    render(<ChatPanel />);
    await screen.findByText("Earlier question");

    fireEvent.click(screen.getByRole("button", { name: "New chat" }));

    expect(screen.queryByText("Earlier question")).toBeNull();
    expect(sessionStorage.getItem(THREAD_KEY)).toBe("00000000-0000-4000-8000-000000000001");
  });

  // history arrives after mount; a message sent before it lands must not be wiped out
  test("a message sent before history loads is kept", async () => {
    let resolveHistory: (t: ChatTurn[]) => void = () => {};
    historyMock.mockReturnValue(new Promise((r) => (resolveHistory = r)));
    replyWith([{ event: "token", data: { text: "Fresh answer." } }, { event: "done", data: {} }]);
    render(<ChatPanel />);
    await send("New question");

    await act(async () => resolveHistory([{ role: "user", text: "Old question", tools: [] }]));

    expect(screen.getByText("Fresh answer.")).toBeDefined();
    expect(screen.queryByText("Old question")).toBeNull();
  });

  // crypto.randomUUID only exists on https and localhost; the phone demo opens http://<lan-ip>:3000
  test("works when crypto.randomUUID is unavailable", async () => {
    vi.restoreAllMocks();
    const original = Object.getOwnPropertyDescriptor(crypto, "randomUUID");
    Object.defineProperty(crypto, "randomUUID", { value: undefined, configurable: true });
    try {
      replyWith([{ event: "token", data: { text: "Works on the phone." } }, { event: "done", data: {} }]);
      render(<ChatPanel />);
      await send();

      expect(await screen.findByText("Works on the phone.")).toBeDefined();
      const threadId = streamMock.mock.calls[0][0];
      expect(threadId).toMatch(/^[A-Za-z0-9-]{1,64}$/);
    } finally {
      // jsdom defines randomUUID on Crypto.prototype, so usually there's no own property to put back
      if (original) Object.defineProperty(crypto, "randomUUID", original);
      else delete (crypto as { randomUUID?: unknown }).randomUUID;
    }
  });

  // the browser reports a dropped stream as a bare "terminated" or "Load failed"
  test("shows a plain note when the connection drops", async () => {
    streamMock.mockRejectedValue(new TypeError("terminated"));
    render(<ChatPanel />);
    await send();

    expect(await screen.findByText("Lost the connection to the server. Try again.")).toBeDefined();
    expect(screen.queryByText("terminated")).toBeNull();
  });

  // safari private mode and blocked site data make storage throw; chat must still work
  test("works when sessionStorage throws", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    replyWith([{ event: "token", data: { text: "Still works." } }, { event: "done", data: {} }]);
    render(<ChatPanel />);
    await send();

    expect(await screen.findByText("Still works.")).toBeDefined();
    await waitFor(() => expect(streamMock).toHaveBeenCalledWith("00000000-0000-4000-8000-000000000001", expect.any(String), expect.any(Function)));
  });
});

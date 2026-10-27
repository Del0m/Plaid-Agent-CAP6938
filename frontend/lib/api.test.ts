import { afterEach, describe, expect, test, vi } from "vitest";
import { ApiError, getChatHistory, streamChat, type ChatEvent } from "@/lib/api";

// a fetch response whose body arrives in the given byte chunks
function streamed(chunks: Uint8Array[], status = 200) {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      chunks.forEach((c) => controller.enqueue(c));
      controller.close();
    },
  });
  return new Response(body, { status, headers: { "Content-Type": "text/event-stream" } });
}

const bytes = (s: string) => new TextEncoder().encode(s);

async function collect(response: Response) {
  const fetchMock = vi.fn().mockResolvedValue(response);
  vi.stubGlobal("fetch", fetchMock);
  const events: ChatEvent[] = [];
  await streamChat("t1", "hi", (e) => events.push(e));
  return { events, fetchMock };
}

afterEach(() => vi.unstubAllGlobals());

describe("streamChat", () => {
  test("delivers events in order across chunk boundaries", async () => {
    const wire = 'event: sources\ndata: {"sources":[]}\n\nevent: token\ndata: {"text":"Pay "}\n\nevent: token\ndata: {"text":"it"}\n\nevent: done\ndata: {}\n\n';
    const { events } = await collect(streamed([bytes(wire.slice(0, 30)), bytes(wire.slice(30, 75)), bytes(wire.slice(75))]));
    expect(events.map((e) => e.event)).toEqual(["sources", "token", "token", "done"]);
    expect(events.filter((e) => e.event === "token").map((e) => (e.data as { text: string }).text).join("")).toBe("Pay it");
  });

  test("decodes a character split across chunks", async () => {
    const wire = bytes('event: token\ndata: {"text":"café"}\n\n');
    const at = wire.indexOf(0xc3) + 1; // between the two bytes of é
    const { events } = await collect(streamed([wire.slice(0, at), wire.slice(at)]));
    expect(events).toEqual([{ event: "token", data: { text: "café" } }]);
  });

  test("throws ApiError 409 when busy", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "A reply is already in progress." }), { status: 409 })));
    await expect(streamChat("t1", "hi", () => {})).rejects.toMatchObject({ status: 409 });
  });

  test("throws ApiError on 500", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("boom", { status: 500 })));
    await expect(streamChat("t1", "hi", () => {})).rejects.toBeInstanceOf(ApiError);
  });

  test("posts thread_id and message as JSON to /api/chat", async () => {
    const { fetchMock } = await collect(streamed([bytes("event: done\ndata: {}\n\n")]));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/chat");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ thread_id: "t1", message: "hi" });
  });
});

describe("getChatHistory", () => {
  test("returns the turns", async () => {
    const turns = [
      { role: "user", text: "q", tools: [] },
      { role: "assistant", text: "a", tools: [{ name: "get_debts", args: {}, ok: true }], sources: [] },
    ];
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(turns), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    expect(await getChatHistory("t1")).toEqual(turns);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/chat/t1");
  });
});

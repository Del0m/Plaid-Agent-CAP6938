import { afterEach, describe, expect, test, vi } from "vitest";
import {
  acceptAlert, advanceSim, ApiError, dismissAlert, errorMessage, getAlerts, getBudget, getChatHistory, getRun, getSim,
  getSimJob, runBudget, streamChat, type ChatEvent,
} from "@/lib/api";
import { daysToMonthEnd } from "@/lib/format";

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

describe("budget and alerts calls", () => {
  function stub(body: unknown, status = 200) {
    // a fresh response per call: a body can only be read once
    const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify(body), { status }));
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  test("each call hits its route with the right method and body", async () => {
    const f = stub({});
    await getBudget();
    await runBudget("2026-10-20");
    await runBudget();
    await getRun(7);
    await getAlerts("pending_user");
    await getAlerts();
    await acceptAlert(3);
    await dismissAlert(4);
    const calls = f.mock.calls.map(([url, init]) => [url, init?.method ?? "GET", init?.body ?? null]);
    expect(calls).toEqual([
      ["/api/budget", "GET", null],
      ["/api/budget/run", "POST", '{"date":"2026-10-20"}'],
      ["/api/budget/run", "POST", "{}"],
      ["/api/budget/runs/7", "GET", null],
      ["/api/alerts?status=pending_user", "GET", null],
      ["/api/alerts?status=all", "GET", null],
      ["/api/alerts/3/accept", "POST", null],
      ["/api/alerts/4/dismiss", "POST", null],
    ]);
  });

  test("a 409 run reads as its detail", async () => {
    stub({ detail: "A budget run is already in progress." }, 409);
    const err = await runBudget().catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(errorMessage(err)).toBe("A budget run is already in progress.");
  });
});

describe("simulation calls", () => {
  test("hit the admin routes", async () => {
    const f = vi.fn().mockImplementation(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", f);
    await getSim();
    await advanceSim(7);
    await getSimJob(5);
    expect(f.mock.calls.map(([url, init]) => [url, init?.method ?? "GET", init?.body ?? null])).toEqual([
      ["/api/admin/sandbox", "GET", null],
      ["/api/admin/sandbox/advance", "POST", '{"days":7}'],
      ["/api/admin/sandbox/advance/5", "GET", null],
    ]);
  });

  test("days to month end", () => {
    expect(daysToMonthEnd("2026-10-03")).toBe(28);
    expect(daysToMonthEnd("2026-10-31")).toBe(1);
    expect(daysToMonthEnd("2028-02-28")).toBe(1);
  });
});

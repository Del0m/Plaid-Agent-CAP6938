import { describe, expect, test } from "vitest";
import { parseSSE } from "@/lib/sse";

describe("parseSSE", () => {
  test("parses one event", () => {
    expect(parseSSE('event: token\ndata: {"text":"hi"}\n\n')).toEqual({
      events: [{ event: "token", data: { text: "hi" } }],
      rest: "",
    });
  });

  test("parses several events in one buffer", () => {
    const { events, rest } = parseSSE('event: token\ndata: {"text":"a"}\n\nevent: done\ndata: {}\n\n');
    expect(events).toEqual([
      { event: "token", data: { text: "a" } },
      { event: "done", data: {} },
    ]);
    expect(rest).toBe("");
  });

  // a network read can stop mid-event; the tail waits for the next read
  test("keeps a partial event in rest", () => {
    const partial = 'event: token\ndata: {"te';
    expect(parseSSE(partial)).toEqual({ events: [], rest: partial });
    const done = 'event: token\ndata: {"text":"a"}\n\nevent: do';
    expect(parseSSE(done)).toEqual({ events: [{ event: "token", data: { text: "a" } }], rest: "event: do" });
  });

  test("ignores comments and unknown fields", () => {
    expect(parseSSE(': keep-alive\nid: 7\nevent: done\ndata: {}\n\n').events).toEqual([{ event: "done", data: {} }]);
  });

  test("keeps bad JSON as a string", () => {
    expect(parseSSE("event: token\ndata: not json\n\n").events).toEqual([{ event: "token", data: "not json" }]);
  });
});

// server-sent events from POST /api/chat. EventSource can't send a POST body, so the client
// reads the response stream itself and feeds the text through this parser

export type SSEEvent = { event: string; data: unknown };

// splits complete events (each ends with a blank line) off the front of the buffer; a partial
// event at the end comes back in rest, to be prefixed to the next network read
export function parseSSE(buffer: string): { events: SSEEvent[]; rest: string } {
  const blocks = buffer.split("\n\n");
  const rest = blocks.pop() ?? "";
  const events: SSEEvent[] = [];
  for (const block of blocks) {
    let event = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      // ":" comments and other fields (id, retry) aren't used by the chat stream
    }
    if (data.length === 0) continue;
    const raw = data.join("\n");
    let parsed: unknown = raw;
    try {
      parsed = JSON.parse(raw);
    } catch {
      // not json; keep the raw text
    }
    events.push({ event, data: parsed });
  }
  return { events, rest };
}

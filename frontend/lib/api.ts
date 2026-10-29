import { parseSSE } from "@/lib/sse";

// typed calls to the fastapi backend through the /api proxy (see next.config.ts).
// field names mirror the backend's pydantic models exactly.

export type Account = {
  id: number;
  name: string;
  mask: string | null;
  type: string;
  subtype: string | null;
  current_balance_cents: number | null;
  available_balance_cents: number | null;
  institution_name: string | null;
};

export type Debt = {
  account_id: number;
  name: string;
  mask: string | null;
  kind: string;
  apr: number | null;
  balance_cents: number | null;
  min_payment_cents: number | null;
  next_due_date: string | null;
};

export type Dashboard = {
  net_worth_cents: number;
  assets_cents: number;
  debts_cents: number;
  accounts_missing_balance: number;
  accounts: Account[];
  debts: Debt[];
  // linked items whose first sync never finished; each gets a Retry sync button
  unsynced_items: { id: number; institution_name: string | null }[];
};

export type ExchangeResult = { item_id: number; synced: boolean };

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown) {
    super(`request failed (${status})`);
    this.status = status;
    this.detail = detail;
  }
}

// one readable line for any failure: fastapi's {detail}, a plaid error code, or a network error
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    const detail = (err.detail as { detail?: unknown } | null)?.detail;
    if (typeof detail === "string") return detail;
    const code = (detail as { plaid_error_code?: string } | undefined)?.plaid_error_code;
    if (code) return `Plaid error ${code}`;
    return `Request failed (${err.status})`;
  }
  return err instanceof Error ? err.message : String(err);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  // read as text first: a proxy error page is html, and res.json() would throw a SyntaxError
  const text = await res.text();
  let body: unknown = text;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    // not json; keep the raw text as the detail
  }
  if (!res.ok) throw new ApiError(res.status, body);
  return body as T;
}

export function getDashboard(): Promise<Dashboard> {
  return request<Dashboard>("/dashboard");
}

export async function createLinkToken(): Promise<string> {
  const { link_token } = await request<{ link_token: string }>("/plaid/link-token", { method: "POST" });
  return link_token;
}

export function exchangePublicToken(publicToken: string, institutionName?: string | null): Promise<ExchangeResult> {
  return request<ExchangeResult>("/plaid/exchange", {
    method: "POST",
    body: JSON.stringify({ public_token: publicToken, institution_name: institutionName ?? null }),
  });
}

export async function syncItem(itemId: number): Promise<void> {
  await request(`/plaid/items/${itemId}/sync`, { method: "POST" });
}

// the budget agent (tpc-20): live pace for the dashboard, runs started from the alerts page, and the
// alerts the user accepts or dismisses

export type BudgetLine = {
  category: string;
  kind: "fixed" | "flexible";
  limit_cents: number;
  spent_cents: number;
  pace: number | null;
  flagged: boolean;
  reason: "pace" | "over_limit" | null;
};

export type Budget = {
  period: { start: string; end: string } | null; // null: no budget covers today
  ended_period: { start: string; end: string } | null; // the latest budget, when today is past it
  days_elapsed: number;
  days_in_period: number;
  lines: BudgetLine[];
  unbudgeted: { category: string; spent_cents: number }[];
};

export type Alert = {
  id: number;
  kind: string;
  category: string | null;
  severity: string;
  cause: string | null;
  action: string | null;
  message: string;
  proposed_limit_cents: number | null; // only for adjust_next_budget
  status: "pending_user" | "accepted" | "dismissed";
  created_at: string;
  period_start: string | null;
  awaiting_approval: boolean; // a paused budget run is waiting on this decision
};

export type RunStatus = {
  run_id: number;
  status: "running" | "ok" | "no_budget" | "period_ended" | "awaiting_user" | "error";
  alert_ids: number[];
  waiting_for: number[];
  error: string | null;
};

export function getBudget(): Promise<Budget> {
  return request<Budget>("/budget");
}

// starts a run in the background (it can outlast the proxy's 30 s); poll getRun with the id
export function runBudget(date?: string): Promise<{ run_id: number | null }> {
  return request("/budget/run", { method: "POST", body: JSON.stringify(date ? { date } : {}) });
}

export function getRun(runId: number): Promise<RunStatus> {
  return request<RunStatus>(`/budget/runs/${runId}`);
}

export function getAlerts(status: "pending_user" | "all" = "all"): Promise<Alert[]> {
  return request<Alert[]>(`/alerts?status=${status}`);
}

export function acceptAlert(id: number): Promise<Alert> {
  return request<Alert>(`/alerts/${id}/accept`, { method: "POST" });
}

export function dismissAlert(id: number): Promise<Alert> {
  return request<Alert>(`/alerts/${id}/dismiss`, { method: "POST" });
}

export type ChatSource = { title: string; section: string; url: string };
export type ChatTool = { name: string; args: Record<string, unknown>; ok: boolean };

export type ChatEvent =
  | { event: "sources"; data: { sources: ChatSource[] } }
  | { event: "tool_call"; data: { id: string; name: string; args: Record<string, unknown> } }
  | { event: "tool_result"; data: { id: string; name: string; ok: boolean } }
  | { event: "token"; data: { text: string } }
  | { event: "done"; data: Record<string, never> }
  | { event: "error"; data: { message: string } };

export type ChatTurn = {
  role: "user" | "assistant";
  text: string;
  tools: ChatTool[];
  sources?: ChatSource[]; // assistant turns only
};

// one chat turn: posts the message, then calls onEvent for each server-sent event as it arrives.
// a 409 means a reply is already streaming for this user
export async function streamChat(
  threadId: string,
  message: string,
  onEvent: (e: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ thread_id: threadId, message }),
    signal,
  });
  if (!res.ok || !res.body) {
    const text = await res.text();
    let body: unknown = text;
    try {
      body = text ? JSON.parse(text) : null;
    } catch {
      // not json; keep the raw text as the detail
    }
    throw new ApiError(res.status, body);
  }
  const reader = res.body.getReader();
  // stream: true keeps a multi-byte character that's split across reads from turning into "�"
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    const parsed = parseSSE(buffer + decoder.decode(value, { stream: true }));
    buffer = parsed.rest;
    parsed.events.forEach((e) => onEvent(e as ChatEvent));
  }
}

export function getChatHistory(threadId: string): Promise<ChatTurn[]> {
  return request<ChatTurn[]>(`/chat/${threadId}`);
}

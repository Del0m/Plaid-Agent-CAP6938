import type { Alert } from "@/lib/api";
import { categoryLabel, formatCents } from "@/lib/format";

const CAUSES: Record<string, string> = { one_time: "One-time", recurring: "Recurring", seasonal: "Seasonal" };
const OUTCOMES: Record<string, string> = { accepted: "Accepted", dismissed: "Dismissed" };

// one alert from the budget agent. pending alerts get Accept / Dismiss; a budget proposal shows the limit the
// user would be accepting, and says so when a paused run is waiting on the answer
export default function AlertCard({ alert, onAccept, onDismiss, busy }: {
  alert: Alert;
  onAccept: (id: number) => void;
  onDismiss: (id: number) => void;
  busy: boolean;
}) {
  const pending = alert.status === "pending_user";
  const adjust = alert.action === "adjust_next_budget";
  return (
    <article className={`space-y-2 rounded-lg border border-black/10 p-4 dark:border-white/15 ${pending ? "" : "opacity-60"}`}>
      <header className="flex flex-wrap items-center gap-2">
        <h3 className="font-semibold">{alert.category ? categoryLabel(alert.category) : "Budget"}</h3>
        <span className={`rounded px-1.5 text-xs ${alert.severity === "high" ? "bg-red-600 text-white" : "bg-amber-500 text-black"}`}>
          {alert.severity === "high" ? "Over limit" : "Ahead of pace"}
        </span>
        {alert.cause && <span className="text-xs text-neutral-500">{CAUSES[alert.cause] ?? alert.cause}</span>}
      </header>
      <p>{alert.message}</p>
      {adjust && alert.proposed_limit_cents !== null && (
        <p className="text-sm">Proposed limit next month: {formatCents(alert.proposed_limit_cents)}</p>
      )}
      {pending && alert.awaiting_approval && (
        <p className="text-sm text-neutral-500">The agent is waiting for your decision.</p>
      )}
      {pending ? (
        <div className="flex gap-2">
          <button type="button" disabled={busy} onClick={() => onAccept(alert.id)}
            className="rounded bg-black px-3 py-1 text-sm text-white disabled:opacity-50 dark:bg-white dark:text-black">
            Accept
          </button>
          <button type="button" disabled={busy} onClick={() => onDismiss(alert.id)}
            className="rounded border border-black/20 px-3 py-1 text-sm disabled:opacity-50 dark:border-white/30">
            Dismiss
          </button>
        </div>
      ) : (
        <p className="text-sm font-medium">{OUTCOMES[alert.status] ?? alert.status}</p>
      )}
    </article>
  );
}

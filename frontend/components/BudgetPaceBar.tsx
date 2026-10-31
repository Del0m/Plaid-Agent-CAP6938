import type { BudgetLine } from "@/lib/api";
import { categoryLabel, formatCents } from "@/lib/format";

// one budget line: how much of the limit is spent, against how much of the month has passed (the marker).
// amber when spending is ahead of pace, red once it's over the limit
export default function BudgetPaceBar({ line, daysElapsed, daysInPeriod }: {
  line: BudgetLine;
  daysElapsed: number;
  daysInPeriod: number;
}) {
  const share = line.limit_cents > 0 ? Math.min(line.spent_cents / line.limit_cents, 1) : 0;
  const percent = Math.round(share * 100);
  const today = (daysElapsed / daysInPeriod) * 100;
  const color = line.reason === "over_limit" ? "bg-red-600" : line.reason === "pace" ? "bg-amber-500" : "bg-neutral-500";
  const label = categoryLabel(line.category);
  return (
    <div className="space-y-1">
      <div className="flex justify-between gap-2 text-sm">
        <span>
          {label}
          {line.kind === "fixed" && <span className="text-neutral-500"> (fixed)</span>}
        </span>
        <span className="tabular-nums">
          {formatCents(line.spent_cents)} of {formatCents(line.limit_cents)}
          {line.pace !== null && <span className="text-neutral-500"> · pace {line.pace.toFixed(2)}</span>}
        </span>
      </div>
      <div
        role="meter"
        aria-label={`${label} spent`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
        className="relative h-2 rounded bg-black/10 dark:bg-white/15"
      >
        <div data-testid="fill" className={`h-2 rounded ${color}`} style={{ width: `${percent}%` }} />
        <div data-testid="today" className="absolute -top-1 h-4 w-px bg-black/60 dark:bg-white/70" style={{ left: `${today}%` }} />
      </div>
    </div>
  );
}

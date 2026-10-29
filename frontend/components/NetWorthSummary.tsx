import type { Dashboard } from "@/lib/api";
import { formatCents } from "@/lib/format";

export default function NetWorthSummary({ data }: { data: Dashboard }) {
  const tiles = [
    { label: "Net worth", cents: data.net_worth_cents },
    { label: "Assets", cents: data.assets_cents },
    { label: "Debts", cents: data.debts_cents },
  ];
  const missing = data.accounts_missing_balance;

  return (
    <section className="space-y-2">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        {tiles.map((t) => (
          <div key={t.label} className="rounded-lg border border-black/10 p-4 dark:border-white/15">
            <p className="text-sm text-neutral-500">{t.label}</p>
            <p className="mt-1 text-2xl font-semibold tabular-nums">{formatCents(t.cents)}</p>
          </div>
        ))}
      </div>
      {missing > 0 && (
        <p className="text-sm text-neutral-500">
          {missing} account{missing === 1 ? "" : "s"} had no balance and {missing === 1 ? "isn't" : "aren't"} included.
        </p>
      )}
    </section>
  );
}

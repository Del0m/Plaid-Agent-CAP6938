import type { Debt } from "@/lib/api";
import { accountLabel, formatApr, formatCents, formatDate } from "@/lib/format";

// rows arrive already sorted by APR (highest first) from GET /dashboard; don't re-sort here
export default function DebtsTable({ debts }: { debts: Debt[] }) {
  if (debts.length === 0) return <p className="text-sm text-neutral-500">No debts found.</p>;

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[36rem] text-sm">
        <thead>
          <tr className="text-left text-neutral-500">
            <th className="py-2 pr-4 font-medium">Account</th>
            <th className="py-2 pr-4 font-medium">Kind</th>
            <th className="py-2 pr-4 text-right font-medium">APR</th>
            <th className="py-2 pr-4 text-right font-medium">Balance</th>
            <th className="py-2 pr-4 text-right font-medium">Min payment</th>
            <th className="py-2 text-right font-medium">Due</th>
          </tr>
        </thead>
        <tbody>
          {debts.map((d) => (
            <tr key={d.account_id} className="border-t border-black/10 dark:border-white/15">
              <td className="py-2 pr-4">{accountLabel(d.name, d.mask)}</td>
              <td className="py-2 pr-4 capitalize">{d.kind}</td>
              <td className="py-2 pr-4 text-right tabular-nums">{formatApr(d.apr)}</td>
              <td className="py-2 pr-4 text-right tabular-nums">{formatCents(d.balance_cents)}</td>
              <td className="py-2 pr-4 text-right tabular-nums">{formatCents(d.min_payment_cents)}</td>
              <td className="py-2 text-right">{formatDate(d.next_due_date)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

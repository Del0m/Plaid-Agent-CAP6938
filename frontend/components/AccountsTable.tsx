import type { Account } from "@/lib/api";
import { accountLabel, formatCents } from "@/lib/format";

export default function AccountsTable({ accounts }: { accounts: Account[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[32rem] text-sm">
        <thead>
          <tr className="text-left text-neutral-500">
            <th className="py-2 pr-4 font-medium">Account</th>
            <th className="py-2 pr-4 font-medium">Type</th>
            <th className="py-2 pr-4 font-medium">Institution</th>
            <th className="py-2 text-right font-medium">Balance</th>
          </tr>
        </thead>
        <tbody>
          {accounts.map((a) => (
            <tr key={a.id} className="border-t border-black/10 dark:border-white/15">
              <td className="py-2 pr-4">{accountLabel(a.name, a.mask)}</td>
              <td className="py-2 pr-4">{a.subtype ? `${a.type} / ${a.subtype}` : a.type}</td>
              <td className="py-2 pr-4">{a.institution_name ?? "—"}</td>
              <td className="py-2 text-right tabular-nums">{formatCents(a.current_balance_cents)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

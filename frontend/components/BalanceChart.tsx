"use client";

import { Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { Account } from "@/lib/api";
import { accountLabel, formatCents } from "@/lib/format";

// same split as the backend's ASSET_TYPES / DEBT_TYPES in api/dashboard.py
const ASSET_TYPES = new Set(["depository", "investment"]);
const DEBT_TYPES = new Set(["credit", "loan"]);
const ASSET_COLOR = "#059669";
const DEBT_COLOR = "#e11d48";

// long names would squeeze the bars on a phone; the tooltip still shows the full label
function shorten(label: string): string {
  return label.length > 16 ? `${label.slice(0, 15)}…` : label;
}

export default function BalanceChart({ accounts }: { accounts: Account[] }) {
  const rows = accounts
    .filter((a) => a.current_balance_cents !== null && (ASSET_TYPES.has(a.type) || DEBT_TYPES.has(a.type)))
    .map((a) => ({
      name: accountLabel(a.name, a.mask),
      value: (a.current_balance_cents ?? 0) / 100,
      debt: DEBT_TYPES.has(a.type),
    }));

  if (rows.length === 0) return null;

  return (
    <div>
      <div className="mb-2 flex gap-4 text-sm text-neutral-500">
        <span><span className="mr-1 inline-block size-2.5 rounded-sm" style={{ background: ASSET_COLOR }} />Assets</span>
        <span><span className="mr-1 inline-block size-2.5 rounded-sm" style={{ background: DEBT_COLOR }} />Debts</span>
      </div>
      <ResponsiveContainer width="100%" height={rows.length * 40 + 40}>
        <BarChart data={rows} layout="vertical" margin={{ left: 0, right: 16 }}>
          <XAxis type="number" tickFormatter={(v: number) => formatCents(v * 100)} tick={{ fontSize: 12 }} />
          <YAxis type="category" dataKey="name" width={120} tickFormatter={shorten} tick={{ fontSize: 12 }} />
          <Tooltip formatter={(v) => formatCents(Number(v) * 100)} />
          <Bar dataKey="value" name="Balance">
            {rows.map((r, i) => (
              <Cell key={i} fill={r.debt ? DEBT_COLOR : ASSET_COLOR} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

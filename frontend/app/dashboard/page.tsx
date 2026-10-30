"use client";

import Link from "next/link";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import AccountsTable from "@/components/AccountsTable";
import BalanceChart from "@/components/BalanceChart";
import DebtsTable from "@/components/DebtsTable";
import NetWorthSummary from "@/components/NetWorthSummary";
import PlaidLinkButton from "@/components/PlaidLinkButton";
import { errorMessage, getDashboard, syncItem, type Dashboard, type ExchangeResult } from "@/lib/api";

function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-lg border border-black/10 p-4 dark:border-white/15">
      <h2 className="mb-3 font-semibold">{title}</h2>
      {children}
    </section>
  );
}

export default function DashboardPage() {
  const [data, setData] = useState<Dashboard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // "Account linked." after a link whose first sync worked; failed syncs come back from the
  // server as unsynced_items instead, so their Retry sync survives a page refresh
  const [linkedNotice, setLinkedNotice] = useState(false);
  const [syncError, setSyncError] = useState<{ itemId: number; message: string } | null>(null);
  const [retrying, setRetrying] = useState<number | null>(null);
  const [linkError, setLinkError] = useState<string | null>(null);
  // bumping this re-runs the fetch effect below
  const [reloadKey, setReloadKey] = useState(0);
  const load = useCallback(() => setReloadKey((k) => k + 1), []);

  useEffect(() => {
    let cancelled = false; // drop a response that arrives after a newer fetch started
    getDashboard()
      .then((d) => {
        if (cancelled) return;
        setData(d);
        setError(null);
      })
      .catch((err) => !cancelled && setError(errorMessage(err)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  const syncing = data?.unsynced_items.some((item) => item.syncing) ?? false;
  useEffect(() => {
    if (!syncing) return;
    // Bound automatic polling; a stopped API must still leave a visible Retry sync action.
    const interval = setInterval(load, 2000);
    const timeout = setTimeout(() => clearInterval(interval), 120_000);
    return () => {
      clearInterval(interval);
      clearTimeout(timeout);
    };
  }, [syncing, load]);

  const onLinked = useCallback(
    (r: ExchangeResult) => {
      setLinkError(null);
      setSyncError(null);
      setLinkedNotice(r.synced || r.syncing);
      load();
    },
    [load],
  );

  const onLinkError = useCallback((message: string) => setLinkError(message), []);

  async function retrySync(itemId: number) {
    setRetrying(itemId);
    setSyncError(null);
    try {
      await syncItem(itemId);
    } catch (err) {
      setSyncError({ itemId, message: errorMessage(err) });
    } finally {
      setRetrying(null);
      load(); // a partial sync may still have stored accounts
    }
  }

  const linked = data !== null && data.accounts.length > 0;

  return (
    <main className="mx-auto w-full max-w-5xl space-y-6 p-4 sm:p-6">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <h1 className="text-2xl font-semibold">Dashboard</h1>
        <div className="flex items-center gap-3">
          <Link href="/chat" className="text-sm underline underline-offset-2">
            Chat
          </Link>
          {linked && <PlaidLinkButton label="Link another account" onLinked={onLinked} onError={onLinkError} />}
        </div>
      </header>

      {linkError && (
        <p role="alert" className="text-sm text-red-600 dark:text-red-400">
          {linkError}
        </p>
      )}

      {linkedNotice && (
        <div
          role="status"
          className="flex items-center justify-between gap-2 rounded-lg border border-black/10 p-3 text-sm dark:border-white/15"
        >
          <span>Account linked.{syncing ? " Syncing bank data…" : ""}</span>
          <button type="button" onClick={() => setLinkedNotice(false)} className="px-2 text-neutral-500" aria-label="Dismiss">
            ✕
          </button>
        </div>
      )}

      {data?.unsynced_items.map((item) => (
        <div
          key={item.id}
          role="status"
          className="flex flex-col gap-2 rounded-lg border border-amber-300 p-3 text-sm sm:flex-row sm:items-center sm:justify-between"
        >
          <span>
            {item.institution_name ?? "A bank"}{item.syncing
              ? " is linked. Bank data is syncing…"
              : " is linked, but loading its data failed."}
            {syncError?.itemId === item.id && (
              <span className="block text-red-600 dark:text-red-400">{syncError.message}</span>
            )}
          </span>
          <button
            type="button"
            onClick={() => retrySync(item.id)}
            disabled={retrying !== null}
            className="rounded-md border border-black/10 px-3 py-1.5 disabled:opacity-50 dark:border-white/15"
          >
            {retrying === item.id ? "Syncing…" : "Retry sync"}
          </button>
        </div>
      ))}

      {loading ? (
        <p className="text-neutral-500">Loading…</p>
      ) : error ? (
        <div role="alert" className="flex flex-col gap-3 rounded-lg border border-red-300 p-4 sm:flex-row sm:items-center sm:justify-between">
          <span className="text-red-700 dark:text-red-400">Couldn&apos;t load your dashboard: {error}</span>
          <button
            type="button"
            onClick={() => {
              setLoading(true);
              load();
            }}
            className="rounded-md border border-black/10 px-3 py-1.5 text-sm dark:border-white/15"
          >
            Retry
          </button>
        </div>
      ) : !linked ? (
        <section className="mx-auto max-w-md space-y-4 rounded-lg border border-black/10 p-6 text-center dark:border-white/15">
          <h2 className="text-lg font-semibold">Connect a bank</h2>
          <p className="text-sm text-neutral-500">
            Link an account through Plaid to see your balances, net worth and debts. Your bank login goes to Plaid,
            not to this app.
          </p>
          <PlaidLinkButton label="Connect a bank" onLinked={onLinked} onError={onLinkError} />
        </section>
      ) : (
        <>
          <NetWorthSummary data={data} />
          <Card title="Balances">
            <BalanceChart accounts={data.accounts} />
          </Card>
          <Card title="Debts by APR">
            <DebtsTable debts={data.debts} />
          </Card>
          <Card title="Accounts">
            <AccountsTable accounts={data.accounts} />
          </Card>
        </>
      )}
    </main>
  );
}

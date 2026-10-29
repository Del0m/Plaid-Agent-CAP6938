"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import AlertCard from "@/components/AlertCard";
import {
  acceptAlert, dismissAlert, errorMessage, getAlerts, getRun, runBudget, type Alert, type RunStatus,
} from "@/lib/api";

// a run can outlast the proxy's 30 s, so it runs in the background and the page asks how it's going
const POLL_MS = 2000;
// a run whose server stopped never finishes; give up after 10 minutes instead of spinning forever
const MAX_POLLS = 300;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

function resultLine(run: RunStatus): string {
  switch (run.status) {
    case "awaiting_user":
      return `Waiting for your approval on ${plural(run.waiting_for.length, "proposal")}.`;
    case "error":
      return `The run failed (${run.error}).`;
    case "no_budget":
    case "period_ended":
      return "No budget to check for that date.";
    default:
      return run.alert_ids.length ? `${plural(run.alert_ids.length, "alert")} written.` : "No new alerts.";
  }
}

export default function AlertsPage() {
  const [alerts, setAlerts] = useState<Alert[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // blank means today, decided by the server: next 16 won't prerender a client component that reads the clock
  const [date, setDate] = useState("");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [deciding, setDeciding] = useState<number | null>(null);
  // bumping this re-runs the fetch effect below
  const [reloadKey, setReloadKey] = useState(0);
  const load = useCallback(() => setReloadKey((k) => k + 1), []);
  // false once the page is left, so an unfinished poll loop stops instead of running in the background
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    let cancelled = false; // drop a response that arrives after a newer fetch started
    getAlerts()
      .then((a) => {
        if (cancelled) return;
        setAlerts(a);
        setError(null);
      })
      .catch((err) => !cancelled && setError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  async function run() {
    setRunning(true);
    setResult(null);
    try {
      const { run_id } = await runBudget(date || undefined);
      if (run_id === null) {
        setResult("The run started but hasn't reported back yet. Refresh in a moment.");
        return;
      }
      let status: RunStatus | null = null;
      for (let i = 0; i < MAX_POLLS && mounted.current; i++) {
        await sleep(POLL_MS);
        if (!mounted.current) return;
        status = await getRun(run_id);
        if (status.status !== "running") break;
      }
      if (!mounted.current) return;
      setResult(status && status.status !== "running" ? resultLine(status) : "Still running. Check back later.");
    } catch (err) {
      setResult(errorMessage(err));
    } finally {
      setRunning(false);
      load();
    }
  }

  async function decide(id: number, accept: boolean) {
    setDeciding(id);
    setError(null);
    try {
      await (accept ? acceptAlert(id) : dismissAlert(id));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setDeciding(null);
      load();
    }
  }

  const pending = alerts?.filter((a) => a.status === "pending_user") ?? [];
  const history = alerts?.filter((a) => a.status !== "pending_user") ?? [];
  const card = (a: Alert) => (
    <AlertCard key={a.id} alert={a} busy={deciding !== null} onAccept={(id) => decide(id, true)}
      onDismiss={(id) => decide(id, false)} />
  );

  return (
    <main className="mx-auto w-full max-w-3xl space-y-6 p-4 sm:p-6">
      <header className="flex items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">Alerts</h1>
        <Link href="/dashboard" className="text-sm underline underline-offset-2">
          Dashboard
        </Link>
      </header>

      <section className="flex flex-col gap-3 rounded-lg border border-black/10 p-4 sm:flex-row sm:items-end dark:border-white/15">
        <label className="flex flex-col gap-1 text-sm">
          Date (blank for today)
          <input type="date" value={date} onChange={(e) => setDate(e.target.value)}
            className="rounded-md border border-black/10 px-2 py-1 dark:border-white/15" />
        </label>
        <button type="button" onClick={run} disabled={running}
          className="rounded-md bg-black px-3 py-1.5 text-sm text-white disabled:opacity-50 dark:bg-white dark:text-black">
          {running ? "Running…" : "Run budget check"}
        </button>
        {result && <p role="status" className="text-sm">{result}</p>}
      </section>

      {error && (
        <p role="alert" className="text-sm text-red-600 dark:text-red-400">
          {error}
        </p>
      )}

      {alerts === null && !error ? (
        <p className="text-neutral-500">Loading…</p>
      ) : (
        <>
          <section className="space-y-3">
            <h2 className="font-semibold">Pending</h2>
            {pending.length ? pending.map(card) : <p className="text-sm text-neutral-500">Nothing waiting on you.</p>}
          </section>
          <section className="space-y-3">
            <h2 className="font-semibold">History</h2>
            {history.length ? history.map(card) : <p className="text-sm text-neutral-500">No decided alerts yet.</p>}
          </section>
        </>
      )}
    </main>
  );
}

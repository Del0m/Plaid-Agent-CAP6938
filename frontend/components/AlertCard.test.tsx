import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import AlertCard from "@/components/AlertCard";
import type { Alert } from "@/lib/api";

afterEach(cleanup);

function alert(over: Partial<Alert> = {}): Alert {
  return { id: 3, kind: "overspend", category: "GENERAL_MERCHANDISE", severity: "high", cause: "one_time",
           action: "alert", message: "A $600 purchase put you over.", proposed_limit_cents: null,
           status: "pending_user", created_at: "2026-10-20T12:00:00Z", period_start: "2026-10-01",
           awaiting_approval: false, ...over };
}

function draw(a: Alert, busy = false) {
  const onAccept = vi.fn(), onDismiss = vi.fn();
  render(<AlertCard alert={a} onAccept={onAccept} onDismiss={onDismiss} busy={busy} />);
  return { onAccept, onDismiss };
}

describe("AlertCard", () => {
  test("shows the alert and calls back with its id", () => {
    const { onAccept, onDismiss } = draw(alert());
    expect(screen.getByText("General merchandise")).toBeTruthy();
    expect(screen.getByText("A $600 purchase put you over.")).toBeTruthy();
    expect(screen.getByText(/one-time/i)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(onAccept).toHaveBeenCalledWith(3);
    expect(onDismiss).toHaveBeenCalledWith(3);
  });

  test("a decided alert has no buttons and shows its outcome", () => {
    draw(alert({ status: "dismissed" }));
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("Dismissed")).toBeTruthy();
  });

  test("buttons are disabled while busy", () => {
    draw(alert(), true);
    expect((screen.getByRole("button", { name: "Accept" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Dismiss" }) as HTMLButtonElement).disabled).toBe(true);
  });

  test("the proposed limit shows only for a budget adjustment", () => {
    draw(alert({ action: "adjust_next_budget", cause: "recurring", proposed_limit_cents: 13500 }));
    expect(screen.getByText("Proposed limit next month: $135.00")).toBeTruthy();
    cleanup();
    draw(alert({ proposed_limit_cents: null }));
    expect(screen.queryByText(/Proposed limit/)).toBeNull();
  });

  test("the waiting note shows only when a run is waiting on it", () => {
    draw(alert({ action: "adjust_next_budget", proposed_limit_cents: 13500, awaiting_approval: true }));
    expect(screen.getByText("The agent is waiting for your decision.")).toBeTruthy();
    cleanup();
    draw(alert({ action: "adjust_next_budget", proposed_limit_cents: 13500 }));
    expect(screen.queryByText(/waiting for your decision/)).toBeNull();
  });
});

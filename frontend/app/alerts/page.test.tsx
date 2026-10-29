import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import AlertsPage from "@/app/alerts/page";
import { acceptAlert, ApiError, getAlerts, getRun, runBudget, type Alert, type RunStatus } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  getAlerts: vi.fn(),
  runBudget: vi.fn(),
  getRun: vi.fn(),
  acceptAlert: vi.fn(),
  dismissAlert: vi.fn(),
}));

const alertsMock = vi.mocked(getAlerts);
const runMock = vi.mocked(runBudget);
const getRunMock = vi.mocked(getRun);
const acceptMock = vi.mocked(acceptAlert);

function alert(id: number, over: Partial<Alert> = {}): Alert {
  return { id, kind: "overspend", category: "GENERAL_MERCHANDISE", severity: "high", cause: "one_time", action: "alert",
           message: `Alert ${id}`, proposed_limit_cents: null, status: "pending_user", created_at: "2026-10-20T12:00:00Z",
           period_start: "2026-10-01", awaiting_approval: false, ...over };
}

function run(over: Partial<RunStatus>): RunStatus {
  return { run_id: 7, status: "ok", alert_ids: [], waiting_for: [], error: null, ...over };
}

// starts a run and lets the page poll through every scripted status, 2 s apart
async function runThrough(...statuses: RunStatus[]) {
  runMock.mockResolvedValue({ run_id: 7 });
  statuses.forEach((s) => getRunMock.mockResolvedValueOnce(s));
  fireEvent.click(screen.getByRole("button", { name: "Run budget check" }));
  for (let i = 0; i < statuses.length; i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
  }
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  alertsMock.mockResolvedValue([alert(2), alert(1, { status: "accepted" })]);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.resetAllMocks();
});

describe("Alerts page", () => {
  test("loads pending and history", async () => {
    render(<AlertsPage />);
    expect(await screen.findByText("Alert 2")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Pending" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "History" })).toBeTruthy();
    expect(screen.getByText("Alert 1")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Accept" })).toHaveLength(1);
  });

  test("run polls until done then shows result and reloads", async () => {
    render(<AlertsPage />);
    await screen.findByText("Alert 2");
    await runThrough(run({ status: "running" }), run({ status: "ok", alert_ids: [3] }));
    expect(await screen.findByText("1 alert written.")).toBeTruthy();
    expect(getRunMock).toHaveBeenCalledWith(7);
    expect(alertsMock.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  test("run awaiting shows approval line", async () => {
    render(<AlertsPage />);
    await screen.findByText("Alert 2");
    await runThrough(run({ status: "awaiting_user", alert_ids: [3], waiting_for: [3] }));
    expect(await screen.findByText("Waiting for your approval on 1 proposal.")).toBeTruthy();
  });

  test("run with no budget says so", async () => {
    render(<AlertsPage />);
    await screen.findByText("Alert 2");
    await runThrough(run({ status: "no_budget" }));
    expect(await screen.findByText("No budget to check for that date.")).toBeTruthy();
  });

  test("run error shows failure", async () => {
    render(<AlertsPage />);
    await screen.findByText("Alert 2");
    await runThrough(run({ status: "error", error: "ConnectionError" }));
    expect(await screen.findByText("The run failed (ConnectionError).")).toBeTruthy();
  });

  test("a run that never finishes stops polling after 10 minutes", async () => {
    render(<AlertsPage />);
    await screen.findByText("Alert 2");
    runMock.mockResolvedValue({ run_id: 7 });
    getRunMock.mockResolvedValue(run({ status: "running" }));
    fireEvent.click(screen.getByRole("button", { name: "Run budget check" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10 * 60 * 1000 + 5000);
    });
    expect(await screen.findByText("Still running. Check back later.")).toBeTruthy();
    expect(getRunMock).toHaveBeenCalledTimes(300);
  });

  test("leaving the page stops polling", async () => {
    const { unmount } = render(<AlertsPage />);
    await screen.findByText("Alert 2");
    runMock.mockResolvedValue({ run_id: 7 });
    getRunMock.mockResolvedValue(run({ status: "running" }));
    fireEvent.click(screen.getByRole("button", { name: "Run budget check" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    const calls = getRunMock.mock.calls.length;
    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20000);
    });
    expect(getRunMock.mock.calls.length).toBeLessThanOrEqual(calls + 1);
  });

  test("409 shows message", async () => {
    render(<AlertsPage />);
    await screen.findByText("Alert 2");
    runMock.mockRejectedValue(new ApiError(409, { detail: "A budget run is already in progress." }));
    fireEvent.click(screen.getByRole("button", { name: "Run budget check" }));
    expect(await screen.findByText("A budget run is already in progress.")).toBeTruthy();
  });

  test("accept reloads", async () => {
    render(<AlertsPage />);
    await screen.findByText("Alert 2");
    acceptMock.mockResolvedValue(alert(2, { status: "accepted" }));
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(acceptMock).toHaveBeenCalledWith(2));
    await waitFor(() => expect(alertsMock.mock.calls.length).toBeGreaterThanOrEqual(2));
  });
});

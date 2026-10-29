import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import AlertsPage from "@/app/alerts/page";
import {
  acceptAlert, advanceSim, ApiError, getAlerts, getRun, getSim, getSimJob, runBudget, type Alert, type RunStatus,
  type SimJob,
} from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  getAlerts: vi.fn(),
  runBudget: vi.fn(),
  getRun: vi.fn(),
  acceptAlert: vi.fn(),
  dismissAlert: vi.fn(),
  getSim: vi.fn(),
  advanceSim: vi.fn(),
  getSimJob: vi.fn(),
}));

const alertsMock = vi.mocked(getAlerts);
const runMock = vi.mocked(runBudget);
const getRunMock = vi.mocked(getRun);
const acceptMock = vi.mocked(acceptAlert);
const simMock = vi.mocked(getSim);
const advanceMock = vi.mocked(advanceSim);
const simJobMock = vi.mocked(getSimJob);

function job(over: Partial<SimJob>): SimJob {
  return { job_id: 5, status: "ok", days_requested: 7, days_done: 7, start_date: "2026-10-03", date: "2026-10-10",
           run_ids: [1, 2, 3, 4, 5, 6, 7], alerts_written: 1, error: null, ...over };
}

const SIMULATED = { simulated: true, profile: "overspend-one-time", date: "2026-10-03" };

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
  simMock.mockRejectedValue(new ApiError(404, { detail: "Not found." })); // admin off unless a test says so
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

  test("no panel when admin is off", async () => {
    render(<AlertsPage />);
    await screen.findByText("Alert 2");
    expect(screen.queryByText(/Simulated date/)).toBeNull();
  });

  test("no panel for a real user", async () => {
    simMock.mockResolvedValue({ simulated: false, profile: null, date: null });
    render(<AlertsPage />);
    await screen.findByText("Alert 2");
    await waitFor(() => expect(simMock).toHaveBeenCalled());
    expect(screen.queryByText(/Simulated date/)).toBeNull();
  });

  test("advance 7 days polls and reloads", async () => {
    simMock.mockResolvedValue(SIMULATED);
    advanceMock.mockResolvedValue({ job_id: 5 });
    simJobMock.mockResolvedValueOnce(job({ status: "running", days_done: 3 })).mockResolvedValueOnce(job({}));
    render(<AlertsPage />);
    expect(await screen.findByText("Simulated date: Oct 3, 2026")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Advance 7 days" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(await screen.findByText("Day 3 of 7…")).toBeTruthy();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(await screen.findByText("Advanced 7 days to Oct 10, 2026: 7 runs, 1 alert written.")).toBeTruthy();
    expect(advanceMock).toHaveBeenCalledWith(7);
    expect(simMock.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(alertsMock.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  test("simulation error shows where it stopped", async () => {
    simMock.mockResolvedValue(SIMULATED);
    advanceMock.mockResolvedValue({ job_id: 5 });
    simJobMock.mockResolvedValue(job({ status: "error", days_done: 2, date: "2026-10-05", error: "ConnectionError" }));
    render(<AlertsPage />);
    await screen.findByText("Simulated date: Oct 3, 2026");
    fireEvent.click(screen.getByRole("button", { name: "Advance 1 day" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(await screen.findByText("The simulation stopped on Oct 5, 2026 (ConnectionError).")).toBeTruthy();
  });

  test("simulation 409 shows message", async () => {
    simMock.mockResolvedValue(SIMULATED);
    advanceMock.mockRejectedValue(new ApiError(409, { detail: "A budget run is already in progress." }));
    render(<AlertsPage />);
    await screen.findByText("Simulated date: Oct 3, 2026");
    fireEvent.click(screen.getByRole("button", { name: "Advance 1 day" }));
    expect(await screen.findByText("A budget run is already in progress.")).toBeTruthy();
  });

  test("run button disabled while simulating", async () => {
    simMock.mockResolvedValue(SIMULATED);
    advanceMock.mockResolvedValue({ job_id: 5 });
    simJobMock.mockResolvedValue(job({ status: "running", days_done: 1 }));
    render(<AlertsPage />);
    await screen.findByText("Simulated date: Oct 3, 2026");
    fireEvent.click(screen.getByRole("button", { name: "Advance 7 days" }));
    await waitFor(() =>
      expect((screen.getByRole("button", { name: /Run budget check|Running/ }) as HTMLButtonElement).disabled).toBe(true));
  });

  test("month end on the last day advances one", async () => {
    simMock.mockResolvedValue({ ...SIMULATED, date: "2026-10-31" });
    advanceMock.mockResolvedValue({ job_id: 5 });
    simJobMock.mockResolvedValue(job({ days_requested: 1, days_done: 1 }));
    render(<AlertsPage />);
    await screen.findByText("Simulated date: Oct 31, 2026");
    fireEvent.click(screen.getByRole("button", { name: "To month end" }));
    await waitFor(() => expect(advanceMock).toHaveBeenCalledWith(1));
  });

  test("a month-long advance is followed past 10 minutes", async () => {
    // review finding: 31 days on the real model can take longer than the run button's 10-minute cap
    simMock.mockResolvedValue(SIMULATED);
    advanceMock.mockResolvedValue({ job_id: 5 });
    simJobMock.mockImplementation(async () =>
      simJobMock.mock.calls.length < 400 ? job({ status: "running", days_requested: 28, days_done: 20 })
        : job({ days_requested: 28, days_done: 28, date: "2026-10-31" }));
    render(<AlertsPage />);
    await screen.findByText("Simulated date: Oct 3, 2026");
    fireEvent.click(screen.getByRole("button", { name: "To month end" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(820 * 1000);
    });
    expect(await screen.findByText(/^Advanced 28 days to Oct 31, 2026/)).toBeTruthy();
    expect(screen.queryByText("Still running. Check back later.")).toBeNull();
  });
});

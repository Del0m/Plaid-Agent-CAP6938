import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test } from "vitest";
import BudgetPaceBar from "@/components/BudgetPaceBar";
import type { BudgetLine } from "@/lib/api";

afterEach(cleanup);

function line(over: Partial<BudgetLine> = {}): BudgetLine {
  return { category: "FOOD_AND_DRINK", kind: "flexible", limit_cents: 47200, spent_cents: 23600, pace: 0.8,
           flagged: false, reason: null, ...over };
}

function draw(l: BudgetLine) {
  render(<BudgetPaceBar line={l} daysElapsed={20} daysInPeriod={31} />);
  return { meter: screen.getByRole("meter"), fill: screen.getByTestId("fill"), today: screen.getByTestId("today") };
}

describe("BudgetPaceBar", () => {
  test("fills by spent over limit and marks today", () => {
    const { meter, fill, today } = draw(line());
    expect(meter.getAttribute("aria-valuenow")).toBe("50");
    expect(fill.style.width).toBe("50%");
    expect(today.style.left).toBe(`${(20 / 31) * 100}%`);
    expect(screen.getByText("Food and drink")).toBeTruthy();
    expect(screen.getByText(/\$236\.00 of \$472\.00/)).toBeTruthy();
  });

  test("amber on pace, red over the limit, neutral otherwise", () => {
    expect(draw(line({ flagged: true, reason: "pace" })).fill.className).toMatch(/amber/);
    cleanup();
    expect(draw(line({ flagged: true, reason: "over_limit", spent_cents: 60000 })).fill.className).toMatch(/red/);
    cleanup();
    const plain = draw(line()).fill.className;
    expect(plain).not.toMatch(/amber|red/);
  });

  test("caps the bar at 100% but shows what was really spent", () => {
    const { fill, meter } = draw(line({ spent_cents: 70531, limit_cents: 14996, reason: "over_limit", flagged: true }));
    expect(fill.style.width).toBe("100%");
    expect(meter.getAttribute("aria-valuenow")).toBe("100");
    expect(screen.getByText(/\$705\.31 of \$149\.96/)).toBeTruthy();
  });

  test("a zero limit draws an empty bar", () => {
    expect(draw(line({ limit_cents: 0, pace: null })).fill.style.width).toBe("0%");
  });

  test("fixed lines say so", () => {
    draw(line({ category: "RENT_AND_UTILITIES", kind: "fixed", pace: null, limit_cents: 150000, spent_cents: 150000 }));
    expect(screen.getByText(/\(fixed\)/)).toBeTruthy();
  });
});

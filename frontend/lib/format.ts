// display helpers; money arrives from the backend as integer cents

const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

export function formatCents(cents: number | null): string {
  return cents === null ? "—" : usd.format(cents / 100);
}

export function formatApr(apr: number | null): string {
  return apr === null ? "—" : `${apr.toFixed(2)}%`;
}

// "2026-11-15" -> "Nov 15, 2026"; formatted in UTC so the day doesn't shift by timezone
const day = new Intl.DateTimeFormat("en-US", { dateStyle: "medium", timeZone: "UTC" });

export function formatDate(iso: string | null): string {
  return iso === null ? "—" : day.format(new Date(iso));
}

// "Plaid Checking ••0000"
export function accountLabel(name: string, mask: string | null): string {
  return mask ? `${name} ••${mask}` : name;
}

// plaid's category enum for people: "GENERAL_MERCHANDISE" -> "General merchandise"
export function categoryLabel(category: string): string {
  const words = category.toLowerCase().replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// days from an iso date to the last day of its month, at least 1 (on the last day, "to month end" is one day)
export function daysToMonthEnd(iso: string): number {
  const [y, m, d] = iso.split("-").map(Number);
  const last = new Date(Date.UTC(y, m, 0)).getUTCDate();
  return Math.max(last - d, 1);
}

// Presentation helpers that belong to the reporting screen, not to the
// archive. Colours are token names so both palettes stay in app.css.

const SERIES = [
  "var(--series-1)",
  "var(--series-2)",
  "var(--series-3)",
  "var(--series-4)",
  "var(--series-5)",
  "var(--series-6)",
  "var(--series-7)",
  "var(--series-8)",
];

const EXIT_COLOURS = {
  target: "var(--up)",
  stop_loss: "var(--down)",
  timeout: "var(--accent)",
  trailing_stop: "var(--series-3)",
  session_close: "var(--series-4)",
  not_recorded: "var(--faint)",
};

export function seriesColour(index) {
  return SERIES[Math.max(0, index) % SERIES.length];
}

export function exitColour(reason) {
  return EXIT_COLOURS[reason] ?? "var(--muted)";
}

export function exitTone(reason) {
  if (reason === "target") return "up";
  if (reason === "stop_loss") return "down";
  return "";
}

export function shortDay(day) {
  if (!day) return "—";
  const stamp = new Date(`${day}T00:00:00`);
  if (Number.isNaN(stamp.getTime())) return day;
  return stamp.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
}

export function chartDay(value) {
  if (!value) return "";
  const stamp = new Date(value);
  if (Number.isNaN(stamp.getTime())) return "";
  return stamp.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
}

export function tradeMoment(row) {
  const value = row?.ended_at || row?.filled_at || row?.received_at;
  if (!value) return "—";
  const stamp = new Date(value);
  if (Number.isNaN(stamp.getTime())) return "—";
  return stamp.toLocaleTimeString("en-IN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export function holdDuration(row) {
  const start = Date.parse(row?.filled_at || row?.received_at || "");
  const end = Date.parse(row?.ended_at || "");
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
  return (end - start) / 60000;
}

export function shareOf(value, total) {
  const number = Number(value);
  const whole = Number(total);
  if (!Number.isFinite(number) || !Number.isFinite(whole) || whole <= 0) return 0;
  return Math.max(0, Math.min(100, (number / whole) * 100));
}

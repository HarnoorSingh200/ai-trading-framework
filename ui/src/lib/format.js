// Formatting helpers. Prices arrive as numbers rounded to two decimals; the
// UI never invents precision it was not given.

export function price(value, { dash = "—" } = {}) {
  if (value === null || value === undefined || value === "") return dash;
  const number = Number(value);
  if (Number.isNaN(number)) return String(value);
  return number.toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function signed(value, digits = 2) {
  if (value === null || value === undefined) return "—";
  const number = Number(value);
  if (Number.isNaN(number)) return "—";
  const text = Math.abs(number).toLocaleString("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  if (number > 0) return `+${text}`;
  if (number < 0) return `−${text}`;
  return text;
}

export function percent(value) {
  if (value === null || value === undefined) return "—";
  return `${signed(value)}%`;
}

export function compact(value) {
  if (value === null || value === undefined) return "—";
  const number = Number(value);
  if (Number.isNaN(number)) return "—";
  if (number >= 1e7) return `${(number / 1e7).toFixed(2)} Cr`;
  if (number >= 1e5) return `${(number / 1e5).toFixed(2)} L`;
  return number.toLocaleString("en-IN", { maximumFractionDigits: 0 });
}

export function rupees(value, { digits = 0, dash = "—" } = {}) {
  if (value === null || value === undefined || value === "") return dash;
  const number = Number(value);
  if (Number.isNaN(number)) return dash;
  return number.toLocaleString("en-IN", {
    style: "currency",
    currency: "INR",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function signedRupees(value, { digits = 0, dash = "—" } = {}) {
  if (value === null || value === undefined || value === "") return dash;
  const number = Number(value);
  if (Number.isNaN(number)) return dash;
  const text = rupees(Math.abs(number), { digits });
  if (number > 0) return `+${text}`;
  if (number < 0) return `−${text}`;
  return text;
}

export function compactRupees(value, { signed = false, dash = "—" } = {}) {
  if (value === null || value === undefined || value === "") return dash;
  const number = Number(value);
  if (Number.isNaN(number)) return dash;
  const absolute = Math.abs(number);
  let text;
  if (absolute >= 1e7) text = `₹${(absolute / 1e7).toFixed(2)} Cr`;
  else if (absolute >= 1e5) text = `₹${(absolute / 1e5).toFixed(1)} L`;
  else if (absolute >= 1e3) text = `₹${(absolute / 1e3).toFixed(1)}k`;
  else text = `₹${absolute.toFixed(0)}`;
  if (number < 0) return `−${text}`;
  return signed && number > 0 ? `+${text}` : text;
}

export function duration(minutes) {
  if (minutes === null || minutes === undefined) return "—";
  const number = Number(minutes);
  if (Number.isNaN(number) || number < 0) return "—";
  const whole = Math.round(number);
  if (whole < 60) return `${whole}m`;
  const hours = Math.floor(whole / 60);
  const rest = whole % 60;
  return rest ? `${hours}h ${String(rest).padStart(2, "0")}m` : `${hours}h`;
}

export function tone(value) {
  if (value === null || value === undefined) return "";
  const number = Number(value);
  if (Number.isNaN(number) || number === 0) return "";
  return number > 0 ? "up" : "down";
}

export function age(iso) {
  if (!iso) return "unknown";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

export function clock(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString("en-IN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function shortDate(iso) {
  if (!iso) return "";
  const date = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(date.getTime())) return "";
  const sameYear = date.getFullYear() === new Date().getFullYear();
  return date.toLocaleDateString("en-IN", {
    day: "numeric",
    month: "short",
    ...(sameYear ? {} : { year: "2-digit" }),
  });
}

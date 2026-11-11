// How indicator fields become table columns.
//
// The field list is published by Module 1 rather than repeated here: forty
// names in two places would drift, and the drift would be silent. This module
// only decides which of those fields are shown, and how a value reads.

import { compact, percent, price, tone } from "./format.js";

const GROUP_KEY = "tf-indicator-groups";

//: The candle block already shows the bar's change, and it belongs beside the
//: price rather than forty columns away. Everything else appears once.
const SHOWN_IN_CANDLE_BLOCK = new Set(["change_pct"]);

export function loadVisibleGroups(catalogue) {
  try {
    const saved = JSON.parse(localStorage.getItem(GROUP_KEY) || "null");
    if (Array.isArray(saved)) return saved;
  } catch {
    // A blocked storage backend is not a reason to refuse to render.
  }
  return (catalogue?.groups ?? []).filter((group) => group.default).map((g) => g.key);
}

export function saveVisibleGroups(keys) {
  try {
    localStorage.setItem(GROUP_KEY, JSON.stringify(keys));
  } catch {
    // Ignored on purpose; the choice simply does not persist.
  }
}

export function indicatorColumns(catalogue, visible) {
  const columns = [];
  for (const group of catalogue?.groups ?? []) {
    if (!visible.includes(group.key)) continue;
    for (const field of group.fields) {
      if (field.surface !== "table") continue;
      if (SHOWN_IN_CANDLE_BLOCK.has(field.name)) continue;
      columns.push(field);
    }
  }
  return columns;
}

export function isFlag(field) {
  return field.unit === "bool";
}

export function displayValue(field, value) {
  if (value === null || value === undefined) return "—";
  switch (field.unit) {
    case "price":
      return price(value);
    case "percent":
      return percent(value);
    case "ratio":
      return Number(value).toFixed(2);
    case "volume":
      return compact(value);
    case "text":
      return String(value);
    case "bool":
      return value ? "yes" : "no";
    default:
      return Number(value).toFixed(field.decimals ?? 2);
  }
}

export function valueTone(field, value) {
  if (field.unit === "percent" || field.unit === "ratio") return tone(value);
  return "";
}

export function fieldIndex(catalogue) {
  const index = new Map();
  for (const group of catalogue?.groups ?? []) {
    for (const field of group.fields) index.set(field.name, field);
  }
  return index;
}

export function groupLabels(catalogue) {
  const labels = new Map();
  for (const group of catalogue?.groups ?? []) labels.set(group.key, group.label);
  return labels;
}

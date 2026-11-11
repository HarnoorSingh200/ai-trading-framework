/**
 * Browser smoke test for the four Reporting views.
 *
 * Module 4 must still be reachable, but the browser fixture supplies stable
 * archive and usage rows so every view, including per-cycle timing, is
 * exercised without depending on what happens to be in the local archive.
 *
 *   node tests/reporting-smoke.mjs [baseUrl] [outDir]
 */

import { mkdir } from "node:fs/promises";

import { chromium } from "playwright";

const BASE = process.argv[2] || "http://127.0.0.1:5200";
const OUT = process.argv[3] || "../logs/qa";
const problems = [];
const consoleErrors = [];

function check(condition, message) {
  if (!condition) problems.push(message);
  return condition;
}

const NOW = "2026-10-22T10:00:00.000Z";
const ANALYTICS = {
  generated_at: NOW,
  filters: { agent_id: null, day: null },
  basis: "gross realised P&L before fees and taxes; R is measured at entry",
  summary: {
    orders: 5,
    open: 1,
    closed: 4,
    expired: 0,
    realised: 1250,
    wins: 3,
    losses: 1,
    flats: 0,
    win_rate: 75,
    average_gross: 312.5,
    average_r: 0.82,
    average_win: 610,
    average_loss: -580,
    profit_factor: 3.16,
    average_risk: 500,
    total_risk: 2000,
    average_hold_minutes: 31,
    r_samples: 4,
    trading_days: 2,
    green_days: 1,
    red_days: 1,
    flat_days: 0,
    day_win_rate: 50,
    max_drawdown: 180,
    drawdown: { gross: 180, peak: 1310 },
    current_streak: { outcome: "win", count: 2 },
    longest_win_streak: 2,
    longest_loss_streak: 1,
    first_day: "2026-10-21",
    last_day: "2026-10-22",
    best: { symbol: "INFY", gross: 860 },
    worst: { symbol: "TCS", gross: -580 },
  },
  agents: [
    {
      agent_id: "alpha",
      orders: 4,
      open: 0,
      closed: 3,
      expired: 0,
      realised: 1060,
      wins: 2,
      losses: 1,
      flats: 0,
      win_rate: 66.7,
      average_r: 1.1,
      average_gross: 353.33,
      average_win: 820,
      average_loss: -580,
      profit_factor: 2.83,
      average_risk: 500,
      average_hold_minutes: 29,
      r_samples: 3,
      trading_days: 2,
      green_days: 1,
      red_days: 1,
      flat_days: 0,
      max_drawdown: 180,
      final_cumulative: 1060,
    },
    {
      agent_id: "beta",
      orders: 2,
      open: 1,
      closed: 1,
      expired: 0,
      realised: 190,
      wins: 1,
      losses: 0,
      flats: 0,
      win_rate: 100,
      average_r: 0.4,
      average_gross: 190,
      average_win: 190,
      average_loss: null,
      profit_factor: null,
      average_risk: 475,
      average_hold_minutes: 36,
      r_samples: 1,
      trading_days: 1,
      green_days: 1,
      red_days: 0,
      flat_days: 0,
      max_drawdown: 0,
      final_cumulative: 190,
    },
  ],
  days: [
    {
      day: "2026-10-22",
      orders: 3,
      open: 1,
      closed: 2,
      expired: 0,
      realised: 910,
      wins: 2,
      losses: 0,
      flats: 0,
      win_rate: 100,
      agents: [],
    },
    {
      day: "2026-10-21",
      orders: 2,
      open: 0,
      closed: 2,
      expired: 0,
      realised: 340,
      wins: 1,
      losses: 1,
      flats: 0,
      win_rate: 50,
      agents: [],
    },
  ],
  context_days: [
    {
      day: "2026-10-22",
      orders: 3,
      open: 1,
      closed: 2,
      expired: 0,
      realised: 910,
      wins: 2,
      losses: 0,
      flats: 0,
      win_rate: 100,
      agents: [
        { agent_id: "alpha", realised: 720 },
        { agent_id: "beta", realised: 190 },
      ],
    },
    {
      day: "2026-10-21",
      orders: 2,
      open: 0,
      closed: 2,
      expired: 0,
      realised: 340,
      wins: 1,
      losses: 1,
      flats: 0,
      win_rate: 50,
      agents: [{ agent_id: "alpha", realised: 340 }],
    },
  ],
  equity: [
    {
      at: "2026-10-21T05:00:00.000Z",
      day: "2026-10-21",
      agent_id: "alpha",
      order_id: "ord_1",
      symbol: "TCS",
      side: "buy",
      status: "closed",
      exit_reason: "stop_loss",
      r_multiple: -1,
      gross: -580,
      cumulative: -580,
      trade_index: 0,
    },
    {
      at: "2026-10-21T06:00:00.000Z",
      day: "2026-10-21",
      agent_id: "alpha",
      order_id: "ord_2",
      symbol: "INFY",
      side: "buy",
      status: "closed",
      exit_reason: "target",
      r_multiple: 1.8,
      gross: 920,
      cumulative: 340,
      trade_index: 1,
    },
    {
      at: "2026-10-22T05:00:00.000Z",
      day: "2026-10-22",
      agent_id: "beta",
      order_id: "ord_3",
      symbol: "HDFC",
      side: "buy",
      status: "closed",
      exit_reason: "target",
      r_multiple: 0.4,
      gross: 190,
      cumulative: 530,
      trade_index: 2,
    },
    {
      at: "2026-10-22T06:00:00.000Z",
      day: "2026-10-22",
      agent_id: "alpha",
      order_id: "ord_4",
      symbol: "INFY",
      side: "sell",
      status: "closed",
      exit_reason: "target",
      r_multiple: 1.9,
      gross: 720,
      cumulative: 1250,
      trade_index: 3,
    },
  ],
  agent_curves: [
    {
      agent_id: "alpha",
      final_cumulative: 1060,
      max_drawdown: 180,
      points: [],
    },
    {
      agent_id: "beta",
      final_cumulative: 190,
      max_drawdown: 0,
      points: [],
    },
  ],
  r_distribution: [
    { key: "m2_m1", label: "−2 to −1R", count: 1, gross: -580 },
    { key: "0_1", label: "0 to 1R", count: 1, gross: 190 },
    { key: "1_2", label: "1 to 2R", count: 2, gross: 1640 },
  ],
  exits: [
    { reason: "target", label: "Target", count: 3, gross: 1830 },
    { reason: "stop_loss", label: "Stop", count: 1, gross: -580 },
  ],
  sides: [
    {
      side: "buy",
      label: "Long",
      orders: 3,
      closed: 3,
      realised: 530,
      win_rate: 66.7,
      average_r: 0.4,
    },
    {
      side: "sell",
      label: "Short",
      orders: 1,
      closed: 1,
      realised: 720,
      win_rate: 100,
      average_r: 1.9,
    },
  ],
  symbols: [
    {
      symbol: "INFY",
      orders: 2,
      closed: 2,
      wins: 2,
      losses: 0,
      realised: 1640,
      win_rate: 100,
      average_r: 1.85,
      average_gross: 820,
    },
    {
      symbol: "TCS",
      orders: 1,
      closed: 1,
      wins: 0,
      losses: 1,
      realised: -580,
      win_rate: 0,
      average_r: -1,
      average_gross: -580,
    },
  ],
};

const ORDERS = [
  {
    order_id: "ord_4",
    agent_id: "alpha",
    symbol: "INFY",
    side: "sell",
    quantity: 20,
    entry_price: 1510,
    exit_price: 1474,
    status: "closed",
    session_date: "2026-10-22",
    received_at: "2026-10-22T05:45:00.000Z",
    filled_at: "2026-10-22T05:45:01.000Z",
    ended_at: "2026-10-22T06:15:00.000Z",
    exit_reason: "target",
    pnl: { gross: 720, r_multiple: 1.9 },
  },
  {
    order_id: "ord_3",
    agent_id: "beta",
    symbol: "HDFC",
    side: "buy",
    quantity: 10,
    entry_price: 1700,
    exit_price: 1719,
    status: "closed",
    session_date: "2026-10-22",
    received_at: "2026-10-22T04:45:00.000Z",
    filled_at: "2026-10-22T04:45:01.000Z",
    ended_at: "2026-10-22T05:15:00.000Z",
    exit_reason: "target",
    pnl: { gross: 190, r_multiple: 0.4 },
  },
  {
    order_id: "ord_5",
    agent_id: "beta",
    symbol: "SBIN",
    side: "buy",
    quantity: 8,
    entry_price: 830,
    status: "open",
    session_date: "2026-10-22",
    received_at: "2026-10-22T06:30:00.000Z",
    filled_at: "2026-10-22T06:30:01.000Z",
    pnl: null,
  },
];

function cycles(day, agentId, offset) {
  return [
    {
      cycle_id: `cyc_${day}_${agentId}_1`,
      agent_id: agentId,
      bar_open_time: `${day}T09:45:00+05:30`,
      started_at: `${day}T04:15:0${offset}.000Z`,
      finished_at: `${day}T04:15:02.800Z`,
      duration_ms: 2800,
      duration_source: "measured",
      model_calls: 2,
      model_time_ms: 1900,
      tool_calls: 3,
      tool_time_ms: 420,
      rounds: 2,
      decisions: 1,
      notes_kept: true,
      status: "decided",
      prompt_tokens: 7000,
      completion_tokens: 600,
      prompt_cache_hit_tokens: 5600,
      prompt_cache_miss_tokens: 1400,
      reasoning_tokens: 250,
      total_tokens: 7600,
    },
    {
      cycle_id: `cyc_${day}_${agentId}_2`,
      agent_id: agentId,
      bar_open_time: `${day}T10:00:00+05:30`,
      started_at: `${day}T04:30:0${offset}.000Z`,
      finished_at: `${day}T04:30:01.900Z`,
      duration_ms: 1900,
      duration_source: "measured",
      model_calls: 1,
      model_time_ms: 1400,
      tool_calls: 2,
      tool_time_ms: 300,
      rounds: 1,
      decisions: 0,
      notes_kept: false,
      status: "decided",
      prompt_tokens: 5200,
      completion_tokens: 450,
      prompt_cache_hit_tokens: 4000,
      prompt_cache_miss_tokens: 1200,
      reasoning_tokens: 160,
      total_tokens: 5650,
    },
  ];
}

const USAGE = {
  count: 2,
  tracked_days: 2,
  updated_at: NOW,
  days: [
    {
      day: "2026-10-22",
      agents: [
        {
          agent_id: "alpha",
          name: "Alpha",
          model: "gpt-6-luna",
          model_calls: 3,
          cycles: 2,
          tool_calls: 5,
          decisions: 1,
          prompt_tokens: 12200,
          completion_tokens: 1050,
          prompt_cache_hit_tokens: 9600,
          prompt_cache_miss_tokens: 2600,
          reasoning_tokens: 410,
          total_tokens: 13250,
          cycle_time_ms: 4700,
          model_time_ms: 3300,
          tool_time_ms: 720,
          timed_cycles: 2,
          models: ["gpt-6-luna"],
          cycle_rows: cycles("2026-10-22", "alpha", 2),
        },
        {
          agent_id: "beta",
          name: "Beta",
          model: "deepseek-v4-flash",
          model_calls: 3,
          cycles: 2,
          tool_calls: 5,
          decisions: 1,
          prompt_tokens: 12200,
          completion_tokens: 1050,
          prompt_cache_hit_tokens: 9600,
          prompt_cache_miss_tokens: 2600,
          reasoning_tokens: 410,
          total_tokens: 13250,
          cycle_time_ms: 4700,
          model_time_ms: 3300,
          tool_time_ms: 720,
          timed_cycles: 2,
          models: ["deepseek-v4-flash"],
          cycle_rows: cycles("2026-10-22", "beta", 4),
        },
      ],
      totals: {
        model_calls: 6,
        cycles: 4,
        tool_calls: 10,
        decisions: 2,
        prompt_tokens: 24400,
        completion_tokens: 2100,
        prompt_cache_hit_tokens: 19200,
        prompt_cache_miss_tokens: 5200,
        reasoning_tokens: 820,
        total_tokens: 26500,
        cycle_time_ms: 9400,
        model_time_ms: 6600,
        tool_time_ms: 1440,
        timed_cycles: 4,
        models: ["deepseek-v4-flash", "gpt-6-luna"],
      },
    },
    {
      day: "2026-10-21",
      agents: [
        {
          agent_id: "alpha",
          name: "Alpha",
          model: "gpt-6-luna",
          model_calls: 3,
          cycles: 2,
          tool_calls: 5,
          decisions: 1,
          prompt_tokens: 12200,
          completion_tokens: 1050,
          prompt_cache_hit_tokens: 9600,
          prompt_cache_miss_tokens: 2600,
          reasoning_tokens: 410,
          total_tokens: 13250,
          cycle_time_ms: 4700,
          model_time_ms: 3300,
          tool_time_ms: 720,
          timed_cycles: 2,
          models: ["gpt-6-luna"],
          cycle_rows: cycles("2026-10-21", "alpha", 8),
        },
      ],
      totals: {
        model_calls: 3,
        cycles: 2,
        tool_calls: 5,
        decisions: 1,
        prompt_tokens: 12200,
        completion_tokens: 1050,
        prompt_cache_hit_tokens: 9600,
        prompt_cache_miss_tokens: 2600,
        reasoning_tokens: 410,
        total_tokens: 13250,
        cycle_time_ms: 4700,
        model_time_ms: 3300,
        tool_time_ms: 720,
        timed_cycles: 2,
        models: ["gpt-6-luna"],
      },
    },
  ],
};

async function stubModules(page) {
  await page.route(
    /\/api\/|\/agents\/|\/orders\/|\/central\//,
    async (route) => {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({
          ok: false,
          error: {
            code: "NOT_UNDER_TEST",
            message: "This module is deliberately stubbed by the reporting smoke test.",
          },
        }),
      });
    },
  );
  await page.route("**/report/**", async (route) => {
    const url = new URL(route.request().url());
    let data;
    if (url.pathname.endsWith("/v1/analytics")) data = structuredClone(ANALYTICS);
    else if (url.pathname.endsWith("/v1/agents")) {
      data = { count: ANALYTICS.agents.length, agents: ANALYTICS.agents };
    } else if (url.pathname.endsWith("/v1/orders")) {
      data = { count: ORDERS.length, orders: ORDERS };
    } else if (url.pathname.endsWith("/v1/usage")) data = structuredClone(USAGE);
    else if (url.pathname.endsWith("/v1/benchmark")) {
      data = {
        symbol: "NIFTY 50",
        notional: 50000,
        candles: [
          { date: "2026-10-21", close: 25000 },
          { date: "2026-10-22", close: 25100 },
        ],
      };
    } else if (url.pathname.endsWith("/v1/status")) {
      data = {
        archive: {
          synced_at: NOW,
          archive: {
            present: true,
            written_at: NOW,
            count: ORDERS.length,
          },
        },
        usage: { updated_at: NOW, days: 2 },
        benchmark: { updated_at: NOW, error: null },
        last_error: null,
      };
    } else {
      data = {};
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: true, data }),
    });
  });
}

async function openReporting(page) {
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: /Reporting/ }).click();
  await page.waitForSelector("h1", { timeout: 10000 });
  await page.waitForTimeout(500);
}

async function capture(page, name) {
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: true });
}

await mkdir(OUT, { recursive: true });
const moduleFourUp = await fetch("http://127.0.0.1:5204/health", {
  signal: AbortSignal.timeout(2000),
})
  .then((response) => response.ok)
  .catch(() => false);
check(moduleFourUp, "Module 4 is not answering on 127.0.0.1:5204");

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1600, height: 1100 } });
const page = await context.newPage();
page.on("console", (message) => {
  if (message.type() !== "error") return;
  const url = message.location()?.url ?? "";
  if (/\/api\/|\/agents\/|\/orders\/|\/central\//.test(url)) return;
  consoleErrors.push({ text: message.text(), url });
});
page.on("pageerror", (error) =>
  consoleErrors.push({ text: `pageerror: ${error.message}`, url: "" }),
);
await stubModules(page);
await openReporting(page);

const heading = await page.locator("h1").first().innerText();
check(/reporting/i.test(heading), `unexpected heading: ${heading}`);
check((await page.locator(".report-tab").count()) === 4, "Reporting does not have four views");
check((await page.locator(".report-overview").count()) === 1, "the performance overview is missing");
check(
  (await page.locator("svg[role='img']").count()) >= 2,
  "the performance charts did not render",
);
check(
  (await page.locator(".report-table tbody tr").count()) >= 1,
  "the performance scorecard is empty",
);
await capture(page, "24-reporting-performance");

await page.getByRole("tab", { name: /AI performance/ }).click();
await page.waitForSelector(".cycle-chart svg", { timeout: 10000 });
check(
  /AI costs and cycle time/i.test(await page.locator("#report-panel-ai h2").first().innerText()),
  "the AI performance view did not open",
);
check(
  (await page.locator(".cycle-chart circle.point").count()) >= 4,
  "the cycle trace has no observations",
);
check(
  (await page.locator("#report-panel-ai .report-cycle-scroll tbody tr").count()) >= 4,
  "the cycle ledger has no rows",
);
const metricButtons = page.locator(".metric-switch button");
await metricButtons.nth(1).click();
check(
  (await metricButtons.nth(1).getAttribute("aria-pressed")) === "true",
  "the cycle chart did not switch to tokens",
);
await page.locator(".report-day-select select").selectOption("2026-10-21");
await page.waitForTimeout(350);
check(
  (await page.locator(".report-scope-meta").innerText()).includes("21 Oct"),
  "selecting a usage day did not update the shared day scope",
);
await capture(page, "25-reporting-ai");
await page.locator(".report-day-select select").selectOption("2026-10-22");
await page.waitForTimeout(350);

await page.getByRole("tab", { name: /Order archive/ }).click();
check(
  (await page.locator("#report-panel-orders tbody tr").count()) === 3,
  "the order archive did not render every fixture order",
);
await page.locator(".report-search").fill("INFY");
check(
  (await page.locator("#report-panel-orders tbody tr").count()) === 1,
  "the order archive symbol filter did not narrow the table",
);
await capture(page, "26-reporting-orders");

await page.getByRole("tab", { name: /Data coverage/ }).click();
check(
  (await page.locator(".report-status-strip").count()) === 1,
  "the coverage provenance strip is missing",
);
check(
  (await page.locator("#report-panel-coverage tbody tr").count()) >= 2,
  "the coverage day table is empty",
);
await capture(page, "27-reporting-coverage");

const initialTheme = await page.evaluate(() => document.documentElement.dataset.theme);
await page.locator("button.theme").click();
await page.waitForTimeout(300);
const nextTheme = await page.evaluate(() => document.documentElement.dataset.theme);
check(nextTheme !== initialTheme, "the theme toggle did not change the reporting palette");
await capture(page, `28-reporting-${nextTheme}`);

for (const [width, height, name] of [
  [900, 1100, "29-reporting-900"],
  [560, 1100, "30-reporting-560"],
]) {
  await page.setViewportSize({ width, height });
  await page.waitForTimeout(250);
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  check(overflow <= 1, `horizontal overflow of ${overflow}px at ${width}px wide`);
  await capture(page, name);
}

await browser.close();
console.log(
  JSON.stringify(
    {
      moduleFourUp,
      heading,
      views: await Promise.resolve(4),
      consoleErrors,
      problems,
    },
    null,
    2,
  ),
);

if (consoleErrors.length || problems.length) process.exit(1);

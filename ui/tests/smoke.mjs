/**
 * Browser smoke test for the operator site.
 *
 * Drives the real site against the real Module 1 service in Chromium: opens the
 * page, checks the module tabs and the timeframe tabs, walks every module tab,
 * drills into a stock, checks the layout holds at a narrow width, and fails on
 * any console or page error.
 *
 *   node tests/smoke.mjs [baseUrl] [outDir]
 */

import { mkdir } from "node:fs/promises";

import { chromium } from "playwright";

const BASE = process.argv[2] || "http://127.0.0.1:5200";
const OUT = process.argv[3] || "../logs/qa";

const problems = [];

function check(condition, message) {
  if (!condition) problems.push(message);
  return condition;
}

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });

const consoleErrors = [];
page.on("console", (message) => {
  if (message.type() === "error") {
    let url = "";
    try {
      url = message.location()?.url ?? "";
    } catch {
      url = "";
    }
    consoleErrors.push({ text: message.text(), url });
  }
});
page.on("pageerror", (error) =>
  consoleErrors.push({ text: `pageerror: ${error.message}`, url: "" }),
);

// Every module except Module 1 is optional infrastructure for this site.
// Whether each is *up when its tab is visited* decides what that tab should
// show, and which console noise is expected rather than a failure - so each
// is probed at the moment its tab is opened, not once at the start (the
// central process may still be starting the later ones).
const health = (port) =>
  fetch(`http://127.0.0.1:${port}/health`, { signal: AbortSignal.timeout(2000) })
    .then((response) => response.ok)
    .catch(() => false);
let moduleTwoUp = false;
let moduleThreeUp = false;
let moduleFourUp = false;

// The central process is what the Control tab reads. It is optional for this
// test in the same way: without it, the tab has to say so plainly.
const centralUp = await health(5205);
// What the central process says about each module decides what "its tab
// should show when it is not answering" means: a fault, or the market being
// closed. The tab has to tell those apart.
const centralStatus = centralUp
  ? await fetch("http://127.0.0.1:5205/v1/status", { signal: AbortSignal.timeout(3000) })
      .then((response) => response.json())
      .then((payload) => payload.data ?? null)
      .catch(() => null)
  : null;
const resting = (name) => centralStatus?.services?.[name]?.state === "resting";

await mkdir(OUT, { recursive: true });
await page.goto(BASE, { waitUntil: "domcontentloaded" });
// The board fills in as Module 1's history lands, and the browser may have
// been opened before the framework was ready, so wait for the whole universe
// rather than the first row.
await page.waitForFunction(
  () => document.querySelectorAll("tbody tr:not(.skeleton)").length >= 50,
  undefined,
  { timeout: 180000 },
);
await page.waitForTimeout(800);

// -- theme ----------------------------------------------------------------- //
const background = () =>
  page.evaluate(() => getComputedStyle(document.body).backgroundColor);
const themeAttr = () => page.evaluate(() => document.documentElement.dataset.theme);
const shot = async (name) => {
  const info = await page.evaluate(() => ({
    theme: document.documentElement.dataset.theme,
    bg: getComputedStyle(document.body).backgroundColor,
  }));
  console.log(`SHOT ${name}: theme=${info.theme} bg=${info.bg}`);
  await page.screenshot({ path: `${OUT}/${name}.png` });
};

const startTheme = await themeAttr();
check(["light", "dark"].includes(startTheme), `unexpected theme: ${startTheme}`);
const lightBackground = await background();

// Every row should carry a shape, not only numbers.
const sparklines = await page.locator("tbody tr td.trend svg").count();
check(sparklines === 50, `expected 50 sparklines, saw ${sparklines}`);

// -- indicator columns and their group toggles ------------------------------ //
const headers = async () =>
  (await page.locator("thead th").allInnerTexts()).map((text) => text.trim());

const groupButtons = await page.locator(".groups button.group").allInnerTexts();
check(
  groupButtons.length === 6,
  `expected 6 column groups, saw ${groupButtons.length}`,
);

const wideColumns = (await headers()).length;
check(
  wideColumns > 30,
  `expected a wide table of indicator columns, saw ${wideColumns}`,
);
check(
  (await headers()).some((text) => text.startsWith("RSI 14")),
  "the indicator columns are missing from the header",
);
const filled = await page.locator("tbody tr").first().locator("td").count();
check(filled === wideColumns - 1, `row has ${filled} cells for ${wideColumns} columns`);
await shot("12-indicator-columns");

// Hiding a group must remove its columns, and the choice must stick.
const momentum = page.locator(".groups button.group", { hasText: "Momentum" });
await momentum.click();
await page.waitForTimeout(400);
const narrowColumns = (await headers()).length;
check(
  narrowColumns < wideColumns,
  `hiding Momentum left ${narrowColumns} columns of ${wideColumns}`,
);
await shot("13-columns-hidden");
await momentum.click();
await page.waitForTimeout(400);
check(
  (await headers()).length === wideColumns,
  "re-enabling Momentum did not restore its columns",
);

await shot(`theme-${startTheme}`);

const other = startTheme === "dark" ? "light" : "dark";
await page.locator("button.theme").click();
await page.waitForTimeout(300);
check((await themeAttr()) === other, `the toggle did not switch to ${other}`);
const otherBackground = await background();
check(
  otherBackground !== lightBackground,
  `the palette did not actually change (still ${otherBackground})`,
);
await shot(`theme-${other}`);

// The choice has to survive a reload, and be applied before the first paint.
await page.reload({ waitUntil: "domcontentloaded" });
await page.waitForSelector("tbody tr:not(.skeleton)", { timeout: 60000 });
check(
  (await themeAttr()) === other,
  `the theme was not remembered across a reload (${await themeAttr()})`,
);
// The attribute is not the point; the rendered palette is.
const reloadedBackground = await background();
check(
  reloadedBackground === otherBackground,
  `after a reload the palette was ${reloadedBackground}, expected ${otherBackground}`,
);

// Continue the rest of the walk in dark, which is the harder palette.
if ((await themeAttr()) !== "dark") {
  await page.locator("button.theme").click();
  await page.waitForTimeout(300);
}
check((await themeAttr()) === "dark", "could not get into the dark theme");

// -- the module tabs ------------------------------------------------------- //
const tabs = await page.locator("nav button.tab").allInnerTexts();
check(
  tabs.length === 6,
  `expected 6 tabs (four modules, Control and Design), saw ${tabs.length}`,
);

// -- the timeframe tabs ---------------------------------------------------- //
const timeframeLabels = await page.locator(".segments button").allInnerTexts();
check(
  timeframeLabels.length === 8,
  `expected 8 timeframe tabs, saw ${timeframeLabels.length}: ${timeframeLabels.join(", ")}`,
);
check(
  timeframeLabels.join(",") === "1m,5m,10m,15m,20m,30m,60m,1D",
  `unexpected timeframe labels: ${timeframeLabels.join(",")}`,
);

const rows15 = await page.locator("tbody tr").count();
check(rows15 === 50, `expected 50 rows on 15m, saw ${rows15}`);
const heading15 = await page.locator("h1").innerText();
const firstSymbol = await page.locator("tbody tr th .symbol").first().innerText();
check(/^[A-Z0-9&-]+$/.test(firstSymbol.trim()), `odd symbol: ${firstSymbol}`);
const firstName = await page.locator("tbody tr th .name").first().innerText();
check(firstName.includes("·"), `the row should carry the company name: ${firstName}`);

const summaries = [];
const switchTimeframe = async (label, expected) => {
  await page.getByRole("tab", { name: label, exact: true }).click();
  await page.waitForFunction(
    (text) => document.querySelector("h1")?.innerText.includes(text),
    expected,
    { timeout: 90000 },
  );
  await page.waitForFunction(
    () => document.querySelectorAll("tbody tr").length > 1,
    undefined,
    { timeout: 90000 },
  );
  await page.waitForTimeout(400);
  summaries.push((await page.locator(".tfmeta").innerText()).replace(/\s+/g, " "));
  return page.locator("tbody tr").count();
};

await page.screenshot({ path: `${OUT}/01-market-15m.png` });

const rows5 = await switchTimeframe("5m", "5 minutes");
check(rows5 === 50, `expected 50 rows on 5m, saw ${rows5}`);
await page.screenshot({ path: `${OUT}/02-market-5m.png` });

const rows1 = await switchTimeframe("1m", "1 minute");
check(rows1 === 50, `expected 50 rows on 1m, saw ${rows1}`);
await page.screenshot({ path: `${OUT}/03-market-1m.png` });

const rowsDaily = await switchTimeframe("1D", "1 day");
check(rowsDaily === 50, `expected 50 rows on 1D, saw ${rowsDaily}`);
await page.screenshot({ path: `${OUT}/04-market-1d.png` });

await switchTimeframe("15m", "15 minutes");
check(
  new Set(summaries).size > 1,
  "every timeframe reported the same summary, so the tabs may not be switching",
);

// -- filtering ------------------------------------------------------------- //
await page.fill(".search", "bank");
await page.waitForTimeout(400);
const filtered = await page.locator("tbody tr").count();
check(filtered > 0 && filtered < 50, `filter returned ${filtered} rows`);
await page.screenshot({ path: `${OUT}/05-filter.png` });
await page.fill(".search", "");
await page.waitForTimeout(300);

// -- the agent engine ------------------------------------------------------ //
moduleTwoUp = await health(5202);
await page.locator("nav button.tab").nth(1).click();
await page.waitForTimeout(600);
const agentsHeading = await page.locator("h1").first().innerText();
check(
  /agent engine/i.test(agentsHeading),
  `unexpected heading on the agent tab: ${agentsHeading}`,
);
const agentsBanner = await page.locator(".banner.bad").count();
const agentsResting = await page.locator(".banner.calm").count();
if (moduleTwoUp) {
  check(agentsBanner === 0, "the agent tab reported Module 2 as down, but it is up");
} else if (resting("module2")) {
  check(agentsBanner === 0, "a Module 2 resting for the market is not a failure");
  check(
    agentsResting > 0,
    "the agent tab should say the market is not open rather than that Module 2 broke",
  );
} else {
  // Saying so plainly is the honest behaviour when the control service is absent.
  check(agentsBanner > 0, "the agent tab should say Module 2 is not answering");
}
await page.screenshot({ path: `${OUT}/06-agents.png` });

// -- starting a second agent while the first is running -------------------- //
// Two agents may run at once: the control service keeps one child process per
// agent. What used to stop you was this page - the registry polls every five
// seconds, and the poll took the selection back while the new-agent form was
// open, turning Save into an edit of the agent that was already running.
if (moduleTwoUp && (await page.locator(".list .card").count()) > 0) {
  await page.locator(".head button", { hasText: "New agent" }).click();
  await page.waitForTimeout(400);
  const opened = await page.locator(".panel h2").first().innerText();
  check(/new agent/i.test(opened), `the form did not open as a new agent: ${opened}`);
  // Longer than the poll, so a poll happens while the form is open.
  await page.waitForTimeout(6500);
  const stillNew = await page.locator(".panel h2").first().innerText();
  check(
    /new agent/i.test(stillNew),
    `the registry poll stole the selection from the new-agent form: ${stillNew}`,
  );
  await page.screenshot({ path: `${OUT}/14-new-agent.png` });
  await page.locator(".panel button", { hasText: "Cancel" }).click();
  await page.waitForTimeout(250);
}

// -- the order manager ----------------------------------------------------- //
moduleThreeUp = await health(5203);
await page.locator("nav button.tab").nth(2).click();
await page.waitForTimeout(600);
const ordersHeading = await page.locator("h1").first().innerText();
check(
  /order manager/i.test(ordersHeading),
  `unexpected heading on the orders tab: ${ordersHeading}`,
);
const ordersBanner = await page.locator(".banner.bad").count();
const ordersResting = await page.locator(".banner.calm").count();
if (moduleThreeUp) {
  check(ordersBanner === 0, "the orders tab reported Module 3 as down, but it is up");
} else if (resting("module3")) {
  check(ordersBanner === 0, "a Module 3 resting for the market is not a failure");
  check(
    ordersResting > 0,
    "the orders tab should say the market is not open rather than that Module 3 broke",
  );
} else {
  // Saying so plainly is the honest behaviour when the order book is absent.
  check(ordersBanner > 0, "the orders tab should say Module 3 is not answering");
}
// The book summarises itself whether or not anything is on it.
check(
  (await page.locator(".strip dt").allInnerTexts()).length >= 6,
  "the orders tab should summarise the book",
);
await page.screenshot({ path: `${OUT}/07-orders.png` });

// -- the reporting dashboard ----------------------------------------------- //
moduleFourUp = await health(5204);
await page.locator("nav button.tab").nth(3).click();
await page.waitForTimeout(600);
const reportingHeading = await page.locator("h1").first().innerText();
check(
  /reporting/i.test(reportingHeading),
  `unexpected heading on the reporting tab: ${reportingHeading}`,
);
const reportingBanner = await page.locator(".report-banner.bad").count();
if (moduleFourUp) {
  check(reportingBanner === 0, "the reporting tab reported Module 4 as down, but it is up");
} else {
  check(reportingBanner > 0, "the reporting tab should say Module 4 is not answering");
}
await page.screenshot({ path: `${OUT}/08-reporting.png` });

// -- the central process ---------------------------------------------------- //
await page.locator("nav button.tab").nth(4).click();
await page.waitForTimeout(800);
const controlHeading = await page.locator("h1").first().innerText();
check(
  /control/i.test(controlHeading),
  `unexpected heading on the control tab: ${controlHeading}`,
);
const controlBanner = await page.locator(".banner.bad").count();
if (centralUp) {
  check(controlBanner === 0, "the control tab reported the central process as down, but it is up");
  const serviceRows = await page.locator("tbody tr").count();
  check(serviceRows === 4, `expected 4 services on the control tab, saw ${serviceRows}`);
  // The log viewer has to actually read a file, not only open a panel.
  await page.locator("tbody tr").first().locator("button", { hasText: "Logs" }).click();
  await page.waitForSelector(".logs pre", { timeout: 10000 });
  await page.waitForTimeout(1200);
  const logText = await page.locator(".logs pre").innerText();
  check(
    /module 1 starting|uvicorn|INFO/i.test(logText),
    `the Module 1 log panel did not show a real log: ${logText.slice(0, 120)}`,
  );
  await page.locator(".logs header button", { hasText: "Close" }).click();
  await page.waitForTimeout(250);
  check((await page.locator(".logs").count()) === 0, "the log panel did not close");
} else {
  check(controlBanner > 0, "the control tab should say the central process is not answering");
}
await page.screenshot({ path: `${OUT}/15-control.png` });

// -- the design guide ------------------------------------------------------- //
await page.locator("nav button.tab").nth(5).click();
await page.waitForTimeout(400);
const designHeading = await page.locator("h1").first().innerText();
check(
  /how the framework moves a market fact/i.test(
    designHeading.replace(/\s+/g, " "),
  ),
  `unexpected heading on the design tab: ${designHeading}`,
);
const designModules = await page.locator("article.module").count();
check(designModules === 4, `expected 4 explained modules, saw ${designModules}`);
const designIllustrations = await page.locator("article.module .sequence-shell svg").count();
check(
  designIllustrations === 4,
  `expected one message sequence per module, saw ${designIllustrations}`,
);
await page.screenshot({ path: `${OUT}/16-design.png`, fullPage: true });

await page.locator("nav button.tab").first().click();
await page.waitForSelector("tbody tr:not(.skeleton)", { timeout: 60000 });
await page.waitForTimeout(400);

// -- drill into one stock -------------------------------------------------- //
await page.locator("tbody tr th button.symbol").first().click();
await page.waitForSelector("aside table tbody tr", { timeout: 60000 });
await page.waitForTimeout(600);
const inspectorTitle = await page.locator("aside h2").innerText();
const bars15 = await page.locator("aside table tbody tr").count();
const summaryOf = async () =>
  (await page.locator("aside .summary").innerText()).replace(/\s+/g, " ");
const summary15 = await summaryOf();
check(bars15 === 60, `the inspector should show 60 bars, saw ${bars15}`);
// The pivot ladder and the grouped indicator values.
const ladder = await page.locator("aside .ladder > div").count();
check(ladder === 10, `expected 10 level tiles, saw ${ladder}`);
const indicatorValues = await page.locator("aside .indicator-group dd").count();
check(
  indicatorValues > 20,
  `expected grouped indicator values in the inspector, saw ${indicatorValues}`,
);
await page.screenshot({ path: `${OUT}/09-inspector-15m.png` });

const waitForNewSummary = async (previous) => {
  await page.waitForFunction(
    (prev) => {
      const el = document.querySelector("aside .summary");
      return el !== null && el.innerText.replace(/\s+/g, " ") !== prev;
    },
    previous,
    { timeout: 90000 },
  );
};

await page.locator("aside .choices button", { hasText: /^1D$/ }).click();
await waitForNewSummary(summary15);
const barsDaily = await page.locator("aside table tbody tr").count();
check(barsDaily === 60, `expected 60 daily bars, saw ${barsDaily}`);
// A daily table shows dates where an intraday one shows a clock time.
const dailyLabel = await page.locator("aside table tbody tr th").first().innerText();
check(!/^\d{2}:\d{2}$/.test(dailyLabel.trim()), `the daily view still shows times: ${dailyLabel}`);
await page.screenshot({ path: `${OUT}/10-inspector-1d.png` });

await page.locator("aside button.close").click();
await page.waitForTimeout(250);
check((await page.locator("aside").count()) === 0, "the inspector did not close");

// -- narrow ---------------------------------------------------------------- //
await page.setViewportSize({ width: 900, height: 1000 });
await page.waitForTimeout(400);
const overflow = await page.evaluate(
  () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
);
check(overflow <= 1, `horizontal overflow of ${overflow}px at 900px wide`);
await page.screenshot({ path: `${OUT}/11-narrow.png` });

// A refused request to a module's proxy is expected when that module is not
// running, and its tab says so. Anything else is a defect.
const offline = [];
if (!moduleTwoUp) offline.push("/agents");
if (!moduleThreeUp) offline.push("/orders");
if (!moduleFourUp) offline.push("/report");
if (!centralUp) offline.push("/central");
const unexpectedConsoleErrors = consoleErrors.filter(
  (entry) => !offline.some((prefix) => entry.url.includes(prefix)),
);

await browser.close();

console.log(
  JSON.stringify(
    {
      tabs,
      timeframeLabels,
      rows: { "15m": rows15, "5m": rows5, "1m": rows1, "1D": rowsDaily },
      heading15,
      filtered,
      orders: {
        heading: ordersHeading,
        banner: ordersBanner,
        resting: ordersResting,
        moduleThreeUp,
      },
      reporting: {
        heading: reportingHeading,
        banner: reportingBanner,
        moduleFourUp,
      },
      control: { heading: controlHeading, banner: controlBanner, centralUp },
      inspector: { inspectorTitle, bars15, barsDaily },
      consoleErrors,
      unexpectedConsoleErrors,
      problems,
    },
    null,
    2,
  ),
);

if (unexpectedConsoleErrors.length || problems.length) process.exit(1);

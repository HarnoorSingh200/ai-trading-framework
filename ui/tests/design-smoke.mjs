/**
 * Focused browser smoke test for the Design tab.
 *
 * This page is static and does not need the trading services. It verifies the
 * tab is reachable, every module has a message sequence and contract, SVG text
 * stays inside its canvas without overlaps, both themes render, and the page
 * has no horizontal overflow at narrow widths.
 *
 *   node tests/design-smoke.mjs [baseUrl] [outDir]
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

await mkdir(OUT, { recursive: true });
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });

const pageErrors = [];
page.on("pageerror", (error) => pageErrors.push(error.message));

await page.goto(BASE, { waitUntil: "domcontentloaded" });
await page.getByRole("button", { name: /Design/ }).click();
await page.waitForTimeout(300);

if ((await page.evaluate(() => document.documentElement.dataset.theme)) !== "light") {
  await page.locator("button.theme").click();
  await page.waitForTimeout(200);
}

const heading = (await page.locator("h1").first().innerText()).replace(/\s+/g, " ");
check(
  /how the framework moves a market fact/i.test(heading),
  `unexpected heading: ${heading}`,
);

const moduleHeadings = await page.locator("article.module h2").allInnerTexts();
check(moduleHeadings.length === 4, `expected 4 module sections, saw ${moduleHeadings.length}`);
const moduleNames = await page.locator(".module-index strong").allInnerTexts();
check(
  moduleNames.map((name) => name.toLowerCase()).join(",") ===
    "market data,agent engine,order manager,reporting",
  "the four module explanations are incomplete",
);

const sequences = await page.locator("article.module .sequence-shell svg").count();
check(sequences === 4, `expected 4 module sequences, saw ${sequences}`);
const systemMap = await page.locator(".system-map").count();
check(systemMap === 1, `expected one system map, saw ${systemMap}`);
const lifecycle = await page.locator(".lifecycle").count();
check(lifecycle === 1, `expected one order lifecycle, saw ${lifecycle}`);

const messageRows = await page.locator("article.module table tbody tr").count();
check(messageRows >= 16, `expected detailed message contracts, saw ${messageRows} rows`);
const latencyLedger = await page.locator(".metric-list code").allInnerTexts();
check(
  ["latency_ms", "ms", "price_age_seconds"].every((key) =>
    latencyLedger.includes(key),
  ),
  `latency ledger is incomplete: ${latencyLedger.join(", ")}`,
);

const svgGeometryProblems = await page.evaluate(() =>
  [...document.querySelectorAll("svg.system-map, article.module .sequence-shell svg, svg.lifecycle")]
    .flatMap((svg) => {
      const problemList = [];
      const canvas = svg.viewBox.baseVal;
      const labels = [...svg.querySelectorAll("text")].map((text) => ({
        text: text.textContent.trim(),
        box: text.getBBox(),
      }));
      for (const label of labels) {
        const { x, y, width, height } = label.box;
        if (
          x < -1 ||
          y < -1 ||
          x + width > canvas.width + 1 ||
          y + height > canvas.height + 1
        ) {
          problemList.push(`text outside canvas: ${label.text}`);
        }
      }
      for (let left = 0; left < labels.length; left += 1) {
        for (let right = left + 1; right < labels.length; right += 1) {
          const a = labels[left].box;
          const b = labels[right].box;
          const overlapX = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x);
          const overlapY = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y);
          if (overlapX > 1 && overlapY > 1) {
            problemList.push(
              `overlapping labels: ${labels[left].text} / ${labels[right].text}`,
            );
          }
        }
      }
      return problemList;
    }),
);
check(
  svgGeometryProblems.length === 0,
  `SVG geometry problems: ${svgGeometryProblems.join("; ")}`,
);

const pipelineLinks = await page.locator(".module-index a").count();
check(pipelineLinks === 4, `expected 4 overview links, saw ${pipelineLinks}`);

const lightBackground = await page.evaluate(
  () => getComputedStyle(document.body).backgroundColor,
);
const lightTheme = await page.evaluate(() => document.documentElement.dataset.theme);
check(lightTheme === "light", `could not reach the light theme (${lightTheme})`);
await page.screenshot({ path: `${OUT}/design-light.png`, fullPage: true });
await page.locator("button.theme").click();
await page.waitForTimeout(250);
const darkTheme = await page.evaluate(() => document.documentElement.dataset.theme);
const darkBackground = await page.evaluate(
  () => getComputedStyle(document.body).backgroundColor,
);
check(darkTheme === "dark", `theme toggle ended in ${darkTheme}`);
check(darkBackground !== lightBackground, "the dark palette did not change the page");

await page.screenshot({ path: `${OUT}/design-dark.png`, fullPage: true });

await page.setViewportSize({ width: 900, height: 1000 });
await page.waitForTimeout(250);
const overflow = await page.evaluate(
  () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
);
check(overflow <= 1, `horizontal overflow of ${overflow}px at 900px wide`);
await page.screenshot({ path: `${OUT}/design-narrow.png`, fullPage: true });

await page.setViewportSize({ width: 560, height: 900 });
await page.waitForTimeout(250);
const phoneOverflow = await page.evaluate(
  () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
);
check(phoneOverflow <= 1, `horizontal overflow of ${phoneOverflow}px at 560px wide`);
await page.screenshot({ path: `${OUT}/design-phone.png`, fullPage: true });

await browser.close();

console.log(
  JSON.stringify(
    {
      heading,
      moduleHeadings,
      moduleNames,
      sequences,
      systemMap,
      lifecycle,
      messageRows,
      svgGeometryProblems,
      pipelineLinks,
      lightTheme,
      theme: darkTheme,
      overflow,
      phoneOverflow,
      pageErrors,
      problems,
    },
    null,
    2,
  ),
);

if (pageErrors.length || problems.length) process.exit(1);

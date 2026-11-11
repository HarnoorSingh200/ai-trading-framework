<script>
  import { untrack } from "svelte";

  import { getCandles, getIndicators, getTimeframes } from "../lib/api.js";
  import { compact, percent, price, signed, tone } from "../lib/format.js";
  import { displayValue, fieldIndex, groupLabels, valueTone } from "../lib/indicatorView.js";
  import CandleChart from "./CandleChart.svelte";

  let { row, onclose, indicatorCatalogue = null, interval: openingInterval = "15m" } = $props();

  const LIMIT = 60;

  let catalogue = $state(null);
  // Captured once on purpose: after that the inspector owns its timeframe.
  let interval = $state(untrack(() => openingInterval));
  let series = $state(null);
  let frame = $state(null);
  let error = $state("");
  let loading = $state(true);
  // The inspector keeps its own timeframe once it is open; only switching to a
  // different stock re-adopts the one the board is showing.
  let openedFor = $state(untrack(() => row.symbol));

  const timeframes = $derived(catalogue?.timeframes ?? []);
  const fields = $derived(fieldIndex(indicatorCatalogue));
  const groupNames = $derived(groupLabels(indicatorCatalogue));
  const candles = $derived(series?.candles ?? []);
  const summary = $derived(series?.summary ?? {});

  function label(name) {
    return fields.get(name)?.label ?? name;
  }

  function render(name, value) {
    return displayValue(fields.get(name) ?? { unit: "number", decimals: 2 }, value);
  }

  function toneFor(name, value) {
    return valueTone(fields.get(name) ?? { unit: "number" }, value);
  }

  function pivotLabel(name) {
    return name === "pivot" ? "P" : name.toUpperCase();
  }

  function timeLabel(iso) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "—";
    if (interval === "1d") {
      return date.toLocaleDateString("en-IN", {
        day: "2-digit",
        month: "short",
        year: "2-digit",
      });
    }
    return date.toLocaleTimeString("en-IN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  }

  async function load() {
    loading = true;
    try {
      const [bars, indicators] = await Promise.all([
        getCandles(row.symbol, interval, LIMIT),
        getIndicators(row.symbol, interval).catch(() => null),
      ]);
      series = bars;
      frame = indicators;
      error = "";
    } catch (exc) {
      error = String(exc.message || exc);
      series = null;
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    // Runs once per symbol + timeframe selection.
    row.symbol;
    interval;
    load();
  });

  $effect(() => {
    if (row.symbol === openedFor) return;
    openedFor = row.symbol;
    interval = openingInterval;
  });

  $effect(() => {
    if (catalogue) return;
    getTimeframes()
      .then((payload) => {
        catalogue = payload;
        if (payload.default) interval = payload.default;
      })
      .catch(() => {
        // The switcher falls back to whatever is in the list below.
        catalogue = {
          default: "15m",
          timeframes: ["1m", "5m", "10m", "15m", "20m", "30m", "60m", "1d"].map(
            (value) => ({ value, label: value }),
          ),
        };
      });
  });
</script>

<aside aria-label={`${row.symbol} candles`}>
  <header>
    <div class="identity">
      <h2>{row.symbol}</h2>
      <p>{row.name} · {row.industry}</p>
    </div>
    <button class="btn close" onclick={onclose} aria-label="Close">Close</button>
  </header>

  <dl class="facts">
    <div>
      <dt class="label">Close</dt>
      <dd class="numeric strong">{price(row.close)}</dd>
    </div>
    <div>
      <dt class="label">Bar change</dt>
      <dd class="numeric {tone(row.change_percent)}">
        {signed(row.change)} ({percent(row.change_percent)})
      </dd>
    </div>
    <div>
      <dt class="label">Previous bar</dt>
      <dd class="numeric muted">{price(row.previous_close)}</dd>
    </div>
    <div>
      <dt class="label">Bar range</dt>
      <dd class="numeric muted">{price(row.low)} – {price(row.high)}</dd>
    </div>
    <div>
      <dt class="label">Bar volume</dt>
      <dd class="numeric muted">
        {compact(row.volume)}
        {#if row.volume_ratio !== null && row.volume_ratio !== undefined}
          <span class="muted">· {row.volume_ratio.toFixed(2)}× avg</span>
        {/if}
      </dd>
    </div>
    <div>
      <dt class="label">Day</dt>
      <dd class="numeric {tone(row.day_change_percent)}">
        {percent(row.day_change_percent)}
        <span class="muted">({price(row.day_low)} – {price(row.day_high)})</span>
      </dd>
    </div>
  </dl>

  <div class="switcher" role="group" aria-label="Timeframe">
    <span class="label">Timeframe</span>
    <div class="choices">
      {#each timeframes as option (option.value)}
        <button
          class:active={interval === option.value}
          aria-pressed={interval === option.value}
          onclick={() => (interval = option.value)}
        >
          {option.value === "1d" ? "1D" : option.value}
        </button>
      {/each}
    </div>
  </div>

  {#if error}
    <p class="error">{error}</p>
  {:else if loading && !candles.length}
    <p class="waiting">Loading {interval} candles…</p>
  {:else if series}
    <div class="summary">
      <span>
        <span class="label">Window</span>
        <span class="numeric {tone(summary.change_percent)}">
          {percent(summary.change_percent)}
        </span>
      </span>
      <span>
        <span class="label">High</span>
        <span class="numeric">{price(summary.high)}</span>
      </span>
      <span>
        <span class="label">Low</span>
        <span class="numeric">{price(summary.low)}</span>
      </span>
      <span>
        <span class="label">Volume</span>
        <span class="numeric">{compact(summary.volume)}</span>
      </span>
      <span>
        <span class="label">Up / down bars</span>
        <span class="numeric">
          <span class="up">{summary.up}</span> / <span class="down">{summary.down}</span>
        </span>
      </span>
    </div>

    <CandleChart {candles} {interval} />

    {#if frame?.day_levels}
      <section class="block">
        <h3>
          Levels
          <span class="muted">previous session {frame.day_levels.session_date ?? ""}</span>
        </h3>
        <dl class="ladder">
          <div>
            <dt class="label">Day high</dt>
            <dd class="numeric">{price(frame.day_levels.high)}</dd>
          </div>
          <div>
            <dt class="label">Day low</dt>
            <dd class="numeric">{price(frame.day_levels.low)}</dd>
          </div>
          <div>
            <dt class="label">Day close</dt>
            <dd class="numeric">{price(frame.day_levels.close)}</dd>
          </div>
          {#each Object.entries(frame.day_levels.pivots ?? {}) as [name, value] (name)}
            <div class:active={frame.nearest_level?.name === name}>
              <dt class="label">{pivotLabel(name)}</dt>
              <dd class="numeric">{price(value)}</dd>
            </div>
          {/each}
        </dl>
        {#if frame.nearest_level}
          <p class="note">
            Closest level is <strong>{pivotLabel(frame.nearest_level.name)}</strong> at
            {price(frame.nearest_level.value)} —
            {percent(frame.nearest_level.distance_pct)} away.
          </p>
        {/if}
      </section>
    {/if}

    {#if frame?.values}
      <section class="block">
        <h3>
          Indicators
          <span class="muted">
            {frame.indicator_version} · bar {frame.bar_open_time?.slice(11, 16) ?? "—"}
          </span>
        </h3>
        <div class="indicator-groups">
          {#each Object.entries(frame.groups ?? {}) as [key, values] (key)}
            <div class="indicator-group">
              <span class="label">{groupNames.get(key) ?? key}</span>
              <dl>
                {#each Object.entries(values) as [name, value] (name)}
                  <div>
                    <dt>{label(name)}</dt>
                    <dd class="numeric {toneFor(name, value)}">{render(name, value)}</dd>
                  </div>
                {/each}
              </dl>
            </div>
          {/each}
        </div>
      </section>
    {/if}

    <table>
      <thead>
        <tr>
          <th class="left">Time</th>
          <th>Open</th>
          <th>High</th>
          <th>Low</th>
          <th>Close</th>
          <th>Chg %</th>
          <th>Move %</th>
          <th>Volume</th>
          <th>Vol ×</th>
        </tr>
      </thead>
      <tbody>
        {#each [...candles].reverse() as candle (candle.open_time)}
          <tr class:forming={candle.is_forming} class:gappy={candle.is_gappy}>
            <th class="left">
              {timeLabel(candle.open_time)}
              {#if candle.is_forming}
                <span class="mark" title="This bar is still forming">*</span>
              {:else if candle.is_gappy}
                <span
                  class="mark"
                  title="Minutes are missing from the source for this bar"
                  >~</span
                >
              {/if}
            </th>
            <td class="numeric">{price(candle.open)}</td>
            <td class="numeric muted">{price(candle.high)}</td>
            <td class="numeric muted">{price(candle.low)}</td>
            <td class="numeric strong">{price(candle.close)}</td>
            <td class="numeric {tone(candle.change_percent)}">{percent(candle.change_percent)}</td>
            <td class="numeric {tone(candle.move_percent)}">{percent(candle.move_percent)}</td>
            <td class="numeric muted">{compact(candle.volume)}</td>
            <td class="numeric muted">
              {candle.volume_ratio === null ? "—" : candle.volume_ratio.toFixed(2)}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>

    <footer>
      <span>
        {candles.length} bars · {series.source} · {series.sessions} sessions held
      </span>
      <span>
        {#if interval === "1d"}
          Daily candles come from the broker and close with the session.
        {:else}
          Buckets align to the 09:15 IST session open. * is still forming; ~ is short of
          minutes.
        {/if}
      </span>
    </footer>
  {/if}
</aside>

<style>
  aside {
    position: fixed;
    top: 0;
    right: 0;
    bottom: 0;
    z-index: 30;
    width: min(880px, 94vw);
    padding: var(--space-4) var(--space-5) var(--space-5);
    background: var(--bg);
    border-left: 1px solid var(--line-2);
    box-shadow: var(--shadow-lg);
    overflow-y: auto;
  }

  header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-4);
  }

  h2 {
    margin: 0;
    font-size: var(--step-2);
    font-weight: 650;
    letter-spacing: -0.02em;
  }

  .identity p {
    margin: 3px 0 0;
    color: var(--muted);
    font-size: var(--step--1);
  }

  .facts {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-5);
    margin: var(--space-4) 0;
    padding: var(--space-3) 0;
    border-top: 1px solid var(--line);
    border-bottom: 1px solid var(--line);
  }

  dt {
    margin: 0 0 2px;
  }

  dd {
    margin: 0;
  }

  .strong {
    font-weight: 620;
  }

  .muted {
    color: var(--muted);
  }

  .up {
    color: var(--up);
  }

  .down {
    color: var(--down);
  }

  .switcher {
    display: flex;
    align-items: center;
    gap: var(--space-4);
    margin-bottom: var(--space-3);
  }

  .choices {
    display: flex;
    gap: 2px;
    padding: 2px;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
  }

  .choices button {
    padding: 5px 11px;
    background: none;
    border: 0;
    border-radius: 5px;
    color: var(--muted);
    font-weight: 550;
    font-variant-numeric: tabular-nums;
  }

  .choices button:hover {
    color: var(--ink);
  }

  .choices button.active {
    background: var(--bg);
    color: var(--ink);
    box-shadow: 0 1px 2px rgba(15, 19, 24, 0.09);
  }

  .summary {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-5);
    padding: var(--space-3) 0 var(--space-4);
  }

  .summary > span {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .waiting,
  .error {
    padding: var(--space-5) 0;
    color: var(--muted);
  }

  .error {
    color: var(--down);
  }

  .block {
    padding: var(--space-4) 0;
    border-top: 1px solid var(--line);
  }

  .block h3 {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--space-3);
    margin: 0 0 var(--space-3);
    font-size: var(--step-0);
    font-weight: 620;
    letter-spacing: -0.005em;
  }

  .block h3 .muted {
    font-size: var(--step--1);
    font-weight: 450;
  }

  .ladder {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    margin: 0;
  }

  .ladder > div {
    min-width: 74px;
    padding: 6px 10px;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
  }

  .ladder > div.active {
    border-color: var(--accent);
    background: var(--accent-soft);
  }

  .ladder dt {
    margin: 0 0 2px;
  }

  .ladder dd {
    margin: 0;
    font-weight: 600;
  }

  .note {
    margin: var(--space-3) 0 0;
    font-size: var(--step--1);
    color: var(--ink-2);
  }

  .indicator-groups {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
    gap: var(--space-3) var(--space-5);
  }

  .indicator-group dl {
    margin: var(--space-2) 0 0;
  }

  .indicator-group dl > div {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--space-3);
    padding: 3px 0;
    border-bottom: 1px solid var(--line);
  }

  .indicator-group dt {
    color: var(--muted);
    font-size: var(--step--1);
  }

  .indicator-group dd {
    margin: 0;
    font-weight: 550;
    white-space: nowrap;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    margin-top: var(--space-4);
  }

  thead th {
    padding: 0 8px 7px;
    text-align: right;
    font-size: var(--step--1);
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--muted);
    border-bottom: 1px solid var(--line-2);
    white-space: nowrap;
  }

  thead th.left {
    text-align: left;
    padding-left: 0;
  }

  tbody th {
    text-align: left;
    padding-left: 0;
    font-weight: 500;
    white-space: nowrap;
  }

  tbody td {
    text-align: right;
    white-space: nowrap;
  }

  tbody th,
  tbody td {
    padding: 6px 8px;
    border-bottom: 1px solid var(--line);
  }

  tbody tr:hover th,
  tbody tr:hover td {
    background: var(--surface);
  }

  tr.forming th,
  tr.forming td,
  tr.gappy th,
  tr.gappy td {
    color: var(--faint);
  }

  .mark {
    color: var(--warn);
  }

  footer {
    display: flex;
    flex-direction: column;
    gap: 3px;
    padding-top: var(--space-3);
    font-size: var(--step--1);
    color: var(--faint);
  }

  @media (max-width: 820px) {
    aside {
      width: 100vw;
      padding: var(--space-4);
    }
  }
</style>

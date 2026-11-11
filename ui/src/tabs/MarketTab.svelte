<script>
  import {
    getBoard,
    getIndicatorCatalogue,
    getStatus,
    getTimeframes,
    refresh,
  } from "../lib/api.js";
  import { age, clock, compact, percent, price, shortDate, signed, tone } from "../lib/format.js";
  import Sparkline from "../lib/Sparkline.svelte";
  import {
    displayValue,
    indicatorColumns,
    isFlag,
    loadVisibleGroups,
    saveVisibleGroups,
    valueTone,
  } from "../lib/indicatorView.js";
  import SymbolInspector from "../panels/SymbolInspector.svelte";

  const REFRESH_MS = 5000;
  //: How long to wait before rebuilding a stream the browser has given up on,
  //: and how often the board is polled while the stream is down.
  const STREAM_RETRY_MS = 4000;
  //: The board's slow heartbeat. The stream pushes only when a price *moves*,
  //: and a closed or quiet tape does not move: without this, a page opened
  //: while history was still loading would keep the partial answer it got
  //: first and never fill in.
  const BOARD_POLL_MS = 20000;

  const STATE_LABEL = { live: "live", broker: "exchange", mixed: "mixed" };

  let catalogue = $state(null);
  let indicatorCatalogue = $state(null);
  let visibleGroups = $state([]);
  let interval = $state("15m");
  let board = $state(null);
  let status = $state(null);
  let error = $state("");
  let busy = $state(false);
  let streaming = $state(false);
  let query = $state("");
  let sortKey = $state("symbol");
  let sortDir = $state("asc");
  let loadedAt = $state(null);
  let now = $state(Date.now());
  let selectedSymbol = $state(null);
  let revision = $state(0);

  const timeframes = $derived(catalogue?.timeframes ?? []);
  const groups = $derived(indicatorCatalogue?.groups ?? []);
  const columns = $derived(indicatorColumns(indicatorCatalogue, visibleGroups));
  const market = $derived(status?.board?.session_clock ?? {});
  const holidayNote = $derived(
    market.is_holiday
      ? `NSE holiday: ${market.holiday_name ?? "the exchange is shut"}`
      : market.holidays_problem
        ? `holiday list: ${market.holidays_problem}`
        : market.holidays_modelled
          ? "holiday list loaded"
          : "holiday list not loaded for this year",
  );
  const feed = $derived(status?.board?.feed ?? {});
  const session = $derived(status?.session ?? {});
  const rows = $derived(board?.rows ?? []);
  const summary = $derived(board?.summary ?? {});

  const selectedRow = $derived(
    selectedSymbol ? (rows.find((row) => row.symbol === selectedSymbol) ?? null) : null,
  );

  // The best and worst of the timeframe, which is what an operator looks for
  // first and would otherwise have to sort to find.
  const movers = $derived.by(() => {
    const scored = rows.filter(
      (row) => typeof row.indicators?.change_pct === "number",
    );
    if (scored.length < 2) return null;
    const ranked = [...scored].sort(
      (a, b) => b.indicators.change_pct - a.indicators.change_pct,
    );
    return { top: ranked[0], bottom: ranked[ranked.length - 1] };
  });

  // Bars behind the change numbers are scaled to the loudest move on screen, so
  // the column still reads as absolute size at a glance.
  const loudest = $derived(
    Math.max(0.01, ...rows.map((row) => Math.abs(row.indicators?.change_pct ?? 0))),
  );

  function barWidth(value) {
    if (typeof value !== "number") return 0;
    return Math.min(100, (Math.abs(value) / loudest) * 100);
  }

  const visible = $derived.by(() => {
    const needle = query.trim().toLowerCase();
    const filtered = needle
      ? rows.filter(
          (row) =>
            row.symbol.toLowerCase().includes(needle) ||
            (row.name || "").toLowerCase().includes(needle) ||
            (row.industry || "").toLowerCase().includes(needle),
        )
      : rows;
    const direction = sortDir === "asc" ? 1 : -1;
    return [...filtered].sort((a, b) => compare(a, b) * direction);
  });

  // Reading `now` is what makes the relative ages tick.
  const freshness = $derived.by(() => {
    void now;
    return {
      updated: loadedAt ? `updated ${age(loadedAt.toISOString())}` : "loading…",
      asOf: clock(board?.as_of),
    };
  });

  function compare(a, b) {
    if (sortKey === "symbol") return a.symbol.localeCompare(b.symbol);
    const left = cellValue(a, sortKey);
    const right = cellValue(b, sortKey);
    // A row with no value sorts last in either direction: unknown is not small.
    if (left === null || left === undefined) return 1;
    if (right === null || right === undefined) return -1;
    if (typeof left === "string" || typeof right === "string") {
      return String(left).localeCompare(String(right));
    }
    return left - right;
  }

  // A column key belongs either to the candle block or to the indicators.
  const CANDLE_KEYS = new Set(["open", "high", "low", "close", "move_percent", "volume"]);

  function cellValue(row, key) {
    if (CANDLE_KEYS.has(key)) return row[key];
    return row.indicators ? row.indicators[key] : null;
  }

  function toggleGroup(key) {
    visibleGroups = visibleGroups.includes(key)
      ? visibleGroups.filter((item) => item !== key)
      : [...visibleGroups, key];
    saveVisibleGroups(visibleGroups);
  }

  function sortBy(key) {
    if (sortKey === key) {
      sortDir = sortDir === "asc" ? "desc" : "asc";
      return;
    }
    sortKey = key;
    sortDir = key === "symbol" ? "asc" : "desc";
  }

  function marker(key) {
    if (sortKey !== key) return "";
    return sortDir === "asc" ? "↑" : "↓";
  }

  function barLabel(row) {
    if (interval === "1d") return shortDate(row.open_time.slice(0, 10));
    const stamp = new Date(row.open_time);
    return Number.isNaN(stamp.getTime())
      ? "—"
      : stamp.toLocaleTimeString("en-IN", {
          hour: "2-digit",
          minute: "2-digit",
          hour12: false,
        });
  }

  async function loadBoard() {
    try {
      board = await getBoard(interval);
      loadedAt = new Date();
      error = "";
    } catch (exc) {
      error = String(exc.message || exc);
    }
  }

  async function loadStatus() {
    try {
      status = await getStatus();
    } catch (exc) {
      error = String(exc.message || exc);
    }
  }

  async function refreshNow() {
    busy = true;
    try {
      await refresh();
      await new Promise((resolve) => setTimeout(resolve, 1500));
      await Promise.all([loadBoard(), loadStatus()]);
    } catch (exc) {
      error = String(exc.message || exc);
    } finally {
      busy = false;
    }
  }

  async function retryNow() {
    busy = true;
    try {
      await Promise.all([loadBoard(), loadStatus()]);
    } finally {
      busy = false;
    }
  }

  // One load per timeframe selection, and one per pushed update.
  $effect(() => {
    void interval;
    void revision;
    loadBoard();
  });

  $effect(() => {
    let stopped = false;
    const run = () => {
      if (!stopped) loadStatus();
    };
    run();
    const poll = setInterval(run, REFRESH_MS);
    const tick = setInterval(() => (now = Date.now()), 1000);

    // Prices are pushed as they change. The stream has to be rebuilt by hand
    // when it dies: a proxy that answers 500 while Module 1 is not up *fails*
    // an EventSource for good - the browser retries a dropped connection, not
    // a refused one - and the tab would otherwise stay on the error screen the
    // framework came up behind.
    let source = null;
    let reconnect = null;
    const connect = () => {
      if (stopped) return;
      source = new EventSource("/api/v1/market/stream");
      source.addEventListener("board", () => {
        streaming = true;
        revision += 1;
      });
      source.addEventListener("open", () => (streaming = true));
      source.addEventListener("error", () => {
        streaming = false;
        if (source.readyState === EventSource.CLOSED) {
          source.close();
          reconnect = setTimeout(connect, STREAM_RETRY_MS);
        }
      });
    };
    connect();

    return () => {
      stopped = true;
      clearInterval(poll);
      clearInterval(tick);
      if (reconnect) clearTimeout(reconnect);
      source?.close();
    };
  });

  // The board is polled whatever the stream is doing: quickly while the
  // stream is down (which is what brings the table back when the site was
  // opened before the framework - the situation the stream itself cannot
  // recover from), and on a slow heartbeat otherwise.
  $effect(() => {
    const every = streaming ? BOARD_POLL_MS : STREAM_RETRY_MS;
    const poll = setInterval(() => {
      loadBoard();
      loadStatus();
    }, every);
    return () => clearInterval(poll);
  });

  $effect(() => {
    if (catalogue) return;
    getTimeframes()
      .then((payload) => {
        catalogue = payload;
        if (payload.default) interval = payload.default;
      })
      .catch(() => {
        catalogue = {
          default: "15m",
          timeframes: ["1m", "5m", "10m", "15m", "20m", "30m", "60m", "1d"].map(
            (value) => ({ value, label: value }),
          ),
        };
      });
  });

  $effect(() => {
    if (indicatorCatalogue) return;
    getIndicatorCatalogue()
      .then((payload) => {
        indicatorCatalogue = payload;
        visibleGroups = loadVisibleGroups(payload);
      })
      .catch(() => {
        indicatorCatalogue = { groups: [] };
        visibleGroups = [];
      });
  });
</script>

<section class="head">
  <div>
    <h1>NIFTY 50 · <span class="interval-name">{board?.interval_label ?? "…"}</span></h1>
    <p class="sub">
      Every constituent's latest candle on one timeframe. Switching a timeframe is
      computed here, not fetched.
    </p>
  </div>
  <div class="actions">
    <span class="updated">{freshness.updated}</span>
    <button class="btn primary" onclick={refreshNow} disabled={busy}>
      {busy ? "Refreshing…" : "Refresh history"}
    </button>
  </div>
</section>

<dl class="strip">
  <div>
    <dt class="label">Market</dt>
    <dd>
      <span class="dot" class:ok={market.is_open}></span>
      {market.is_open ? "Open" : "Closed"}
      <span class="muted">{market.exchange_time_label ?? "—"}</span>
    </dd>
  </div>
  <div>
    <dt class="label">Price feed</dt>
    <dd>
      {#if feed.connected}
        <span class="dot ok"></span>{streaming ? "Streaming" : "Live"}
        <span class="muted numeric">{feed.ticks ?? 0} ticks</span>
      {:else}
        <span class="dot idle"></span>{feed.state ?? "not configured"}
      {/if}
      {#if feed.dropped}<span class="muted numeric">{feed.dropped} dropped</span>{/if}
    </dd>
  </div>
  <div>
    <dt class="label">Broker session</dt>
    <dd>
      {#if session.valid}
        <span class="dot ok"></span>{session.user_id}
        <span class="muted">auto sign-in</span>
      {:else}
        <span class="dot bad"></span>not signed in
      {/if}
    </dd>
  </div>
  <div>
    <dt class="label">Candles held</dt>
    <dd>
      <span class="dot ok"></span>
      <span class="numeric">{status?.candles?.book?.minutes_held?.toLocaleString("en-IN") ?? "—"}</span>
      <span class="muted">minutes · {status?.candles?.book?.built_minutes ?? 0} built live</span>
    </dd>
  </div>
</dl>

{#if error}
  {#if board === null}
    <div class="banner quiet">
      <strong>Waiting for the market data service…</strong>
      <span>
        {error}. This page retries by itself every few seconds, and the table
        appears as soon as Module 1 answers. If the framework is not running,
        start it with <code>start-module-1.cmd</code> in the v3 folder.
      </span>
      <button class="btn" onclick={retryNow}>Try now</button>
    </div>
  {:else}
    <div class="banner bad">
      <strong>Module 1 is not answering.</strong>
      <span>{error}</span>
      <button class="btn" onclick={retryNow}>Try again</button>
    </div>
  {/if}
{:else if market.is_holiday}
  <div class="banner quiet">
    <strong>NSE holiday: {market.holiday_name ?? "the exchange is shut"}.</strong>
    <span>
      The exchange has no session today. The newest bar is the last one the previous
      session produced; the next session opens at {market.session_open_ist} IST.
    </span>
  </div>
{:else if market.is_open === false}
  <div class="banner quiet">
    <strong>The market is closed.</strong>
    <span>
      The newest bar is the last one the session produced. The next session opens at
      {market.session_open_ist} IST.
    </span>
  </div>
{/if}

<div class="tfbar">
  <div class="segments" role="tablist" aria-label="Timeframe">
    {#each timeframes as option (option.value)}
      <button
        role="tab"
        class:active={interval === option.value}
        aria-selected={interval === option.value}
        onclick={() => (interval = option.value)}
        title={option.label}
      >
        {option.value === "1d" ? "1D" : option.value}
      </button>
    {/each}
  </div>
  <div class="tfmeta">
    {#if board}
      {#if movers}
        <span>
          <span class="label">Top</span>
          <span class="up">{movers.top.symbol} {percent(movers.top.change_percent)}</span>
        </span>
        <span>
          <span class="label">Bottom</span>
          <span class="down">{movers.bottom.symbol} {percent(movers.bottom.change_percent)}</span>
        </span>
      {/if}
      <span><span class="label">Advancing</span> <span class="up">{summary.advancing ?? 0}</span></span>
      <span><span class="label">Declining</span> <span class="down">{summary.declining ?? 0}</span></span>
      <span><span class="label">Volume</span> <span class="numeric">{compact(summary.volume)}</span></span>
    {/if}
  </div>
</div>

<div class="toolbar">
  <input
    class="search"
    type="search"
    placeholder="Filter by symbol, company or industry"
    bind:value={query}
    aria-label="Filter"
  />
  <span class="count numeric">{visible.length} of {board?.count ?? 0}</span>
</div>

{#if groups.length}
  <div class="groups" role="group" aria-label="Indicator columns">
    <span class="label">Columns</span>
    {#each groups as group (group.key)}
      <button
        class="group"
        class:on={visibleGroups.includes(group.key)}
        aria-pressed={visibleGroups.includes(group.key)}
        onclick={() => toggleGroup(group.key)}
      >
        {group.label}
        <span class="n numeric">
          {group.fields.filter((field) => field.surface === "table").length}
        </span>
      </button>
    {/each}
  </div>
{/if}

<div class="grid">
<table>
  <thead>
    <tr>
      <th class="left freeze" scope="col"><button onclick={() => sortBy("symbol")}>Symbol {marker("symbol")}</button></th>
      <th scope="col"><span class="plain">Trend</span></th>
      <th scope="col"><span class="plain">Bar</span></th>
      <th scope="col"><span class="plain">Open</span></th>
      <th scope="col"><span class="plain">High</span></th>
      <th scope="col"><span class="plain">Low</span></th>
      <th scope="col"><span class="plain">Close</span></th>
      <th scope="col"><button onclick={() => sortBy("change_pct")}>Chg % {marker("change_pct")}</button></th>
      <th scope="col"><button onclick={() => sortBy("move_percent")}>Move % {marker("move_percent")}</button></th>
      <th scope="col"><button onclick={() => sortBy("volume")}>Volume {marker("volume")}</button></th>
      {#each columns as field (field.name)}
        <th scope="col">
          <button onclick={() => sortBy(field.name)}>{field.label} {marker(field.name)}</button>
        </th>
      {/each}
      <th scope="col"><span class="plain">State</span></th>
    </tr>
  </thead>
  <tbody>
    {#if !board}
      {#each Array(12) as _, index (index)}
        <tr class="skeleton"><td colspan={11 + columns.length}></td></tr>
      {/each}
    {:else if visible.length === 0}
      <tr class="empty"><td colspan={11 + columns.length}>No symbol matches “{query}”.</td></tr>
    {:else}
      {#each visible as row (row.symbol)}
        <tr class:selected={selectedSymbol === row.symbol} onclick={() => (selectedSymbol = row.symbol)}>
          <th class="left freeze" scope="row">
            <button
              class="symbol"
              onclick={(event) => {
                event.stopPropagation();
                selectedSymbol = row.symbol;
              }}
            >
              {row.symbol}
            </button>
            <span class="name">{row.name}{row.industry ? ` · ${row.industry}` : ""}</span>
          </th>
          <td class="trend"><Sparkline values={row.spark} /></td>
          <td class="numeric muted">{barLabel(row)}</td>
          <td class="numeric">{price(row.open)}</td>
          <td class="numeric muted">{price(row.high)}</td>
          <td class="numeric muted">{price(row.low)}</td>
          <td class="numeric strong">{price(row.close)}</td>
          <td class="numeric bar-cell {tone(cellValue(row, "change_pct"))}">
            <span class="fill" style={`width:${barWidth(cellValue(row, "change_pct"))}%`}></span>
            <span class="value">{percent(cellValue(row, "change_pct"))}</span>
          </td>
          <td class="numeric {tone(row.move_percent)}">{percent(row.move_percent)}</td>
          <td class="numeric muted">{compact(row.volume)}</td>
          {#each columns as field (field.name)}
            <td class="numeric {valueTone(field, cellValue(row, field.name))}">
              {#if isFlag(field)}
                <span
                  class="flag"
                  class:on={cellValue(row, field.name) === true}
                  title={cellValue(row, field.name) === null ? "no history yet" : ""}
                ></span>
              {:else}
                {displayValue(field, cellValue(row, field.name))}
              {/if}
            </td>
          {/each}
          <td>
            <span class="state {row.source}">{STATE_LABEL[row.source] ?? row.source}</span>
            {#if row.is_forming}<span class="mark" title="This bar is still forming">*</span>{/if}
            {#if row.is_gappy}<span class="mark" title="Minutes are missing from this bar">~</span>{/if}
          </td>
        </tr>
      {/each}
    {/if}
  </tbody>
</table>
</div>

<footer>
  <span>
    {board?.alignment ?? ""} · snapshot {freshness.asOf} IST · {board?.derived
      ? "aggregated here"
      : "from the exchange"}
  </span>
  <span>* still forming · ~ short of minutes · {holidayNote}</span>
</footer>

{#if selectedRow}
  <SymbolInspector
    row={selectedRow}
    indicatorCatalogue={indicatorCatalogue}
    interval={interval}
    onclose={() => (selectedSymbol = null)}
  />
{/if}

<style>
  .head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: var(--space-5);
    padding-bottom: var(--space-4);
  }

  h1 {
    margin: 0;
    font-size: var(--step-3);
    font-weight: 640;
    letter-spacing: -0.022em;
    line-height: 1.1;
  }

  .interval-name {
    color: var(--muted);
    font-weight: 550;
  }

  .sub {
    margin: var(--space-2) 0 0;
    color: var(--muted);
    max-width: 74ch;
  }

  .actions {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    white-space: nowrap;
  }

  .updated {
    font-size: var(--step--1);
    color: var(--faint);
    font-variant-numeric: tabular-nums;
  }

  .strip {
    display: flex;
    flex-wrap: wrap;
    margin: 0 0 var(--space-4);
    padding: var(--space-3) 0;
    border-top: 1px solid var(--line);
    border-bottom: 1px solid var(--line);
  }

  .strip > div {
    padding-right: var(--space-5);
    margin-right: var(--space-5);
    border-right: 1px solid var(--line);
  }

  .strip > div:last-child {
    border-right: 0;
  }

  dt {
    margin: 0 0 3px;
  }

  dd {
    margin: 0;
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-weight: 550;
  }

  .dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--line-2);
    flex: none;
  }

  .dot.ok {
    background: var(--up);
  }

  .dot.idle {
    background: var(--warn);
  }

  .dot.bad {
    background: var(--down);
  }

  .muted {
    color: var(--muted);
    font-weight: 450;
  }

  .banner {
    display: flex;
    align-items: baseline;
    gap: var(--space-3);
    padding: var(--space-3) var(--space-4);
    margin-bottom: var(--space-4);
    border-radius: var(--radius);
  }

  .banner strong {
    font-weight: 620;
    white-space: nowrap;
  }

  .banner.quiet {
    background: var(--surface);
    border: 1px solid var(--line);
    color: var(--ink-2);
  }

  .banner.bad {
    background: var(--down-soft);
    border: 1px solid #f0c6c2;
    color: #7d1c16;
  }

  .banner button {
    margin-left: auto;
  }

  .banner code {
    padding: 1px 4px;
    background: var(--surface-2);
    border-radius: 4px;
    font-size: var(--step--1);
  }

  .tfbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-5);
    flex-wrap: wrap;
    margin-bottom: var(--space-4);
  }

  .segments {
    display: flex;
    gap: 2px;
    padding: 3px;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
  }

  .segments button {
    min-width: 50px;
    padding: 6px 12px;
    background: none;
    border: 0;
    border-radius: 6px;
    color: var(--muted);
    font-size: var(--step-0);
    font-weight: 560;
    font-variant-numeric: tabular-nums;
  }

  .segments button:hover {
    color: var(--ink);
  }

  .segments button.active {
    background: var(--bg);
    color: var(--ink);
    box-shadow: 0 1px 2px rgba(15, 19, 24, 0.1);
  }

  .tfmeta {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-4);
    font-size: var(--step--1);
  }

  .tfmeta > span {
    display: inline-flex;
    align-items: baseline;
    gap: var(--space-2);
  }

  .toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-4);
    margin-bottom: var(--space-3);
  }

  .search {
    width: 340px;
    max-width: 55vw;
    padding: 8px 11px;
    background: var(--bg);
    border: 1px solid var(--line-2);
    border-radius: var(--radius-sm);
  }

  .search::placeholder {
    color: var(--faint);
  }

  .count {
    font-size: var(--step--1);
    color: var(--muted);
  }

  /* Forty columns need their own scroll box: the header stays put, the symbol
     column stays put, and everything else moves under them. */
  .grid {
    max-height: calc(100vh - 250px);
    overflow: auto;
    border-bottom: 1px solid var(--line);
  }

  table {
    width: max-content;
    min-width: 100%;
    border-collapse: separate;
    border-spacing: 0;
  }

  thead th {
    position: sticky;
    top: 0;
    z-index: 5;
    padding: 9px 9px;
    text-align: right;
    border-bottom: 1px solid var(--line-2);
    white-space: nowrap;
    background: var(--bg);
  }

  th.freeze {
    position: sticky;
    left: 0;
    z-index: 3;
    background: var(--bg);
    box-shadow: 1px 0 0 var(--line);
  }

  thead th.freeze {
    z-index: 6;
  }

  tbody tr:hover th.freeze {
    background: var(--surface);
  }

  tbody tr.selected th.freeze {
    background: var(--accent-soft);
  }

  .groups {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-2);
    margin-bottom: var(--space-3);
  }

  .group {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 11px;
    background: var(--bg);
    border: 1px solid var(--line-2);
    border-radius: 999px;
    color: var(--muted);
    font-size: var(--step--1);
    font-weight: 550;
  }

  .group:hover {
    color: var(--ink);
  }

  .group.on {
    background: var(--accent-soft);
    border-color: var(--accent);
    color: var(--ink);
  }

  .group .n {
    font-size: 10px;
    color: var(--faint);
  }

  .flag {
    display: inline-block;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--line-2);
    vertical-align: middle;
  }

  .flag.on {
    background: var(--up);
  }

  thead th.left {
    text-align: left;
    padding-left: 0;
  }

  thead button,
  thead .plain {
    background: none;
    border: 0;
    padding: 0;
    font-size: var(--step--1);
    font-weight: 600;
    letter-spacing: 0.07em;
    text-transform: uppercase;
    color: var(--muted);
  }

  thead button:hover {
    color: var(--ink);
  }

  tbody tr {
    cursor: pointer;
  }

  tbody th {
    text-align: left;
    padding-left: 0;
    font-weight: 500;
  }

  tbody td {
    text-align: right;
    white-space: nowrap;
  }

  tbody th,
  tbody td {
    padding: 7px 9px;
    border-bottom: 1px solid var(--line);
    vertical-align: middle;
  }

  /* A row is easier to read as a trend than as a number. */
  .trend {
    width: 98px;
    padding-right: 0;
    text-align: right;
    line-height: 0;
  }

  /* The change column carries its own magnitude, so the eye can scan the
     column instead of reading every value. */
  .bar-cell {
    position: relative;
  }

  .bar-cell .fill {
    position: absolute;
    right: 9px;
    top: 50%;
    transform: translateY(-50%);
    height: 15px;
    border-radius: 2px;
    background: currentColor;
    opacity: 0.15;
    pointer-events: none;
  }

  .bar-cell .value {
    position: relative;
  }

  tbody tr:hover .bar-cell .fill {
    opacity: 0.24;
  }

  tbody tr:hover th,
  tbody tr:hover td {
    background: var(--surface);
  }

  tbody tr.selected th,
  tbody tr.selected td {
    background: var(--accent-soft);
  }

  tbody tr.selected th {
    box-shadow: inset 2px 0 0 var(--accent);
  }

  .symbol {
    display: block;
    padding: 0;
    background: none;
    border: 0;
    color: var(--ink);
    font-size: var(--step-0);
    font-weight: 620;
    text-align: left;
  }

  .symbol:hover {
    color: var(--accent);
    text-decoration: underline;
    text-underline-offset: 2px;
  }

  .name {
    display: block;
    font-size: var(--step--1);
    color: var(--muted);
    font-weight: 450;
  }

  .strong {
    font-weight: 600;
  }

  .state {
    font-size: var(--step--1);
    font-weight: 550;
    color: var(--muted);
  }

  .state.live {
    color: var(--up);
  }

  .state.mixed {
    color: var(--warn);
  }

  .mark {
    margin-left: 3px;
    color: var(--warn);
  }

  .skeleton td {
    height: 46px;
    border-bottom: 1px solid var(--line);
    background: linear-gradient(
      90deg,
      var(--surface) 0%,
      var(--surface-2) 50%,
      var(--surface) 100%
    );
    background-size: 240px 100%;
    animation: sweep 1.4s linear infinite;
  }

  @keyframes sweep {
    from {
      background-position: -240px 0;
    }
    to {
      background-position: 240px 0;
    }
  }

  .empty td {
    padding: var(--space-5) 0;
    text-align: center;
    color: var(--muted);
    border-bottom: 1px solid var(--line);
  }

  footer {
    display: flex;
    justify-content: space-between;
    gap: var(--space-4);
    padding-top: var(--space-3);
    font-size: var(--step--1);
    color: var(--faint);
  }

  @media (max-width: 900px) {
    .head {
      flex-direction: column;
      align-items: flex-start;
    }

    footer {
      flex-direction: column;
    }
  }
</style>

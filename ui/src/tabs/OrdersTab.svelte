<script>
  /**
   * The Order manager tab.
   *
   * Module 2 decides; this is what the framework did with the decision. The
   * four things an operator asks about a trade are the four things this page
   * answers at a glance - was it executed, did it reach its target, did it hit
   * its stop, or did it simply run out of the time it was given - and every
   * number here is the order's own record rather than a re-estimate of it.
   *
   * Nothing on this page can place an order. The module behind it has no
   * connection to the broker at all, which is why a row can say what it would
   * have made without that ever having been at risk.
   */

  import {
    getOrderBookStatus,
    listOrders,
    orderStreamUrl,
    reconcileOrders,
    restingReport,
  } from "../lib/api.js";
  import { clock, price, signed, tone } from "../lib/format.js";

  const POLL_MS = 5000;

  const FILTERS = [
    { key: "all", label: "All" },
    { key: "open", label: "Open" },
    { key: "pending", label: "Pending" },
    { key: "closed", label: "Closed" },
    { key: "expired", label: "Expired" },
  ];

  const EXIT_LABEL = {
    target: "reached its target",
    stop_loss: "hit its stop",
    timeout: "ran out of time",
  };

  let orders = $state([]);
  let stats = $state({});
  let status = $state(null);
  let error = $state("");
  //: Set when Module 3 is down *because the central process is resting it for
  //: the market*: the tab says so instead of calling it a failure.
  let resting = $state(null);
  let busy = $state(false);
  let streaming = $state(false);
  let filter = $state("all");
  let revision = $state(0);

  const visible = $derived(
    filter === "all" ? orders : orders.filter((order) => order.status === filter),
  );
  const byStatus = $derived(stats.by_status ?? {});
  const realised = $derived(
    orders.reduce((total, order) => total + (order.pnl?.gross ?? 0), 0),
  );
  const unrealised = $derived(
    orders.reduce((total, order) => total + (order.unrealised_pnl ?? 0), 0),
  );
  const feed = $derived(status?.price_stream ?? {});
  const watched = $derived(stats.watching ?? 0);
  const quotes = $derived(status?.prices ?? []);

  function held(order) {
    if (order.status === "open" && order.deadline_seconds !== null) {
      return `${Math.max(0, Math.round(order.deadline_seconds / 60))}m left`;
    }
    if (order.filled_at && order.ended_at) {
      const minutes = Math.round(
        (new Date(order.ended_at) - new Date(order.filled_at)) / 60000,
      );
      return `${minutes}m held`;
    }
    return "—";
  }

  function money(order) {
    if (order.status === "open") return order.unrealised_pnl;
    return order.pnl?.gross ?? null;
  }

  function rMultiple(order) {
    const value = order.pnl?.r_multiple;
    if (value === null || value === undefined) return "";
    return `${value > 0 ? "+" : ""}${value.toFixed(2)}R`;
  }

  function entryNote(order) {
    if (order.decision_entry_price === null || order.decision_entry_price === undefined) {
      return "";
    }
    if (order.decision_entry_price === order.entry_price) {
      return "entered at the price the plan was built on";
    }
    return `the plan was built at ${price(order.decision_entry_price)}`;
  }

  function statusNote(order) {
    if (order.exit_reason) return order.exit_note ?? "";
    if (order.status === "open") return "the position is open and being watched";
    if (order.status === "pending") return "waiting for Module 1 to confirm a live price";
    return "";
  }

  async function load() {
    try {
      const payload = await listOrders();
      orders = payload.orders ?? [];
      stats = payload.stats ?? {};
      error = "";
      resting = null;
    } catch (exc) {
      error = String(exc.message || exc);
      orders = [];
      resting = await restingReport("module3");
    }
  }

  async function loadStatus() {
    try {
      status = await getOrderBookStatus();
    } catch {
      status = null;
    }
  }

  async function reconcile() {
    busy = true;
    try {
      await reconcileOrders();
      await load();
      error = "";
    } catch (exc) {
      error = String(exc.message || exc);
    } finally {
      busy = false;
    }
  }

  // One load per change, whether it was pushed or noticed by the poll.
  $effect(() => {
    void revision;
    load();
  });

  $effect(() => {
    let stopped = false;
    const run = () => {
      if (!stopped) loadStatus();
    };
    run();
    const poll = setInterval(run, POLL_MS);
    const tick = setInterval(() => (revision += 1), POLL_MS);

    // Every order change is pushed as it is written, so a position that closes
    // appears here as it closes rather than at the next poll. A refused stream
    // (the proxy answers 500 while Module 3 is down) is dead for good as far
    // as the browser is concerned, so the tab rebuilds it.
    let source = null;
    let reconnect = null;
    const connect = () => {
      if (stopped) return;
      source = new EventSource(orderStreamUrl());
      source.addEventListener("order", () => {
        streaming = true;
        revision += 1;
      });
      source.addEventListener("open", () => (streaming = true));
      source.addEventListener("error", () => {
        streaming = false;
        if (source.readyState === EventSource.CLOSED) {
          source.close();
          reconnect = setTimeout(connect, POLL_MS);
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
</script>

<section class="head">
  <div>
    <h1>Order manager</h1>
    <p class="sub">
      Every decision Module 2 handed over, tracked against the price of its own
      stock until a target, a stop or the clock ended it. Nothing here reaches the
      broker: an order is a position the framework holds, not one it placed.
    </p>
  </div>
  <div class="actions">
    <span class="stream">{streaming ? "live" : "polling"}</span>
    <button class="btn primary" onclick={reconcile} disabled={busy}>
      {busy ? "Reconciling…" : "Reconcile"}
    </button>
  </div>
</section>

{#if error}
  {#if resting}
    <div class="banner calm">
      <strong>{resting.headline}</strong>
      <span>{resting.note}.</span>
      <button class="btn" onclick={load}>Check again</button>
    </div>
  {:else}
    <div class="banner bad">
      <strong>Module 3 is not answering.</strong>
      <span>{error}</span>
      <button class="btn" onclick={load}>Try again</button>
    </div>
  {/if}
{/if}

<dl class="strip">
  <div>
    <dt class="label">Open</dt>
    <dd class="numeric">{byStatus.open ?? 0}</dd>
  </div>
  <div>
    <dt class="label">Waiting for a price</dt>
    <dd class="numeric">{byStatus.pending ?? 0}</dd>
  </div>
  <div>
    <dt class="label">Closed</dt>
    <dd class="numeric">{byStatus.closed ?? 0}</dd>
  </div>
  <div>
    <dt class="label">Never filled</dt>
    <dd class="numeric">{byStatus.expired ?? 0}</dd>
  </div>
  <div>
    <dt class="label">Realised</dt>
    <dd class="numeric {tone(realised)}">{signed(realised)}</dd>
  </div>
  <div>
    <dt class="label">Open P&amp;L</dt>
    <dd class="numeric {tone(unrealised)}">{signed(unrealised)}</dd>
  </div>
  <div>
    <dt class="label">Price feed</dt>
    <dd>
      {#if feed.connected}
        <span class="dot ok"></span>watching {watched}
      {:else}
        <span class="dot idle"></span>{feed.state ?? "not configured"}
      {/if}
    </dd>
  </div>
</dl>

{#if quotes.length}
  <div class="quotes" aria-label="Prices being received from Module 1">
    <span class="label">Live prices</span>
    {#each quotes as quote (quote.symbol)}
      <span class="quote">
        <strong>{quote.symbol}</strong>
        <span class="numeric">{price(quote.price)}</span>
        <span class="muted">
          {#if quote.age_seconds === null}
            {quote.source}
          {:else}
            {quote.age_seconds}s ago
          {/if}
        </span>
      </span>
    {/each}
  </div>
{/if}

<div class="toolbar">
  <div class="filters" role="group" aria-label="Filter by status">
    {#each FILTERS as option (option.key)}
      <button
        class="filter"
        class:on={filter === option.key}
        aria-pressed={filter === option.key}
        onclick={() => (filter = option.key)}
      >
        {option.label}
        <span class="n numeric">
          {option.key === "all" ? orders.length : (byStatus[option.key] ?? 0)}
        </span>
      </button>
    {/each}
  </div>
  <span class="count numeric">{visible.length} shown</span>
</div>

<div class="grid">
  <table>
    <thead>
      <tr>
        <th class="left">Order</th>
        <th>Side</th>
        <th>Qty</th>
        <th>Entry</th>
        <th>Target</th>
        <th>Stop</th>
        <th>Last</th>
        <th>Hold</th>
        <th>P&amp;L</th>
        <th class="left">Status</th>
        <th class="left">Why</th>
      </tr>
    </thead>
    <tbody>
      {#if !orders.length && !error}
        <tr class="empty"
          ><td colspan="11">
            No orders yet. A decision Module 2 makes becomes one here.
          </td></tr
        >
      {:else if !visible.length}
        <tr class="empty"><td colspan="11">Nothing is {filter}.</td></tr>
      {:else}
        {#each visible as order (order.order_id)}
          <tr class:closed={order.status === "closed"}>
            <th class="left">
              <span class="symbol">{order.symbol}</span>
              <span class="meta">
                {order.agent_id} · {order.timeframe ?? "—"} · {clock(order.received_at)}
              </span>
            </th>
            <td class={order.side === "buy" ? "up" : "down"}>{order.side}</td>
            <td class="numeric">{order.quantity}</td>
            <td class="numeric strong" title={entryNote(order)}>
              {price(order.entry_price)}
            </td>
            <td class="numeric">{price(order.target)}</td>
            <td class="numeric">{price(order.stop_loss)}</td>
            <td class="numeric">
              {#if order.last_price === null || order.last_price === undefined}
                <span class="muted">—</span>
              {:else}
                {price(order.last_price)}
                {#if order.last_price_source !== "live"}
                  <span class="muted" title="a stored price, not a live tick">
                    {order.last_price_source}
                  </span>
                {/if}
              {/if}
            </td>
            <td class="numeric muted">{held(order)}</td>
            <td class="numeric {tone(money(order))}">
              {signed(money(order))}
              {#if order.pnl}
                <span class="muted r" title="the result in the risk the plan carried">
                  {rMultiple(order)}
                </span>
              {/if}
            </td>
            <td class="left">
              <span class="state {order.status}">{order.status}</span>
              {#if order.exit_reason}
                <span class="exit" title={statusNote(order)}
                  >{EXIT_LABEL[order.exit_reason] ?? order.exit_reason}</span
                >
              {/if}
              {#if order.ambiguous_exit}
                <span class="mark" title={statusNote(order)}>~</span>
              {/if}
            </td>
            <td class="left why" title={order.reason}>{order.reason}</td>
          </tr>
        {/each}
      {/if}
    </tbody>
  </table>
</div>

<footer>
  <span>
    An order is executed at the first live price Module 1 publishes for its stock,
    and settles its own P&amp;L when it ends.
  </span>
  <span>* a move that crossed the target and the stop at once is booked against the position</span>
</footer>

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

  .sub {
    margin: var(--space-2) 0 0;
    color: var(--muted);
    max-width: 80ch;
  }

  .actions {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    white-space: nowrap;
  }

  .stream {
    font-size: var(--step--1);
    font-weight: 600;
    letter-spacing: 0.07em;
    text-transform: uppercase;
    color: var(--muted);
  }

  .banner {
    display: flex;
    align-items: baseline;
    gap: var(--space-3);
    padding: var(--space-3) var(--space-4);
    margin-bottom: var(--space-4);
    border-radius: var(--radius);
  }

  .banner.bad {
    background: var(--down-soft);
    border: 1px solid #f0c6c2;
    color: #7d1c16;
  }

  /* A service resting because the market is closed is the framework working,
     not failing; it gets the quiet treatment. */
  .banner.calm {
    background: var(--surface-2);
    border: 1px solid var(--line-2);
    color: var(--ink-2);
  }

  .banner button {
    margin-left: auto;
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

  .toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-4);
    margin-bottom: var(--space-3);
  }

  /* The prices actually arriving, one per watched stock: the feed is only
     believable if you can watch what it is being told. */
  .quotes {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-2) var(--space-4);
    padding: var(--space-2) 0 var(--space-3);
    border-bottom: 1px solid var(--line);
    margin-bottom: var(--space-3);
  }

  .quote {
    display: inline-flex;
    align-items: baseline;
    gap: var(--space-2);
    font-size: var(--step--1);
  }

  .quote strong {
    font-weight: 620;
    color: var(--ink);
  }

  .quote .muted {
    font-size: 10px;
  }

  .filters {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .filter {
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

  .filter:hover {
    color: var(--ink);
  }

  .filter.on {
    background: var(--accent-soft);
    border-color: var(--accent);
    color: var(--ink);
  }

  .filter .n {
    font-size: 10px;
    color: var(--faint);
  }

  .count {
    font-size: var(--step--1);
    color: var(--muted);
  }

  .grid {
    overflow-x: auto;
    border-bottom: 1px solid var(--line);
  }

  table {
    width: 100%;
    min-width: 1080px;
    border-collapse: collapse;
  }

  thead th {
    position: sticky;
    top: 0;
    z-index: 5;
    padding: 9px;
    text-align: right;
    border-bottom: 1px solid var(--line-2);
    white-space: nowrap;
    background: var(--bg);
    font-size: var(--step--1);
    font-weight: 600;
    letter-spacing: 0.07em;
    text-transform: uppercase;
    color: var(--muted);
  }

  thead th.left,
  tbody th.left,
  td.left {
    text-align: left;
  }

  tbody th,
  tbody td {
    padding: 7px 9px;
    border-bottom: 1px solid var(--line);
    vertical-align: top;
    white-space: nowrap;
  }

  tbody th {
    padding-left: 0;
    font-weight: 500;
  }

  tbody td {
    text-align: right;
  }

  tbody tr:hover th,
  tbody tr:hover td {
    background: var(--surface);
  }

  tr.closed {
    color: var(--ink-2);
  }

  .symbol {
    display: block;
    font-weight: 620;
    color: var(--ink);
  }

  .meta {
    display: block;
    font-size: var(--step--1);
    color: var(--muted);
    font-weight: 450;
  }

  .strong {
    font-weight: 600;
  }

  .muted {
    color: var(--muted);
    font-weight: 450;
  }

  .r {
    display: block;
    font-size: var(--step--1);
  }

  .state {
    display: inline-block;
    font-size: var(--step--1);
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--muted);
  }

  .state.open {
    color: var(--accent);
  }

  .state.closed {
    color: var(--ink-2);
  }

  .state.expired {
    color: var(--warn);
  }

  .exit {
    display: block;
    font-size: var(--step--1);
    color: var(--muted);
  }

  .mark {
    margin-left: 3px;
    color: var(--warn);
    cursor: help;
  }

  .why {
    max-width: 320px;
    overflow: hidden;
    text-overflow: ellipsis;
    font-size: var(--step--1);
    color: var(--ink-2);
  }

  .empty td {
    padding: var(--space-5) 0;
    text-align: center;
    color: var(--muted);
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

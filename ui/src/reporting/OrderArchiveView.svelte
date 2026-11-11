<script>
  /**
   * Exact archived order records. This is a record of what Module 3 settled,
   * not a live book and not an account statement.
   */

  import { duration, price, signedRupees, tone } from "../lib/format.js";
  import {
    exitTone,
    holdDuration,
    shortDay,
    tradeMoment,
  } from "../lib/reporting.js";

  let {
    orders = [],
    selectedAgent = "",
    selectedDay = "",
  } = $props();

  const FILTERS = ["all", "open", "closed", "expired"];
  let filter = $state("all");
  let symbolQuery = $state("");

  const counts = $derived.by(() => ({
    all: orders.length,
    open: orders.filter((row) => row.status === "open").length,
    closed: orders.filter((row) => row.status === "closed").length,
    expired: orders.filter((row) => row.status === "expired").length,
  }));

  const visibleOrders = $derived.by(() => {
    const needle = symbolQuery.trim().toUpperCase();
    return orders.filter(
      (row) =>
        (filter === "all" || row.status === filter) &&
        (!needle ||
          String(row.symbol ?? "").toUpperCase().includes(needle)),
    );
  });

  function rMultiple(row) {
    const value = row.pnl?.r_multiple;
    if (value === null || value === undefined) return "—";
    return `${Number(value) > 0 ? "+" : ""}${Number(value).toFixed(2)}R`;
  }

  function money(row) {
    return row.pnl?.gross ?? null;
  }
</script>

<div class="report-view">
  <section class="report-panel">
    <header class="report-panel-head">
      <div>
        <span class="report-kicker">Exact records</span>
        <h2>Order archive</h2>
        <p>
          Newest first. This is the long record mirrored from Module 3, not the
          live order book. It shows up to 500 records for the selected agent
          and day scope.
        </p>
      </div>
      <span class="report-note">
        {visibleOrders.length} of {orders.length}
        {selectedAgent ? ` · ${selectedAgent}` : ""}
        {selectedDay ? ` · ${shortDay(selectedDay)}` : ""}
      </span>
    </header>

    <div class="report-tools">
      <div class="report-filters" role="group" aria-label="Filter by status">
        {#each FILTERS as option (option)}
          <button
            class="report-filter"
            class:on={filter === option}
            aria-pressed={filter === option}
            onclick={() => (filter = option)}
          >
            {option}
            <span class="report-count">{counts[option]}</span>
          </button>
        {/each}
      </div>
      <label>
        <span class="sr-only">Filter archived orders by symbol</span>
        <input
          class="report-search"
          bind:value={symbolQuery}
          placeholder="Filter symbol"
          autocomplete="off"
        />
      </label>
    </div>

    {#if !visibleOrders.length}
      <p class="report-empty-inline">
        {orders.length
          ? "No archived order matches this filter."
          : "No orders have been archived in this scope yet."}
      </p>
    {:else}
      <div class="report-scroll report-book-scroll">
        <table class="report-table">
          <thead>
            <tr>
              <th class="left">Day</th>
              <th class="left">Settled</th>
              <th class="left">Agent</th>
              <th class="left">Symbol</th>
              <th>Side</th>
              <th>Qty</th>
              <th>Entry</th>
              <th>Exit</th>
              <th>Hold</th>
              <th>P&amp;L</th>
              <th>R</th>
              <th class="left">How it ended</th>
            </tr>
          </thead>
          <tbody>
            {#each visibleOrders as row (row.order_id)}
              <tr>
                <td class="left report-muted">{shortDay(row.session_date)}</td>
                <td class="left report-muted numeric">{tradeMoment(row)}</td>
                <td class="left report-muted">{row.agent_id}</td>
                <th class="left">{row.symbol}</th>
                <td class={row.side === "buy" ? "up" : "down"}>{row.side}</td>
                <td class="numeric">{row.quantity}</td>
                <td class="numeric">{price(row.entry_price)}</td>
                <td class="numeric">
                  {row.exit_price ? price(row.exit_price) : "—"}
                </td>
                <td class="numeric">{duration(holdDuration(row))}</td>
                <td class="numeric {tone(money(row))}">
                  {signedRupees(money(row))}
                </td>
                <td class="numeric {tone(row.pnl?.r_multiple)}">
                  {rMultiple(row)}
                </td>
                <td class="left report-muted">
                  <span class={exitTone(row.exit_reason)}>{row.status}</span>
                  {row.exit_reason
                    ? ` · ${row.exit_reason.replaceAll("_", " ")}`
                    : ""}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </section>
</div>

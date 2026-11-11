<script>
  /**
   * Coverage is the honesty layer of Reporting. It shows which exchange days
   * have orders, which have mirrored AI usage, which have cycle timing, and
   * where the archive or benchmark has not caught up.
   */

  import { age, signedRupees } from "../lib/format.js";
  import { shortDay } from "../lib/reporting.js";

  let {
    analytics,
    usageDays = [],
    orders = [],
    status = null,
    benchmark = null,
    selectedAgent = "",
    selectedDay = "",
    onSelectDay = () => {},
  } = $props();

  const archiveStatus = $derived(status?.archive ?? {});
  const archive = $derived(archiveStatus.archive ?? {});
  const usageStatus = $derived(status?.usage ?? {});
  const benchmarkStatus = $derived(status?.benchmark ?? {});

  const performanceDays = $derived(
    new Map((analytics?.days ?? []).map((row) => [row.day, row])),
  );
  const usageByDay = $derived(
    new Map(
      usageDays.map((row) => [
        row.day,
        scopedUsage(row),
      ]),
    ),
  );
  const coverageRows = $derived.by(() => {
    const allDays = new Set([
      ...performanceDays.keys(),
      ...usageByDay.keys(),
      ...orders.map((row) => row.session_date).filter(Boolean),
    ]);
    return [...allDays]
      .sort((left, right) => String(right).localeCompare(String(left)))
      .map((day) => {
        const performance = performanceDays.get(day) ?? null;
        const usage = usageByDay.get(day) ?? null;
        return {
          day,
          performance,
          usage,
          cycleRows: (usage?.agents ?? []).reduce(
            (total, row) => total + (row.cycle_rows?.length ?? 0),
            0,
          ),
          measuredCycles: Number(usage?.timed_cycles ?? 0),
        };
      });
  });

  const orderCounts = $derived.by(() => ({
    all: orders.length,
    open: orders.filter((row) => row.status === "open").length,
    closed: orders.filter((row) => row.status === "closed").length,
    expired: orders.filter((row) => row.status === "expired").length,
  }));

  function scopedUsage(day) {
    const rows = selectedAgent
      ? (day.agents ?? []).filter((row) => row.agent_id === selectedAgent)
      : day.agents ?? [];
    const totals = {
      cycles: 0,
      model_calls: 0,
      tool_calls: 0,
      decisions: 0,
      total_tokens: 0,
      timed_cycles: 0,
      cycle_time_ms: 0,
      agents: rows,
    };
    for (const row of rows) {
      for (const field of [
        "cycles",
        "model_calls",
        "tool_calls",
        "decisions",
        "total_tokens",
        "timed_cycles",
        "cycle_time_ms",
      ]) {
        totals[field] += Number(row[field] || 0);
      }
    }
    return totals;
  }

  function tokenCount(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    if (number >= 1_000_000) return `${(number / 1_000_000).toFixed(1)}M`;
    if (number >= 1_000) return `${(number / 1_000).toFixed(1)}k`;
    return String(Math.round(number));
  }

  function elapsed(ms) {
    const number = Number(ms);
    if (!Number.isFinite(number)) return "—";
    if (number >= 60_000) return `${(number / 60_000).toFixed(1)}m`;
    if (number >= 1_000) return `${(number / 1_000).toFixed(1)}s`;
    return `${Math.round(number)}ms`;
  }

  function benchmarkRange() {
    const candles = benchmark?.candles ?? [];
    if (!candles.length) return "not mirrored";
    const first = candles[0]?.date;
    const last = candles.at(-1)?.date;
    return first && last ? `${shortDay(first)} – ${shortDay(last)}` : `${candles.length} closes`;
  }
</script>

<div class="report-view">
  <section class="report-panel">
    <header class="report-panel-head">
      <div>
        <span class="report-kicker">Data provenance</span>
        <h2>What is covered, and what is missing</h2>
        <p>
          Reporting combines settled orders, mirrored AI usage and a market
          benchmark. Each has its own source and freshness; a missing row is
          shown as missing rather than filled with zero.
        </p>
      </div>
      <span class="report-note">
        {coverageRows.length} exchange day{coverageRows.length === 1 ? "" : "s"} in scope
      </span>
    </header>

    <dl class="report-status-strip">
      <div>
        <dt>Archive write</dt>
        <dd>{archive.written_at ? age(archive.written_at) : "not written"}</dd>
        <span>{archive.count ?? 0} durable orders</span>
      </div>
      <div>
        <dt>Archive sync</dt>
        <dd>{archiveStatus.synced_at ? age(archiveStatus.synced_at) : "not synced"}</dd>
        <span>{status?.last_error ? "last error present" : "no archive error"}</span>
      </div>
      <div>
        <dt>AI usage mirror</dt>
        <dd>{usageStatus.updated_at ? age(usageStatus.updated_at) : "not read"}</dd>
        <span>
          {usageStatus.last_error
            ? `last read: ${usageStatus.last_error}`
            : `${usageDays.length} usage day${usageDays.length === 1 ? "" : "s"} retained`}
        </span>
      </div>
      <div>
        <dt>Benchmark</dt>
        <dd>{benchmarkRange()}</dd>
        <span>
          {benchmark?.symbol ?? "NIFTY 50"} ·
          {benchmarkStatus.last_error ? "mirror error" : "daily closes"}
        </span>
      </div>
      <div>
        <dt>Order outcomes</dt>
        <dd>{orderCounts.closed} settled</dd>
        <span>
          {orderCounts.open} open · {orderCounts.expired} expired
        </span>
      </div>
    </dl>
  </section>

  <div class="report-grid two">
    <section class="report-panel">
      <header class="report-panel-head compact">
        <div>
          <span class="report-kicker">Day availability</span>
          <h2>Which records exist for each day</h2>
          <p>
            Select a day to move every view to that scope. A day can have AI
            cycles without a trade, or a trade without cycle timing if it was
            archived before cycle-level instrumentation existed.
          </p>
        </div>
      </header>

      {#if coverageRows.length}
        <div class="report-scroll coverage-table">
          <table class="report-table">
            <thead>
              <tr>
                <th class="left">Day</th>
                <th>Orders</th>
                <th>Settled</th>
                <th>Realised</th>
                <th>Tokens</th>
                <th>Cycles</th>
                <th>Measured</th>
                <th>Coverage</th>
              </tr>
            </thead>
            <tbody>
              {#each coverageRows as row (row.day)}
                <tr class:selected={selectedDay === row.day}>
                  <th class="left">
                    <button
                      class="day-choice"
                      aria-pressed={selectedDay === row.day}
                      onclick={() =>
                        onSelectDay(selectedDay === row.day ? "" : row.day)}
                    >
                      {shortDay(row.day)}
                    </button>
                  </th>
                  <td class="numeric">{row.performance?.orders ?? "—"}</td>
                  <td class="numeric">{row.performance?.closed ?? "—"}</td>
                  <td class="numeric">{row.performance ? signedRupees(row.performance.realised) : "—"}</td>
                  <td class="numeric">{row.usage ? tokenCount(row.usage.total_tokens) : "—"}</td>
                  <td class="numeric">{row.usage?.cycles ?? "—"}</td>
                  <td class="numeric">{row.measuredCycles}</td>
                  <td class="left">
                    <div class="coverage-flags">
                      <span class:yes={Boolean(row.performance)}>trade</span>
                      <span class:yes={Boolean(row.usage?.cycles)}>AI</span>
                      <span class:yes={row.measuredCycles > 0}>timing</span>
                    </div>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {:else}
        <p class="report-empty-inline">
          No archived trade or usage day exists in this scope.
        </p>
      {/if}
    </section>

    <section class="report-panel">
      <header class="report-panel-head compact">
        <div>
          <span class="report-kicker">Limits of the record</span>
          <h2>What this dashboard does not claim</h2>
        </div>
      </header>
      <ul class="report-caveats">
        <li>P&amp;L is gross realised rupees before brokerage, fees, taxes and slippage beyond the recorded fill.</li>
        <li>Drawdown is measured from realised closed P&amp;L, not account equity or unused capital.</li>
        <li>The archive is a mirror of Module 3 records. It is not a live position book and cannot place an order.</li>
        <li>Token usage exists only for days Module 2 retained and Module 4 successfully mirrored.</li>
        <li>Cycle wall time is measured for new cycles. Older cycles are marked derived, and missing timing is left unknown.</li>
        <li>Tool work overlaps inside a round. Its sum is work observed by tool threads, not serial wall time.</li>
      </ul>
    </section>
  </div>

  {#if status?.last_error}
    <div class="report-banner warn">
      <strong>Module 4 reported an archive problem.</strong>
      <span>{status.last_error}</span>
    </div>
  {/if}
</div>

<style>
  .coverage-table {
    max-height: 520px;
    overflow: auto;
  }

  .coverage-table thead th {
    position: sticky;
    top: 0;
    z-index: 1;
    background: var(--bg);
  }

  .day-choice {
    padding: 0;
    background: none;
    border: 0;
    color: var(--ink);
    font: inherit;
    font-weight: 600;
  }

  .day-choice:hover {
    color: var(--accent);
    text-decoration: underline;
    text-underline-offset: 3px;
  }

  .coverage-flags {
    display: flex;
    gap: 4px;
  }

  .coverage-flags span {
    padding: 2px 5px;
    border: 1px solid var(--line);
    border-radius: 4px;
    color: var(--faint);
    font-size: 9px;
    font-weight: 650;
    letter-spacing: 0.03em;
    text-transform: uppercase;
  }

  .coverage-flags span.yes {
    background: var(--up-soft);
    border-color: var(--up);
    color: var(--up);
  }

  @media (max-width: 620px) {
    .coverage-flags {
      flex-wrap: wrap;
    }
  }
</style>

<script>
  /**
   * General trading performance: what the archived positions made, how those
   * results were distributed, and which agent or symbol carried the result.
   * This view deliberately stops at realised P&L; AI cost and operational
   * coverage have their own views so neither is mistaken for account equity.
   */

  import Sparkline from "../lib/Sparkline.svelte";
  import {
    duration,
    rupees,
    signed,
    signedRupees,
    tone,
  } from "../lib/format.js";
  import { seriesColour, shortDay } from "../lib/reporting.js";
  import DailyPnlChart from "./DailyPnlChart.svelte";
  import EquityChart from "./EquityChart.svelte";
  import ExitMix from "./ExitMix.svelte";
  import RDistribution from "./RDistribution.svelte";

  let {
    analytics,
    benchmark = null,
    scopeRows = [],
    selectedAgent = "",
    selectedDay = "",
    onSelectAgent = () => {},
    onSelectDay = () => {},
  } = $props();

  let showAllSymbols = $state(false);

  const summary = $derived(analytics?.summary ?? {});
  const agents = $derived(analytics?.agents ?? []);
  const dayContext = $derived(analytics?.context_days ?? analytics?.days ?? []);
  const earlyRead = $derived(
    Number(summary.closed || 0) > 0 &&
      (Number(summary.closed) < 30 ||
        Number(summary.trading_days || 0) < 10),
  );
  const scopeName = $derived(
    `${selectedAgent || "All agents"}${selectedDay ? ` · ${shortDay(selectedDay)}` : ""}`,
  );

  const agentColours = $derived.by(() => {
    const colours = {};
    for (const [index, row] of scopeRows.entries()) {
      colours[row.agent_id] = seriesColour(index);
    }
    return colours;
  });

  const visibleAgentColours = $derived.by(() => {
    const visible = new Set(agents.map((row) => row.agent_id));
    return Object.fromEntries(
      Object.entries(agentColours).filter(([agentId]) => visible.has(agentId)),
    );
  });

  const equitySeries = $derived.by(() => {
    if (!analytics) return [];
    const curves = analytics.agent_curves ?? [];
    if (selectedAgent) {
      const curve =
        curves.find((row) => row.agent_id === selectedAgent) ?? curves[0];
      if (!curve) return [];
      return [
        {
          id: curve.agent_id,
          label: curve.agent_id,
          colour: agentColours[curve.agent_id] ?? "var(--ink)",
          points: curve.points ?? [],
          emphasis: true,
        },
      ];
    }
    return [
      {
        id: "all",
        label: "All agents",
        colour: "var(--ink)",
        points: analytics.equity ?? [],
        emphasis: true,
      },
      ...curves.map((curve) => ({
        id: curve.agent_id,
        label: curve.agent_id,
        colour: agentColours[curve.agent_id] ?? "var(--muted)",
        points: curve.points ?? [],
      })),
    ];
  });

  const benchmarkSeries = $derived.by(() => {
    const equity = analytics?.equity ?? [];
    const candles = benchmark?.candles ?? [];
    if (!equity.length || candles.length < 2) return null;
    const closes = candles
      .map((row) => ({ day: String(row.date ?? ""), close: Number(row.close) }))
      .filter((row) => row.day && Number.isFinite(row.close))
      .sort((left, right) => left.day.localeCompare(right.day));
    if (closes.length < 2) return null;
    const notional = Number(benchmark?.notional || 50000);
    let cursor = 0;
    let baseline = null;
    const points = [];
    for (const point of equity) {
      const day = String(point.day ?? "");
      while (
        cursor + 1 < closes.length &&
        closes[cursor + 1].day <= day
      ) {
        cursor += 1;
      }
      if (closes[cursor].day > day) continue;
      const close = closes[cursor].close;
      if (baseline === null) baseline = close;
      if (!baseline) continue;
      points.push({
        ...point,
        cumulative: (close / baseline - 1) * notional,
      });
    }
    if (points.length < 2) return null;
    return {
      id: "benchmark",
      label: `${benchmark.symbol ?? "NIFTY 50"} · buy & hold`,
      colour: "var(--muted)",
      points,
      emphasis: false,
    };
  });

  const symbolRows = $derived.by(() => {
    const rows = [...(analytics?.symbols ?? [])].sort(
      (left, right) =>
        Math.abs(Number(right.realised || 0)) -
        Math.abs(Number(left.realised || 0)),
    );
    return showAllSymbols ? rows : rows.slice(0, 8);
  });

  function profitFactorLabel() {
    if (summary.profit_factor !== null && summary.profit_factor !== undefined) {
      return Number(summary.profit_factor).toFixed(2);
    }
    if (summary.closed && !summary.losses) return "No losses";
    return "—";
  }

  function agentProfitFactor(agent) {
    if (agent.profit_factor !== null && agent.profit_factor !== undefined) {
      return Number(agent.profit_factor).toFixed(2);
    }
    if (agent.closed && !agent.losses) return "No losses";
    return "—";
  }

  function drawdownLabel(value) {
    if (!value) return rupees(0);
    return `−${rupees(value)}`;
  }

  function curveValues(agentId) {
    const curve =
      analytics?.agent_curves?.find((row) => row.agent_id === agentId) ?? null;
    return (curve?.points ?? []).map((point) => point.cumulative);
  }

  function toggleAgent(agentId) {
    onSelectAgent(selectedAgent === agentId ? "" : agentId);
  }
</script>

<div class="report-view">
  {#if (summary.orders ?? 0) === 0}
    <section class="report-panel report-empty">
      <span class="report-kicker">General performance</span>
      <h2>No settled trade is in this scope yet.</h2>
      <p>
        Performance begins with the first archived position. AI cycles and
        archive coverage remain available in their own views.
      </p>
    </section>
  {:else}
    <section class="report-panel report-overview flush">
      <div class="report-overview-lead">
        <div class="report-lead-top">
          <span class="label">Net realised</span>
          <span class="report-scope-chip">{scopeName}</span>
        </div>
        <strong class="report-headline numeric {tone(summary.realised)}">
          {signedRupees(summary.realised ?? 0, { digits: 2 })}
        </strong>
        <p>
          {summary.closed ?? 0} settled of {summary.orders ?? 0} orders across
          {summary.trading_days ?? 0} exchange days. Gross, before fees and taxes.
        </p>
        {#if earlyRead}
          <p class="early">
            Early read: the sample is {summary.closed} closed trades over
            {summary.trading_days} day{summary.trading_days === 1 ? "" : "s"}.
            Treat rankings as provisional.
          </p>
        {/if}
      </div>

      <dl class="report-metrics">
        <div>
          <dt>Win rate</dt>
          <dd>
            {summary.win_rate === null || summary.win_rate === undefined
              ? "—"
              : `${summary.win_rate}%`}
          </dd>
          <span>
            {summary.wins ?? 0}W · {summary.losses ?? 0}L ·
            {summary.flats ?? 0} flat
          </span>
        </div>
        <div>
          <dt>Average R</dt>
          <dd class={tone(summary.average_r)}>
            {summary.average_r === null || summary.average_r === undefined
              ? "—"
              : `${signed(summary.average_r)}R`}
          </dd>
          <span>
            {summary.average_gross === null ||
            summary.average_gross === undefined
              ? "no settled average"
              : `${signedRupees(summary.average_gross)} average`}
          </span>
        </div>
        <div>
          <dt>Profit factor</dt>
          <dd>{profitFactorLabel()}</dd>
          <span>
            {summary.average_win === null
              ? "no winning side yet"
              : `${rupees(summary.average_win)} win`}
            {summary.average_loss === null
              ? ""
              : ` / ${rupees(Math.abs(summary.average_loss))} loss`}
          </span>
        </div>
        <div>
          <dt>Max drawdown</dt>
          <dd class:down={Number(summary.max_drawdown || 0) > 0}>
            {drawdownLabel(summary.max_drawdown ?? 0)}
          </dd>
          <span>peak-to-trough closed P&amp;L</span>
        </div>
      </dl>

      <div class="report-overview-foot">
        <span>
          <small>Green days</small>
          <strong>{summary.green_days ?? 0} / {summary.trading_days ?? 0}</strong>
        </span>
        <span>
          <small>Average hold</small>
          <strong>{duration(summary.average_hold_minutes)}</strong>
        </span>
        <span>
          <small>Average risk</small>
          <strong>{rupees(summary.average_risk ?? 0)}</strong>
        </span>
        <span>
          <small>Decision outcomes</small>
          <strong>{summary.closed ?? 0} settled · {summary.expired ?? 0} expired</strong>
        </span>
        <span>
          <small>Current streak</small>
          <strong
            class={summary.current_streak?.outcome === "win"
              ? "up"
              : summary.current_streak?.outcome === "loss"
                ? "down"
                : ""}
          >
            {summary.current_streak
              ? `${summary.current_streak.count} ${summary.current_streak.outcome}`
              : "—"}
          </strong>
        </span>
        <span class="basis">
          {summary.r_samples ?? 0} trades carry an R multiple
        </span>
      </div>
    </section>

    <section class="report-panel">
      <header class="report-panel-head">
        <div>
          <span class="report-kicker">Cumulative</span>
          <h2>Realised P&amp;L path</h2>
          <p>
            Each point is one closed trade in settlement order. The bold line
            is the current scope; agent lines separate its contributors, and
            the muted line is NIFTY 50 buy-and-hold on
            {rupees(benchmark?.notional ?? 50000)}.
          </p>
        </div>
        <div class="report-facts">
          <span>
            <small>Peak</small>
            <strong>
              {signedRupees(summary.drawdown?.peak ?? summary.realised ?? 0)}
            </strong>
          </span>
          <span>
            <small>Drawdown</small>
            <strong class:down={Number(summary.max_drawdown || 0) > 0}>
              {drawdownLabel(summary.max_drawdown ?? 0)}
            </strong>
          </span>
        </div>
      </header>
      <EquityChart
        series={benchmarkSeries ? [...equitySeries, benchmarkSeries] : equitySeries}
      />
    </section>

    <section class="report-panel">
      <header class="report-panel-head">
        <div>
          <span class="report-kicker">Day by day</span>
          <h2>Realised P&amp;L by exchange day</h2>
          <p>
            Positive and negative agent contributions grow independently from
            zero. Select a day to scope the whole Reporting tab; select it
            again to clear the filter.
          </p>
        </div>
        <div class="report-facts">
          <span>
            <small>Green days</small>
            <strong>{summary.green_days ?? 0} / {summary.trading_days ?? 0}</strong>
          </span>
          <span>
            <small>Day win rate</small>
            <strong>
              {summary.day_win_rate === null ||
              summary.day_win_rate === undefined
                ? "—"
                : `${summary.day_win_rate}%`}
            </strong>
          </span>
        </div>
      </header>
      <DailyPnlChart
        days={dayContext}
        agentColours={visibleAgentColours}
        selectedDay={selectedDay}
        onSelectDay={onSelectDay}
      />
    </section>

    <div class="report-grid">
      <section class="report-panel">
        <header class="report-panel-head compact">
          <div>
            <span class="report-kicker">Distribution</span>
            <h2>Where trades land in R</h2>
            <p>Every settled trade in one bucket of realised risk multiple.</p>
          </div>
        </header>
        <RDistribution bins={analytics.r_distribution ?? []} />
      </section>

      <section class="report-panel">
        <header class="report-panel-head compact">
          <div>
            <span class="report-kicker">Exit quality</span>
            <h2>How trades ended</h2>
            <p>Counts and gross rupees by the reason Module 3 recorded.</p>
          </div>
        </header>
        <ExitMix exits={analytics.exits ?? []} />
      </section>

      <section class="report-panel">
        <header class="report-panel-head compact">
          <div>
            <span class="report-kicker">Direction</span>
            <h2>Long vs short</h2>
            <p>The same settled trades, split by the side taken.</p>
          </div>
        </header>
        {#if analytics.sides?.length}
          <div class="report-list">
            {#each analytics.sides as side (side.side)}
              <div class="report-list-row">
                <div>
                  <strong>{side.label}</strong>
                  <span>{side.closed} settled · {side.orders} orders</span>
                </div>
                <div class="report-list-result">
                  <strong class={tone(side.realised)}>
                    {signedRupees(side.realised)}
                  </strong>
                  <span>
                    {side.win_rate === null ? "—" : `${side.win_rate}% win`}
                    {side.average_r === null
                      ? ""
                      : ` · ${signed(side.average_r)}R avg`}
                  </span>
                </div>
              </div>
            {/each}
          </div>
        {:else}
          <p class="report-empty-inline">No closed side to compare yet.</p>
        {/if}
      </section>
    </div>

    <section class="report-panel">
      <header class="report-panel-head">
        <div>
          <span class="report-kicker">Scorecard</span>
          <h2>Performance by agent</h2>
          <p>
            Select an agent to scope every Reporting view. Drawdown is measured
            from that agent's own cumulative realised P&amp;L peak.
          </p>
        </div>
        <span class="report-note">
          {agents.length} agent{agents.length === 1 ? "" : "s"}
        </span>
      </header>

      {#if agents.length}
        <div class="report-scroll">
          <table class="report-table">
            <thead>
              <tr>
                <th class="left">Agent</th>
                <th class="left">Equity shape</th>
                <th>Closed</th>
                <th>Net P&amp;L</th>
                <th>Win %</th>
                <th>Avg R</th>
                <th>Profit factor</th>
                <th>Max DD</th>
                <th>Green days</th>
                <th>Avg hold</th>
              </tr>
            </thead>
            <tbody>
              {#each agents as agent (agent.agent_id)}
                <tr class:selected={selectedAgent === agent.agent_id}>
                  <th class="left">
                    <button
                      class="report-agent-choice"
                      aria-pressed={selectedAgent === agent.agent_id}
                      onclick={() => toggleAgent(agent.agent_id)}
                    >
                      <span
                        class="report-agent-swatch"
                        style={`background: ${agentColours[agent.agent_id] ?? "var(--muted)"}`}
                      ></span>
                      {agent.agent_id}
                    </button>
                  </th>
                  <td class="report-trend">
                    {#if curveValues(agent.agent_id).length > 1}
                      <Sparkline
                        values={curveValues(agent.agent_id)}
                        width={102}
                        height={25}
                      />
                    {:else}
                      <span class="report-muted">—</span>
                    {/if}
                  </td>
                  <td class="numeric">{agent.closed}</td>
                  <td class="numeric {tone(agent.realised)}">
                    {signedRupees(agent.realised)}
                  </td>
                  <td class="numeric">
                    {agent.win_rate === null ? "—" : `${agent.win_rate}%`}
                  </td>
                  <td class="numeric {tone(agent.average_r)}">
                    {agent.average_r === null
                      ? "—"
                      : `${signed(agent.average_r)}R`}
                  </td>
                  <td class="numeric">{agentProfitFactor(agent)}</td>
                  <td
                    class="numeric"
                    class:down={Number(agent.max_drawdown || 0) > 0}
                  >
                    {drawdownLabel(agent.max_drawdown ?? 0)}
                  </td>
                  <td class="numeric">
                    {agent.green_days} / {agent.trading_days}
                  </td>
                  <td class="numeric">
                    {duration(agent.average_hold_minutes)}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {:else}
        <p class="report-empty-inline">No agent has an archived order yet.</p>
      {/if}
    </section>

    <section class="report-panel">
      <header class="report-panel-head">
        <div>
          <span class="report-kicker">Contribution</span>
          <h2>Symbols that moved the result</h2>
          <p>
            Sorted by absolute realised impact, so the largest losses remain as
            visible as the largest wins.
          </p>
        </div>
        {#if (analytics.symbols?.length ?? 0) > 8}
          <button
            class="btn small"
            onclick={() => (showAllSymbols = !showAllSymbols)}
          >
            {showAllSymbols
              ? "Show top 8"
              : `Show all ${analytics.symbols.length}`}
          </button>
        {/if}
      </header>

      {#if symbolRows.length}
        <div class="report-scroll">
          <table class="report-table">
            <thead>
              <tr>
                <th class="left">Symbol</th>
                <th>Orders</th>
                <th>Closed</th>
                <th>W / L</th>
                <th>Net P&amp;L</th>
                <th>Win %</th>
                <th>Avg R</th>
                <th>Average trade</th>
              </tr>
            </thead>
            <tbody>
              {#each symbolRows as row (row.symbol)}
                <tr>
                  <th class="left">{row.symbol}</th>
                  <td class="numeric">{row.orders}</td>
                  <td class="numeric">{row.closed}</td>
                  <td class="numeric">{row.wins} / {row.losses}</td>
                  <td class="numeric {tone(row.realised)}">
                    {signedRupees(row.realised)}
                  </td>
                  <td class="numeric">
                    {row.win_rate === null ? "—" : `${row.win_rate}%`}
                  </td>
                  <td class="numeric {tone(row.average_r)}">
                    {row.average_r === null
                      ? "—"
                      : `${signed(row.average_r)}R`}
                  </td>
                  <td class="numeric {tone(row.average_gross)}">
                    {signedRupees(row.average_gross ?? 0)}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {:else}
        <p class="report-empty-inline">
          No symbol has a settled result in this scope.
        </p>
      {/if}
    </section>
  {/if}
</div>

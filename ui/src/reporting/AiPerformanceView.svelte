<script>
  /**
   * AI performance is deliberately separate from trade performance. Tokens,
   * model time and cycle wall time describe the agent process; they do not say
   * whether a position made money. The view keeps those two questions apart
   * while allowing the same agent and day scope to be read side by side.
   */

  import CycleScatter from "./CycleScatter.svelte";
  import { shortDay } from "../lib/reporting.js";

  let {
    usageDays = [],
    selectedAgent = "",
    selectedDay = "",
    agentColours = {},
    onSelectDay = () => {},
  } = $props();

  const days = $derived(
    [...usageDays].sort((left, right) => String(right.day).localeCompare(String(left.day))),
  );
  const activeDay = $derived(
    days.find((row) => row.day === selectedDay) ?? days[0] ?? null,
  );

  const agents = $derived.by(() => {
    if (!activeDay) return [];
    const rows = activeDay.agents ?? [];
    return selectedAgent
      ? rows.filter((row) => row.agent_id === selectedAgent)
      : rows;
  });

  const cycles = $derived.by(() =>
    agents.flatMap((agent) =>
      (agent.cycle_rows ?? []).map((cycle) => ({
        ...cycle,
        agent_id: agent.agent_id,
        model: agent.model,
        day: activeDay?.day,
      })),
    ),
  );

  const totals = $derived.by(() => {
    if (selectedAgent) {
      return aggregate(agents);
    }
    return activeDay?.totals ?? {};
  });

  const cycleRows = $derived(
    [...cycles].sort((left, right) =>
      String(right.finished_at || right.started_at || "").localeCompare(
        String(left.finished_at || left.started_at || ""),
      ),
    ),
  );
  const slowest = $derived(
    [...cycles]
      .filter((row) => Number.isFinite(Number(row.duration_ms)))
      .sort((left, right) => Number(right.duration_ms) - Number(left.duration_ms))[0] ??
      null,
  );
  const heaviest = $derived(
    [...cycles].sort(
      (left, right) => Number(right.total_tokens || 0) - Number(left.total_tokens || 0),
    )[0] ?? null,
  );
  const missingNotes = $derived(
    cycles.filter((row) => row.notes_kept === false).length,
  );
  const timedCycles = $derived(
    cycles.filter((row) => row.duration_source === "measured").length,
  );

  function aggregate(rows) {
    const fields = [
      "model_calls",
      "cycles",
      "tool_calls",
      "decisions",
      "prompt_tokens",
      "completion_tokens",
      "prompt_cache_hit_tokens",
      "prompt_cache_miss_tokens",
      "reasoning_tokens",
      "total_tokens",
      "cycle_time_ms",
      "model_time_ms",
      "tool_time_ms",
      "timed_cycles",
    ];
    const sum = Object.fromEntries(fields.map((field) => [field, 0]));
    for (const row of rows) {
      for (const field of fields) sum[field] += Number(row[field] || 0);
    }
    sum.models = [...new Set(rows.flatMap((row) => row.models ?? []))];
    return sum;
  }

  function tokenCount(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    if (number >= 1_000_000) return `${(number / 1_000_000).toFixed(2)}M`;
    if (number >= 1_000) return `${(number / 1_000).toFixed(1)}k`;
    return String(Math.round(number));
  }

  function elapsed(ms) {
    const number = Number(ms);
    if (!Number.isFinite(number)) return "—";
    if (number >= 60_000) return `${(number / 60_000).toFixed(1)}m`;
    if (number >= 1_000) return `${(number / 1_000).toFixed(number >= 10_000 ? 0 : 1)}s`;
    return `${Math.round(number)}ms`;
  }

  function averageCycles(cyclesValue, timedValue) {
    const timed = Number(timedValue || 0);
    return timed ? Number(cyclesValue || 0) / timed : null;
  }

  function cacheRate(row) {
    const prompt = Number(row.prompt_tokens || 0);
    return prompt ? (Number(row.prompt_cache_hit_tokens || 0) / prompt) * 100 : null;
  }

  function sourceLabel(row) {
    if (row.duration_source === "measured") return "measured";
    if (row.duration_source === "derived_from_log") return "derived";
    return "missing";
  }

  function cycleWhen(row) {
    const stamp = row.finished_at || row.started_at;
    if (!stamp) return "—";
    const date = new Date(stamp);
    if (Number.isNaN(date.getTime())) return "—";
    return date.toLocaleTimeString("en-IN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  }
</script>

<div class="report-view">
  {#if !activeDay}
    <section class="report-panel report-empty">
      <span class="report-kicker">AI performance</span>
      <h2>No agent usage has been mirrored yet.</h2>
      <p>
        Module 4 will show cycle token counts, model time and tool work after
        Module 2 has completed a cycle for an archived day and its usage route
        has been synced.
      </p>
    </section>
  {:else}
    <section class="report-panel">
      <header class="report-panel-head">
        <div>
          <span class="report-kicker">Observed work</span>
          <h2>AI costs and cycle time</h2>
          <p>
            One cycle is one closed-candle pass. Wall time comes from the agent
            process and excludes the sleep until the next candle. Model latency
            and tool work are recorded separately because tool calls in a round
            run in parallel.
          </p>
        </div>
        <label class="report-day-select">
          <span class="label">Usage day</span>
          <select
            class="report-select"
            value={activeDay.day}
            onchange={(event) => onSelectDay(event.currentTarget.value)}
          >
            {#each days as row (row.day)}
              <option value={row.day}>{shortDay(row.day)}</option>
            {/each}
          </select>
        </label>
      </header>

      <dl class="report-metrics four">
        <div>
          <dt>Total tokens</dt>
          <dd>{tokenCount(totals.total_tokens)}</dd>
          <span>
            {tokenCount(totals.prompt_tokens)} prompt ·
            {tokenCount(totals.completion_tokens)} completion
          </span>
        </div>
        <div>
          <dt>Tokens / cycle</dt>
          <dd>
            {totals.cycles
              ? tokenCount(Number(totals.total_tokens || 0) / Number(totals.cycles))
              : "—"}
          </dd>
          <span>{tokenCount(totals.cycles)} cycles · {tokenCount(totals.model_calls)} model calls</span>
        </div>
        <div>
          <dt>Average cycle</dt>
          <dd>
            {averageCycles(totals.cycle_time_ms, totals.timed_cycles) === null
              ? "—"
              : elapsed(averageCycles(totals.cycle_time_ms, totals.timed_cycles))}
          </dd>
          <span>
            {timedCycles} measured of {cycles.length} cycle rows
          </span>
        </div>
        <div>
          <dt>Decisions</dt>
          <dd>{totals.decisions ?? 0}</dd>
          <span>
            {totals.tool_calls ?? 0} tools · {missingNotes} cycles without a notebook write
          </span>
        </div>
      </dl>

      <div class="report-ai-foot">
        <span>
          <strong>{totals.reasoning_tokens ?? 0}</strong> reasoning tokens
        </span>
        <span>
          <strong>
            {totals.prompt_tokens
              ? `${((Number(totals.prompt_cache_hit_tokens || 0) / Number(totals.prompt_tokens)) * 100).toFixed(1)}%`
              : "—"}
          </strong>
          prompt cache hit
        </span>
        <span>
          <strong>{elapsed(totals.model_time_ms)}</strong> model latency observed
        </span>
        <span>
          <strong>{elapsed(totals.tool_time_ms)}</strong> tool work observed
        </span>
        <span class="report-muted">
          {totals.models?.join(", ") || "model not recorded"}
        </span>
      </div>
    </section>

    <section class="report-panel">
      <header class="report-panel-head compact">
        <div>
          <span class="report-kicker">Cycle trace</span>
          <h2>Where time and tokens moved</h2>
          <p>
            The same cycle order under either metric. Hover or focus a dot for
            the agent, candle and observation. A derived point predates the
            explicit cycle_finished event.
          </p>
        </div>
        <span class="report-note">
          slowest {slowest ? elapsed(slowest.duration_ms) : "—"} ·
          heaviest {heaviest ? tokenCount(heaviest.total_tokens) : "—"}
        </span>
      </header>
      <CycleScatter
        {cycles}
        {agentColours}
        {selectedAgent}
      />
    </section>

    <section class="report-panel">
      <header class="report-panel-head">
        <div>
          <span class="report-kicker">Agent comparison</span>
          <h2>Cost and cycle time by agent</h2>
          <p>
            A cycle can make several decisions. Compare tokens per cycle and
            wall time per measured cycle without treating a higher token count
            as a better or worse trading result.
          </p>
        </div>
        <span class="report-note">{agents.length} agent{agents.length === 1 ? "" : "s"}</span>
      </header>

      {#if agents.length}
        <div class="report-scroll">
          <table class="report-table">
            <thead>
              <tr>
                <th class="left">Agent</th>
                <th class="left">Model</th>
                <th>Cycles</th>
                <th>Tokens</th>
                <th>Tokens / cycle</th>
                <th>Avg cycle</th>
                <th>Model time</th>
                <th>Tool work</th>
                <th>Cache hit</th>
                <th>Timed</th>
                <th>Decisions</th>
              </tr>
            </thead>
            <tbody>
              {#each agents as row (row.agent_id)}
                <tr class:selected={selectedAgent === row.agent_id}>
                  <th class="left">
                    <span class="agent-inline">
                      <span
                        class="report-agent-swatch"
                        style={`background: ${agentColours[row.agent_id] ?? "var(--muted)"}`}
                      ></span>
                      {row.agent_id}
                    </span>
                  </th>
                  <td class="left report-muted">{row.model ?? "—"}</td>
                  <td class="numeric">{row.cycles}</td>
                  <td class="numeric">{tokenCount(row.total_tokens)}</td>
                  <td class="numeric">
                    {row.cycles
                      ? tokenCount(Number(row.total_tokens || 0) / Number(row.cycles))
                      : "—"}
                  </td>
                  <td class="numeric">
                    {averageCycles(row.cycle_time_ms, row.timed_cycles) === null
                      ? "—"
                      : elapsed(averageCycles(row.cycle_time_ms, row.timed_cycles))}
                  </td>
                  <td class="numeric">{elapsed(row.model_time_ms)}</td>
                  <td class="numeric">{elapsed(row.tool_time_ms)}</td>
                  <td class="numeric">
                    {cacheRate(row) === null ? "—" : `${cacheRate(row).toFixed(1)}%`}
                  </td>
                  <td class="numeric">{row.timed_cycles ?? 0} / {row.cycles ?? 0}</td>
                  <td class="numeric">{row.decisions ?? 0}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {:else}
        <p class="report-empty-inline">No agent has a cycle in this scope yet.</p>
      {/if}
    </section>

    <section class="report-panel">
      <header class="report-panel-head">
        <div>
          <span class="report-kicker">Exact observations</span>
          <h2>Cycle ledger</h2>
          <p>
            Newest first. A cycle that has no token or timing row remains
            visible rather than being silently omitted; the source column says
            whether its duration was measured or derived from log timestamps.
          </p>
        </div>
        <span class="report-note">{cycleRows.length} cycle{cycleRows.length === 1 ? "" : "s"}</span>
      </header>

      {#if cycleRows.length}
        <div class="report-scroll report-cycle-scroll">
          <table class="report-table">
            <thead>
              <tr>
                <th class="left">Finished</th>
                <th class="left">Candle</th>
                <th class="left">Agent</th>
                <th>Wall</th>
                <th>Source</th>
                <th>Model</th>
                <th>Tools</th>
                <th>Tokens</th>
                <th>Decisions</th>
                <th>Notebook</th>
                <th class="left">Status</th>
              </tr>
            </thead>
            <tbody>
              {#each cycleRows as row (row.cycle_id)}
                <tr>
                  <td class="left report-muted numeric">{cycleWhen(row)}</td>
                  <td class="left report-muted">{row.bar_open_time ?? "—"}</td>
                  <td class="left">{row.agent_id}</td>
                  <td class="numeric">{elapsed(row.duration_ms)}</td>
                  <td class="numeric">
                    <span class="source {sourceLabel(row)}">
                      {sourceLabel(row)}
                    </span>
                  </td>
                  <td class="numeric">
                    {row.model_calls ?? 0} · {elapsed(row.model_time_ms)}
                  </td>
                  <td class="numeric">
                    {row.tool_calls ?? 0} · {elapsed(row.tool_time_ms)}
                  </td>
                  <td class="numeric">{tokenCount(row.total_tokens)}</td>
                  <td class="numeric">{row.decisions ?? 0}</td>
                  <td class="numeric">{row.notes_kept === false ? "missing" : "kept"}</td>
                  <td class="left report-muted">{row.status ?? "—"}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {:else}
        <p class="report-empty-inline">
          The mirrored aggregate exists, but this day has no per-cycle rows.
          Older logs predate cycle-level timing.
        </p>
      {/if}
    </section>
  {/if}
</div>

<style>
  .report-day-select {
    display: grid;
    gap: 4px;
    justify-items: end;
  }

  .report-metrics.four {
    grid-template-columns: repeat(4, minmax(0, 1fr));
    padding: 0;
    margin: 0 0 var(--space-4);
  }

  .report-ai-foot {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: var(--space-2) var(--space-5);
    padding-top: var(--space-3);
    border-top: 1px solid var(--line);
    color: var(--faint);
    font-size: var(--step--1);
  }

  .report-ai-foot strong {
    color: var(--ink-2);
    font-weight: 650;
    font-variant-numeric: tabular-nums;
  }

  .agent-inline {
    display: inline-flex;
    align-items: center;
    gap: 7px;
  }

  .report-cycle-scroll {
    max-height: 620px;
    overflow: auto;
  }

  .report-cycle-scroll thead th {
    position: sticky;
    top: 0;
    z-index: 1;
    background: var(--bg);
  }

  .source {
    font-size: 10px;
    font-weight: 650;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .source.measured {
    color: var(--up);
  }

  .source.derived_from_log {
    color: var(--warn);
  }

  .source.missing {
    color: var(--faint);
  }

  @media (max-width: 1160px) {
    .report-metrics.four {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }

  @media (max-width: 620px) {
    .report-day-select {
      width: 100%;
      justify-items: stretch;
    }

    .report-metrics.four {
      grid-template-columns: 1fr;
    }
  }
</style>

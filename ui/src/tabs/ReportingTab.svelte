<script>
  /**
   * Module 4's operator surface.
   *
   * The old screen mixed trading results, AI cost and exact order records in
   * one long page. This shell keeps one scope and one refresh loop, then gives
   * each question its own view: trade performance, AI cost and cycle time,
   * exact archive records, and data coverage.
   */

  import { age } from "../lib/format.js";
  import {
    getAnalytics,
    getBenchmark,
    getHistory,
    getHistoryAgents,
    getReportingStatus,
    getTokenUsage,
    syncHistory,
  } from "../lib/api.js";
  import { seriesColour, shortDay } from "../lib/reporting.js";
  import AiPerformanceView from "../reporting/AiPerformanceView.svelte";
  import CoverageView from "../reporting/CoverageView.svelte";
  import OrderArchiveView from "../reporting/OrderArchiveView.svelte";
  import PerformanceView from "../reporting/PerformanceView.svelte";
  import "../reporting/reporting.css";

  const POLL_MS = 10000;
  const BOOK_LIMIT = 500;
  const VIEWS = [
    {
      id: "performance",
      label: "General performance",
      note: "P&L, risk and distribution",
    },
    {
      id: "ai",
      label: "AI performance",
      note: "tokens, cycles and latency",
    },
    {
      id: "orders",
      label: "Order archive",
      note: "exact archived records",
    },
    {
      id: "coverage",
      label: "Data coverage",
      note: "what is known and missing",
    },
  ];

  let analytics = $state(null);
  let orders = $state([]);
  let directory = $state([]);
  let status = $state(null);
  let tokenUsage = $state(null);
  let benchmark = $state(null);
  let selectedAgent = $state("");
  let selectedDay = $state("");
  let activeView = $state("performance");
  let error = $state("");
  let loading = $state(true);
  let scopeBusy = $state(false);
  let busy = $state(false);

  let loadSequence = 0;

  const summary = $derived(analytics?.summary ?? {});
  const usageDays = $derived(tokenUsage?.days ?? []);
  const latestUsage = $derived(usageDays[0] ?? null);
  const archiveStatus = $derived(status?.archive ?? {});
  const archive = $derived(archiveStatus.archive ?? {});

  const usageAgentRows = $derived.by(() => {
    const found = new Map();
    for (const day of usageDays) {
      for (const agent of day.agents ?? []) {
        if (agent.agent_id && !found.has(agent.agent_id)) {
          found.set(agent.agent_id, { agent_id: agent.agent_id });
        }
      }
    }
    return [...found.values()];
  });

  const scopeRows = $derived.by(() => {
    const found = new Map();
    for (const row of [...directory, ...usageAgentRows]) {
      if (row.agent_id) found.set(row.agent_id, row);
    }
    return [...found.values()].sort((left, right) =>
      left.agent_id.localeCompare(right.agent_id),
    );
  });

  const agentColours = $derived.by(() => {
    const colours = {};
    for (const [index, row] of scopeRows.entries()) {
      colours[row.agent_id] = seriesColour(index);
    }
    return colours;
  });

  const rangeLabel = $derived.by(() => {
    if (!summary.first_day) return "No dated trades";
    if (summary.first_day === summary.last_day) return shortDay(summary.first_day);
    return `${shortDay(summary.first_day)} – ${shortDay(summary.last_day)}`;
  });

  const aiCycleCount = $derived.by(() => {
    const day = selectedDay
      ? usageDays.find((row) => row.day === selectedDay)
      : latestUsage;
    return (day?.agents ?? [])
      .filter((row) => !selectedAgent || row.agent_id === selectedAgent)
      .reduce((total, row) => total + Number(row.cycles || 0), 0);
  });

  const viewCounts = $derived({
    performance: summary.closed ?? 0,
    ai: aiCycleCount,
    orders: orders.length,
    coverage: analytics?.days?.length ?? 0,
  });

  function selectAgent(agentId) {
    selectedAgent = selectedAgent === agentId ? "" : agentId;
  }

  function selectDay(day) {
    selectedDay = selectedDay === day ? "" : day;
  }

  function setView(viewId) {
    activeView = viewId;
  }

  function moveView(event) {
    const current = VIEWS.findIndex((view) => view.id === activeView);
    const direction =
      event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!direction) return;
    event.preventDefault();
    const next = (current + direction + VIEWS.length) % VIEWS.length;
    activeView = VIEWS[next].id;
    document.querySelector(`[data-report-view="${activeView}"]`)?.focus();
  }

  async function load(
    scope = selectedAgent,
    day = selectedDay,
    { quiet = false } = {},
  ) {
    const sequence = ++loadSequence;
    if (!quiet) scopeBusy = true;
    try {
      const [chartData, book, agentRows, reporting, usage, benchmarkData] =
        await Promise.all([
          getAnalytics({
            agentId: scope || undefined,
            day: day || undefined,
          }),
          getHistory({
            agentId: scope || undefined,
            day: day || undefined,
            limit: BOOK_LIMIT,
          }),
          getHistoryAgents(),
          getReportingStatus(),
          getTokenUsage(),
          getBenchmark(),
        ]);
      if (
        sequence !== loadSequence ||
        scope !== selectedAgent ||
        day !== selectedDay
      ) {
        return;
      }
      analytics = chartData ?? null;
      orders = book.orders ?? [];
      directory = agentRows.agents ?? [];
      status = reporting;
      tokenUsage = usage ?? null;
      benchmark = benchmarkData ?? null;
      error = "";
      if (
        scope &&
        !directory.some((row) => row.agent_id === scope) &&
        !usageAgentRows.some((row) => row.agent_id === scope)
      ) {
        selectedAgent = "";
      }
    } catch (exc) {
      if (sequence !== loadSequence) return;
      error = String(exc.message || exc);
    } finally {
      if (sequence === loadSequence) {
        loading = false;
        scopeBusy = false;
      }
    }
  }

  async function syncNow() {
    busy = true;
    try {
      await syncHistory();
      await load(selectedAgent, selectedDay, { quiet: true });
    } catch (exc) {
      error = String(exc.message || exc);
    } finally {
      busy = false;
    }
  }

  $effect(() => {
    const scope = selectedAgent;
    const day = selectedDay;
    load(scope, day);
    const poll = setInterval(
      () => load(scope, day, { quiet: true }),
      POLL_MS,
    );
    return () => clearInterval(poll);
  });
</script>

<section class="report-head">
  <div>
    <span class="report-eyebrow">Module 4 · long record</span>
    <h1>Reporting</h1>
    <p class="report-sub">
      Four views of the same archive: realised trading performance, the AI cost
      of producing decisions, exact order records, and honest coverage. Gross
      P&amp;L is shown before fees and taxes.
    </p>
  </div>
  <div class="report-actions">
    <span class="report-synced">
      {archiveStatus.synced_at
        ? `synced ${age(archiveStatus.synced_at)}`
        : archive.written_at
          ? `last write ${age(archive.written_at)}`
          : "not synced yet"}
    </span>
    <button class="btn primary" onclick={syncNow} disabled={busy}>
      {busy ? "Syncing…" : "Sync archive + AI"}
    </button>
  </div>
</section>

{#if error}
  <div class="report-banner bad" role="alert">
    <strong>Module 4 is not answering.</strong>
    <span>{error}</span>
    <button class="btn" onclick={() => load(selectedAgent, selectedDay)}>
      Try again
    </button>
  </div>
{/if}

{#if status?.last_error}
  <div class="report-banner warn" role="status">
    <strong>The last archive sync reported a problem.</strong>
    <span>{status.last_error}</span>
  </div>
{/if}

{#if scopeRows.length}
  <section class="report-scope" aria-label="Reporting scope" aria-busy={scopeBusy}>
    <span class="label">Agent scope</span>
    <div class="report-scope-options" role="group" aria-label="Filter Reporting by agent">
      <button
        class="report-scope-option"
        class:on={!selectedAgent}
        aria-pressed={!selectedAgent}
        onclick={() => (selectedAgent = "")}
      >
        All agents
      </button>
      {#each scopeRows as agent (agent.agent_id)}
        <button
          class="report-scope-option"
          class:on={selectedAgent === agent.agent_id}
          aria-pressed={selectedAgent === agent.agent_id}
          onclick={() => selectAgent(agent.agent_id)}
        >
          <span
            class="report-agent-swatch"
            style={`background: ${agentColours[agent.agent_id] ?? "var(--muted)"}`}
          ></span>
          {agent.agent_id}
        </button>
      {/each}
    </div>
    {#if selectedDay}
      <span class="day-scope">
        <span class="label">Day</span>
        <button
          class="report-scope-option on"
          aria-label={`Clear ${shortDay(selectedDay)} filter`}
          onclick={() => (selectedDay = "")}
        >
          {shortDay(selectedDay)}
          <span aria-hidden="true">×</span>
        </button>
      </span>
    {/if}
    <span class="report-scope-meta">
      {rangeLabel} · {summary.closed ?? 0} settled
      {#if scopeBusy}<span class="updating"> · updating…</span>{/if}
    </span>
  </section>
{/if}

<div class="report-tabs" role="tablist" aria-label="Reporting views">
  {#each VIEWS as view (view.id)}
    <button
      id={`report-tab-${view.id}`}
      class="report-tab"
      class:on={activeView === view.id}
      role="tab"
      aria-selected={activeView === view.id}
      aria-controls={`report-panel-${view.id}`}
      tabindex={activeView === view.id ? 0 : -1}
      data-report-view={view.id}
      title={view.note}
      onclick={() => setView(view.id)}
      onkeydown={moveView}
    >
      {view.label}
      <small>{viewCounts[view.id]}</small>
    </button>
  {/each}
</div>

{#if loading && !analytics}
  <section class="report-panel report-loading" aria-live="polite">
    <span class="label">Reading the archive</span>
    <div class="report-skeleton wide"></div>
    <div class="report-skeleton short"></div>
  </section>
{:else if analytics}
  <div
    id={`report-panel-${activeView}`}
    role="tabpanel"
    aria-labelledby={`report-tab-${activeView}`}
  >
    {#if activeView === "performance"}
      <PerformanceView
        {analytics}
        {benchmark}
        {scopeRows}
        {selectedAgent}
        {selectedDay}
        onSelectAgent={selectAgent}
        onSelectDay={selectDay}
      />
    {:else if activeView === "ai"}
      <AiPerformanceView
        {usageDays}
        {selectedAgent}
        {selectedDay}
        {agentColours}
        onSelectDay={selectDay}
      />
    {:else if activeView === "orders"}
      <OrderArchiveView {orders} {selectedAgent} {selectedDay} />
    {:else}
      <CoverageView
        {analytics}
        {usageDays}
        {orders}
        {status}
        {benchmark}
        {selectedAgent}
        {selectedDay}
        onSelectDay={selectDay}
      />
    {/if}
  </div>
{:else}
  <section class="report-panel report-empty">
    <span class="label">No view yet</span>
    <h2>The reporting service is not reachable.</h2>
    <p>Start Module 4, then try the request again.</p>
    <button class="btn" onclick={() => load(selectedAgent, selectedDay)}>
      Try again
    </button>
  </section>
{/if}

<footer class="report-footer">
  <span>
    Module 4 keeps this record from Module 3's daily book and Module 2's usage
    mirror. It is gross realised P&amp;L, not an account statement, and nothing
    here can place or change an order.
  </span>
  <span>
    {archive.present
      ? `Archive written ${age(archive.written_at)} · ${archive.count ?? 0} orders`
      : "Local archive has not been written yet"}
  </span>
</footer>

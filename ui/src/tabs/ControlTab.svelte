<script>
  /**
   * The Control tab: the central process, on one screen.
   *
   * The runner is the one process that starts the framework: Module 1 first,
   * given time to fetch history and compute indicators, then Module 4 for the
   * reports, then Modules 3 and 2 for the trading day - and only for the
   * trading day. This page is that process's own answer: what is up, what it
   * last said, and one button to restart any of them. The log panel reads the
   * same files the services write, so "it says it is fine" can be checked
   * rather than trusted.
   */

  import { getControlStatus, getServiceLog, restartService } from "../lib/api.js";
  import { age } from "../lib/format.js";

  const POLL_MS = 3000;
  const LOG_POLL_MS = 2500;
  const LOG_LINES = 400;
  const ORDER = ["module1", "module2", "module3", "module4"];

  const PHASES = {
    starting: "starting up",
    pre_market: "waiting for the market",
    open: "trading",
    after_decisions: "closing the day",
    day_over: "day over",
    closed_today: "market closed",
    development: "development",
  };

  let payload = $state(null);
  let error = $state("");
  let busy = $state("");
  let note = $state("");
  let logService = $state(null);
  let logKind = $state("log");
  let logLines = $state([]);
  let logFile = $state("");
  let logError = $state("");

  const services = $derived(
    ORDER.map((name) => payload?.services?.[name]).filter(Boolean),
  );
  const phase = $derived(payload ? (PHASES[payload.phase] ?? payload.phase) : "");
  const market = $derived(payload?.market ?? {});
  const openLogsFor = $derived(
    logService ? (payload?.services?.[logService] ?? { label: logService }) : null,
  );

  async function load() {
    try {
      payload = await getControlStatus();
      error = "";
    } catch (exc) {
      error = String(exc.message || exc);
    }
  }

  async function restart(name) {
    busy = name;
    note = "";
    try {
      await restartService(name);
      note = `${name} restarted; the status refreshes on its own.`;
      await load();
    } catch (exc) {
      note = `${name} could not be restarted: ${String(exc.message || exc)}`;
    } finally {
      busy = "";
    }
  }

  function openLogs(service) {
    logService = service.name;
    logKind = "log";
  }

  function uptime(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    const whole = Math.max(0, Math.round(seconds));
    if (whole < 60) return `${whole}s`;
    if (whole < 3600) return `${Math.floor(whole / 60)}m ${whole % 60}s`;
    return `${Math.floor(whole / 3600)}h ${Math.floor((whole % 3600) / 60)}m`;
  }

  //: A log line's own word for what happened, so the eye can find the two
  //: lines out of three hundred that matter.
  function level(line) {
    if (/\b(ERROR|CRITICAL|Traceback)\b/.test(line)) return "error";
    if (/\b(WARN|WARNING)\b/.test(line)) return "warn";
    return "";
  }

  function when(iso) {
    if (!iso) return "—";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "—";
    const time = date.toLocaleTimeString("en-IN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
    const sameDay = date.toDateString() === new Date().toDateString();
    if (sameDay) return `${time} today`;
    const day = date.toLocaleDateString("en-IN", {
      weekday: "short",
      day: "numeric",
      month: "short",
    });
    return `${day} ${time}`;
  }

  $effect(() => {
    load();
    const poll = setInterval(load, POLL_MS);
    return () => clearInterval(poll);
  });

  $effect(() => {
    if (!logService) return;
    const service = logService;
    const kind = logKind;
    let cancelled = false;
    const tick = async () => {
      try {
        const found = await getServiceLog(service, kind, LOG_LINES);
        if (cancelled) return;
        logLines = found.lines ?? [];
        logFile = found.file ?? "";
        logError = found.exists
          ? ""
          : "no file yet - the service has not written one";
      } catch (exc) {
        if (cancelled) return;
        logLines = [];
        logError = String(exc.message || exc);
      }
    };
    tick();
    const poll = setInterval(tick, LOG_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(poll);
    };
  });
</script>

<section class="head">
  <div>
    <h1>Control</h1>
    <p class="sub">
      One central process runs the framework. Module 1 comes up first, fetches the
      latest data and computes the indicators; Module 4 keeps the reports; Modules 3
      and 2 run for the trading day and rest while the market is closed. This page
      is that process: what is running, what it said, and a restart for each service.
    </p>
  </div>
  <div class="actions">
    <span class="stream">{error || !payload ? "no signal" : "live"}</span>
    <button class="btn" onclick={load}>Refresh</button>
  </div>
</section>

{#if error}
  <div class="banner bad">
    <strong>The central process is not answering.</strong>
    <span>
      {error}. Start it with <code>uv run python -m runner</code> in the v3 folder,
      or double-click <code>start-module-1.cmd</code>.
    </span>
    <button class="btn" onclick={load}>Try again</button>
  </div>
{/if}

{#if note}
  <div class="banner info"><span>{note}</span></div>
{/if}

{#if payload}
  <dl class="strip">
    <div>
      <dt class="label">Phase</dt>
      <dd>{phase}</dd>
    </div>
    <div>
      <dt class="label">Exchange</dt>
      <dd>
        {#if market.is_holiday}
          <span class="dot idle"></span>holiday
        {:else if market.open === true}
          <span class="dot ok"></span>open
        {:else if market.open === false}
          <span class="dot idle"></span>closed
        {:else}
          <span class="dot"></span>unknown
        {/if}
      </dd>
    </div>
    <div>
      <dt class="label">Decision window</dt>
      <dd class="numeric">
        {payload.window?.decision_open_ist}–{payload.window?.decision_close_ist}
        <span class="muted">flat by {payload.window?.framework_close_ist}</span>
      </dd>
    </div>
    <div>
      <dt class="label">Next day start</dt>
      <dd class="numeric">{when(payload.next_day_start)}</dd>
    </div>
    <div>
      <dt class="label">Central process</dt>
      <dd>up {uptime(payload.uptime_seconds)} <span class="muted">since {age(payload.started_at)}</span></dd>
    </div>
  </dl>

  <p class="note">{payload.note}</p>

  <div class="grid">
    <table>
      <thead>
        <tr>
          <th class="left">Service</th>
          <th class="left">State</th>
          <th>Pid</th>
          <th>Up</th>
          <th>Starts</th>
          <th class="left">Health</th>
          <th class="left"></th>
        </tr>
      </thead>
      <tbody>
        {#each services as service (service.name)}
          <tr>
            <th class="left">
              <span class="name">{service.label}</span>
              <span class="meta">{service.name} · {service.url}</span>
            </th>
            <td class="left">
              <span class="chip {service.state}">{service.state}</span>
              <span class="meta">{service.detail}</span>
            </td>
            <td class="numeric">{service.pid ?? "—"}</td>
            <td class="numeric">{uptime(service.uptime_seconds)}</td>
            <td class="numeric">{service.restarts}</td>
            <td class="left">
              {#if service.health === true}
                <span class="dot ok"></span>ok
              {:else if service.health === false}
                <span class="dot bad"></span>{service.health_detail ?? "failing"}
              {:else}
                <span class="muted">—</span>
              {/if}
            </td>
            <td class="left actions-cell">
              <button class="btn small" onclick={() => openLogs(service)}>Logs</button>
              <button
                class="btn small accent"
                onclick={() => restart(service.name)}
                disabled={busy === service.name}
              >
                {busy === service.name ? "Restarting…" : service.state === "resting" ? "Start" : "Restart"}
              </button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>

  {#if logService}
    <section class="logs">
      <header>
        <h2>{openLogsFor?.label ?? logService} logs</h2>
        <div class="kinds">
          <button
            class="btn small"
            class:on={logKind === "log"}
            onclick={() => (logKind = "log")}
          >
            Service log
          </button>
          <button
            class="btn small"
            class:on={logKind === "process"}
            onclick={() => (logKind = "process")}
          >
            Process output
          </button>
        </div>
        <span class="file">
          {logLines.length} lines · refreshing · {logFile}
        </span>
        <button class="btn small" onclick={() => (logService = null)}>Close</button>
      </header>
      {#if logError}
        <p class="meta">{logError}</p>
      {/if}
      <pre>{#if logLines.length === 0}<span class="muted">nothing written yet</span>{:else}{#each logLines as line, index (index)}<span class:error={level(line) === "error"} class:warn={level(line) === "warn"}>{line}{index < logLines.length - 1 ? "\n" : ""}</span>{/each}{/if}</pre>
    </section>
  {/if}
{/if}

<style>
  .head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-4);
    margin-bottom: var(--space-4);
  }

  h1 {
    margin: 0 0 2px;
    font-size: var(--step-3);
    font-weight: 650;
    letter-spacing: -0.02em;
  }

  .sub {
    margin: 0;
    max-width: 88ch;
    color: var(--muted);
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

  .banner.info {
    background: var(--accent-soft);
    border: 1px solid var(--line-2);
    color: var(--ink-2);
  }

  .banner button {
    margin-left: auto;
  }

  code {
    padding: 1px 4px;
    background: var(--surface-2);
    border-radius: 4px;
    font-size: var(--step--1);
  }

  .strip {
    display: flex;
    flex-wrap: wrap;
    margin: 0 0 var(--space-3);
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

  .note {
    margin: 0 0 var(--space-4);
    color: var(--ink-2);
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
    padding: 8px 9px;
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

  .name {
    display: block;
    font-weight: 620;
    color: var(--ink);
  }

  .meta {
    display: block;
    max-width: 44ch;
    white-space: normal;
    font-size: var(--step--1);
    color: var(--muted);
    font-weight: 450;
  }

  .muted {
    color: var(--muted);
    font-weight: 450;
  }

  td.actions-cell {
    white-space: nowrap;
  }

  .actions-cell button + button {
    margin-left: var(--space-2);
  }

  .chip {
    display: inline-block;
    margin-bottom: 3px;
    padding: 2px 9px;
    border: 1px solid var(--line-2);
    border-radius: 999px;
    background: var(--surface);
    font-size: var(--step--1);
    font-weight: 600;
    letter-spacing: 0.03em;
  }

  .chip.running {
    background: var(--up-soft);
    border-color: var(--up);
    color: var(--up);
  }

  .chip.resting {
    background: var(--surface);
    color: var(--muted);
  }

  .chip.starting,
  .chip.waiting,
  .chip.restarting {
    background: var(--warn-soft);
    border-color: var(--warn);
    color: var(--warn);
  }

  .chip.failed {
    background: var(--down-soft);
    border-color: var(--down);
    color: var(--down);
  }

  .logs {
    margin-top: var(--space-4);
    border: 1px solid var(--line-2);
    border-radius: var(--radius);
    background: var(--surface);
  }

  .logs header {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-2) var(--space-3);
    border-bottom: 1px solid var(--line);
  }

  .logs h2 {
    margin: 0;
    font-size: var(--step-0);
    font-weight: 620;
  }

  .kinds {
    display: flex;
    gap: var(--space-2);
  }

  .file {
    margin-left: auto;
    max-width: 50ch;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--step--1);
    color: var(--faint);
  }

  .logs pre {
    margin: 0;
    padding: var(--space-3);
    max-height: 380px;
    overflow: auto;
    white-space: pre-wrap;
    word-break: break-word;
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: var(--step--1);
    line-height: 1.5;
    color: var(--ink-2);
  }

  /* The two lines out of three hundred that matter, in their own colour. */
  .logs pre .error {
    color: var(--down);
  }

  .logs pre .warn {
    color: var(--warn);
  }

  .logs pre .muted {
    color: var(--muted);
  }

  @media (max-width: 900px) {
    .head {
      flex-direction: column;
      align-items: flex-start;
    }

    .logs header {
      flex-wrap: wrap;
    }

    .file {
      margin-left: 0;
    }
  }
</style>

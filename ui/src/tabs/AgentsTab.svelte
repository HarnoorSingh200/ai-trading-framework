<script>
  /**
   * The Agent engine tab.
   *
   * Agents are defined here, started here, and watched here. The important part
   * is the feed: an agent that decides "ignore" thirty times in a row is
   * working, and the only way to see that it is working is to watch what it
   * asked for and what it was told. So every tool call, its arguments, its
   * result and the model's own words arrive as they happen.
   */

  import {
    agentStreamUrl,
    clearAgentNotes,
    createAgent,
    deleteAgentNote,
    getAgentActivity,
    getAgentDecisions,
    getAgentNotes,
    listAgents,
    restingReport,
    retireAgent,
    startAgent,
    stopAgent,
    updateAgent,
  } from "../lib/api.js";
  import { DEFAULT_SYSTEM_PROMPT } from "../lib/defaultPrompt.js";
  import { age, clock } from "../lib/format.js";

  const TIMEFRAMES = ["1m", "5m", "10m", "15m", "20m", "30m", "60m", "1d"];
  const MODELS = ["deepseek-v4-flash", "gpt-6-luna"];
  const REASONING_EFFORTS = ["none", "low", "medium", "high", "xhigh", "max"];
  const FEED_LIMIT = 400;
  const POLL_MS = 5000;
  //: How much of a long tool result is shown before the rest is folded away.
  //: A screen's answer runs to thousands of characters, and a wall of JSON
  //: pushed everything else out of the feed.
  const PREVIEW = 240;

  let agents = $state([]);
  let selectedId = $state(null);
  let activity = $state([]);
  let decisions = $state([]);
  let notes = $state([]);
  let notesMeta = $state({ count: 0, capacity: 24 });
  let defaults = $state({ model: "deepseek-v4-flash", reasoning_effort: "medium" });
  let error = $state("");
  //: Set when the module is down *because the central process is resting it
  //: for the market*: "not answering" is then expected, not a fault.
  let resting = $state(null);
  let busy = $state(false);
  let streaming = $state(false);
  let editing = $state(false);
  let draft = $state(emptyDraft());

  const selected = $derived(agents.find((agent) => agent.agent_id === selectedId) ?? null);
  const derived = $derived(selected?.derived ?? {});
  const modelOptions = $derived(
    [...new Set([defaults.model, ...MODELS].filter(Boolean))],
  );

  function emptyDraft() {
    return {
      agent_id: "",
      name: "",
      timeframe: "15m",
      context: "",
      model: defaults.model,
      reasoning_effort: defaults.reasoning_effort ?? "medium",
      system_prompt: DEFAULT_SYSTEM_PROMPT,
      capital: 50000,
      leverage: 4,
      risk_per_trade_pct: 0.5,
      max_capital_per_trade_pct: 20,
      max_hold_minutes: 60,
      allow_short: true,
      enabled: true,
    };
  }

  function draftFrom(agent) {
    return {
      agent_id: agent.agent_id,
      name: agent.name,
      timeframe: agent.timeframe,
      context: (agent.context_timeframes ?? []).join(", "),
      model: agent.model,
      reasoning_effort: agent.reasoning_effort ?? "medium",
      system_prompt: agent.system_prompt,
      capital: Number(agent.capital),
      leverage: Number(agent.leverage),
      risk_per_trade_pct: Number(agent.risk_per_trade_pct),
      max_capital_per_trade_pct: Number(agent.max_capital_per_trade_pct),
      max_hold_minutes: Number(agent.max_hold_minutes),
      allow_short: Boolean(agent.allow_short),
      enabled: agent.enabled !== false,
    };
  }

  function payloadFrom(form) {
    return {
      agent_id: form.agent_id.trim(),
      name: form.name.trim() || form.agent_id.trim(),
      timeframe: form.timeframe,
      context_timeframes: form.context
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean),
      model: form.model.trim() || defaults.model,
      reasoning_effort: form.reasoning_effort,
      system_prompt: form.system_prompt,
      capital: Number(form.capital),
      leverage: Number(form.leverage),
      risk_per_trade_pct: Number(form.risk_per_trade_pct),
      max_capital_per_trade_pct: Number(form.max_capital_per_trade_pct),
      max_hold_minutes: Number(form.max_hold_minutes),
      allow_short: Boolean(form.allow_short),
      enabled: Boolean(form.enabled),
    };
  }

  async function guard(work) {
    busy = true;
    try {
      await work();
      error = "";
    } catch (exc) {
      error = String(exc.message || exc);
    } finally {
      busy = false;
    }
  }

  async function loadAgents() {
    try {
      const payload = await listAgents();
      agents = payload.agents ?? [];
      defaults = { ...defaults, ...(payload.settings ?? {}) };
      error = "";
      resting = null;
      // Never adopt a selection while a form is open. This poll runs every few
      // seconds, and taking the selection back mid-form turns Save from a
      // create into an edit of whichever agent is running - which the service
      // refuses, so the operator is told to stop an agent they were not
      // touching. It would be worse if it were not refused: the new agent's
      // settings would silently overwrite the existing one.
      if (!selectedId && !editing && agents.length) selectedId = agents[0].agent_id;
    } catch (exc) {
      error = String(exc.message || exc);
      agents = [];
      resting = await restingReport("module2");
    }
  }

  async function loadLogs(agentId) {
    try {
      const [events, orders] = await Promise.all([
        getAgentActivity(agentId, FEED_LIMIT),
        getAgentDecisions(agentId, 50),
      ]);
      activity = events.events ?? [];
      decisions = orders.decisions ?? [];
    } catch (exc) {
      error = String(exc.message || exc);
    }
  }

  async function loadNotes(agentId) {
    try {
      const payload = await getAgentNotes(agentId);
      const { notes: rows = [], ...meta } = payload;
      notes = rows;
      notesMeta = {
        count: payload.count ?? rows.length,
        capacity: payload.capacity ?? 24,
        ...meta,
      };
    } catch {
      notes = [];
    }
  }

  const removeNote = (key) =>
    guard(async () => {
      await deleteAgentNote(selectedId, key);
      await loadNotes(selectedId);
    });

  async function clearNotes() {
    if (!selectedId) return;
    if (!confirm(`Delete every note ${selectedId} has written?`)) return;
    await guard(async () => {
      await clearAgentNotes(selectedId);
      await loadNotes(selectedId);
    });
  }

  function beginNew() {
    selectedId = null;
    draft = emptyDraft();
    editing = true;
  }

  function beginEdit() {
    if (!selected) return;
    draft = draftFrom(selected);
    editing = true;
  }

  function select(agentId) {
    // Clicking the agent that is already selected is not a change: clearing the
    // lists would blank the panel until the next poll, because the effects that
    // load them only re-run when the id actually moves.
    if (agentId !== selectedId) {
      activity = [];
      decisions = [];
      notes = [];
      notesMeta = { count: 0, capacity: 24 };
    }
    selectedId = agentId;
    editing = false;
  }

  async function save() {
    await guard(async () => {
      const payload = payloadFrom(draft);
      if (selectedId) {
        await updateAgent(selectedId, payload);
      } else {
        await createAgent(payload);
      }
      editing = false;
      await loadAgents();
      selectedId = payload.agent_id;
    });
  }

  const run = (agentId) => guard(async () => {
    await startAgent(agentId);
    await loadAgents();
  });

  const halt = (agentId) => guard(async () => {
    await stopAgent(agentId);
    await loadAgents();
  });

  const retire = (agentId) => guard(async () => {
    await retireAgent(agentId);
    selectedId = null;
    await loadAgents();
  });

  $effect(() => {
    loadAgents();
    const poll = setInterval(loadAgents, POLL_MS);
    return () => clearInterval(poll);
  });

  $effect(() => {
    if (!selectedId) return;
    loadLogs(selectedId);
  });

  // The notebook is the agent's memory, so the operator watches it the same
  // way they watch its activity: it is read with the registry, on the poll.
  $effect(() => {
    const agentId = selectedId;
    if (!agentId) {
      notes = [];
      notesMeta = { count: 0, capacity: 24 };
      return;
    }
    let stopped = false;
    const load = async () => {
      try {
        const payload = await getAgentNotes(agentId);
        if (stopped) return;
        const { notes: rows = [], ...meta } = payload;
        notes = rows;
        notesMeta = {
          count: payload.count ?? rows.length,
          capacity: payload.capacity ?? 24,
          ...meta,
        };
      } catch {
        if (!stopped) notes = [];
      }
    };
    load();
    const poll = setInterval(load, POLL_MS);
    return () => {
      stopped = true;
      clearInterval(poll);
    };
  });

  $effect(() => {
    const agentId = selectedId;
    if (!agentId) return;
    let stopped = false;
    let source = null;
    let reconnect = null;
    const onActivity = (event) => {
      try {
        const item = JSON.parse(event.data);
        activity = [...activity, item].slice(-FEED_LIMIT);
      } catch {
        // A frame we cannot read is not worth dropping the stream for.
      }
    };
    const onDecision = (event) => {
      try {
        const item = JSON.parse(event.data);
        decisions = [item, ...decisions].slice(0, 50);
      } catch {
        // Ignored on purpose.
      }
    };
    // A refused stream (the proxy answers 500 while Module 2 is down) is dead
    // for good as far as the browser is concerned; the tab rebuilds it, so the
    // feed starts working when the control service comes up.
    const connect = () => {
      if (stopped) return;
      source = new EventSource(agentStreamUrl(agentId));
      source.addEventListener("activity", onActivity);
      source.addEventListener("decision", onDecision);
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
      streaming = false;
      if (reconnect) clearTimeout(reconnect);
      source?.close();
    };
  });

  function feedLabel(event) {
    switch (event.type) {
      case "cycle_started":
        return "cycle";
      case "cycle_finished":
        return "cycle finished";
      case "tool_call":
        return `→ ${event.tool}`;
      case "tool_result":
        return event.ok ? "← result" : "← refused";
      case "model_text":
        return "model";
      case "decision":
        return "decision";
      case "model_error":
        return "model error";
      case "cycle_failed":
        return "cycle failed";
      case "cycle_no_decision":
        return "no decision";
      case "budget_exhausted":
        return "tool budget";
      case "agent_started":
        return "started";
      case "agent_stopped":
        return "stopped";
      case "session_checkpointed":
        return "EOD memory saved";
      case "market_error":
        return "market error";
      case "skipped":
        return "skipped";
      default:
        return event.type ?? "event";
    }
  }

  function feedDetail(event) {
    if (event.type === "tool_call") return JSON.stringify(event.arguments ?? {});
    if (event.type === "cycle_started") return `on the bar that opened ${event.bar_open_time ?? "—"}`;
    if (event.type === "model_text") {
      const more = event.truncated ? "\n[… the model said more than this]" : "";
      return `${event.text ?? ""}${more}`;
    }
    if (event.type === "decision") {
      const order = event.decision ?? {};
      const size = order.quantity ? ` × ${order.quantity}` : "";
      return `${order.action ?? "?"}${order.symbol ? ` ${order.symbol}` : ""}${size} — ${order.reason ?? ""}`;
    }
    if (event.type === "agent_started") return `timeframe ${event.timeframe}, model ${event.model}`;
    if (event.type === "cycle_finished") {
      const seconds = Number(event.duration_ms || 0) / 1000;
      return `${seconds.toFixed(1)}s wall time · ${event.decision_count ?? 0} decisions · ${event.notes_kept ? "notebook kept" : "notebook missing"}`;
    }
    if (event.type === "session_checkpointed") {
      return `${event.day ?? "day"} · ${event.count ?? 0} lessons saved for the next session`;
    }
    return event.reason || event.error || event.message || event.text || "";
  }

  function tone(type) {
    if (type === "decision" || type === "session_checkpointed") return "good";
    if (type === "tool_result") return "quiet";
    if (type === "cycle_failed" || type === "model_error" || type === "market_error") return "bad";
    if (type === "budget_exhausted" || type === "skipped" || type === "cycle_no_decision") return "warn";
    return "";
  }

  function orderHeadline(order) {
    if (order.action === "ignore") return "no trade";
    const bits = [order.action, order.symbol];
    if (order.quantity) bits.push(`× ${order.quantity}`);
    return bits.filter(Boolean).join(" ");
  }
</script>

<section class="head">
  <div>
    <h1>Agent engine</h1>
    <p class="sub">
      One process per agent, each deciding on the closed candle of its own timeframe.
      Nothing here places a real order: a decision is an order <em>proposal</em>, and it
      stays on this page.
    </p>
  </div>
  <div class="actions">
    <span class="stream">{streaming ? "live" : "polling"}</span>
    <button class="btn primary" onclick={beginNew} disabled={busy}>New agent</button>
  </div>
</section>

{#if error}
  {#if resting}
    <div class="banner calm">
      <strong>{resting.headline}</strong>
      <span>{resting.note}.</span>
      <span>
        The agent engine exists only for the trading day; use Start on the Control
        tab to see and edit the agents while the market rests.
      </span>
      <button class="btn" onclick={loadAgents}>Check again</button>
    </div>
  {:else}
    <div class="banner bad">
      <strong>Module 2 is not answering.</strong>
      <span>{error}</span>
      <button class="btn" onclick={loadAgents}>Try again</button>
    </div>
  {/if}
{/if}

<div class="layout">
  <aside class="list">
    {#if agents.length === 0}
      <p class="empty">No agents yet. Define one and start it.</p>
    {/if}
    {#each agents as agent (agent.agent_id)}
      <button
        class="card"
        class:active={agent.agent_id === selectedId}
        onclick={() => select(agent.agent_id)}
      >
        <span class="row">
          <span class="name">{agent.name}</span>
          <span class="dot" class:on={agent.running}></span>
        </span>
        <span class="row meta">
          <span>{agent.timeframe}</span>
          <span class="muted">{agent.model}</span>
          {#if agent.enabled === false}
            <span class="muted">paused</span>
          {/if}
        </span>
        <span class="row meta">
          <span class="muted">{agent.state?.last_status ?? "not run yet"}</span>
        </span>
      </button>
    {/each}
  </aside>

  <div class="detail">
    {#if editing}
      <section class="panel">
        <h2>{selectedId ? "Edit agent" : "New agent"}</h2>
        <div class="form">
          <label>Id<input bind:value={draft.agent_id} placeholder="momentum-alpha" disabled={Boolean(selectedId)} /></label>
          <label>Name<input bind:value={draft.name} placeholder="Momentum alpha" /></label>
          <label>
            Timeframe
            <select bind:value={draft.timeframe}>
              {#each TIMEFRAMES as option (option)}
                <option value={option}>{option}</option>
              {/each}
            </select>
          </label>
          <label>
            Also reads
            <input bind:value={draft.context} placeholder="1d, 60m" />
          </label>
          <label>
            Model
            <input
              bind:value={draft.model}
              list="agent-model-options"
              placeholder="gpt-6-luna"
              autocomplete="off"
            />
            <datalist id="agent-model-options">
              {#each modelOptions as model (model)}
                <option value={model}></option>
              {/each}
            </datalist>
          </label>
          <label>
            Reasoning effort
            <select bind:value={draft.reasoning_effort}>
              {#each REASONING_EFFORTS as option (option)}
                <option value={option}>{option}</option>
              {/each}
            </select>
          </label>
          <label>Capital<input type="number" bind:value={draft.capital} /></label>
          <label>Leverage<input type="number" step="0.5" bind:value={draft.leverage} /></label>
          <label>Risk per trade %<input type="number" step="0.1" bind:value={draft.risk_per_trade_pct} /></label>
          <label>Max in one position %<input type="number" step="1" bind:value={draft.max_capital_per_trade_pct} /></label>
          <label>Max hold (minutes)<input type="number" step="5" bind:value={draft.max_hold_minutes} /></label>
          <label class="check">
            <input type="checkbox" bind:checked={draft.allow_short} />
            May open a short
          </label>
          <label class="check">
            <input type="checkbox" bind:checked={draft.enabled} />
            Enabled in the fleet
          </label>
        </div>
        <label class="prompt">
          System prompt
          <textarea bind:value={draft.system_prompt} rows="10"
            placeholder="Your instructions to this agent. It is quoted verbatim."></textarea>
        </label>
        <div class="actions">
          <button class="btn primary" onclick={save} disabled={busy}>Save</button>
          <button class="btn" onclick={() => (editing = false)} disabled={busy}>Cancel</button>
        </div>
      </section>
    {:else if selected}
      <section class="panel">
        <header class="identity">
          <div>
            <h2>{selected.name}</h2>
            <p class="muted">
              {selected.agent_id} · {selected.timeframe}
              {selected.context_timeframes?.length ? ` (+${selected.context_timeframes.join(", ")})` : ""}
              · {selected.model}
              {#if selected.enabled === false}· paused{/if}
            </p>
          </div>
          <div class="actions">
            {#if selected.running}
              <button class="btn" onclick={() => halt(selected.agent_id)} disabled={busy}>Stop</button>
            {:else}
              <button
                class="btn accent"
                onclick={() => run(selected.agent_id)}
                disabled={busy || selected.enabled === false}
              >
                {selected.enabled === false ? "Disabled" : "Start"}
              </button>
            {/if}
            <button class="btn" onclick={beginEdit} disabled={busy || selected.running}>Edit</button>
            <button class="btn danger" onclick={() => retire(selected.agent_id)} disabled={busy}>Retire</button>
          </div>
        </header>

        <dl class="facts">
          <div>
            <dt class="label">State</dt>
            <dd>
              {selected.enabled === false
                ? "disabled (not in the fleet)"
                : selected.running
                  ? `running (pid ${selected.pid})`
                  : "stopped"}
            </dd>
          </div>
          <div><dt class="label">Last bar</dt><dd class="numeric">{selected.state?.last_attempted_bar ?? "—"}</dd></div>
          <div><dt class="label">Last outcome</dt><dd>{selected.state?.last_status ?? "—"}</dd></div>
          <div><dt class="label">Reasoning</dt><dd>{selected.reasoning_effort ?? "default"}</dd></div>
          <div><dt class="label">Buying power</dt><dd class="numeric">{derived.buying_power ?? "—"}</dd></div>
          <div><dt class="label">Risk per trade</dt><dd class="numeric">{derived.risk_per_trade ?? "—"}</dd></div>
          <div><dt class="label">Max notional</dt><dd class="numeric">{derived.max_notional ?? "—"}</dd></div>
        </dl>

        <p class="note">
          {selected.description || "No description."} It decides at the close of every
          {selected.timeframe} candle, and one candle is never decided twice —
          the bar it last attempted is remembered on disk.
        </p>
      </section>

      <section class="panel notebook">
        <h2>
          Notebook
          <span class="muted">
            {notesMeta.count} of {notesMeta.capacity} keys · written by the agent
          </span>
          {#if notes.length}
            <button class="btn small" onclick={clearNotes} disabled={busy}>Clear all</button>
          {/if}
        </h2>
        {#if notesMeta.last_checkpoint}
          <p class="notebook-checkpoint">
            End-of-day checkpoint saved {age(notesMeta.last_checkpoint.saved_at)}
            for {notesMeta.last_checkpoint.day} with
            {notesMeta.last_checkpoint.count ?? 0} lesson{(notesMeta.last_checkpoint.count ?? 0) === 1 ? "" : "s"}.
            The next session receives this notebook automatically.
          </p>
        {/if}
        {#if notes.length === 0}
          <p class="empty">
            Nothing recorded yet. After its decisions, before it sleeps, the agent
            writes down what the candle taught it — that is the only memory that
            reaches the next session.
          </p>
        {:else}
          <ul class="notes">
            {#each notes as note (note.key)}
              <li>
                <div class="note-head">
                  <code>{note.key}</code>
                  <span class="muted">
                    {note.updated_at ? `updated ${age(note.updated_at)}` : ""}
                    {note.edits > 1 ? ` · edited ${note.edits}×` : ""}
                  </span>
                  <button
                    class="btn small"
                    onclick={() => removeNote(note.key)}
                    disabled={busy}
                    title="Delete this lesson"
                  >
                    Delete
                  </button>
                </div>
                <p>{note.text}</p>
              </li>
            {/each}
          </ul>
        {/if}
      </section>

      <section class="panel">
        <h2>Live feed <span class="muted">what it asked for, and what it was told</span></h2>
        {#if activity.length === 0}
          <p class="empty">Nothing yet. Start the agent to watch it work.</p>
        {:else}
          <ol class="feed">
            {#each [...activity].reverse() as event, index (`${event.cycle_id ?? "x"}-${event.type}-${index}`)}
              {@const detail = feedDetail(event)}
              <li class={tone(event.type)}>
                <span class="when numeric">{clock(event.ts)}</span>
                <span class="what">{feedLabel(event)}</span>
                <span class="detail">
                  {#if detail.length > PREVIEW}
                    <details>
                      <summary>
                        {detail.slice(0, PREVIEW)}…
                        <span class="more">
                          show all {detail.length.toLocaleString("en-IN")} characters
                        </span>
                      </summary>
                      <pre>{detail}</pre>
                    </details>
                  {:else}
                    {detail}
                  {/if}
                </span>
              </li>
            {/each}
          </ol>
        {/if}
      </section>

      <section class="panel">
        <h2>Orders <span class="muted">{decisions.length} recorded</span></h2>
        {#if decisions.length === 0}
          <p class="empty">No decisions recorded yet.</p>
        {:else}
          <div class="orders">
            {#each decisions as order (order.decision_id ?? order.ts)}
              <article class="order" class:ignore={order.action === "ignore"}>
                <header>
                  <strong>{orderHeadline(order)}</strong>
                  <span class="muted numeric">{clock(order.ts)} · bar {order.bar_open_time ?? "—"}</span>
                </header>
                {#if order.action !== "ignore"}
                  <dl class="numbers">
                    <div><dt class="label">Entry</dt><dd class="numeric">{order.entry_price ?? "—"}</dd></div>
                    <div><dt class="label">Target</dt><dd class="numeric up">{order.target ?? "—"}</dd></div>
                    <div><dt class="label">Stop</dt><dd class="numeric down">{order.stop_loss ?? "—"}</dd></div>
                    <div><dt class="label">Quantity</dt><dd class="numeric">{order.quantity}</dd></div>
                    <div><dt class="label">Hold</dt><dd class="numeric">{order.max_hold_minutes ?? "—"}m</dd></div>
                    <div><dt class="label">Risk</dt><dd class="numeric">{order.sizing?.money_at_risk ?? "—"}</dd></div>
                  </dl>
                {/if}
                <p class="reason">{order.reason}</p>
                <details>
                  <summary>Decision JSON</summary>
                  <pre>{JSON.stringify(order, null, 2)}</pre>
                </details>
              </article>
            {/each}
          </div>
        {/if}
      </section>
    {:else}
      <section class="panel">
        <h2>No agent selected</h2>
        <p class="note">
          Define an agent with the button above: an id, one timeframe, your system
          prompt and the capital it may work with. It will screen the universe
          through Module 1, inspect two or three names, and answer with an order or
          an explicit ignore — and every step of that will appear here as it happens.
        </p>
      </section>
    {/if}
  </div>
</div>

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
    max-width: 78ch;
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

  .layout {
    display: grid;
    grid-template-columns: 280px minmax(0, 1fr);
    gap: var(--space-5);
    align-items: start;
  }

  .list {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .card {
    display: flex;
    flex-direction: column;
    gap: 3px;
    padding: var(--space-3);
    text-align: left;
    background: var(--bg);
    border: 1px solid var(--line);
    border-radius: var(--radius);
  }

  .card:hover {
    border-color: var(--line-2);
    background: var(--surface);
  }

  .card.active {
    border-color: var(--accent);
    background: var(--accent-soft);
  }

  .row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
  }

  .row.meta {
    font-size: var(--step--1);
    color: var(--ink-2);
  }

  .name {
    font-weight: 600;
  }

  .dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--line-2);
    flex: none;
  }

  .dot.on {
    background: var(--up);
  }

  .muted {
    color: var(--muted);
    font-weight: 450;
  }

  .detail {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    min-width: 0;
  }

  .panel {
    padding: var(--space-4);
    background: var(--bg);
    border: 1px solid var(--line);
    border-radius: var(--radius);
  }

  .panel h2 {
    display: flex;
    align-items: baseline;
    gap: var(--space-3);
    margin: 0 0 var(--space-3);
    font-size: var(--step-1);
    font-weight: 620;
    letter-spacing: -0.01em;
  }

  .panel h2 .muted {
    font-size: var(--step--1);
    font-weight: 450;
  }

  /* The notebook: what the agent concluded, kept across every session. */
  .notebook h2 button {
    margin-left: auto;
  }

  .notes {
    list-style: none;
    margin: 0;
    padding: 0;
    border-top: 1px solid var(--line);
  }

  .notes li {
    padding: var(--space-2) 0;
    border-bottom: 1px solid var(--line);
  }

  .note-head {
    display: flex;
    align-items: baseline;
    gap: var(--space-3);
  }

  .notebook-checkpoint {
    margin: 0 0 var(--space-3);
    padding: var(--space-2) var(--space-3);
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    color: var(--muted);
    font-size: var(--step--1);
  }

  .note-head code {
    padding: 1px 6px;
    background: var(--surface-2);
    border-radius: 5px;
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: var(--step--1);
    font-weight: 600;
  }

  .note-head .muted {
    margin-right: auto;
    font-size: var(--step--1);
  }

  .notes p {
    margin: 5px 0 0;
    max-width: 90ch;
    color: var(--ink-2);
  }

  .identity {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-4);
  }

  .identity h2 {
    margin: 0;
  }

  .identity p {
    margin: 3px 0 0;
    font-size: var(--step--1);
  }

  .facts,
  .numbers {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-5);
    margin: var(--space-4) 0 var(--space-3);
    padding: var(--space-3) 0;
    border-top: 1px solid var(--line);
    border-bottom: 1px solid var(--line);
  }

  .numbers {
    margin: var(--space-3) 0;
    border-bottom: 0;
    padding-bottom: 0;
  }

  dt {
    margin: 0 0 2px;
  }

  dd {
    margin: 0;
    font-weight: 550;
  }

  .note {
    margin: var(--space-3) 0 0;
    color: var(--ink-2);
    max-width: 80ch;
  }

  .empty {
    margin: var(--space-4) 0;
    color: var(--muted);
  }

  .form {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    gap: var(--space-3);
  }

  label {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: var(--step--1);
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--muted);
  }

  input,
  select,
  textarea {
    padding: 7px 10px;
    background: var(--bg);
    border: 1px solid var(--line-2);
    border-radius: var(--radius-sm);
    font-weight: 450;
    letter-spacing: normal;
    text-transform: none;
  }

  label.check {
    flex-direction: row;
    align-items: center;
    gap: var(--space-2);
    align-self: end;
    padding-bottom: 8px;
    text-transform: none;
    letter-spacing: normal;
  }

  .prompt {
    margin-top: var(--space-3);
  }

  textarea {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: var(--step-0);
    line-height: 1.5;
    resize: vertical;
  }

  .panel .actions {
    margin-top: var(--space-4);
  }

  .feed {
    list-style: none;
    margin: 0;
    padding: 0;
    max-height: 460px;
    overflow-y: auto;
    border-top: 1px solid var(--line);
  }

  .feed li {
    display: grid;
    grid-template-columns: 72px 110px minmax(0, 1fr);
    gap: var(--space-3);
    padding: 6px 0;
    border-bottom: 1px solid var(--line);
    font-size: var(--step--1);
    align-items: baseline;
  }

  .feed li.warn .what {
    color: var(--warn);
  }

  .feed li.bad .what {
    color: var(--down);
  }

  .feed li.good .what {
    color: var(--up);
  }

  .when {
    color: var(--faint);
  }

  .what {
    font-weight: 600;
    color: var(--ink-2);
  }

  .feed .detail {
    color: var(--ink-2);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    word-break: break-word;
    white-space: pre-wrap;
  }

  /* A tool result is worth reading in full, and not worth pushing every other
     line off the screen to do it. */
  .feed .detail summary {
    cursor: pointer;
    color: var(--muted);
  }

  .feed .detail summary:hover {
    color: var(--ink);
  }

  .feed .detail .more {
    font-family: var(--sans);
    font-size: var(--step--1);
    color: var(--accent);
    white-space: nowrap;
  }

  .feed .detail pre {
    margin: var(--space-2) 0 var(--space-1);
    padding: var(--space-2) var(--space-3);
    max-height: 360px;
    overflow: auto;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    font-size: 11px;
    line-height: 1.45;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .orders {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  .order {
    padding: var(--space-3);
    border: 1px solid var(--line);
    border-left: 3px solid var(--up);
    border-radius: var(--radius-sm);
    background: var(--surface);
  }

  .order.ignore {
    border-left-color: var(--line-2);
  }

  .order header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--space-3);
    flex-wrap: wrap;
  }

  .order header strong {
    font-size: var(--step-0);
    font-weight: 620;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .order .reason {
    margin: 0;
    color: var(--ink-2);
  }

  .order details {
    margin-top: var(--space-2);
  }

  .order summary {
    cursor: pointer;
    font-size: var(--step--1);
    color: var(--muted);
  }

  .order pre {
    margin: var(--space-2) 0 0;
    padding: var(--space-3);
    max-height: 320px;
    overflow: auto;
    background: var(--bg);
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    font-size: 12px;
    line-height: 1.5;
  }

  @media (max-width: 1000px) {
    .layout {
      grid-template-columns: minmax(0, 1fr);
    }
  }
</style>

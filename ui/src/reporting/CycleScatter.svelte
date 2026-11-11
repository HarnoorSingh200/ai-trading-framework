<script>
  /**
   * One dot per agent cycle. Switch between the wall time the agent process
   * measured and the tokens the provider reported; both use the same cycle
   * order, so a slow hour and an expensive hour can be read together.
   */

  import { shortDay } from "../lib/reporting.js";

  let { cycles = [], agentColours = {}, selectedAgent = "" } = $props();

  const WIDTH = 920;
  const HEIGHT = 286;
  const PAD = { top: 34, right: 24, bottom: 42, left: 68 };
  const LIMIT = 160;

  let metric = $state("duration");
  let hovered = $state(null);

  const visible = $derived.by(() => {
    const rows = cycles
      .filter((row) => !selectedAgent || row.agent_id === selectedAgent)
      .filter((row) =>
        metric === "duration"
          ? Number.isFinite(Number(row.duration_ms))
          : Number.isFinite(Number(row.total_tokens)),
      );
    return rows.slice(-LIMIT);
  });

  const geometry = $derived.by(() => {
    if (!visible.length) return null;
    const values = visible.map((row) =>
      metric === "duration"
        ? Math.max(0, Number(row.duration_ms) / 1000)
        : Math.max(0, Number(row.total_tokens)),
    );
    const high = Math.max(...values, 1);
    const plotWidth = WIDTH - PAD.left - PAD.right;
    const plotHeight = HEIGHT - PAD.top - PAD.bottom;
    const x = (index) =>
      PAD.left +
      (visible.length === 1 ? plotWidth / 2 : (index / (visible.length - 1)) * plotWidth);
    const y = (value) => PAD.top + (1 - value / high) * plotHeight;
    const ticks = [0, 0.5, 1].map((fraction) => {
      const value = high * fraction;
      return {
        value,
        y: y(value),
        label:
          metric === "duration"
            ? seconds(value)
            : compact(value),
      };
    });
    const points = visible.map((row, index) => ({
      ...row,
      value: values[index],
      x: x(index),
      y: y(values[index]),
      colour: agentColours[row.agent_id] ?? "var(--muted)",
    }));
    const labelIndexes = [...new Set([0, Math.floor((visible.length - 1) / 2), visible.length - 1])];
    return {
      high,
      ticks,
      points,
      xLabels: labelIndexes.map((index) => ({
        x: x(index),
        label: timeLabel(visible[index]),
      })),
      plotWidth,
      plotHeight,
    };
  });

  const agents = $derived([...new Set(visible.map((row) => row.agent_id))]);

  function seconds(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    if (number >= 60) return `${(number / 60).toFixed(1)}m`;
    if (number >= 10) return `${number.toFixed(0)}s`;
    return `${number.toFixed(1)}s`;
  }

  function compact(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    if (number >= 1_000_000) return `${(number / 1_000_000).toFixed(1)}M`;
    if (number >= 1_000) return `${(number / 1_000).toFixed(1)}k`;
    return String(Math.round(number));
  }

  function timeLabel(row) {
    const stamp = row.bar_open_time || row.started_at;
    if (!stamp) return `#${row.cycle_id ?? "cycle"}`;
    const date = new Date(stamp);
    if (Number.isNaN(date.getTime())) return String(stamp).slice(-8, -3);
    return date.toLocaleTimeString("en-IN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  }

  function pointLabel(point) {
    const value =
      metric === "duration"
        ? seconds(point.duration_ms / 1000)
        : `${compact(point.total_tokens)} tokens`;
    return `${point.agent_id} · ${shortDay(point.day)} · ${point.bar_open_time ?? "cycle"} · ${value}`;
  }
</script>

<section class="cycle-chart" aria-label="Cycle cost over time">
  <div class="cycle-head">
    <div class="metric-switch" role="group" aria-label="Cycle chart metric">
      <button
        class:on={metric === "duration"}
        aria-pressed={metric === "duration"}
        onclick={() => (metric = "duration")}
      >
        Wall time
      </button>
      <button
        class:on={metric === "tokens"}
        aria-pressed={metric === "tokens"}
        onclick={() => (metric = "tokens")}
      >
        Tokens
      </button>
    </div>
    <span class="cycle-note">
      {visible.length ? `Showing ${visible.length} latest cycles` : "No timed cycles in this scope"}
    </span>
  </div>

  {#if geometry}
    <svg
      class="plot"
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      role="img"
      aria-label={`${metric === "duration" ? "Cycle wall time" : "Tokens per cycle"} chart for ${visible.length} cycles`}
    >
      {#each geometry.ticks as tick}
        <line
          class="grid-line"
          x1={PAD.left}
          x2={WIDTH - PAD.right}
          y1={tick.y}
          y2={tick.y}
        />
        <text class="tick y-tick" x={PAD.left - 12} y={tick.y + 4} text-anchor="end">
          {tick.label}
        </text>
      {/each}
      <line
        class="axis"
        x1={PAD.left}
        x2={PAD.left}
        y1={PAD.top}
        y2={HEIGHT - PAD.bottom}
      />
      <line
        class="axis"
        x1={PAD.left}
        x2={WIDTH - PAD.right}
        y1={HEIGHT - PAD.bottom}
        y2={HEIGHT - PAD.bottom}
      />

      {#each geometry.points as point, index (point.cycle_id ?? `${point.agent_id}-${index}`)}
        <circle
          class="point"
          class:dim={hovered && hovered !== index}
          cx={point.x}
          cy={point.y}
          r="4.8"
          style={`fill: ${point.colour}`}
          tabindex="0"
          role="button"
          aria-label={pointLabel(point)}
          onmouseenter={() => (hovered = index)}
          onmouseleave={() => (hovered = null)}
          onfocus={() => (hovered = index)}
          onblur={() => (hovered = null)}
        >
          <title>{pointLabel(point)}</title>
        </circle>
      {/each}

      {#each geometry.xLabels as label}
        <text class="tick x-tick" x={label.x} y={HEIGHT - 16} text-anchor="middle">
          {label.label}
        </text>
      {/each}

      {#if hovered !== null}
        {@const point = geometry.points[hovered]}
        <g class="tooltip" transform={`translate(${Math.min(point.x + 10, WIDTH - 250)} ${Math.max(point.y - 48, 12)})`}>
          <rect width="238" height="38" rx="6" />
          <text x="9" y="15">
            {point.agent_id} · {point.bar_open_time ?? "cycle"}
          </text>
          <text class="tooltip-value" x="9" y="29">
            {metric === "duration"
              ? `${seconds(Number(point.duration_ms) / 1000)} wall time`
              : `${compact(Number(point.total_tokens))} tokens`}
          </text>
        </g>
      {/if}
    </svg>

    {#if agents.length > 1}
      <div class="legend" aria-label="Agents in this chart">
        {#each agents as agentId (agentId)}
          <span>
            <i style={`background: ${agentColours[agentId] ?? "var(--muted)"}`}></i>
            {agentId}
          </span>
        {/each}
      </div>
    {/if}
  {:else}
    <p class="empty">
      This scope has no cycle rows with {metric === "duration" ? "wall-time" : "token"} data yet.
    </p>
  {/if}
</section>

<style>
  .cycle-chart {
    display: grid;
    gap: var(--space-3);
  }

  .cycle-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
  }

  .metric-switch {
    display: inline-flex;
    gap: 2px;
    padding: 2px;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 7px;
  }

  .metric-switch button {
    min-height: 28px;
    padding: 3px 9px;
    background: transparent;
    border: 0;
    border-radius: 5px;
    color: var(--muted);
    font-size: var(--step--1);
    font-weight: 600;
  }

  .metric-switch button.on {
    background: var(--bg);
    color: var(--ink);
    box-shadow: var(--shadow-sm);
  }

  .cycle-note {
    color: var(--faint);
    font-size: var(--step--1);
  }

  .plot {
    display: block;
    width: 100%;
    height: auto;
    overflow: visible;
  }

  .grid-line {
    stroke: var(--line);
    stroke-width: 1;
  }

  .axis {
    stroke: var(--line-2);
    stroke-width: 1;
  }

  .tick {
    fill: var(--faint);
    font-family: var(--sans);
    font-size: 10px;
  }

  .point {
    stroke: var(--bg);
    stroke-width: 1.5;
    cursor: crosshair;
    transition: opacity 0.12s ease, r 0.12s ease;
  }

  .point:hover,
  .point:focus {
    r: 6.5;
    outline: none;
  }

  .point.dim {
    opacity: 0.35;
  }

  .tooltip rect {
    fill: var(--surface-2);
    stroke: var(--line-2);
  }

  .tooltip text {
    fill: var(--ink-2);
    font-family: var(--sans);
    font-size: 10px;
  }

  .tooltip .tooltip-value {
    fill: var(--ink);
    font-weight: 650;
  }

  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2) var(--space-4);
    color: var(--muted);
    font-size: var(--step--1);
  }

  .legend span {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }

  .legend i {
    width: 8px;
    height: 8px;
    border-radius: 50%;
  }

  .empty {
    margin: var(--space-5) 0;
    color: var(--muted);
    text-align: center;
  }

  @media (max-width: 620px) {
    .cycle-head {
      align-items: flex-start;
      flex-direction: column;
    }

    .metric-switch button {
      min-height: 44px;
    }
  }
</style>

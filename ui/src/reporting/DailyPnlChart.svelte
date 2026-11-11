<script>
  /**
   * Daily realised P&L, with each agent's contribution stacked on the day.
   * Positive and negative stacks grow away from zero independently, so a
   * winning agent cannot visually erase a losing one.
   *
   * Each day is also a filter control: click it, or focus it and press Enter,
   * to scope the rest of the Reporting tab to that exchange day.
   */

  import { compactRupees, signedRupees } from "../lib/format.js";
  import { shortDay } from "../lib/reporting.js";

  let {
    days = [],
    agentColours = {},
    selectedDay = "",
    onSelectDay = null,
  } = $props();

  const WIDTH = 860;
  const HEIGHT = 276;
  const PAD = { top: 58, right: 24, bottom: 44, left: 72 };

  let hoveredDay = $state(null);

  const geometry = $derived.by(() => {
    const ordered = [...days].reverse();
    if (!ordered.length) return null;
    const agentOrder = Object.keys(agentColours);
    const prepared = ordered.map((day) => {
      let positive = 0;
      let negative = 0;
      const segments = [];
      for (const agentId of agentOrder) {
        const agent = (day.agents ?? []).find((row) => row.agent_id === agentId);
        const value = Number(agent?.realised ?? 0);
        if (!value) continue;
        const from = value >= 0 ? positive : negative;
        const to = from + value;
        if (value >= 0) positive = to;
        else negative = to;
        segments.push({
          agentId,
          value,
          from,
          to,
          colour: agentColours[agentId] ?? "var(--muted)",
        });
      }
      return {
        day: day.day,
        label: shortDay(day.day),
        total: positive + negative,
        closed: day.closed,
        orders: day.orders,
        segments,
      };
    });
    const values = prepared.flatMap((day) => [0, day.total]);
    const rawLow = Math.min(...values);
    const rawHigh = Math.max(...values);
    const span = Math.max(rawHigh - rawLow, Math.abs(rawHigh) * 0.08, 1);
    const low = rawLow - span * 0.12;
    const high = rawHigh + span * 0.12;
    const plotWidth = WIDTH - PAD.left - PAD.right;
    const plotHeight = HEIGHT - PAD.top - PAD.bottom;
    const slot = plotWidth / prepared.length;
    const barWidth = Math.max(22, Math.min(84, slot * 0.54));
    const y = (value) =>
      PAD.top + ((high - value) / (high - low || 1)) * plotHeight;
    const bars = prepared.map((day, index) => ({
      ...day,
      x: PAD.left + slot * index + slot / 2,
      bandX: PAD.left + slot * index,
      segments: day.segments.map((segment) => ({
        ...segment,
        top: y(Math.max(segment.from, segment.to)),
        height: Math.max(1, Math.abs(y(segment.from) - y(segment.to))),
      })),
      labelY: day.total >= 0 ? y(day.total) - 9 : y(day.total) + 16,
    }));
    const yTicks = [0, 1, 2, 3].map((index) => {
      const value = high - ((high - low) * index) / 3;
      return { y: y(value), value };
    });
    return {
      bars,
      barWidth,
      plotHeight,
      slot,
      yTicks,
      zeroY: y(0),
      labels: Object.keys(agentColours).map((agentId) => ({
        agentId,
        colour: agentColours[agentId],
      })),
    };
  });

  const activeDay = $derived.by(() => {
    const id = hoveredDay ?? selectedDay;
    return geometry?.bars.find((day) => day.day === id) ?? null;
  });

  const tooltipLeft = $derived(
    activeDay && geometry
      ? Math.max(10, Math.min(90, (activeDay.x / WIDTH) * 100))
      : 50,
  );

  const description = $derived(
    geometry
      ? geometry.bars
          .map((day) => `${day.label}: ${signedRupees(day.total)}`)
          .join(", ")
      : "No daily results to chart",
  );

  function chooseDay(day) {
    if (typeof onSelectDay === "function") onSelectDay(day);
  }

  function onDayKeyDown(event, day) {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    chooseDay(day);
  }
</script>

{#if geometry}
  <div class="chart-wrap">
    <div class="interaction-head">
      <span class="hint">
        Select a day to scope every chart and the long book. Select it again to
        clear the day filter.
      </span>
      <span class="hint numeric">
        {activeDay
          ? `${activeDay.label} · ${activeDay.closed} closed`
          : `${geometry.bars.length} exchange days`}
      </span>
    </div>

    <div class="plot">
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label={description}>
        {#each geometry.yTicks as tick}
          <line
            class="grid"
            x1={PAD.left}
            x2={WIDTH - PAD.right}
            y1={tick.y}
            y2={tick.y}
          />
          <text class="axis" x={PAD.left - 10} y={tick.y + 4}>
            {compactRupees(tick.value)}
          </text>
        {/each}

        <line
          class="zero"
          x1={PAD.left}
          x2={WIDTH - PAD.right}
          y1={geometry.zeroY}
          y2={geometry.zeroY}
        />

        {#each geometry.bars as day (day.day)}
          <g
            class="day-group"
            role="button"
            tabindex="0"
            aria-pressed={selectedDay === day.day}
            aria-label={`${day.label}: ${signedRupees(day.total)}, ${day.closed} closed trades. Press to ${selectedDay === day.day ? "clear the day filter" : "filter the report to this day"}.`}
            onclick={() => chooseDay(day.day)}
            onkeydown={(event) => onDayKeyDown(event, day.day)}
            onpointerenter={() => (hoveredDay = day.day)}
            onpointerleave={() => (hoveredDay = null)}
            onfocus={() => (hoveredDay = day.day)}
            onblur={() => (hoveredDay = null)}
          >
            <rect
              class="day-band"
              class:selected={selectedDay === day.day}
              class:hovered={hoveredDay === day.day}
              x={day.bandX + 3}
              y={PAD.top - 8}
              width={geometry.slot - 6}
              height={geometry.plotHeight + 16}
              rx="5"
            />
            {#each day.segments as segment (segment.agentId)}
              <rect
                x={day.x - geometry.barWidth / 2}
                y={segment.top}
                width={geometry.barWidth}
                height={segment.height}
                rx="2"
                style={`fill: ${segment.colour}`}
              >
                <title>{day.label} · {segment.agentId}: {signedRupees(segment.value)}</title>
              </rect>
            {/each}
            <text
              class="total numeric"
              class:up={day.total > 0}
              class:down={day.total < 0}
              x={day.x}
              y={day.labelY}
              text-anchor="middle"
            >
              {signedRupees(day.total)}
            </text>
            <text class="axis x" x={day.x} y={HEIGHT - 22} text-anchor="middle">
              {day.label}
            </text>
            <text class="axis x sub" x={day.x} y={HEIGHT - 8} text-anchor="middle">
              {day.closed} closed
            </text>
          </g>
        {/each}
      </svg>

      {#if activeDay}
        <div
          class="tooltip"
          aria-live="polite"
          style={`left: ${tooltipLeft}%`}
        >
          <div class="tooltip-head">
            <strong>{activeDay.label}</strong>
            <span class={activeDay.total > 0 ? "up" : activeDay.total < 0 ? "down" : ""}>
              {signedRupees(activeDay.total)}
            </span>
          </div>
          <div class="agent-breakdown">
            {#each activeDay.segments as segment (segment.agentId)}
              <span>
                <i style={`background: ${segment.colour}`}></i>
                {segment.agentId}
                <strong class="numeric">{signedRupees(segment.value)}</strong>
              </span>
            {/each}
          </div>
          <div class="tooltip-meta">
            {activeDay.closed} closed · {activeDay.orders} orders
            {selectedDay === activeDay.day ? " · selected" : ""}
          </div>
        </div>
      {/if}
    </div>

    <div class="legend" aria-hidden="true">
      {#each geometry.labels as item (item.agentId)}
        <span class="legend-item">
          <span class="swatch" style={`background: ${item.colour}`}></span>
          {item.agentId}
        </span>
      {/each}
    </div>
  </div>
{:else}
  <p class="empty">No exchange day has a settled result yet.</p>
{/if}

<style>
  .chart-wrap {
    position: relative;
    min-width: 0;
  }

  .interaction-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--space-3);
  }

  .hint {
    color: var(--faint);
    font-size: 10px;
  }

  .plot {
    position: relative;
  }

  svg {
    display: block;
    width: 100%;
    height: auto;
    overflow: visible;
  }

  .grid {
    stroke: var(--line);
    stroke-width: 1;
  }

  .zero {
    stroke: var(--line-2);
    stroke-width: 1.2;
  }

  .axis {
    fill: var(--faint);
    font-size: 10px;
    font-variant-numeric: tabular-nums;
    text-anchor: end;
  }

  .axis.x {
    text-anchor: middle;
  }

  .axis.sub {
    font-size: 9px;
    fill: var(--faint);
  }

  .day-group {
    cursor: pointer;
    outline: none;
    -webkit-tap-highlight-color: transparent;
  }

  .day-group:focus,
  .day-group:focus-visible {
    outline: none;
  }

  .day-band {
    fill: transparent;
    transition: fill 0.12s ease;
  }

  .day-band.hovered {
    fill: var(--surface);
  }

  .day-band.selected {
    fill: var(--accent-soft);
  }

  .day-group:focus-visible .day-band {
    stroke: var(--accent);
    stroke-width: 1.5;
  }

  .total {
    fill: var(--ink-2);
    font-size: 10px;
    font-weight: 600;
  }

  .total.up {
    fill: var(--up);
  }

  .total.down {
    fill: var(--down);
  }

  .tooltip {
    position: absolute;
    top: 4px;
    z-index: 2;
    width: max-content;
    max-width: 260px;
    padding: 8px 10px;
    transform: translateX(-50%);
    background: var(--bg);
    border: 1px solid var(--line-2);
    border-radius: var(--radius-sm);
    box-shadow: var(--shadow-sm);
    pointer-events: none;
  }

  .tooltip-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--space-4);
  }

  .tooltip-head strong {
    color: var(--ink);
    font-size: var(--step-0);
  }

  .tooltip-head span {
    font-size: var(--step--1);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }

  .agent-breakdown {
    display: grid;
    gap: 3px;
    margin-top: 6px;
  }

  .agent-breakdown span {
    display: grid;
    grid-template-columns: 7px minmax(70px, 1fr) auto;
    align-items: center;
    gap: 6px;
    color: var(--muted);
    font-size: 10px;
  }

  .agent-breakdown i {
    width: 7px;
    height: 7px;
    border-radius: 2px;
  }

  .agent-breakdown strong {
    color: var(--ink-2);
    font-weight: 600;
  }

  .tooltip-meta {
    margin-top: 5px;
    color: var(--faint);
    font-size: 9px;
  }

  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2) var(--space-4);
    padding-top: var(--space-2);
  }

  .legend-item {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    color: var(--muted);
    font-size: var(--step--1);
  }

  .swatch {
    width: 8px;
    height: 8px;
    border-radius: 2px;
  }

  .empty {
    margin: var(--space-3) 0;
    color: var(--muted);
  }

  @media (max-width: 620px) {
    .interaction-head {
      align-items: flex-start;
      flex-direction: column;
      gap: 2px;
    }

    .tooltip {
      max-width: 220px;
    }
  }
</style>

<script>
  /**
   * Cumulative realised P&L at settlement time.
   *
   * This is also the archive's trade scrubber: hover to inspect a trade, click
   * to pin it, or focus the chart and use the arrow keys. The total is drawn in
   * ink and each agent keeps one stable series colour.
   */

  import {
    clock,
    compactRupees,
    signed,
    signedRupees,
    tone,
  } from "../lib/format.js";
  import { chartDay, shortDay } from "../lib/reporting.js";

  let { series = [] } = $props();

  const WIDTH = 860;
  const HEIGHT = 286;
  const PAD = { top: 22, right: 24, bottom: 38, left: 72 };

  let hiddenIds = $state([]);
  let hoverPosition = $state(null);
  let pinnedPosition = $state(null);
  let previousSeriesKey = "";

  const visibleSeries = $derived(
    series.filter((item) => !hiddenIds.includes(item.id)),
  );
  const activePosition = $derived(hoverPosition ?? pinnedPosition);

  const legendItems = $derived(
    series.map((item) => ({
      ...item,
      value: Number(item.points?.at(-1)?.cumulative ?? 0),
      hidden: hiddenIds.includes(item.id),
    })),
  );

  $effect(() => {
    const key = series.map((item) => item.id).join("|");
    if (key === previousSeriesKey) return;
    previousSeriesKey = key;
    hiddenIds = [];
    hoverPosition = null;
    pinnedPosition = null;
  });

  const geometry = $derived.by(() => {
    const prepared = visibleSeries
      .map((item) => ({
        ...item,
        points: (item.points ?? [])
          .map((point, index) => ({
            ...point,
            time: Date.parse(point.at),
            value: Number(point.cumulative),
            position:
              point.trade_index !== null &&
              point.trade_index !== undefined &&
              Number.isFinite(Number(point.trade_index))
                ? Number(point.trade_index)
                : index,
          }))
          .filter(
            (point) =>
              Number.isFinite(point.time) && Number.isFinite(point.value),
          ),
      }))
      .filter((item) => item.points.length);
    const tradeSeries =
      series.find((item) => item.emphasis) ?? series[0] ?? null;
    const tradePoints = (tradeSeries?.points ?? [])
      .map((point, index) => ({
        ...point,
        time: Date.parse(point.at),
        value: Number(point.cumulative),
        position:
          point.trade_index !== null &&
          point.trade_index !== undefined &&
          Number.isFinite(Number(point.trade_index))
            ? Number(point.trade_index)
            : index,
      }))
      .filter(
        (point) => Number.isFinite(point.time) && Number.isFinite(point.value),
      );
    if (!prepared.length || !tradePoints.length) return null;

    const values = prepared.flatMap((item) =>
      item.points.map((point) => point.value),
    );
    const rawLow = Math.min(0, ...values);
    const rawHigh = Math.max(0, ...values);
    const span = Math.max(rawHigh - rawLow, Math.abs(rawHigh) * 0.06, 1);
    const low = rawLow - span * 0.09;
    const high = rawHigh + span * 0.09;
    const firstPosition = Math.min(...tradePoints.map((point) => point.position));
    const lastPosition = Math.max(...tradePoints.map((point) => point.position));
    const plotWidth = WIDTH - PAD.left - PAD.right;
    const plotHeight = HEIGHT - PAD.top - PAD.bottom;
    const x = (position) =>
      firstPosition === lastPosition
        ? PAD.left + plotWidth / 2
        : PAD.left +
          ((position - firstPosition) / (lastPosition - firstPosition)) *
            plotWidth;
    const y = (value) =>
      PAD.top + ((high - value) / (high - low || 1)) * plotHeight;

    const lines = prepared.map((item) => {
      const path = item.points
        .map(
          (point, index) =>
            `${index === 0 ? "M" : "L"}${x(point.position).toFixed(1)} ${y(point.value).toFixed(1)}`,
        )
        .join(" ");
      const first = item.points[0];
      const last = item.points[item.points.length - 1];
      return {
        ...item,
        path,
        area: item.emphasis
          ? `${path} L${x(last.position).toFixed(1)} ${y(0).toFixed(1)} ` +
            `L${x(first.position).toFixed(1)} ${y(0).toFixed(1)} Z`
          : null,
        lastX: x(last.position),
        lastY: y(last.value),
        value: last.value,
      };
    });

    const yTicks = [0, 1, 2, 3].map((index) => {
      const value = high - ((high - low) * index) / 3;
      return { y: y(value), value };
    });
    const xPositions =
      firstPosition === lastPosition
        ? [firstPosition]
        : [firstPosition, (firstPosition + lastPosition) / 2, lastPosition];
    const xTicks = xPositions.map((position) => {
      const nearest = tradePoints.reduce((best, point) =>
        Math.abs(point.position - position) < Math.abs(best.position - position)
          ? point
          : best,
      );
      return {
        x: x(position),
        label: `${chartDay(nearest.day)} · #${nearest.position + 1}`,
      };
    });

    return {
      lines,
      yTicks,
      xTicks,
      zeroY: y(0),
      x,
      y,
      tradePoints,
      firstPosition,
      lastPosition,
    };
  });

  const activePoint = $derived.by(() => {
    if (!geometry || activePosition === null) return null;
    return (
      geometry.tradePoints.find(
        (point) => point.position === activePosition,
      ) ?? null
    );
  });

  const tooltipLeft = $derived(
    activePoint && geometry
      ? Math.max(
          13,
          Math.min(87, (geometry.x(activePoint.position) / WIDTH) * 100),
        )
      : 50,
  );

  const description = $derived(
    geometry
      ? geometry.lines
          .map((item) => `${item.label}: ${signedRupees(item.value)}`)
          .join(", ")
      : "No settled trades to chart",
  );

  function nearestTrade(position) {
    if (!geometry?.tradePoints?.length) return null;
    return geometry.tradePoints.reduce((best, point) =>
      Math.abs(point.position - position) < Math.abs(best.position - position)
        ? point
        : best,
    );
  }

  function pointFromPointer(event) {
    if (!geometry) return null;
    const rect = event.currentTarget.getBoundingClientRect();
    if (!rect.width) return null;
    const localX = ((event.clientX - rect.left) / rect.width) * WIDTH;
    const boundedX = Math.max(
      PAD.left,
      Math.min(WIDTH - PAD.right, localX),
    );
    const ratio =
      geometry.lastPosition === geometry.firstPosition
        ? 0.5
        : (boundedX - PAD.left) / (WIDTH - PAD.left - PAD.right);
    const position =
      geometry.firstPosition +
      ratio * (geometry.lastPosition - geometry.firstPosition);
    return nearestTrade(position);
  }

  function onPointerMove(event) {
    hoverPosition = pointFromPointer(event)?.position ?? null;
  }

  function onPointerLeave() {
    hoverPosition = null;
  }

  function onPointerClick(event) {
    // Keyboard activation also emits click, but has no useful pointer position.
    if (event.detail === 0) return;
    const point = pointFromPointer(event);
    if (!point) return;
    pinnedPosition =
      pinnedPosition === point.position ? null : point.position;
    hoverPosition = point.position;
  }

  function onKeyDown(event) {
    const points = geometry?.tradePoints ?? [];
    if (!points.length) return;
    if (event.key === "Escape") {
      hoverPosition = null;
      pinnedPosition = null;
      event.preventDefault();
      return;
    }
    if (event.key === "Home") {
      pinnedPosition = points[0].position;
      hoverPosition = null;
      event.preventDefault();
      return;
    }
    if (event.key === "End") {
      pinnedPosition = points[points.length - 1].position;
      hoverPosition = null;
      event.preventDefault();
      return;
    }
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    const currentIndex = points.findIndex(
      (point) => point.position === (hoverPosition ?? pinnedPosition),
    );
    if (currentIndex < 0) {
      pinnedPosition = points[0].position;
    } else {
      const delta = event.key === "ArrowRight" ? 1 : -1;
      pinnedPosition =
        points[
          Math.max(0, Math.min(points.length - 1, currentIndex + delta))
        ].position;
    }
    hoverPosition = null;
    event.preventDefault();
  }

  function toggleSeries(id) {
    if (hiddenIds.includes(id)) {
      hiddenIds = hiddenIds.filter((held) => held !== id);
      return;
    }
    if (visibleSeries.length <= 1) return;
    hiddenIds = [...hiddenIds, id];
  }
</script>

{#if geometry}
  <div class="chart-wrap">
    <div class="interaction-head">
      <span class="hint">
        Hover a trade, click to pin, or use ← → while the chart is focused.
      </span>
      <span class="hint numeric">
        {activePoint
          ? `trade ${activePoint.position + 1} of ${geometry.tradePoints.length}`
          : `${geometry.tradePoints.length} settled trades`}
      </span>
    </div>

    <div class="plot">
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-label={description}
        aria-hidden="true"
      >
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

        {#each geometry.lines as item (item.id)}
          {#if item.area}
            <path class="area" d={item.area} style={`fill: ${item.colour}`} />
          {/if}
          <path
            class:emphasis={item.emphasis}
            class="line"
            d={item.path}
            style={`stroke: ${item.colour}`}
          >
            <title>{item.label}: {signedRupees(item.value)}</title>
          </path>
          <circle
            class="end"
            cx={item.lastX}
            cy={item.lastY}
            r={item.emphasis ? 3.6 : 3}
            style={`fill: ${item.colour}`}
          >
            <title>{item.label} ended at {signedRupees(item.value)}</title>
          </circle>
        {/each}

        {#if activePoint}
          <line
            class="crosshair"
            x1={geometry.x(activePoint.position)}
            x2={geometry.x(activePoint.position)}
            y1={PAD.top}
            y2={HEIGHT - PAD.bottom}
          />
          <circle
            class="active-dot"
            cx={geometry.x(activePoint.position)}
            cy={geometry.y(activePoint.value)}
            r="4.5"
          />
        {/if}

        {#each geometry.xTicks as tick}
          <text class="axis x" x={tick.x} y={HEIGHT - 12} text-anchor="middle">
            {tick.label}
          </text>
        {/each}
      </svg>

      <button
        class="chart-hit"
        type="button"
        aria-label={`Interactive realised P&L chart. ${description}. Hover or use the left and right arrow keys to inspect ${geometry.tradePoints.length} trades.`}
        onpointermove={onPointerMove}
        onpointerleave={onPointerLeave}
        onclick={onPointerClick}
        onkeydown={onKeyDown}
      ></button>

      {#if activePoint}
        <div
          class="tooltip"
          aria-live="polite"
          style={`left: ${tooltipLeft}%`}
        >
          <div class="tooltip-head">
            <strong>{activePoint.symbol ?? "Unknown"}</strong>
            <span>{activePoint.agent_id ?? "unassigned"}</span>
          </div>
          <div class="tooltip-values">
            <span>
              Trade
              <strong class={tone(activePoint.gross)}>
                {signedRupees(activePoint.gross)}
              </strong>
            </span>
            <span>
              Running
              <strong class={tone(activePoint.cumulative)}>
                {signedRupees(activePoint.cumulative)}
              </strong>
            </span>
            <span>
              Result
              <strong>
                {activePoint.r_multiple === null ||
                activePoint.r_multiple === undefined
                  ? "—"
                  : `${signed(activePoint.r_multiple)}R`}
              </strong>
            </span>
          </div>
          <div class="tooltip-meta">
            {shortDay(activePoint.day)} · {clock(activePoint.at)} ·
            {activePoint.side ?? "—"}
            {activePoint.exit_reason
              ? ` · ${activePoint.exit_reason.replaceAll("_", " ")}`
              : ""}
          </div>
        </div>
      {/if}
    </div>

    <div class="legend">
      {#each legendItems as item (item.id)}
        <button
          class="legend-item"
          class:off={item.hidden}
          aria-pressed={!item.hidden}
          onclick={() => toggleSeries(item.id)}
          title={item.hidden ? `Show ${item.label}` : `Hide ${item.label}`}
        >
          <span class="swatch" style={`background: ${item.colour}`}></span>
          <span>{item.label}</span>
          <strong class="numeric {tone(item.value)}">{signedRupees(item.value)}</strong>
        </button>
      {/each}
    </div>
  </div>
{:else}
  <p class="empty">No settled trade has a timestamp to plot yet.</p>
{/if}

<style>
  .chart-wrap {
    min-width: 0;
    border-radius: var(--radius-sm);
  }

  .interaction-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--space-3);
    margin-bottom: 2px;
  }

  .hint {
    color: var(--faint);
    font-size: 10px;
  }

  .plot {
    position: relative;
    min-width: 0;
  }

  .chart-hit {
    position: absolute;
    inset: 0;
    z-index: 1;
    padding: 0;
    background: transparent;
    border: 0;
    cursor: crosshair;
  }

  .chart-hit:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 3px;
    border-radius: var(--radius-sm);
  }

  svg {
    display: block;
    width: 100%;
    height: auto;
    overflow: visible;
    cursor: crosshair;
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

  .line {
    fill: none;
    stroke-width: 1.7;
    stroke-linecap: round;
    stroke-linejoin: round;
    vector-effect: non-scaling-stroke;
  }

  .line.emphasis {
    stroke-width: 2.3;
  }

  .area {
    opacity: 0.045;
  }

  .end {
    stroke: var(--bg);
    stroke-width: 2;
  }

  .crosshair {
    stroke: var(--line-2);
    stroke-width: 1;
    stroke-dasharray: 3 4;
    vector-effect: non-scaling-stroke;
  }

  .active-dot {
    fill: var(--accent);
    stroke: var(--bg);
    stroke-width: 2;
    vector-effect: non-scaling-stroke;
  }

  .tooltip {
    position: absolute;
    top: 8px;
    z-index: 2;
    width: max-content;
    max-width: 270px;
    padding: 9px 11px;
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
    overflow: hidden;
    color: var(--muted);
    font-size: 10px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .tooltip-values {
    display: flex;
    flex-wrap: wrap;
    gap: 3px 12px;
    margin-top: 6px;
  }

  .tooltip-values span {
    color: var(--faint);
    font-size: 10px;
  }

  .tooltip-values strong {
    margin-left: 4px;
    color: var(--ink-2);
    font-size: var(--step--1);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }

  .tooltip-meta {
    margin-top: 5px;
    color: var(--faint);
    font-size: 9px;
    text-transform: capitalize;
  }

  .legend {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-2) var(--space-4);
    padding-top: var(--space-2);
  }

  .legend-item {
    display: inline-grid;
    grid-template-columns: 8px auto auto;
    align-items: center;
    gap: 7px;
    min-height: 24px;
    padding: 2px 0;
    background: none;
    border: 0;
    color: var(--muted);
    font-size: var(--step--1);
    text-align: left;
  }

  .legend-item:hover {
    color: var(--ink);
  }

  .legend-item strong {
    color: var(--ink-2);
    font-weight: 600;
  }

  .legend-item.off {
    color: var(--faint);
    text-decoration: line-through;
  }

  .legend-item.off .swatch {
    opacity: 0.35;
  }

  .legend-item.off strong {
    color: var(--faint);
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

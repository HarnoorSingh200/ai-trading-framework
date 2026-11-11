<script>
  /**
   * A candlestick chart with volume, drawn as SVG.
   *
   * Deliberately dependency-free: sixty candles is not a reason to ship a
   * charting library, and the drawing rules here (body, wick, volume, four
   * price levels) are the whole specification.
   */

  let { candles = [], interval = "" } = $props();

  const WIDTH = 820;
  const HEIGHT = 300;
  const PAD_LEFT = 62;
  const PAD_RIGHT = 14;
  const PAD_TOP = 14;
  const AXIS_HEIGHT = 26;
  const VOLUME_HEIGHT = 62;

  const priceHeight = $derived(HEIGHT - PAD_TOP - AXIS_HEIGHT - VOLUME_HEIGHT);
  const plotWidth = $derived(WIDTH - PAD_LEFT - PAD_RIGHT);

  const scale = $derived.by(() => {
    if (!candles.length) return null;
    const highs = candles.map((c) => c.high);
    const lows = candles.map((c) => c.low);
    const top = Math.max(...highs);
    const bottom = Math.min(...lows);
    const span = Math.max(top - bottom, Math.max(top * 0.001, 0.05));
    const volumeTop = Math.max(...candles.map((c) => c.volume || 0), 1);
    const slot = plotWidth / candles.length;
    const bodyWidth = Math.max(2, Math.min(slot * 0.62, 16));
    const levels = [0, 1, 2, 3].map((index) => top - (span * index) / 3);
    return { top, bottom, span, volumeTop, slot, bodyWidth, levels };
  });

  const x = (index) => PAD_LEFT + (index + 0.5) * scale.slot;
  const y = (price) => PAD_TOP + ((scale.top - price) / scale.span) * priceHeight;
  const volumeY = (volume) =>
    HEIGHT - AXIS_HEIGHT - ((volume || 0) / scale.volumeTop) * VOLUME_HEIGHT;

  function label(iso) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "";
    if (interval === "1d") {
      return date.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
    }
    return date.toLocaleTimeString("en-IN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  }

  const ticks = $derived.by(() => {
    if (!candles.length) return [];
    const last = candles.length - 1;
    return [...new Set([0, Math.floor(last / 2), last])].map((index) => ({
      index,
      text: label(candles[index].open_time),
    }));
  });
</script>

{#if scale}
  <svg viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label={`${interval} candles`}>
    {#each scale.levels as level, index}
      <line
        class="grid"
        x1={PAD_LEFT}
        x2={WIDTH - PAD_RIGHT}
        y1={PAD_TOP + (index * priceHeight) / 3}
        y2={PAD_TOP + (index * priceHeight) / 3}
      />
      <text class="axis" x={PAD_LEFT - 10} y={PAD_TOP + (index * priceHeight) / 3 + 4}
        >{level.toFixed(2)}</text
      >
    {/each}

    <line
      class="grid"
      x1={PAD_LEFT}
      x2={WIDTH - PAD_RIGHT}
      y1={HEIGHT - AXIS_HEIGHT}
      y2={HEIGHT - AXIS_HEIGHT}
    />

    {#each candles as candle, index (candle.open_time)}
      <rect
        class={candle.close >= candle.open ? "up" : "down"}
        x={x(index) - scale.bodyWidth / 2}
        y={volumeY(candle.volume)}
        width={scale.bodyWidth}
        height={Math.max(HEIGHT - AXIS_HEIGHT - volumeY(candle.volume), 0.6)}
        opacity="0.45"
      />
      <line
        class={candle.close >= candle.open ? "up" : "down"}
        x1={x(index)}
        x2={x(index)}
        y1={y(candle.high)}
        y2={y(candle.low)}
        stroke-width="1"
      />
      <rect
        class={candle.close >= candle.open ? "up" : "down"}
        x={x(index) - scale.bodyWidth / 2}
        y={y(Math.max(candle.open, candle.close))}
        width={scale.bodyWidth}
        height={Math.max(Math.abs(y(candle.open) - y(candle.close)), 1)}
      />
    {/each}

    {#each ticks as tick (tick.index)}
      <text class="axis" x={x(tick.index)} y={HEIGHT - 8} text-anchor="middle">{tick.text}</text>
    {/each}
  </svg>
{/if}

<style>
  svg {
    display: block;
    width: 100%;
    height: auto;
  }

  .grid {
    stroke: var(--line);
    stroke-width: 1;
  }

  .axis {
    fill: var(--faint);
    font-size: 10px;
    font-variant-numeric: tabular-nums;
    text-anchor: end;
  }

  .up {
    fill: var(--up);
    stroke: var(--up);
  }

  .down {
    fill: var(--down);
    stroke: var(--down);
  }
</style>

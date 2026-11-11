<script>
  /**
   * A bare price shape: no axes, no labels, no interaction. It exists so a row
   * can be read as a trend rather than as a single number, which is the one
   * thing a table of prices is worst at.
   */

  let { values = [], width = 92, height = 22, pad = 2 } = $props();

  const geometry = $derived.by(() => {
    const points = (values || []).filter((value) => Number.isFinite(value));
    if (points.length < 2) return null;
    const low = Math.min(...points);
    const high = Math.max(...points);
    const span = high - low || 1;
    const step = width / (points.length - 1);
    const path = points
      .map((value, index) => {
        const x = index * step;
        const y = height - pad - ((value - low) / span) * (height - pad * 2);
        return `${index === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`;
      })
      .join(" ");
    return { path, rising: points[points.length - 1] >= points[0] };
  });
</script>

{#if geometry}
  <svg
    class={geometry.rising ? "up" : "down"}
    viewBox={`0 0 ${width} ${height}`}
    width={width}
    height={height}
    aria-hidden="true"
  >
    <path d={geometry.path} />
  </svg>
{/if}

<style>
  svg {
    /* Inline, so the table cell's own alignment places it. A block-level svg
       would ignore the cell's text-align and sit against the left edge. */
    display: inline-block;
    vertical-align: middle;
    overflow: visible;
  }

  path {
    fill: none;
    stroke: currentColor;
    stroke-width: 1.4;
    stroke-linecap: round;
    stroke-linejoin: round;
    vector-effect: non-scaling-stroke;
  }

  .up {
    color: var(--up);
  }

  .down {
    color: var(--down);
  }
</style>

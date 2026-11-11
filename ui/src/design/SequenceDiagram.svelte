<script>
  let { diagram } = $props();

  const WIDTH = 980;
  const ACTOR_Y = 22;
  const ACTOR_H = 62;
  const LIFELINE_TOP = 96;
  const MESSAGE_START = 140;
  const MESSAGE_GAP = 46;
  const LEFT = 82;
  const RIGHT = 898;

  const centers = $derived(
    diagram.actors.map((_, index) =>
      diagram.actors.length === 1
        ? WIDTH / 2
        : LEFT + (index * (RIGHT - LEFT)) / (diagram.actors.length - 1),
    ),
  );
  const height = $derived(
    Math.max(360, MESSAGE_START + diagram.messages.length * MESSAGE_GAP + 48),
  );

  function marker(kind) {
    if (kind === "async") return `url(#arrow-open-${diagram.id})`;
    if (kind === "accent") return `url(#arrow-accent-${diagram.id})`;
    return `url(#arrow-filled-${diagram.id})`;
  }

  function messageY(index) {
    return MESSAGE_START + index * MESSAGE_GAP;
  }

  function labelWidth(text) {
    return Math.min(320, Math.max(92, text.length * 6.4 + 18));
  }

  function frameBounds() {
    if (!diagram.frame) return null;
    const first = diagram.frame.first ?? 0;
    const last = diagram.frame.last ?? diagram.messages.length - 1;
    const participants = [
      ...new Set(
        diagram.messages
          .slice(first, last + 1)
          .flatMap((message) => [message.from, message.to]),
      ),
    ];
    const xs = participants.map((index) => centers[index]);
    return {
      x: Math.min(...xs) - 48,
      y: messageY(first) - 54,
      width: Math.max(...xs) - Math.min(...xs) + 96,
      height: (last - first) * MESSAGE_GAP + 70,
      first,
      last,
    };
  }

  const frame = $derived(frameBounds());
</script>

<div class="sequence-shell">
  <svg
    viewBox={`0 0 ${WIDTH} ${height}`}
    role="img"
    aria-labelledby={`${diagram.id}-title ${diagram.id}-desc`}
  >
    <title id={`${diagram.id}-title`}>{diagram.title}</title>
    <desc id={`${diagram.id}-desc`}>{diagram.description}</desc>

    <defs>
      <marker
        id={`arrow-filled-${diagram.id}`}
        markerWidth="8"
        markerHeight="7"
        refX="7"
        refY="3.5"
        orient="auto"
        markerUnits="userSpaceOnUse"
      >
        <path class="arrow-filled" d="M0,0 L8,3.5 L0,7 Z" />
      </marker>
      <marker
        id={`arrow-open-${diagram.id}`}
        markerWidth="8"
        markerHeight="7"
        refX="7"
        refY="3.5"
        orient="auto"
        markerUnits="userSpaceOnUse"
      >
        <path class="arrow-open" d="M0,0 L8,3.5 L0,7" />
      </marker>
      <marker
        id={`arrow-accent-${diagram.id}`}
        markerWidth="8"
        markerHeight="7"
        refX="7"
        refY="3.5"
        orient="auto"
        markerUnits="userSpaceOnUse"
      >
        <path class="arrow-accent" d="M0,0 L8,3.5 L0,7 Z" />
      </marker>
    </defs>

    {#each centers as center}
      <line
        class="lifeline"
        x1={center}
        y1={ACTOR_Y + ACTOR_H}
        x2={center}
        y2={height - 24}
      />
    {/each}

    {#if frame}
      <rect
        class="fragment"
        x={frame.x}
        y={frame.y}
        width={frame.width}
        height={frame.height}
        rx="7"
      />
      <rect
        class="fragment-tab"
        x={frame.x}
        y={frame.y}
        width={diagram.frame.operator === "LOOP" ? 58 : 52}
        height="22"
        rx="4"
      />
      <text
        class="fragment-operator"
        x={frame.x + 29}
        y={frame.y + 15}
        text-anchor="middle"
      >
        {diagram.frame.operator}
      </text>
      <text class="fragment-label" x={frame.x + 68} y={frame.y + 15}>
        [{diagram.frame.label}]
      </text>
    {/if}

    {#each diagram.messages as message, index (index)}
      {@const y = messageY(index)}
      {@const x1 = centers[message.from]}
      {@const x2 = centers[message.to]}
      {@const middle = (x1 + x2) / 2}
      {@const width = labelWidth(message.label)}
      <line
        class:message-call={message.kind === "call"}
        class:message-return={message.kind === "return"}
        class:message-async={message.kind === "async"}
        class:message-accent={message.kind === "accent"}
        x1={x1}
        y1={y}
        x2={x2}
        y2={y}
        marker-end={marker(message.kind)}
      />
      <rect
        class="message-mask"
        x={middle - width / 2}
        y={y - 23}
        width={width}
        height="17"
        rx="3"
      />
      <text
        class="message-label"
        class:accent={message.kind === "accent"}
        x={middle}
        y={y - 11}
        text-anchor="middle"
      >
        {message.label}
      </text>
    {/each}

    {#each diagram.actors as actor, index (actor.label)}
      <rect
        class="actor-box"
        x={centers[index] - 70}
        y={ACTOR_Y}
        width="140"
        height={ACTOR_H}
        rx="7"
      />
      <text class="actor-name" x={centers[index]} y={ACTOR_Y + 26} text-anchor="middle">
        {actor.label}
      </text>
      <text class="actor-meta" x={centers[index]} y={ACTOR_Y + 44} text-anchor="middle">
        {actor.meta}
      </text>
    {/each}
  </svg>
</div>

<style>
  .sequence-shell {
    overflow-x: auto;
    border-top: 1px solid var(--line);
    border-bottom: 1px solid var(--line);
    background: var(--surface);
  }

  svg {
    display: block;
    width: 100%;
    min-width: 760px;
    height: auto;
  }

  .lifeline {
    stroke: var(--line-2);
    stroke-width: 1;
    stroke-dasharray: 4 5;
  }

  .actor-box {
    fill: var(--bg);
    stroke: var(--line-2);
    stroke-width: 1;
  }

  .actor-name {
    fill: var(--ink);
    font-family: var(--sans);
    font-size: 13px;
    font-weight: 650;
  }

  .actor-meta {
    fill: var(--muted);
    font-family: var(--sans);
    font-size: 10px;
  }

  .fragment {
    fill: color-mix(in srgb, var(--ink) 2%, transparent);
    stroke: color-mix(in srgb, var(--ink) 22%, transparent);
    stroke-width: 1;
  }

  .fragment-tab {
    fill: var(--bg);
    stroke: color-mix(in srgb, var(--ink) 22%, transparent);
    stroke-width: 1;
  }

  .fragment-operator {
    fill: var(--muted);
    font-family: var(--sans);
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.12em;
  }

  .fragment-label {
    fill: var(--muted);
    font-family: var(--sans);
    font-size: 10px;
  }

  line[class^="message-"] {
    stroke-linecap: round;
    stroke-width: 1.4;
  }

  .message-call {
    stroke: var(--muted);
  }

  .message-return {
    stroke: var(--muted);
    stroke-dasharray: 5 4;
  }

  .message-async {
    stroke: var(--faint);
    stroke-dasharray: 5 4;
  }

  .message-accent {
    stroke: var(--accent);
    stroke-width: 1.8;
  }

  .arrow-filled {
    fill: var(--muted);
  }

  .arrow-open {
    fill: none;
    stroke: var(--faint);
    stroke-width: 1.2;
  }

  .arrow-accent {
    fill: var(--accent);
  }

  .message-mask {
    fill: var(--surface);
  }

  .message-label {
    fill: var(--ink-2);
    font-family: var(--sans);
    font-size: 11px;
    font-weight: 550;
  }

  .message-label.accent {
    fill: var(--accent);
    font-weight: 650;
  }
</style>

"""Run Module 2.

    python -m module2                  the control service (127.0.0.1:5202)
    python -m module2 --agent <id>     one agent's loop, in this process

The control service is what the operator site talks to. The second form is what
the control service starts, one child process per running agent, so that agents
are isolated from each other and from the service that supervises them: a model
that hangs, a tool that loops or a cycle that dies takes one agent down, not the
fleet.
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
from typing import Any

from common.logging import setup_logging
from common.telegram import notifier_from_env
from module2.config import load_settings
from module2.market import MarketClient
from module2.orders import OrderForwarder, OrderReader
from module2.providers import build_model_client, resolve_api_mode, resolve_model_route
from module2.secrets import LLM_API_KEY, OPENAI_API_KEY, SecretStore
from module2.store import AgentStore
from module2.worker import AgentWorker

logger = logging.getLogger("module2")


def run_agent(agent_id: str) -> int:
    """Run one agent until interrupted."""

    settings = load_settings()
    # One file per agent: a fleet of two is two logs, and a crash in one is
    # readable without the other's lines interleaved through it.
    setup_logging(f"module2-agent-{agent_id}")
    store = AgentStore(settings.data_dir)
    definition = store.load(agent_id)
    if definition is None:
        logger.error("no agent is defined with the id %r", agent_id)
        return 2
    if not definition.enabled:
        logger.warning(
            "%s is disabled in its definition; nothing was started", agent_id
        )
        return 0

    secrets = SecretStore(settings.secrets_file)
    route = resolve_model_route(
        definition.model,
        default_base_url=settings.llm_base_url,
        openai_base_url=settings.openai_base_url,
        default_key=secrets.get(LLM_API_KEY) or "",
        openai_key=secrets.get(OPENAI_API_KEY) or "",
    )
    market = MarketClient(
        settings.module1_url, timeout_seconds=settings.http_timeout_seconds
    )
    effort = definition.reasoning_effort or settings.reasoning_effort
    api_mode = resolve_api_mode(settings.llm_api, definition.model, effort)
    client = build_model_client(
        route=route,
        model=definition.model,
        api_mode=settings.llm_api,
        reasoning_effort=effort,
        timeout_seconds=definition.cycle_timeout_seconds,
    )
    if api_mode == "responses" and route.provider != "openai":
        logger.warning(
            "%s: the Responses API is selected but %s is not the OpenAI route "
            "(no OPENAI_API_KEY, or a gateway in front). Make sure %s "
            "implements /v1/responses",
            agent_id,
            definition.model,
            route.base_url,
        )
    if api_mode == "responses" and definition.max_tokens < 8192:
        logger.warning(
            "%s: max_tokens is %d and on the Responses API that budget includes "
            "the reasoning tokens; a cycle may be cut short. Consider 8192 or "
            "more for a reasoning model",
            agent_id,
            definition.max_tokens,
        )
    if api_mode == "chat" and effort != "none" and definition.model.lower().startswith("gpt-"):
        logger.warning(
            "%s: Chat Completions cannot do reasoning and tool calls together "
            "for %s; set MODULE2_LLM_API=responses so the model can think and "
            "still call tools",
            agent_id,
            definition.model,
        )
    if not client.has_credential:
        # Said once, loudly, at start-up. The agent still runs: every cycle will
        # record why it failed rather than sitting silent.
        logger.warning(
            "%s has no model credential; set %s in the environment or in %s",
            agent_id,
            route.credential_name,
            settings.secrets_file or "FRAMEWORK_SECRETS_FILE",
        )

    # Where a decided order goes to be tracked. Module 3 may not be running;
    # the hand-over is best-effort and the reconciliation on the other side
    # picks up whatever it missed.
    forwarder = OrderForwarder(settings.module3_url)
    # The other direction: where the agent reads back what it already holds.
    reader = OrderReader(settings.module3_url)
    worker = AgentWorker(
        definition,
        store=store,
        market=market,
        client=client,
        settings=settings,
        sink=forwarder,
        book=reader,
        notifier=notifier_from_env(f"agent-{agent_id}"),
    )
    stop = threading.Event()

    def halt(signum: int, frame: Any) -> None:  # noqa: ARG001 - signal handler signature
        logger.info("%s: stopping on signal %s", agent_id, signum)
        stop.set()

    for name in ("SIGINT", "SIGTERM"):
        held = getattr(signal, name, None)
        if held is not None:
            signal.signal(held, halt)

    logger.info(
        "%s starting: %s on %s, model %s via %s (%s, reasoning %s)",
        agent_id,
        definition.name,
        definition.timeframe,
        definition.model,
        route.provider,
        api_mode,
        effort,
    )
    try:
        worker.run(stop=stop)
    finally:
        market.close()
        client.close()
        forwarder.close()
        reader.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="module2",
        description="Trading Framework v3 - Module 2 agent engine",
    )
    parser.add_argument("--agent", help="run this agent's loop in this process")
    args = parser.parse_args(argv)
    setup_logging("module2")
    if args.agent:
        return run_agent(args.agent)

    import uvicorn

    settings = load_settings()
    uvicorn.run(
        "module2.service:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
        # Uvicorn's own loggers propagate into the handlers set up above, so
        # one process writes one IST-stamped file. Access logs are the UI's
        # polling, not the agent's work.
        log_config=None,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

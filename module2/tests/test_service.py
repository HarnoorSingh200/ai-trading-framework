"""The control surface the operator site drives agents through."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from module2 import notes as notebook
from module2.definition import AgentDefinition
from module2.service import create_app, tail_events
from module2.store import AgentStore

pytestmark = pytest.mark.unit


class FakeSupervisor:
    """Stands in for the process launcher, so tests spawn nothing."""

    def __init__(self) -> None:
        self.running_ids: set[str] = set()
        self.calls: list[tuple[str, str]] = []

    def start(self, agent_id: str) -> dict[str, Any]:
        self.calls.append(("start", agent_id))
        self.running_ids.add(agent_id)
        return {"agent_id": agent_id, "pid": 4242, "running": True}

    def stop(self, agent_id: str) -> bool:
        self.calls.append(("stop", agent_id))
        was = agent_id in self.running_ids
        self.running_ids.discard(agent_id)
        return was

    def status(self, agent_id: str) -> dict[str, Any]:
        return {"running": agent_id in self.running_ids}

    def running(self) -> dict[str, int]:
        return dict.fromkeys(sorted(self.running_ids), 4242)

    def shutdown(self) -> None:
        self.running_ids.clear()


def definition(agent_id: str = "alpha", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "agent_id": agent_id,
        "name": f"Agent {agent_id}",
        "system_prompt": "Decide on closed candles only.",
    }
    payload.update(overrides)
    return payload


def make(tmp_path: Path) -> tuple[TestClient, AgentStore, FakeSupervisor]:
    store = AgentStore(tmp_path / "agents")
    supervisor = FakeSupervisor()
    app = create_app(store=store, supervisor=supervisor)  # type: ignore[arg-type]
    return TestClient(app), store, supervisor


def test_an_agent_can_be_created_and_listed(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)

    created = client.post("/v1/agents", json=definition())
    listed = client.get("/v1/agents").json()["data"]

    assert created.status_code == 200
    assert created.json()["data"]["agent"]["agent_id"] == "alpha"
    assert [agent["agent_id"] for agent in listed["agents"]] == ["alpha"]
    assert listed["agents"][0]["running"] is False
    assert listed["agents"][0]["timeframe"] == "15m"


def test_token_usage_is_served_for_one_day(tmp_path: Path) -> None:
    client, store, _ = make(tmp_path)

    client.post("/v1/agents", json=definition("alpha", model="gpt-6-luna"))
    path = store.activity_path("alpha")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "model_reply",
                "ts": "2026-10-22T05:00:00.000Z",
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "reasoning_tokens": 20,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    response = client.get("/v1/usage", params={"day": "2026-10-22"})
    bad = client.get("/v1/usage", params={"day": "22-10-2026"})

    assert response.status_code == 200
    report = response.json()["data"]
    assert report["day"] == "2026-10-22"
    assert report["agents"][0]["agent_id"] == "alpha"
    assert report["agents"][0]["prompt_tokens"] == 120
    assert report["agents"][0]["reasoning_tokens"] == 20
    assert report["totals"]["total_tokens"] == 150
    assert bad.status_code == 400
    assert bad.json()["detail"]["error"]["code"] == "USAGE_INVALID"


def test_a_disabled_agent_is_defined_but_never_autostarted(tmp_path: Path) -> None:
    from module2.config import load_settings

    store = AgentStore(tmp_path / "agents")
    supervisor = FakeSupervisor()
    store.save(
        AgentDefinition.from_dict(definition("alpha", enabled=False))
    )
    app = create_app(
        store=store, supervisor=supervisor, settings=load_settings()  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        listed = client.get("/v1/agents").json()["data"]

    assert [agent["agent_id"] for agent in listed["agents"]] == ["alpha"]
    assert listed["agents"][0]["enabled"] is False
    assert not any(kind == "start" for kind, _ in supervisor.calls)


def test_a_disabled_agent_cannot_be_started_by_hand(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    client.post("/v1/agents", json=definition("alpha", enabled=False))

    response = client.post("/v1/agents/alpha/start")

    assert response.status_code == 409
    assert response.json()["detail"]["error"]["code"] == "AGENT_DISABLED"


def test_a_defective_agent_is_refused_with_the_field_that_is_wrong(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)

    response = client.post("/v1/agents", json=definition(timeframe="9m"))

    assert response.status_code == 400
    error = response.json()["detail"]["error"]
    assert error["code"] == "AGENT_INVALID"
    assert "9m" in error["message"]


def test_an_agent_that_already_exists_is_not_silently_replaced(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    client.post("/v1/agents", json=definition())

    response = client.post("/v1/agents", json=definition(system_prompt="Something else."))

    assert response.status_code == 409
    assert "alpha" in response.json()["detail"]["error"]["message"]


def test_an_agent_is_edited_by_name(tmp_path: Path) -> None:
    client, store, _ = make(tmp_path)
    client.post("/v1/agents", json=definition())

    response = client.put("/v1/agents/alpha", json=definition(system_prompt="Sharper."))

    assert response.status_code == 200
    assert store.load("alpha").system_prompt == "Sharper."


def test_editing_a_running_agent_is_refused(tmp_path: Path) -> None:
    """Changing the instructions under a live loop would be a silent mid-cycle edit."""

    client, _, supervisor = make(tmp_path)
    client.post("/v1/agents", json=definition())
    client.post("/v1/agents/alpha/start")

    response = client.put("/v1/agents/alpha", json=definition(system_prompt="Sharper."))

    assert response.status_code == 409
    assert "running" in response.json()["detail"]["error"]["message"].lower()
    assert ("start", "alpha") in supervisor.calls


def test_an_agent_is_started_and_stopped(tmp_path: Path) -> None:
    client, _, supervisor = make(tmp_path)
    client.post("/v1/agents", json=definition())

    started = client.post("/v1/agents/alpha/start").json()["data"]
    stopped = client.post("/v1/agents/alpha/stop").json()["data"]

    assert started["running"] is True
    assert started["pid"] == 4242
    assert stopped["running"] is False
    assert [call[0] for call in supervisor.calls] == ["start", "stop"]


def test_starting_an_agent_that_is_not_defined_is_a_404(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)

    assert client.post("/v1/agents/ghost/start").status_code == 404
    assert client.get("/v1/agents/ghost").status_code == 404


def test_an_agent_is_retired_rather_than_erased(tmp_path: Path) -> None:
    client, store, supervisor = make(tmp_path)
    client.post("/v1/agents", json=definition())
    store.append_decision("alpha", {"action": "ignore", "reason": "quiet"})

    response = client.delete("/v1/agents/alpha")

    assert response.status_code == 200
    assert store.exists("alpha") is False
    assert client.get("/v1/agents").json()["data"]["agents"] == []
    assert ("stop", "alpha") in supervisor.calls
    kept = list((tmp_path / "agents" / "_retired").rglob("decisions.jsonl"))
    assert "quiet" in kept[0].read_text(encoding="utf-8")


def test_the_logs_are_readable_over_http(tmp_path: Path) -> None:
    client, store, _ = make(tmp_path)
    client.post("/v1/agents", json=definition())
    store.append_activity("alpha", {"type": "cycle_started", "cycle_id": "c1"})
    store.append_decision("alpha", {"action": "buy", "symbol": "INFY", "quantity": 120})

    activity = client.get("/v1/agents/alpha/activity").json()["data"]
    decisions = client.get("/v1/agents/alpha/decisions").json()["data"]

    assert activity["events"][0]["type"] == "cycle_started"
    assert decisions["decisions"][0]["symbol"] == "INFY"
    assert decisions["decisions"][0]["quantity"] == 120


def test_health_says_which_agents_are_running(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    client.post("/v1/agents", json=definition())

    body = client.get("/health").json()

    assert body["ok"] is True
    assert body["agents"] == {"defined": 1, "running": 0}


# -- the live feed ----------------------------------------------------------- #


def test_the_live_feed_reports_a_line_the_moment_it_is_written(tmp_path: Path) -> None:
    """The operator watches an agent work; the feed must not wait for the end."""

    store = AgentStore(tmp_path / "agents")
    store.save(AgentDefinition.from_dict(definition()))
    store.append_activity("alpha", {"type": "cycle_started", "cycle_id": "c1"})

    stream = tail_events(store, ["alpha"], interval=0.05)
    try:
        store.append_activity("alpha", {"type": "tool_call", "tool": "screen"})
        seen: list[dict[str, Any]] = []
        for chunk in stream:
            for line in chunk.splitlines():
                if line.startswith("data:"):
                    seen.append(json.loads(line[5:].strip()))
            if any(event["type"] == "tool_call" for event in seen):
                break
    finally:
        stream.close()

    types = [event["type"] for event in seen]
    assert "cycle_started" in types
    assert "tool_call" in types


# -- the notebook the operator can read -------------------------------------- #


def test_the_operator_can_read_an_agents_notebook(tmp_path: Path) -> None:
    client, store, _ = make(tmp_path)
    client.post("/v1/agents", json=definition())
    notebook.apply(
        store, "alpha", action="write", key="orl", text="breaks failed 3 of 4"
    )

    payload = client.get("/v1/agents/alpha/notes").json()["data"]

    assert payload["count"] == 1
    assert payload["capacity"] == notebook.MAX_KEYS
    assert payload["notes"][0]["key"] == "orl"
    assert payload["notes"][0]["text"] == "breaks failed 3 of 4"


def test_the_operator_can_delete_one_lesson_or_the_whole_notebook(tmp_path: Path) -> None:
    client, store, _ = make(tmp_path)
    client.post("/v1/agents", json=definition())
    notebook.apply(store, "alpha", action="write", key="a", text="one")
    notebook.apply(store, "alpha", action="write", key="b", text="two")

    deleted = client.delete("/v1/agents/alpha/notes/a")
    cleared = client.delete("/v1/agents/alpha/notes")

    assert deleted.status_code == 200
    assert deleted.json()["data"]["count"] == 1
    assert cleared.status_code == 200
    assert cleared.json()["data"]["removed"] == 1
    assert client.get("/v1/agents/alpha/notes").json()["data"]["count"] == 0


def test_deleting_a_lesson_that_is_not_there_is_a_404_with_its_name(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)
    client.post("/v1/agents", json=definition())

    response = client.delete("/v1/agents/alpha/notes/never_written")

    assert response.status_code == 404
    assert response.json()["detail"]["error"]["code"] == "NOTE_NOT_FOUND"
    assert "never_written" in response.json()["detail"]["error"]["message"]


def test_the_notebook_of_an_agent_that_does_not_exist_is_a_404(tmp_path: Path) -> None:
    client, _, _ = make(tmp_path)

    assert client.get("/v1/agents/ghost/notes").status_code == 404

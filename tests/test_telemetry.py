# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Opt-in telemetry: install id, payload, transport failures, default counter URL."""

from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from datetime import datetime

import httpx
import pytest
import yaml
from click.testing import CliRunner
from helpers import CounterStub, FakeCollector

from turnup_check import __version__, cli
from turnup_check.collectors import poll_all
from turnup_check.diff import diff
from turnup_check.intent.file import load_intent_file
from turnup_check.report import build_report
from turnup_check.telemetry import (
    DEFAULT_COUNTER_URL,
    SEND_TIMEOUT,
    build_ping,
    config_dir,
    counter_url,
    install_id,
    send_ping,
)


def test_install_id_is_created_once(tmp_path):
    first = install_id(tmp_path / "turnup")
    assert uuid.UUID(first)
    assert (tmp_path / "turnup" / "install_id").read_text().strip() == first
    assert install_id(tmp_path / "turnup") == first


def test_corrupt_install_id_is_replaced(tmp_path):
    d = tmp_path / "turnup"
    d.mkdir()
    (d / "install_id").write_text("not-a-uuid\n")
    new = install_id(d)
    assert uuid.UUID(new) and (d / "install_id").read_text().strip() == new


def test_config_dir_precedence(monkeypatch, tmp_path):
    monkeypatch.delenv("TURNUP_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config_dir() == tmp_path / "xdg" / "turnup"
    monkeypatch.setenv("TURNUP_CONFIG_DIR", str(tmp_path / "override"))
    assert config_dir() == tmp_path / "override"
    monkeypatch.delenv("TURNUP_CONFIG_DIR")
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert config_dir() == tmp_path / "home" / ".config" / "turnup"


async def test_payload_carries_counts_and_nothing_identifying(mini_intent, fake):
    fake.move_cable(mini_intent, "CBL-12-07-03", "leaf4", "swp22")
    obs = await poll_all(mini_intent, fake, timeout=1.0)
    report = build_report(mini_intent, obs, diff(mini_intent, obs), elapsed_s=1.0)
    payload = build_ping(report, mini_intent, install="00000000-0000-4000-8000-000000000000")
    assert payload == {
        "schema": "turnup.ping/v1",
        "install_id": "00000000-0000-4000-8000-000000000000",
        "tool_version": __version__,
        "simulated": True,
        "racks_checked": 1,
        "links_verified": 7,
        "nodes_checked": 2,
        "ts": payload["ts"],
    }
    text = json.dumps(payload)
    for forbidden in ("hall-mini", "node7", "leaf4", "127.0.0.1", "mlx5", "wrong_port"):
        assert forbidden not in text
    assert payload["ts"].endswith("Z")


def test_send_ping_success_and_http_failure():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"ok": True})

    ok, msg = send_ping(
        {"racks_checked": 1, "links_verified": 287},
        url="http://127.0.0.1:9/v1/ping",
        transport=httpx.MockTransport(handler),
    )
    assert ok is True and msg == "sent (1 rack, 287 links)"
    assert seen[0].method == "POST" and json.loads(seen[0].content)["links_verified"] == 287

    bad = httpx.MockTransport(lambda r: httpx.Response(500))
    ok, msg = send_ping(
        {"racks_checked": 1, "links_verified": 0}, url="http://127.0.0.1:9/v1/ping", transport=bad
    )
    assert ok is False and "HTTP 500" in msg


def test_send_ping_never_raises_on_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    ok, msg = send_ping(
        {"racks_checked": 0, "links_verified": 0},
        url="http://127.0.0.1:9/v1/ping",
        transport=httpx.MockTransport(handler),
    )
    assert ok is False and "ConnectError" in msg and "ignored" in msg


def test_default_counter_url_follows_decision_19(monkeypatch):
    monkeypatch.delenv("TURNUP_COUNTER_URL", raising=False)
    assert DEFAULT_COUNTER_URL == "https://counter.legent.xyz/v1/ping"
    assert counter_url() == DEFAULT_COUNTER_URL
    monkeypatch.setenv("TURNUP_COUNTER_URL", "http://127.0.0.1:8700/v1/ping")
    assert counter_url() == "http://127.0.0.1:8700/v1/ping"


# -- the real wire: a local stub plays the counter ---------------------------------------------------
#
# The public counter accepts exactly the eight fields of CONTRACT section 5 with
# ``extra='forbid'``, an install_id that parses as a UUID, and a tool_version matching
# ``^[0-9A-Za-z][0-9A-Za-z.+-]{0,31}$``; anything else is a 422. These tests put a real ping on the wire
# through the CLI and check every one of those rules.

PING_KEYS = frozenset(
    {
        "schema",
        "install_id",
        "tool_version",
        "simulated",
        "racks_checked",
        "links_verified",
        "nodes_checked",
        "ts",
    }
)
TOOL_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,31}$")


def _cli_with_fake(tmp_path, monkeypatch, mini_intent_path):
    """Isolated cwd/config dir and a fake collector installed as the CLI's collector factory."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TURNUP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    shutil.copy(mini_intent_path, tmp_path / "hall-mini.yaml")
    intent = load_intent_file(tmp_path / "hall-mini.yaml")
    fake = FakeCollector.from_intent(intent)
    monkeypatch.setattr(cli, "make_collector", lambda: fake)
    return intent, fake


def _assert_counter_rules(body: dict) -> None:
    assert set(body) == PING_KEYS, body
    assert body["schema"] == "turnup.ping/v1"
    assert uuid.UUID(body["install_id"]).version == 4
    assert TOOL_VERSION_RE.match(body["tool_version"]) and body["tool_version"] == __version__
    assert isinstance(body["simulated"], bool)
    for key in ("racks_checked", "links_verified", "nodes_checked"):
        assert isinstance(body[key], int) and body[key] >= 0
    assert body["ts"].endswith("Z") and datetime.fromisoformat(body["ts"].replace("Z", "+00:00"))


@pytest.mark.parametrize("simulated", [True, False])
def test_cli_posts_exactly_the_eight_fields_to_the_counter(
    tmp_path, monkeypatch, mini_intent_path, simulated
):
    """simhall intents carry ``simulated: true`` (hall-mini.yaml is one); a file intent without the flag is
    a real hall and pings ``simulated: false``."""
    intent, fake = _cli_with_fake(tmp_path, monkeypatch, mini_intent_path)
    if not simulated:
        doc = yaml.safe_load((tmp_path / "hall-mini.yaml").read_text(encoding="utf-8"))
        doc.pop("simulated")
        (tmp_path / "hall-mini.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    fake.move_cable(intent, "CBL-12-07-03", "leaf4", "swp22")
    with CounterStub() as stub:
        monkeypatch.setenv("TURNUP_COUNTER_URL", stub.url)
        res = CliRunner().invoke(
            cli.main, ["--intent", "hall-mini.yaml", "--telemetry"], catch_exceptions=False
        )
    assert res.exit_code == 1, res.output  # the finding, not the ping, decides the exit code
    assert "share stats  sent (1 rack, 7 links)" in res.output
    (path, headers, body) = stub.received[0]
    assert len(stub.received) == 1 and path == "/v1/ping"
    assert headers["content-type"].startswith("application/json")
    _assert_counter_rules(body)
    assert body["simulated"] is simulated
    assert (body["racks_checked"], body["links_verified"], body["nodes_checked"]) == (1, 7, 2)
    assert body["install_id"] == (tmp_path / "cfg" / "install_id").read_text().strip()
    text = json.dumps(body)
    for forbidden in ("hall-mini", "node7", "leaf4", "127.0.0.1", "mlx5", "wrong_port", "CBL-"):
        assert forbidden not in text


def test_counter_rejecting_the_ping_never_changes_the_exit_code(tmp_path, monkeypatch, mini_intent_path):
    _cli_with_fake(tmp_path, monkeypatch, mini_intent_path)
    with CounterStub(status=422) as stub:
        monkeypatch.setenv("TURNUP_COUNTER_URL", stub.url)
        res = CliRunner().invoke(
            cli.main, ["--intent", "hall-mini.yaml", "--telemetry"], catch_exceptions=False
        )
    assert res.exit_code == 0, res.output
    assert "share stats  counter answered HTTP 422 (ignored)" in res.output
    assert len(stub.received) == 1


def test_hanging_counter_never_blocks_the_run(tmp_path, monkeypatch, mini_intent_path):
    _cli_with_fake(tmp_path, monkeypatch, mini_intent_path)
    with CounterStub(delay=SEND_TIMEOUT + 1.5) as stub:
        monkeypatch.setenv("TURNUP_COUNTER_URL", stub.url)
        started = time.monotonic()
        res = CliRunner().invoke(
            cli.main, ["--intent", "hall-mini.yaml", "--telemetry"], catch_exceptions=False
        )
        elapsed = time.monotonic() - started
    assert res.exit_code == 0, res.output
    assert elapsed < SEND_TIMEOUT + 1.0, elapsed  # gave up at SEND_TIMEOUT, well before the stub answered
    assert "share stats  could not reach counter (ReadTimeout; ignored)" in res.output
    assert "report       hall-mini-" in res.output  # the report was on disk before the ping went out


def test_simhall_intent_pings_simulated_true_to_a_strict_counter(tmp_path, monkeypatch):
    """A hall generated by simhall carries ``simulated: true`` and the ping says so.

    The stub enforces the counter's rules: any key beyond the eight, or a non-UUID ``install_id``,
    is answered 422.
    """
    simhall = pytest.importorskip("simhall", exc_type=ImportError)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TURNUP_CONFIG_DIR", str(tmp_path / "cfg"))
    hall = simhall.Hall.generate(simhall.HallSpec(site="sim", racks=1, nodes_per_rack=2, ports_per_node=4))
    hall.write_intent(tmp_path / "sim.yaml")
    intent = load_intent_file(tmp_path / "sim.yaml")
    assert intent.simulated is True
    fake = FakeCollector.from_intent(intent)
    monkeypatch.setattr(cli, "make_collector", lambda: fake)
    with CounterStub(strict=True) as stub:
        monkeypatch.setenv("TURNUP_COUNTER_URL", stub.url)
        res = CliRunner().invoke(cli.main, ["--intent", "sim.yaml", "--telemetry"], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        assert stub.statuses == [202], stub.received  # the strict counter accepted it
        body = stub.received[0][2]
        _assert_counter_rules(body)
        assert body["simulated"] is True
        assert (body["racks_checked"], body["links_verified"], body["nodes_checked"]) == (1, 8, 2)

        # The same stub refuses what the real counter refuses.
        extra = {**body, "site": "sim"}
        assert send_ping(extra, url=stub.url)[0] is False
        assert send_ping({**body, "install_id": "not-a-uuid"}, url=stub.url)[0] is False
        assert stub.statuses[1:] == [422, 422]

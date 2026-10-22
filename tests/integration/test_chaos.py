# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Chaos tier (CONTRACT 9.4) over real sockets: timeouts, HTTP 500, refused connections, partial Redfish
trees and DCGM exporters down, several nodes at once, plus an unreadable switch and a hall where nothing
answers.

The run always completes; affected devices are reported as unreachable (``unreachable_node``) or unknown
(notes), the exit code follows CONTRACT section 4 (1 with findings, 0 when the only trouble is an exporter
down, 2 when no telemetry source answers) and the polling time stays within ``2 x timeout + 1 s``.

Sizing note: simhall serves every device from one host:port, so the collector's per-host limit (4) makes
``timeout`` requests to different devices queue up as they never would against real BMCs. Two
``timeout`` nodes (Redfish root + DCGM each) fit one round of the limit and cost one timeout.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

pytest.importorskip("simhall", reason="simhall not installed in this venv", exc_type=ImportError)

from simhall_helpers import CHECK, CROSS, SimServer, run_cli

pytestmark = pytest.mark.integration

TIMEOUT = 1.0


def _check(golden: SimServer, env: dict[str, str], tmp_path: Path, out: Path, timeout: float = TIMEOUT):
    args = ["--site", "hall-b", "--intent", golden.netbox_url, "--timeout", str(timeout), "--out", str(out)]
    return run_cli(args, env, tmp_path)


def test_every_fault_mode_on_several_nodes_at_once(golden: SimServer, runner_env, tmp_path: Path):
    for node in ("node3", "node4"):
        golden.inject("unreachable_node", node=node, mode="timeout")
    for node in ("node5", "node6"):
        golden.inject("unreachable_node", node=node, mode="http_500")
    for node in ("node8", "node9"):
        golden.inject("unreachable_node", node=node, mode="refused")
    for node in ("node10", "node11"):
        golden.inject("partial_redfish", node=node)
    for node in ("node12", "node13"):
        golden.inject("unreachable_node", node=node, mode="refused", scope="dcgm")  # exporter down

    out = tmp_path / "chaos.json"
    res = _check(golden, runner_env, tmp_path, out)
    assert res.returncode == 1, (res.stdout, res.stderr, golden.log())
    doc = json.loads(out.read_text())

    assert Counter(f["class"] for f in doc["findings"]) == {"unreachable_node": 6, "wrong_port": 1}
    unreachable = {f["device"]: f["observed"] for f in doc["findings"] if f["class"] == "unreachable_node"}
    assert set(unreachable) == {"node3", "node4", "node5", "node6", "node8", "node9"}
    assert unreachable["node3"] == unreachable["node4"] == f"timeout after {TIMEOUT:g} s"
    assert unreachable["node5"] == unreachable["node6"] == "HTTP 500"
    assert unreachable["node8"] == unreachable["node9"] == "HTTP 404"  # "refused" is 404 at the prefix in sim
    assert doc["summary"]["nodes_reachable"] == 12 and doc["summary"]["nodes_total"] == 18
    assert doc["summary"]["links_ok"] == 287 and doc["summary"]["links_total"] == 288
    # Partial trees are unknown, never a firmware mismatch; exporters down are unknown, never a gpu finding.
    assert doc["summary"]["firmware_ok"] is True
    for node in ("node10", "node11"):
        assert any(n.startswith(f"{node}: firmware unknown") for n in doc["notes"]), doc["notes"]
    for node in ("node12", "node13"):
        assert any(
            n.startswith(f"{node}: dcgm unreachable") and n.endswith("gpu: unknown") for n in doc["notes"]
        )
    assert doc["summary"]["gpus_expected"] == 72 and doc["summary"]["gpus_seen"] == 72 - 8 * 4
    assert doc["elapsed_s"] <= 2 * TIMEOUT + 1.0, doc["elapsed_s"]

    assert f"{CHECK} 287 / 288 links match intent" in res.stdout
    assert f"{CHECK} 12 / 18 nodes reachable, firmware baseline ok" in res.stdout
    assert f"{CROSS} offline    node3 redfish unreachable: timeout after {TIMEOUT:g} s" in res.stdout
    assert f"{CROSS} offline    node5 redfish unreachable: HTTP 500" in res.stdout
    assert f"{CROSS} mismatch   node7 mlx5_3" in res.stdout


def test_dcgm_exporter_down_is_unknown_not_a_finding(golden: SimServer, runner_env, tmp_path: Path):
    for f in golden.faults():
        golden.clear(str(f["id"]))
    for node in ("node3", "node4", "node5"):
        golden.inject("unreachable_node", node=node, mode="refused", scope="dcgm")
    out = tmp_path / "dcgm.json"
    res = _check(golden, runner_env, tmp_path, out)
    assert res.returncode == 0, (res.stdout, res.stderr)
    doc = json.loads(out.read_text())
    assert doc["findings"] == []
    assert doc["summary"]["nodes_reachable"] == 18 and doc["summary"]["links_ok"] == 288
    assert doc["summary"]["gpus_seen"] == 72 - 3 * 4
    unknown = [n for n in doc["notes"] if n.endswith("gpu: unknown")]
    assert sorted(n.split(":")[0] for n in unknown) == ["node3", "node4", "node5"], doc["notes"]
    assert f"{CHECK} 288 / 288 links match intent" in res.stdout
    assert f"{CHECK} 18 / 18 nodes reachable, firmware baseline ok" in res.stdout
    assert doc["elapsed_s"] <= 2 * TIMEOUT + 1.0


def test_unreadable_switch_leaves_its_links_unverified(golden: SimServer, runner_env, tmp_path: Path):
    golden.inject("unreachable_node", node="leaf4", mode="timeout")
    out = tmp_path / "leaf.json"
    res = _check(golden, runner_env, tmp_path, out, timeout=0.5)
    assert res.returncode == 1, (res.stdout, res.stderr)
    doc = json.loads(out.read_text())
    # The golden wrong_port lives on leaf4, whose table could not be read: unverified, not a wrong_port.
    (finding,) = doc["findings"]
    assert finding["class"] == "unreachable_node" and finding["device"] == "leaf4"
    assert "144 links not verified" in finding["diagnosis"]
    assert doc["summary"]["links_ok"] == 144 and doc["summary"]["nodes_reachable"] == 18
    assert any(n.startswith("144 links not verified") for n in doc["notes"]), doc["notes"]
    assert f"{CHECK} 144 / 288 links match intent" in res.stdout
    assert doc["elapsed_s"] <= 2 * 0.5 + 1.0


def test_timeout_fault_alone_costs_about_one_timeout(golden: SimServer, runner_env, tmp_path: Path):
    golden.inject("unreachable_node", node="node10", mode="timeout")
    out = tmp_path / "t.json"
    res = _check(golden, runner_env, tmp_path, out, timeout=0.5)
    assert res.returncode == 1, (res.stdout, res.stderr)
    doc = json.loads(out.read_text())
    assert any(f["class"] == "unreachable_node" and f["device"] == "node10" for f in doc["findings"])
    assert doc["elapsed_s"] <= 2 * 0.5 + 1.0


def test_nothing_answering_exits_2(golden: SimServer, runner_env, tmp_path: Path):
    intent = golden.intent()
    for dev in intent["devices"]:  # every node and both leaves
        golden.inject("unreachable_node", node=dev["name"], mode="refused")
    out = tmp_path / "dark.json"
    res = _check(golden, runner_env, tmp_path, out, timeout=0.5)
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert f"{CROSS} no telemetry source reachable" in res.stdout
    doc = json.loads(out.read_text())  # the report is still written
    assert Counter(f["class"] for f in doc["findings"]) == {"unreachable_node": 20}
    assert doc["summary"]["links_ok"] == 0 and doc["summary"]["nodes_reachable"] == 0
    assert doc["elapsed_s"] <= 2 * 0.5 + 1.0

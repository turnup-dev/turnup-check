# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Scale tier: a 10-rack hall (180 nodes, 2,880 cables) checked in under 30 s."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

pytest.importorskip("simhall", reason="simhall not installed in this venv", exc_type=ImportError)

from simhall_helpers import run_cli, start_server

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def test_ten_rack_hall_under_thirty_seconds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from simhall_helpers import cli_env

    spec = {
        "site": "hall-x",
        "racks": 10,
        "nodes_per_rack": 18,
        "ports_per_node": 16,
        "leaves_per_rack": 2,
        "switch_vendor": "nvidia",
        "lldp_port_id_mode": "name",
        "node_model": "GB300 NVL72 compute tray",
        "seed": 7,
    }
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    server = start_server(["--spec", str(spec_path)], tmp_path)
    try:
        monkeypatch.chdir(tmp_path)
        out_path = tmp_path / "scale.json"
        res = run_cli(
            ["--site", "hall-x", "--intent", server.netbox_url, "--out", str(out_path)],
            cli_env(tmp_path),
            tmp_path,
        )
        assert res.returncode == 0, (res.stdout[-2000:], res.stderr[-2000:], server.log())
        doc = json.loads(out_path.read_text())
        assert (
            doc["intent"]["links"] == 2880 and doc["intent"]["nodes"] == 180 and doc["intent"]["racks"] == 10
        )
        assert doc["summary"]["links_ok"] == 2880 and doc["summary"]["nodes_reachable"] == 180
        assert doc["elapsed_s"] < 30.0, doc["elapsed_s"]
    finally:
        server.stop()

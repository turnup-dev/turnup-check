# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""File intent adapter and ``--baseline`` merging."""

from __future__ import annotations

import json

import pytest

from turnup_check.intent import IntentError, describe_intent_arg, load_intent
from turnup_check.intent.file import load_intent_file, merge_baseline_file


def test_load_yaml_intent(mini_intent):
    assert mini_intent.site == "hall-mini"
    assert mini_intent.simulated is True
    assert mini_intent.generated_at == "2026-10-16T18:00:00Z"
    assert len(mini_intent.cables) == 8
    assert [d.name for d in mini_intent.nodes] == ["node7", "node8"]
    assert [d.name for d in mini_intent.leaves] == ["leaf3", "leaf4"]
    assert mini_intent.source == "file"
    assert mini_intent.summary() == {"links": 8, "nodes": 2, "leaves": 2, "racks": 1, "source": "file"}
    assert mini_intent.device("leaf4").lldp_protocol == "nvue"
    assert mini_intent.device("node7").interface("mlx5_3").mac == "b8:3f:d2:00:07:03"


def test_load_json_intent(tmp_path, mini_intent):
    path = tmp_path / "hall.json"
    path.write_text(json.dumps(mini_intent.model_dump(by_alias=True)), encoding="utf-8")
    again = load_intent_file(path)
    assert again.site == "hall-mini"
    assert len(again.devices) == 4


def test_bad_schema_is_a_value_error(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("schema: turnup.intent/v9\nsite: x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported intent schema"):
        load_intent_file(path)


def test_non_mapping_is_a_value_error(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_intent_file(path)


def test_invalid_yaml_is_a_value_error(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("schema: [unclosed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid YAML"):
        load_intent_file(path)


def test_baseline_file_merges_under_intent_baseline(tmp_path, mini_intent):
    path = tmp_path / "baseline.yaml"
    path.write_text(
        "GB300 NVL72 compute tray: {nic: '0.0.0', gpu_vbios: '97.00'}\nHGX H100: {BMC: '7.10'}\n",
        encoding="utf-8",
    )
    merge_baseline_file(mini_intent, path)
    tray = mini_intent.firmware_baseline["GB300 NVL72 compute tray"]
    assert tray["nic"] == "28.43.1014"  # intent wins
    assert tray["gpu_vbios"] == "97.00"
    assert mini_intent.firmware_baseline["HGX H100"] == {"bmc": "7.10"}


def test_baseline_file_accepts_firmware_baseline_wrapper(tmp_path, mini_intent):
    path = tmp_path / "baseline.yaml"
    path.write_text("firmware_baseline:\n  X: {bios: '1'}\n", encoding="utf-8")
    merge_baseline_file(mini_intent, path)
    assert mini_intent.firmware_baseline["X"] == {"bios": "1"}


def test_baseline_file_shape_errors(tmp_path, mini_intent):
    path = tmp_path / "baseline.yaml"
    path.write_text("X: notamapping\n", encoding="utf-8")
    with pytest.raises(ValueError):
        merge_baseline_file(mini_intent, path)


async def test_load_intent_file_prefix_and_baseline(mini_intent_path, tmp_path):
    baseline = tmp_path / "b.yaml"
    baseline.write_text("GB300 NVL72 compute tray: {gpu_vbios: '1'}\n", encoding="utf-8")
    intent = await load_intent(f"file:{mini_intent_path}", site=None, baseline=baseline)
    assert intent.firmware_baseline["GB300 NVL72 compute tray"]["gpu_vbios"] == "1"


async def test_load_intent_missing_file_is_intent_error(tmp_path):
    with pytest.raises(IntentError):
        await load_intent(str(tmp_path / "nope.yaml"), site=None)


async def test_load_intent_netbox_without_env_is_intent_error():
    with pytest.raises(IntentError, match="NETBOX_URL"):
        await load_intent("netbox", site="hall-b", env={})
    with pytest.raises(IntentError, match="NAUTOBOT_URL"):
        await load_intent("nautobot", site="hall-b", env={})


async def test_load_intent_url_without_site_is_intent_error():
    with pytest.raises(IntentError, match="--site"):
        await load_intent("http://127.0.0.1:9/netbox", site=None, env={})


def test_describe_intent_arg():
    assert describe_intent_arg("netbox") == "netbox"
    assert describe_intent_arg("http://127.0.0.1:8900/netbox") == "netbox"
    assert describe_intent_arg("nautobot") == "nautobot"
    assert describe_intent_arg("./hall.yaml") == "file"


def test_unquoted_yaml_versions_keep_their_text(tmp_path, mini_intent_path):
    """``bmc: 1.10`` is the float 1.1 to YAML; as a baseline version it must stay "1.10" (no false
    firmware_off_baseline on every node). Baseline files and intent files alike."""
    base = tmp_path / "baseline.yaml"
    base.write_text('"GB300 NVL72 compute tray": {bmc: 1.10, bios: 2.3.1, nic: 28, cx_firmware: 1.0e3}\n')
    intent = load_intent_file(mini_intent_path)
    intent.firmware_baseline = {}
    merge_baseline_file(intent, base)
    assert intent.firmware_baseline["GB300 NVL72 compute tray"] == {
        "bmc": "1.10",
        "bios": "2.3.1",
        "nic": "28",
        "cx_firmware": "1.0e3",
    }
    doc = mini_intent_path.read_text(encoding="utf-8")
    path = tmp_path / "hall.yaml"
    path.write_text(
        doc.split("firmware_baseline:")[0] + 'firmware_baseline:\n  "GB300 NVL72 compute tray": {bmc: 1.10}\n'
    )
    loaded = load_intent_file(path)
    assert loaded.firmware_baseline == {"GB300 NVL72 compute tray": {"bmc": "1.10"}}
    assert loaded.cables[0].speed_gbps == 400  # numbers elsewhere are still numbers
    assert loaded.model_dump(by_alias=True)["cables"][0]["speed_gbps"] == 400


def test_baseline_model_key_matches_case_insensitively(mini_intent):
    node = mini_intent.device("node7")
    want = dict(mini_intent.baseline_for(node))
    mini_intent.firmware_baseline = {"  gb300 nvl72   COMPUTE tray ": want}
    assert mini_intent.baseline_for(node) == want

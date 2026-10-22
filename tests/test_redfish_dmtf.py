# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Redfish collector against trimmed DMTF public mockups (DSP2043 2026.2; see fixtures/dmtf/LICENSE)."""

from __future__ import annotations

from helpers import load_json

from turnup_check.collectors.redfish import DictFetcher, walk_redfish


def tree(name: str) -> dict:
    return load_json(f"dmtf/{name}/tree.json")


async def test_rackmount1_firmware_and_ethernet_interfaces_fallback():
    obs = await walk_redfish(DictFetcher(tree("public-rackmount1")), timeout=1.0)
    assert obs.reachable is True and obs.raw_error is None
    assert (obs.health, obs.power_state, obs.serial, obs.model) == ("OK", "On", "437XR1138R2", "3500")
    assert obs.firmware_known is True
    # "Contoso BMC Firmware" / "Contoso BIOS Firmware"; AC-RoT0 and Simple Storage are not baseline keys.
    assert obs.firmware == {"bmc": "1.45.455b66-rev4", "bios": "P79 v1.45"}
    # No Chassis NetworkAdapters: Systems/<id>/EthernetInterfaces stand in for ports.
    assert [p.id for p in obs.ports] == ["12446A3B0411", "12446A3B8890", "VLAN1", "ToManager"]
    p = obs.port("12446A3B8890")
    assert p.link_up is True and p.speed_gbps == 1.0 and p.mac == "aa:bb:cc:dd:ee:00"
    assert obs.port("nope", mac="12:44:6a:3b:04:11").id == "12446A3B0411"


async def test_smartnic_chassis_ports_and_physical_system():
    obs = await walk_redfish(DictFetcher(tree("public-smartnic")), timeout=1.0)
    assert obs.serial == "437XR1138R2"
    assert [(p.id, p.link_status, p.speed_gbps) for p in obs.ports] == [
        ("SC2KP1", "LinkUp", 25.0),
        ("SC2KP2", "LinkUp", 25.0),
    ]


async def test_smartnic_dpu_listed_first_still_picks_the_physical_host():
    t = tree("public-smartnic")
    t["/redfish/v1/Systems"]["Members"].reverse()
    assert t["/redfish/v1/Systems/Card1-SoC"]["SystemType"] == "DPU"
    obs = await walk_redfish(DictFetcher(t), timeout=1.0)
    assert obs.serial == "437XR1138R2" and obs.model == "3500"


async def test_smartnic_network_interfaces_ports_when_chassis_has_no_adapters():
    t = tree("public-smartnic")
    t["/redfish/v1/Chassis/Card1"].pop("NetworkAdapters")
    del t["/redfish/v1/Chassis/Card1/NetworkAdapters"]
    obs = await walk_redfish(DictFetcher(t), timeout=1.0)
    # Systems/<id>/NetworkInterfaces/<id>/Ports link back to the Chassis Port resources; each counted once.
    assert sorted(p.id for p in obs.ports) == ["SC2KP1", "SC2KP2"]


async def test_acd_duplicate_port_links_are_counted_once():
    obs = await walk_redfish(DictFetcher(tree("public-acd")), timeout=1.0)
    ids = [p.id for p in obs.ports]
    assert len(ids) == len(set(ids)) and "SC2KP2" in ids
    assert obs.firmware_known is False and obs.firmware == {}  # no UpdateService: unknown, not a mismatch


async def test_bladed_manager_follows_system_managed_by():
    fetch = DictFetcher(tree("public-bladed"))
    obs = await walk_redfish(fetch, timeout=1.0)
    assert obs.reachable is True and obs.serial == "529QB9450R6"
    assert "/redfish/v1/Managers/Blade1BMC" in fetch.calls
    assert "/redfish/v1/Managers/MultiBladeBMC" not in fetch.calls


async def test_pdu_service_root_without_systems_is_explained():
    obs = await walk_redfish(DictFetcher(tree("public-pdu")), timeout=1.0)
    assert obs.reachable is False
    assert obs.raw_error == "HTTP 404 (service root has no Systems collection)"

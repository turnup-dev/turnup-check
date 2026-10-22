# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Redfish collector: tree walk, component matching, partial trees, transport failures, URL prefixes."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from helpers import load_json, redfish_node7

from turnup_check.collectors import HttpCollector
from turnup_check.collectors.redfish import (
    DictFetcher,
    FetchError,
    HttpFetcher,
    component_for,
    odata_path,
    parse_firmware_inventory,
    parse_port,
    walk_redfish,
)
from turnup_check.models import Creds, Device, Mgmt


async def test_full_tree_walk():
    fetch = DictFetcher(load_json("redfish/node7_tree.json"))
    obs = await walk_redfish(fetch, timeout=1.0)
    assert obs.reachable is True
    assert obs.health == "OK" and obs.state == "Enabled" and obs.power_state == "On"
    assert obs.serial == "SN-N7-000123" and obs.model == "GB300 NVL72 compute tray"
    assert obs.manufacturer == "NVIDIA" and obs.hostname == "node7"
    assert obs.bios_version == "2.3.1" and obs.processor_count == 2
    assert obs.firmware_known is True
    assert obs.firmware == {
        "bmc": "1.12.0",
        "bios": "2.3.1",
        "nic": "28.43.1014",
        "gpu_vbios": "97.00.5E.00.01",
    }
    assert [p.id for p in obs.ports] == ["mlx5_0", "mlx5_1", "mlx5_2", "mlx5_3"]
    p3 = obs.port("mlx5_3")
    assert p3.link_up is True and p3.speed_gbps == 400
    assert p3.mac == "b8:3f:d2:00:07:03"
    assert (p3.lldp_system, p3.lldp_port) == ("leaf4", "swp24")
    assert obs.raw_error is None
    # Every link was resolved relative to the device prefix (paths start with /redfish/v1).
    assert all(c.startswith("/redfish/v1") for c in fetch.calls)


async def test_partial_tree_reports_firmware_unknown():
    obs = await walk_redfish(DictFetcher(load_json("redfish/node7_partial_tree.json")), timeout=1.0)
    assert obs.reachable is True
    assert obs.firmware_known is False
    assert obs.firmware == {}
    assert len(obs.ports) == 4  # the rest of the tree is still read
    assert obs.raw_error is None


async def test_warning_tree():
    obs = await walk_redfish(DictFetcher(load_json("redfish/node7_warning_tree.json")), timeout=1.0)
    assert obs.health == "Warning"
    assert obs.firmware["nic"] == "28.42.1000"


async def test_unreachable_root():
    obs = await walk_redfish(DictFetcher({}, unreachable=True), timeout=1.0)
    assert obs.reachable is False
    assert "connection refused" in obs.raw_error


async def test_http_500_on_systems_is_unreachable():
    tree = redfish_node7()
    obs = await walk_redfish(DictFetcher(tree, status_overrides={"/redfish/v1/Systems": 500}), timeout=1.0)
    assert obs.reachable is False
    assert obs.raw_error == "HTTP 500"


async def test_root_without_trailing_slash_is_tried():
    tree = redfish_node7()
    tree["/redfish/v1"] = tree.pop("/redfish/v1/")
    obs = await walk_redfish(DictFetcher(tree), timeout=1.0)
    assert obs.reachable is True


async def test_missing_chassis_branch_gives_no_ports_but_reachable():
    tree = redfish_node7()
    tree["/redfish/v1/"].pop("Chassis")
    obs = await walk_redfish(DictFetcher(tree), timeout=1.0)
    assert obs.reachable is True and obs.ports == [] and obs.firmware_known is True


async def test_firmware_inventory_falls_back_to_manager_and_bios_version_only_when_inventory_exists():
    tree = redfish_node7()
    inv = "/redfish/v1/UpdateService/FirmwareInventory"
    tree[inv]["Members"] = [{"@odata.id": f"{inv}/NIC.1"}]
    obs = await walk_redfish(DictFetcher(tree), timeout=1.0)
    assert obs.firmware == {"nic": "28.43.1014", "bmc": "1.12.0", "bios": "2.3.1"}


async def test_transport_error_mid_walk_keeps_system_fields():
    class Flaky(DictFetcher):
        async def __call__(self, path):
            if "FirmwareInventory" in path:
                raise FetchError("transport", "timeout after 1 s")
            return await super().__call__(path)

    obs = await walk_redfish(Flaky(redfish_node7()), timeout=1.0)
    assert obs.reachable is True
    assert obs.firmware_known is False
    assert obs.raw_error.startswith("partial walk")


async def test_device_deadline_bounds_a_slow_optional_walk():
    class Slow(DictFetcher):
        async def __call__(self, path):
            if "Chassis" in path:
                await asyncio.sleep(0.5)
            return await super().__call__(path)

    obs = await walk_redfish(Slow(redfish_node7()), timeout=0.1)
    assert obs.reachable is True
    assert obs.raw_error == "partial walk: device deadline exceeded"


def test_component_matching_table():
    assert component_for("BMC Firmware") == "bmc"
    assert component_for("Integrated Dell Remote Access Controller") == "bmc"
    assert component_for("iLO 6") == "bmc"
    assert component_for("Lenovo XCC") == "bmc"
    assert component_for("Baseboard Management Controller") == "bmc"
    assert component_for("System BIOS") == "bios"
    assert component_for("UEFI") == "bios"
    assert component_for("ConnectX-7 Adapter") == "nic"
    assert component_for("BlueField-3 DPU") == "nic"
    assert component_for("Network Adapter 1") == "nic"
    assert component_for("NVIDIA GPU VBIOS") == "gpu_vbios"  # VBIOS before BIOS
    assert component_for("GPU Baseboard") == "gpu_vbios"
    assert component_for("Fan Controller") is None
    assert component_for("Dell 64 Bit uEFI Diagnostics") is None
    assert component_for("Broadcom Gigabit Ethernet BCM5720") is None
    assert component_for(None) is None


def test_parse_firmware_inventory_skips_previous_and_keeps_first():
    members = [
        {"Id": "Previous-BIOS", "Name": "System BIOS", "Version": "1.0"},
        {"Id": "Installed-BIOS", "Name": "System BIOS", "Version": "2.0"},
        {"Id": "Installed-BIOS-2", "Name": "System BIOS", "Version": "3.0"},
        {"Id": "NIC.Slot.1", "Name": "Mellanox ConnectX-7", "Version": "28.43"},
        {"Id": "X", "Name": None, "Version": "9"},
    ]
    assert parse_firmware_inventory(members) == {"bios": "2.0", "nic": "28.43"}


def test_parse_port_legacy_networkports_shape():
    res = {
        "Id": "1",
        "Name": "mlx5_0",
        "LinkStatus": "Up",
        "CurrentLinkSpeedMbps": 200000,
        "AssociatedNetworkAddresses": ["B8:3F:D2:00:07:00"],
    }
    p = parse_port(res)
    assert p.link_status == "LinkUp" and p.link_up is True
    assert p.speed_gbps == 200.0 and p.mac == "b8:3f:d2:00:07:00"
    assert p.lldp_system is None


def test_odata_path_forms():
    assert odata_path({"@odata.id": "/redfish/v1/Systems"}) == "/redfish/v1/Systems"
    assert odata_path("https://10.0.0.7/redfish/v1/Systems/1") == "/redfish/v1/Systems/1"
    assert odata_path("redfish/v1/x") == "/redfish/v1/x"
    assert odata_path(None) is None
    assert odata_path({}) is None


def _mock_bmc(tree: dict, *, prefix: str = "/dev/node7", status: int | None = None, slow: bool = False):
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if slow:
            await asyncio.sleep(0.5)
        if status is not None:
            return httpx.Response(status, json={"error": "nope"})
        path = request.url.path
        if not path.startswith(prefix):
            return httpx.Response(404, json={"error": "wrong prefix"})
        key = path[len(prefix) :]
        key = key if key.endswith("/") and key.rstrip("/") == "/redfish/v1" else key.rstrip("/")
        for k, v in tree.items():
            if k.rstrip("/") == key.rstrip("/"):
                return httpx.Response(200, json=v)
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler), seen


async def test_http_fetcher_keeps_device_prefix_and_sends_basic_auth():
    transport, seen = _mock_bmc(redfish_node7())
    async with httpx.AsyncClient(transport=transport) as client:
        fetch = HttpFetcher(client, "http://127.0.0.1:8900/dev/node7/", auth=("admin", "pw"), timeout=1.0)
        obs = await walk_redfish(fetch, timeout=1.0)
    assert obs.reachable is True and obs.firmware["bmc"] == "1.12.0" and len(obs.ports) == 4
    assert all(r.url.path.startswith("/dev/node7/redfish/v1") for r in seen)
    assert seen[0].headers["Authorization"].startswith("Basic ")


async def test_http_fetcher_401_is_unreachable_with_status():
    transport, _ = _mock_bmc(redfish_node7(), status=401)
    async with httpx.AsyncClient(transport=transport) as client:
        obs = await walk_redfish(HttpFetcher(client, "https://bmc", auth=None, timeout=1.0), timeout=1.0)
    assert obs.reachable is False and obs.raw_error == "HTTP 401"


async def test_http_fetcher_timeout_is_unreachable():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        obs = await walk_redfish(HttpFetcher(client, "https://bmc", auth=None, timeout=0.2), timeout=0.2)
    assert obs.reachable is False and obs.raw_error == "timeout after 0.2 s"


async def test_http_collector_redfish_uses_intent_bmc_url_and_creds():
    transport, seen = _mock_bmc(redfish_node7())
    node = Device(name="node7", role="gpu-node", mgmt=Mgmt(bmc_url="http://127.0.0.1:8900/dev/node7"))
    async with httpx.AsyncClient(transport=transport) as client, HttpCollector(client) as collector:
        obs = await collector.redfish(node, Creds(bmc_user="u", bmc_pass="p"), 1.0)
        no_url = await collector.redfish(Device(name="x", role="gpu-node"), Creds(), 1.0)
    assert obs.reachable is True
    assert no_url.reachable is False and "no bmc_url" in no_url.raw_error
    assert seen[0].headers["Authorization"].startswith("Basic ")


@pytest.mark.parametrize("kind", ["transport", "http"])
def test_fetch_error_str(kind):
    err = FetchError(kind, "detail", status=503 if kind == "http" else None)
    assert str(err) == ("HTTP 503" if kind == "http" else "detail")


def test_standby_images_listed_first_do_not_hide_the_running_version():
    """OpenBMC lists the backup BMC bank, Supermicro "BMC Backup", often before the running image."""
    members = [
        {
            "Id": "bmc_backup",
            "Name": "BMC Firmware",
            "Version": "1.10.0",
            "Status": {"State": "StandbySpare"},
        },
        {"Id": "BMC_Backup", "Name": "BMC Backup", "Version": "1.09.0", "Status": {"State": "Enabled"}},
        {"Id": "bmc_active", "Name": "BMC Firmware", "Version": "1.12.0", "Status": {"State": "Enabled"}},
        {"Id": "bios_pending", "Name": "BIOS (staged)", "Version": "2.4.0"},
        {"Id": "bios", "Name": "System BIOS", "Version": "2.3.1"},
        {
            "Id": "nic_inactive",
            "Name": "ConnectX NIC Firmware",
            "Version": "28.1",
            "Status": {"State": "StandbyOffline"},
        },
    ]
    fw = parse_firmware_inventory(members)
    assert fw["bmc"] == "1.12.0"
    assert fw["bios"] == "2.3.1"
    assert fw["nic"] == "28.1"  # only a standby image is listed: better than unknown

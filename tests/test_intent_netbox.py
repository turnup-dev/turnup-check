# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""NetBox and Nautobot intent adapters over canned API pages (httpx.MockTransport)."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest
from helpers import load_json

from turnup_check.intent import IntentError, load_intent
from turnup_check.intent.nautobot import NautobotIntentSource
from turnup_check.intent.netbox import NetBoxIntentSource, normalize_role, slugify, strip_prefix_length

NB = "http://netbox.example.test"


class FakeNetBox:
    """Routes ``/api/dcim/<endpoint>/`` to fixture pages and records every request."""

    def __init__(self, flavor: str = "netbox", *, fail: int | None = None) -> None:
        self.flavor = flavor
        self.requests: list[httpx.Request] = []
        self.fail = fail

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            return httpx.Response(self.fail, json={"detail": "boom"})
        path = request.url.path
        q = parse_qs(request.url.query.decode())
        endpoint = path.rstrip("/").rsplit("/", 1)[-1]
        if self.flavor == "netbox":
            if endpoint == "devices":
                page = "devices_page2" if q.get("offset") == ["3"] else "devices_page1"
                return httpx.Response(200, json=load_json(f"netbox/{page}.json"))
            if endpoint in ("interfaces", "cables", "racks", "sites"):
                return httpx.Response(200, json=load_json(f"netbox/{endpoint}.json"))
        elif self.flavor == "netbox3":
            if endpoint in ("devices", "interfaces", "cables"):
                return httpx.Response(200, json=load_json(f"netbox3/{endpoint}.json"))
            return httpx.Response(404)
        elif self.flavor == "nautobot":
            if endpoint in ("devices", "interfaces", "cables"):
                return httpx.Response(200, json=load_json(f"nautobot/{endpoint}.json"))
            return httpx.Response(404)
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


async def test_netbox4_full_site():
    nb = FakeNetBox()
    async with nb.client() as client:
        intent = await NetBoxIntentSource(f"{NB}/", "tok", "hall-b", client=client).load()

    # Pagination: devices page 1 then its `next` link, plus interfaces, cables, racks.
    paths = [(r.url.path, parse_qs(r.url.query.decode())) for r in nb.requests]
    assert [p for p, _ in paths] == [
        "/api/dcim/devices/",
        "/api/dcim/devices/",
        "/api/dcim/interfaces/",
        "/api/dcim/cables/",
        "/api/dcim/racks/",
        "/api/dcim/sites/",
    ]
    assert paths[5][1]["slug"] == ["hall-b"]
    assert paths[0][1]["site"] == ["hall-b"] and paths[0][1]["limit"] == ["1000"]
    assert paths[1][1]["offset"] == ["3"]
    assert all(r.headers["Authorization"] == "Token tok" for r in nb.requests)

    assert intent.site == "hall-b" and intent.source == "netbox"
    assert intent.simulated is True  # the fixture site says "Simulated by simhall"
    assert {d.name: d.role for d in intent.devices} == {
        "node7": "gpu-node",
        "node8": "gpu-node",
        "leaf3": "leaf-switch",
        "leaf4": "leaf-switch",
        "pdu1": "pdu",
    }
    node7, node8 = intent.device("node7"), intent.device("node8")
    leaf3, leaf4 = intent.device("leaf3"), intent.device("leaf4")
    # Custom fields win; primary_ip4 fallback strips the prefix length.
    assert node7.mgmt.bmc_url == "http://127.0.0.1:8900/dev/node7"
    assert node7.mgmt.dcgm_url == "http://127.0.0.1:8900/dev/node7"
    assert node8.mgmt.bmc_url == "https://10.0.0.8"
    assert node8.mgmt.dcgm_url == "http://10.0.0.8:9400"
    assert leaf3.mgmt.switch_url == "https://10.0.1.3"
    assert leaf4.mgmt.switch_url == "http://127.0.0.1:8900/dev/leaf4"
    assert leaf4.lldp_protocol == "nvue"
    assert node7.model == "GB300 NVL72 compute tray" and node7.manufacturer == "NVIDIA"
    assert node7.rack == "12" and node7.serial == "SN-NODE7" and node7.expected_gpus == 4
    # Interfaces with both MAC shapes (NetBox <=4.1 mac_address, 4.2+ primary_mac_address).
    assert node7.interface("mlx5_3").mac == "b8:3f:d2:00:07:03"
    assert node8.interface("mlx5_1").mac == "b8:3f:d2:00:08:01"
    assert node7.interface("mlx5_0").type == "400gbase-x-osfp"
    assert len(leaf4.interfaces) == 32
    # Config context firmware baseline keyed by model.
    assert intent.firmware_baseline == {
        "GB300 NVL72 compute tray": {"bmc": "1.12.0", "bios": "2.3.1", "nic": "28.43.1014"}
    }
    # Cables: 8 interface cables, power cable skipped, node always on side a, speed from type.
    assert len(intent.cables) == 8
    golden = next(c for c in intent.cables if c.label == "CBL-12-07-03")
    assert (golden.a.device, golden.a.interface) == ("node7", "mlx5_3")
    assert (golden.b.device, golden.b.interface) == ("leaf4", "swp21")
    assert golden.speed_gbps == 400
    assert all(
        intent.device(c.a.device).is_node and intent.device(c.b.device).is_switch for c in intent.cables
    )
    assert intent.rack_names == ["12"]
    assert intent.summary() == {"links": 8, "nodes": 2, "leaves": 2, "racks": 1, "source": "netbox"}


async def test_netbox3_shapes_device_role_and_legacy_terminations():
    nb = FakeNetBox("netbox3")
    async with nb.client() as client:
        intent = await NetBoxIntentSource(NB, None, "hall-b", client=client).load()
    assert {d.name: d.role for d in intent.devices} == {"node7": "gpu-node", "leaf4": "leaf-switch"}
    assert intent.device("leaf4").lldp_protocol == "eapi"
    assert len(intent.cables) == 1
    c = intent.cables[0]
    assert (c.a.device, c.a.interface, c.b.device, c.b.interface) == (
        "node7",
        "mlx5_3",
        "leaf4",
        "Ethernet21/1",
    )
    assert all("Authorization" not in r.headers for r in nb.requests)
    assert intent.rack_names == ["12"]  # racks endpoint 404 -> derived from devices
    assert intent.simulated is False  # no site marker -> a real hall


async def test_nautobot_location_filter_depth_and_role_name():
    nb = FakeNetBox("nautobot")
    async with nb.client() as client:
        intent = await NautobotIntentSource(f"{NB}/api", "tok", "hall-b", client=client).load()
    queries = {r.url.path: parse_qs(r.url.query.decode()) for r in nb.requests}
    for path, q in queries.items():
        if path.endswith("/locations/"):
            assert q["name"] == ["hall-b"]  # Nautobot 2 locations have no slug
        else:
            assert q["location"] == ["hall-b"]
        assert "site" not in q
    by_path = {r.url.path: parse_qs(r.url.query.decode()) for r in nb.requests}
    assert by_path["/api/dcim/devices/"]["depth"] == ["2"]
    assert by_path["/api/dcim/devices/"]["include"] == ["config_context"]
    assert by_path["/api/dcim/interfaces/"]["depth"] == ["1"]
    assert by_path["/api/dcim/cables/"]["depth"] == ["1"]
    assert intent.source == "nautobot"
    assert {d.name: d.role for d in intent.devices} == {"node7": "gpu-node", "leaf4": "leaf-switch"}
    assert intent.cables[0].label == "CBL-12-07-03"
    assert intent.device("node7").mgmt.bmc_url == "https://10.0.0.7"


async def test_next_link_on_another_host_is_rebased():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        endpoint = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        if endpoint == "devices" and b"offset" not in request.url.query:
            page = load_json("netbox/devices_page1.json")
            page["next"] = "http://internal-netbox:8080/api/dcim/devices/?site=hall-b&limit=1000&offset=3"
            return httpx.Response(200, json=page)
        if endpoint == "devices":
            return httpx.Response(200, json=load_json("netbox/devices_page2.json"))
        if endpoint in ("interfaces", "cables", "racks"):
            return httpx.Response(200, json=load_json(f"netbox/{endpoint}.json"))
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        intent = await NetBoxIntentSource(
            "https://netbox.example.test/netbox", "t", "hall-b", client=client
        ).load()
    assert seen[1].startswith("https://netbox.example.test/netbox/api/dcim/devices/?")
    assert "offset=3" in seen[1]
    assert len(intent.devices) == 5


async def test_empty_site_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 0, "next": None, "previous": None, "results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="no devices"):
            await NetBoxIntentSource(NB, "t", "empty", client=client).load()


async def test_http_error_becomes_intent_error_via_load_intent():
    nb = FakeNetBox(fail=500)
    async with nb.client() as client:
        with pytest.raises(IntentError, match=r"HTTPStatusError|500"):
            await load_intent(f"{NB}/netbox", site="hall-b", client=client, env={"NETBOX_TOKEN": "x"})


async def test_load_intent_url_form_uses_netbox_adapter_and_token_env():
    nb = FakeNetBox()
    async with nb.client() as client:
        intent = await load_intent(f"{NB}/", site="hall-b", client=client, env={"NETBOX_TOKEN": "sim"})
    assert intent.source == "netbox"
    assert nb.requests[0].headers["Authorization"] == "Token sim"


async def test_load_intent_netbox_keyword_reads_env():
    nb = FakeNetBox()
    async with nb.client() as client:
        intent = await load_intent(
            "netbox", site="hall-b", client=client, env={"NETBOX_URL": NB, "NETBOX_TOKEN": "t"}
        )
    assert len(intent.devices) == 5


def test_simulation_markers():
    assert NetBoxIntentSource.is_simulated([{"description": "Simulated by simhall"}], []) is True
    assert NetBoxIntentSource.is_simulated([{"custom_fields": {"turnup_simulated": True}}], []) is True
    assert NetBoxIntentSource.is_simulated([], [{"custom_fields": {"turnup_simulated": True}}]) is True
    assert (
        NetBoxIntentSource.is_simulated([{"description": "Hall B, Reno"}], [{"custom_fields": {}}]) is False
    )
    assert NetBoxIntentSource.is_simulated(None, []) is False


def test_role_normalization_and_helpers():
    assert normalize_role("gpu-node") == "gpu-node"
    assert normalize_role("GPU Node") == "gpu-node"
    assert normalize_role("Leaf Switch") == "leaf-switch"
    assert normalize_role("tor") == "leaf-switch"
    assert normalize_role("spine") == "spine-switch"
    assert normalize_role("firewall") == "other"
    assert normalize_role(None) == "other"
    assert slugify("  GPU  Node ") == "gpu-node"
    assert strip_prefix_length("10.0.0.7/24") == "10.0.0.7"
    assert strip_prefix_length("2001:db8::7/64") == "[2001:db8::7]"
    assert strip_prefix_length(None) is None


def test_build_intent_direct_with_minimal_payloads():
    src = NetBoxIntentSource(NB, None, "s")
    devices = [
        {
            "id": 1,
            "name": "n1",
            "role": {"slug": "gpu-node"},
            "device_type": {"model": "HGX", "manufacturer": {"name": "Dell"}},
        }
    ]
    intent = src.build_intent(devices, [], [])
    assert intent.devices[0].mgmt.bmc_url is None
    assert intent.devices[0].expected_gpus == 8
    assert json.loads(json.dumps(intent.model_dump(by_alias=True)))["schema"] == "turnup.intent/v1"

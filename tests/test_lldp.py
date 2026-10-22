# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""LLDP parsers (Arista eAPI, NVIDIA NVUE) and the HTTP collector paths that feed them."""

from __future__ import annotations

import json

import httpx
import pytest
from helpers import load_json

from turnup_check.collectors import CollectorError, HttpCollector
from turnup_check.collectors.lldp_eapi import eapi_request_body, parse_eapi
from turnup_check.collectors.lldp_nvue import parse_nvue, parse_speed
from turnup_check.models import Creds, Device, Mgmt


def by_port(rows):
    return {r.local_port: r for r in rows}


def test_parse_eapi_rows():
    rows = parse_eapi(load_json("eapi/leaf4.json")["result"], "leaf4")
    ports = by_port(rows)
    assert sorted(ports) == ["Ethernet21", "Ethernet22", "Ethernet23", "Ethernet24", "Ethernet25"]
    e21 = ports["Ethernet21"]
    assert e21.switch == "leaf4" and e21.link_up is True and e21.speed_gbps == 400
    assert (e21.remote_system, e21.remote_port, e21.remote_port_is_mac) == ("node7", "mlx5_3", False)
    assert e21.remote_chassis == "b8:3f:d2:00:07:00"
    assert e21.display == "leaf4/21"
    e22 = ports["Ethernet22"]
    assert e22.link_up is False and e22.speed_gbps is None and e22.has_neighbor is False
    e23 = ports["Ethernet23"]
    assert e23.remote_port_is_mac is True and e23.remote_port == "b8:3f:d2:00:08:01"
    assert e23.remote_system == "node8" and e23.speed_gbps == 200
    assert ports["Ethernet24"].link_up is True and ports["Ethernet24"].has_neighbor is False
    assert ports["Ethernet25"].link_up is False


def test_parse_eapi_accepts_whole_response_and_empty():
    rows = parse_eapi(load_json("eapi/leaf4.json"), "leaf4")
    assert len(rows) == 5
    lldp_doc, status_doc = load_json("eapi/leaf4.json")["result"]
    combined = parse_eapi({**lldp_doc, **status_doc}, "leaf4")  # both command bodies in one object
    assert [r.local_port for r in combined] == [r.local_port for r in rows]
    assert by_port(combined)["Ethernet21"].remote_port == "mlx5_3"
    assert parse_eapi([], "leaf4") == []
    assert parse_eapi([{}, {}], "leaf4") == []


def test_eapi_request_body_is_the_contract_one():
    body = eapi_request_body()
    assert body["method"] == "runCmds" and body["id"] == "turnup"
    assert body["params"]["cmds"] == ["show lldp neighbors detail", "show interfaces status"]
    assert body["params"]["format"] == "json" and body["params"]["version"] == 1


def test_parse_nvue_rows():
    rows = parse_nvue(load_json("nvue/leaf4.json"), "leaf4")
    ports = by_port(rows)
    assert sorted(ports) == ["swp21", "swp22", "swp23", "swp24", "swp25"]  # eth0 and lo skipped
    s21 = ports["swp21"]
    assert s21.link_up is True and s21.speed_gbps == 400
    assert (s21.remote_system, s21.remote_port, s21.remote_port_is_mac) == ("node7", "mlx5_3", False)
    assert s21.remote_chassis == "b8:3f:d2:00:07:00"
    assert ports["swp22"].link_up is False and ports["swp22"].speed_gbps is None
    s23 = ports["swp23"]
    assert s23.remote_port_is_mac is True and s23.remote_port == "b8:3f:d2:00:08:01" and s23.speed_gbps == 200
    assert ports["swp24"].has_neighbor is False and ports["swp24"].link_up is True
    assert ports["swp25"].link_up is False


def test_parse_nvue_down_port_reports_no_speed():
    """``link.speed`` on a down port is the configured speed (NVUE shows ``400G`` on an empty port), not a
    negotiated one; the row carries no speed, like eAPI's ``bandwidth: 0`` for ``notconnect``."""
    doc = {"swp5": {"type": "swp", "link": {"state": {"down": {}}, "speed": "400G"}}}
    row = parse_nvue(doc, "leaf1")[0]
    assert row.link_up is False and row.speed_gbps is None and row.has_neighbor is False


def test_parse_nvue_state_as_string_and_missing_system_name():
    doc = {
        "swp1": {
            "type": "swp",
            "link": {"state": "up", "speed": "100G"},
            "lldp": {"neighbor": {"host9": {"port": {"id": "eth1", "id-type": "if_name"}}}},
        }
    }
    row = parse_nvue(doc, "leaf1")[0]
    assert row.link_up is True and row.speed_gbps == 100
    assert row.remote_system == "host9" and row.remote_port == "eth1"


@pytest.mark.parametrize(
    "value,expected",
    [("400G", 400), ("25G", 25), ("100M", 0), ("auto", None), (None, None), (400000, 400), ("800G", 800)],
)
def test_parse_speed(value, expected):
    assert parse_speed(value) == expected


def _switch(name: str, manufacturer: str, url: str = "http://127.0.0.1:8900/dev/leaf4") -> Device:
    return Device(name=name, role="leaf-switch", manufacturer=manufacturer, mgmt=Mgmt(switch_url=url))


async def test_http_collector_eapi_posts_json_rpc():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == "/dev/leaf4/command-api"
        body = json.loads(request.content)
        assert body["params"]["cmds"][0] == "show lldp neighbors detail"
        return httpx.Response(200, json=load_json("eapi/leaf4.json"))

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
        HttpCollector(client) as c,
    ):
        rows = await c.lldp(_switch("leaf4", "Arista"), Creds(switch_user="admin", switch_pass="x"), 1.0)
    assert len(rows) == 5 and seen[0].headers["Authorization"].startswith("Basic ")


async def test_http_collector_nvue_gets_applied_interfaces():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/dev/leaf4/nvue_v1/interface"
        assert request.url.params["rev"] == "applied"
        return httpx.Response(200, json=load_json("nvue/leaf4.json"))

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
        HttpCollector(client) as c,
    ):
        rows = await c.lldp(_switch("leaf4", "NVIDIA"), Creds(), 1.0)
    assert by_port(rows)["swp21"].remote_port == "mlx5_3"


async def test_http_collector_lldp_errors_become_collector_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        if "fail500" in request.url.path:
            return httpx.Response(500, text="boom")
        if "eapierr" in request.url.path:
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": "turnup", "error": {"code": 1002, "message": "bad auth"}}
            )
        raise httpx.ConnectError("refused", request=request)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
        HttpCollector(client) as c,
    ):
        with pytest.raises(CollectorError, match="HTTP 500"):
            await c.lldp(_switch("l", "NVIDIA", "http://x/fail500"), Creds(), 1.0)
        with pytest.raises(CollectorError, match="bad auth"):
            await c.lldp(_switch("l", "Arista", "http://x/eapierr"), Creds(), 1.0)
        with pytest.raises(CollectorError, match="ConnectError"):
            await c.lldp(_switch("l", "Arista", "http://x/refused"), Creds(), 1.0)
        with pytest.raises(CollectorError, match="no switch_url"):
            await c.lldp(Device(name="l", role="leaf-switch"), Creds(), 1.0)


async def test_http_collector_lldp_timeout_message():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
        HttpCollector(client) as c,
    ):
        with pytest.raises(CollectorError, match="timeout after 1 s"):
            await c.lldp(_switch("l", "NVIDIA"), Creds(), 1.0)

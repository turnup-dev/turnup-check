# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""Views: list the turnup.report/v1 files in ``report_dir`` and show one report."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404
from django.shortcuts import render
from django.views import View

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+\.json$")


def plugin_settings() -> dict[str, Any]:
    return settings.PLUGINS_CONFIG.get("netbox_turnup_check", {})


def report_dir() -> Path:
    return Path(plugin_settings().get("report_dir", "/var/lib/turnup/reports"))


def load_report(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("schema") != "turnup.report/v1":
        return None
    return doc


def report_rows(limit: int) -> list[dict[str, Any]]:
    """Newest reports first, with the summary fields the list shows."""
    directory = report_dir()
    if not directory.is_dir():
        return []
    files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    rows: list[dict[str, Any]] = []
    for path in files:
        doc = load_report(path)
        if doc is None:
            continue
        summary = doc.get("summary", {})
        rows.append(
            {
                "name": path.name,
                "site": doc.get("site"),
                "generated_at": doc.get("generated_at"),
                "simulated": doc.get("simulated", False),
                "links_ok": summary.get("links_ok"),
                "links_total": summary.get("links_total"),
                "nodes_reachable": summary.get("nodes_reachable"),
                "nodes_total": summary.get("nodes_total"),
                "firmware_ok": summary.get("firmware_ok"),
                "findings": summary.get("findings", len(doc.get("findings", []))),
                "elapsed_s": doc.get("elapsed_s"),
            }
        )
    return rows


class ReportListView(LoginRequiredMixin, View):
    def get(self, request):
        rows = report_rows(int(plugin_settings().get("max_reports", 200)))
        return render(
            request,
            "netbox_turnup_check/report_list.html",
            {"reports": rows, "report_dir": str(report_dir())},
        )


class ReportDetailView(LoginRequiredMixin, View):
    def get(self, request, name: str):
        if not _SAFE_NAME.match(name):
            raise Http404
        path = report_dir() / name
        doc = load_report(path) if path.is_file() else None
        if doc is None:
            raise Http404
        return render(
            request,
            "netbox_turnup_check/report_detail.html",
            {
                "name": name,
                "report": doc,
                "findings": doc.get("findings", []),
                "raw": json.dumps(doc, indent=1),
            },
        )

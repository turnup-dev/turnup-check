# Copyright 2026 Turnup contributors
# SPDX-License-Identifier: Apache-2.0
"""URL routes: /plugins/turnup-check/ (list) and /plugins/turnup-check/<name>/ (one report)."""

from __future__ import annotations

from django.urls import path

from . import views

urlpatterns = [
    path("", views.ReportListView.as_view(), name="report_list"),
    path("<str:name>/", views.ReportDetailView.as_view(), name="report_detail"),
]

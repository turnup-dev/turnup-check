# netbox-turnup-check

A minimal NetBox plugin that adds a **Turnup check** item to the Plugins menu and a page listing the
`turnup.report/v1` JSON files that [`turnup-check`](https://github.com/turnup-dev/turnup-check) writes.
Click a report to see its findings (class, cable, device, expected, observed, fix) and the raw JSON.

Status: skeleton. It has not yet been started inside a NetBox instance (none is available in this
repository); the code follows the NetBox plugin development docs for NetBox 3.5+ / 4.x
(`netbox.plugins.PluginConfig` and `PluginMenuItem` on NetBox 3.7+, with the `extras.plugins` fallback for
3.5 and 3.6; `urls.py`; templates extending `base/layout.html`).
First run target: netbox-docker seeded by `simhall seed-netbox` (see "Seeding a real NetBox" in the
[simhall README](https://github.com/turnup-dev/simhall#seeding-a-real-netbox)).

## Install

```bash
# in the NetBox virtualenv
pip install netbox-turnup-check            # or: pip install -e /path/to/turnup-check/netbox_plugin
```

`configuration.py`:

```python
PLUGINS = ["netbox_turnup_check"]
PLUGINS_CONFIG = {
    "netbox_turnup_check": {
        "report_dir": "/var/lib/turnup/reports",  # where turnup-check --out writes
        "max_reports": 200,
    },
}
```

Then `python manage.py collectstatic --no-input` (no static files today, harmless) and restart NetBox.

## Feeding it

Run the check on a schedule (cron, systemd timer, a NetBox custom script host) and write into `report_dir`:

```bash
NETBOX_URL=https://netbox.example NETBOX_TOKEN=... \
turnup-check --site hall-b --intent netbox --out /var/lib/turnup/reports/hall-b-$(date -u +%F).json
```

The plugin only reads files; it never talks to devices and needs no credentials.

## Permissions

The menu item and views require `dcim.view_device` plus login. File names are restricted to
`[A-Za-z0-9._-]+.json` and resolved inside `report_dir` only.

## Nautobot

See `NAUTOBOT.md` for the equivalent Nautobot app note.

## License

Apache-2.0 (see the repository `LICENSE`).

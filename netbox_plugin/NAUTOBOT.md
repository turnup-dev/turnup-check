# Nautobot app note

The same page ships for Nautobot as a Nautobot App with one paragraph of differences: the config class
derives from `nautobot.apps.NautobotAppConfig` (fields `name = "nautobot_turnup_check"`, `verbose_name`,
`base_url = "turnup-check"`, `default_settings = {"report_dir": ..., "max_reports": 200}`), the menu is
declared in `navigation.py` with `nautobot.apps.ui.NavMenuTab / NavMenuGroup / NavMenuItem` under the
"Plugins" tab pointing at `plugins:nautobot_turnup_check:report_list`, the views use
`nautobot.apps.views.GenericView` (or Django `View` with `LoginRequiredMixin`, as here) and templates extend
`base.html` instead of `base/layout.html`. The intent for Nautobot is read by `turnup-check --intent nautobot`
(`NAUTOBOT_URL`, `NAUTOBOT_TOKEN`; `location=<slug>`, roles from `role.name`,
`?depth=2&include=config_context` for devices, `?depth=1` for interfaces and cables). The report directory
contract is identical, so one cron line feeds both plugins. Listings on the Nautobot App ecosystem page and
in the NetBox plugin catalog are planned; neither has been submitted yet.

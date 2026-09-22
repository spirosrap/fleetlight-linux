"""GTK4/libadwaita desktop shell. Worker threads never touch GTK widgets."""
import json
from pathlib import Path
import threading
import time
import uuid
from urllib.parse import quote

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import __version__
from . import actions, config
from . import agents as agent_quota
from .monitor import History, issues, linux_update_issues, refresh
from .probe import collect_metrics
from . import sites
from . import updates
from .update_job import installation_changes, history_report


ACTION_NAMES = {"cli": "Codex CLI", "desktop": "ChatGPT", "system": "Linux packages", "restart": "required restarts"}

CSS = b"""
window { background: #10151d; color: #e6edf5; }
headerbar { background: #141b25; border-bottom: 1px solid #293342; }
.sidebar { background: #141b25; border-right: 1px solid #293342; }
.sidebar list { background: transparent; }
.sidebar row { border-radius: 10px; margin: 3px 10px; padding: 7px; }
.sidebar row:selected { background: #26384a; }
.card { background: #1b2430; border-radius: 16px; padding: 20px; border: 1px solid #303c4d; }
.metric { font-size: 32px; font-weight: 700; }
.hero { font-size: 30px; font-weight: 700; }
.eyebrow { font-size: 11px; font-weight: 700; letter-spacing: 1.4px; color: #94a9bf; }
.muted { color: #93a6bb; }
.good { color: #6cd5b0; }
.warning { color: #f6c76e; }
.bad { color: #f1949c; }
.pill { padding: 5px 12px; border-radius: 20px; background: #233b36; font-weight: 600; }
.section-title { font-size: 16px; font-weight: 700; }
progressbar trough { min-height: 5px; background: #303d4d; }
progressbar progress { background: #6cd5b0; }
.warning progress { background: #f6c76e; }
.suggested-action { background: #387c70; color: white; }
button { border-radius: 9px; }
"""


def label(text, css=None, xalign=0):
    widget = Gtk.Label(label=str(text), xalign=xalign)
    if css:
        widget.add_css_class(css)
    return widget


def box(vertical=True, spacing=10):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL, spacing=spacing)


def margins(widget, size):
    for edge in ("top", "bottom", "start", "end"):
        getattr(widget, "set_margin_" + edge)(size)
    return widget


def clear(widget):
    while widget.get_first_child():
        widget.remove(widget.get_first_child())


def age(timestamp):
    if not timestamp:
        return "Not checked yet"
    elapsed = max(0, int(time.time() - timestamp))
    return "Just checked" if elapsed < 10 else (f"Checked {elapsed}s ago" if elapsed < 60 else f"Checked {elapsed // 60}m ago")


def uptime(seconds):
    if seconds is None:
        return "Unavailable"
    hours = seconds // 3600
    return f"{hours // 24}d {hours % 24}h" if hours >= 24 else f"{hours}h {(seconds % 3600) // 60}m"


class Fleetlight(Adw.Application):
    def __init__(self, configuration=None, demo=False):
        super().__init__(application_id="io.github.fleetlight.Linux.Demo" if demo else "io.github.fleetlight.Linux", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.config_file = configuration
        self.demo = demo
        self.window = None
        self.snapshots = {}
        self.busy = False
        self.selected = None
        self.timer = None
        self.local_metrics_busy = False
        self.app_updates = {}
        self.update_checks_running = False
        self.last_update_check = 0
        self.active_job = None
        self.last_jobs = {}
        self.update_history_expanded = {}
        self.batch = None
        self.pending_restarts = {}
        self.agent_usage = {}
        self.agent_checks_running = False
        self.site_status = {}
        self.install_history = {}
        self.keep_detail = False
        self.rebuilding_detail = False
        self.metric_widgets = None
        self.auto_attempted = set()
        self.auto_holdoff_until = 0
        self.journal_path = config.state_path().with_name("update-controller.json")
        if not demo:
            try:
                journal = json.loads(self.journal_path.read_text())
                self.active_job = journal.get("active_job")
                self.batch = journal.get("batch")
                self.pending_restarts = journal.get("pending_restarts", {})
                if self.batch:
                    if self.batch["kind"] not in ("cli", "desktop", "system", "restart") or not isinstance(self.batch["pending"], list) or len(self.batch["pending"]) > 32:
                        raise ValueError("Invalid saved batch")
                    for item in self.batch["pending"]:
                        config.validate({"version": 1, "hosts": [item["host"]]})
                        if not updates.version(item["checked"].get("latest")):
                            raise ValueError("Invalid saved release")
                self.last_jobs = journal.get("last_jobs", {})
                if not isinstance(self.last_jobs, dict):
                    self.last_jobs = {}
                if self.active_job:
                    config.validate({"version": 1, "hosts": [self.active_job["host"]]})
                    if not isinstance(self.active_job["id"], str) or len(self.active_job["id"]) != 32:
                        raise ValueError("Invalid saved job")
            except (ValueError, OSError, TypeError, KeyError, AttributeError):
                self.active_job = None
                self.batch = None
                self.last_jobs = {}
            try:
                saved = json.loads(config.state_path().with_name("application-checks.json").read_text())
                if isinstance(saved, dict):
                    self.app_updates = saved
                    if self.dismiss_resolved_jobs(saved):
                        try:
                            self.persist_jobs()
                        except OSError:
                            pass
            except (ValueError, OSError, TypeError, KeyError):
                pass
        self.history = History() if not demo else History(Path("/nonexistent/fleetlight-demo"))
        if demo:
            self.journal_path = Path("/nonexistent/fleetlight-demo/update-controller.json")
        self.connect("activate", self.activate_window)

    def activate_window(self, *_):
        if self.window:
            self.window.present()
            return
        load_error = None
        try:
            self.configuration = config.default_config() if self.demo else config.load(self.config_file)
        except (ValueError, OSError) as error:
            self.configuration = config.default_config()
            load_error = str(error)
        if self.demo:
            from .demo import demo_data
            self.configuration, self.snapshots = demo_data()
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.window = Adw.ApplicationWindow(application=self, title="Fleetlight", default_width=1100, default_height=780)
        self.window.set_icon_name("io.github.fleetlight.Linux")
        self.window.connect("close-request", self.close_requested)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title="Fleetlight", subtitle=f"Linux · {__version__}")
        header.set_title_widget(self.window_title)
        settings = Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text="Settings and configuration")
        settings.connect("clicked", self.settings)
        header.pack_start(settings)
        self.spinner = Gtk.Spinner()
        header.pack_end(self.spinner)
        self.refresh_button = Gtk.Button(label="Check now", icon_name="view-refresh-symbolic")
        self.refresh_button.set_label("Check now")
        self.refresh_button.add_css_class("suggested-action")
        self.refresh_button.connect("clicked", lambda *_: self.check())
        header.pack_end(self.refresh_button)
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add computer")
        add.set_sensitive(not self.demo)
        add.connect("clicked", self.add_computer)
        header.pack_end(add)
        toolbar.add_top_bar(header)
        fleet_bar = margins(box(True, 6), 10)
        buttons = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False,
                              min_children_per_line=1, max_children_per_line=3,
                              column_spacing=8, row_spacing=6)
        self.batch_buttons = {}
        for kind, title in (("cli", "Update all Codex CLI"), ("desktop", "Update all ChatGPT"), ("system", "Update all Linux packages"), ("restart", "Restart required computers")):
            button = Gtk.Button(label=title)
            button.connect("clicked", lambda _, selected=kind: self.request_batch(selected))
            buttons.insert(button, -1)
            self.batch_buttons[kind] = button
        self.stop_batch = Gtk.Button(label="Stop after current update")
        self.stop_batch.connect("clicked", self.cancel_batch)
        buttons.insert(self.stop_batch, -1)
        fleet_bar.append(buttons)
        self.batch_label = label("", "muted")
        self.batch_label.set_wrap(True)
        fleet_bar.append(self.batch_label)
        self.agent_box = box(True, 8)
        fleet_bar.append(self.agent_box)
        toolbar.add_top_bar(fleet_bar)
        self.toasts = Adw.ToastOverlay()
        layout = Adw.OverlaySplitView(min_sidebar_width=220, max_sidebar_width=260)
        sidebar = box(True, 12)
        sidebar.set_size_request(240, -1)
        sidebar.add_css_class("sidebar")
        title = margins(label("YOUR FLEET", "eyebrow"), 18)
        title.set_margin_bottom(0)
        sidebar.append(title)
        self.summary = margins(label("Checking computers…", "muted"), 18)
        self.summary.set_margin_top(0)
        self.summary.set_margin_bottom(0)
        sidebar.append(self.summary)
        self.search = Gtk.SearchEntry(placeholder_text="Find a computer or site")
        margins(self.search, 12)
        self.search.set_margin_top(0)
        self.search.set_margin_bottom(0)
        self.search.connect("search-changed", lambda *_: self.populate_hosts())
        sidebar.append(self.search)
        self.attention = Gtk.CheckButton(label="Needs attention only")
        margins(self.attention, 14)
        self.attention.set_margin_top(0)
        self.attention.set_margin_bottom(0)
        self.attention.connect("toggled", lambda *_: self.populate_hosts())
        sidebar.append(self.attention)
        self.host_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.host_list.connect("row-selected", self.select_host)
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(self.host_list)
        sidebar.append(scroller)
        footer = margins(label("Direct SSH · Local history", "muted"), 18)
        sidebar.append(footer)
        layout.set_sidebar(sidebar)
        scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.content = margins(box(True, 18), 28)
        scroll.set_child(self.content)
        layout.set_content(scroll)
        toggle = Gtk.Button(icon_name="sidebar-show-symbolic", tooltip_text="Show or hide computers")
        toggle.connect("clicked", lambda *_: layout.set_show_sidebar(not layout.get_show_sidebar()))
        header.pack_start(toggle)
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 900px"))
        breakpoint.add_setter(layout, "collapsed", True)
        self.window.add_breakpoint(breakpoint)
        self.split_view = layout
        self.toasts.set_child(layout)
        toolbar.set_content(self.toasts)
        self.window.set_content(toolbar)
        self.populate_hosts()
        self._fleet_started = False
        self.window.connect("map", self.reveal_fleet)
        self.window.present()
        GLib.idle_add(self.reveal_fleet)
        self.timer = GLib.timeout_add_seconds(self.configuration.get("refresh_seconds", 60), self.auto_check)
        if not self.demo:
            GLib.timeout_add_seconds(2, self.check_local_metrics)
        if self.demo:
            self.refresh_button.set_sensitive(False)
            self.app_updates = {h["id"]: {kind: updates.plan("1.0.0", "1.1.0" if kind == "cli" else "1.0.0", "standalone" if kind == "cli" else "macos-appcast") for kind in ("cli", "desktop")} for h in self.configuration["hosts"]}
            self.agent_usage = agent_quota.demo_usage()
            self.render_agents()
            self.render_detail()
        if load_error:
            self.toast("Configuration was not loaded: " + load_error)

    def reveal_fleet(self, *_):
        if self.window is None:
            return GLib.SOURCE_REMOVE
        self.split_view.set_show_sidebar(True)
        self.populate_hosts()
        if self.demo or self._fleet_started:
            return GLib.SOURCE_REMOVE
        self._fleet_started = True
        if self.active_job:
            self.watch_job()
        elif self.batch and self.batch.get("running") and self.batch.get("pending"):
            self.advance_batch()
        else:
            self.check()
        return GLib.SOURCE_REMOVE

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=6))

    def auto_check(self):
        if not self.demo:
            self.check(force_updates=False)
        return GLib.SOURCE_CONTINUE

    def check(self, force_updates=True):
        if self.busy or self.demo or self.update_checks_running or self.active_job:
            return
        self.force_update_check = force_updates
        self.busy = True
        self.refresh_button.set_sensitive(False)
        self.spinner.start()
        previous = dict(self.snapshots)
        hosts = list(self.configuration["hosts"])
        wanted = [name for name, on in config.enabled_agents(self.configuration).items() if on]
        watched = list(sites.configured(self.configuration))
        def work():
            results = {}
            usage = {}
            site_results = {}
            histories = {}
            def received(snapshot):
                results[snapshot["id"]] = snapshot
                GLib.idle_add(self.receive, snapshot)
            if wanted:
                quota = threading.Thread(target=lambda: usage.update(agent_quota.collect(wanted)), daemon=True)
                quota.start()
            else:
                quota = None
            site_check = threading.Thread(target=lambda: site_results.update(sites.check_all(watched)),
                                          daemon=True) if watched else None
            if site_check:
                site_check.start()
            history_check = threading.Thread(target=lambda: histories.update(updates.collect_install_histories(hosts)), daemon=True)
            history_check.start()
            refresh(hosts, received)
            if quota:
                quota.join()
            if site_check:
                site_check.join()
            history_check.join()
            warning = None
            try:
                self.history.record(results, previous)
            except OSError:
                warning = "History could not be saved; live checks are still available"
            GLib.idle_add(self.receive_agents, usage)
            GLib.idle_add(self.receive_sites, site_results)
            GLib.idle_add(self.receive_install_histories, histories)
            GLib.idle_add(self.finished, warning)
        threading.Thread(target=work, daemon=True).start()

    def check_local_metrics(self):
        hosts = [h for h in self.configuration["hosts"] if h.get("local")]
        if self.demo or self.local_metrics_busy or not hosts:
            return GLib.SOURCE_CONTINUE
        self.local_metrics_busy = True
        def work():
            try:
                metrics = collect_metrics()
            except (OSError, ValueError):
                metrics = None
            GLib.idle_add(self.receive_local_metrics, hosts, metrics)
        threading.Thread(target=work, daemon=True).start()
        return GLib.SOURCE_CONTINUE

    def receive_local_metrics(self, hosts, metrics):
        self.local_metrics_busy = False
        if metrics is None:
            return GLib.SOURCE_REMOVE
        changed = False
        for host in hosts:
            if host not in self.configuration["hosts"]:
                continue
            snapshot = self.snapshots.get(host["id"], {})
            if snapshot.get("status") == "online" and metrics["metrics_checked_at"] > snapshot.get("metrics_checked_at", 0):
                self.snapshots[host["id"]] = {**snapshot, **metrics}
                changed = True
        if changed:
            self.keep_detail = True
            try:
                self.populate_hosts()
            finally:
                self.keep_detail = False
            self.update_open_metrics()
        return GLib.SOURCE_REMOVE

    def receive(self, snapshot):
        current = self.snapshots.get(snapshot["id"], {})
        if snapshot.get("status") == "online" and current.get("metrics_checked_at", 0) > snapshot.get("metrics_checked_at", 0):
            snapshot = {**snapshot, **{key: current[key] for key in (
                "metrics_checked_at", "uptime", "disk_percent", "disk_free", "memory_percent", "load", "cpu_temperature") if key in current}}
        pending = self.pending_restarts.get(snapshot["id"])
        if pending and pending.get("boot_id") and snapshot.get("status") == "online" and snapshot.get("boot_id") and snapshot["boot_id"] != pending.get("boot_id"):
            self.pending_restarts.pop(snapshot["id"], None)
            self.app_updates.setdefault(snapshot["id"], {})["restart"] = {"state": "current", "checked_at": time.time(), "detail": "Restart verified; computer is online"}
            self.toast(pending["name"] + " restarted and is back online")
            try:
                self.persist_jobs()
            except OSError:
                self.toast("Could not save restart verification")
        self.snapshots[snapshot["id"]] = snapshot
        self.populate_hosts()
        return GLib.SOURCE_REMOVE

    def finished(self, warning):
        self.busy = False
        self.refresh_button.set_sensitive(True)
        self.spinner.stop()
        self.populate_hosts()
        if warning:
            self.toast(warning)
        if self.force_update_check or time.time() - self.last_update_check > 900:
            self.check_application_updates()
        return GLib.SOURCE_REMOVE

    def receive_agents(self, usage):
        self.agent_usage = usage if isinstance(usage, dict) else {}
        self.render_agents()
        return GLib.SOURCE_REMOVE

    def receive_install_histories(self, histories):
        if isinstance(histories, dict):
            for ident, records in histories.items():
                if isinstance(records, list):
                    self.install_history[ident] = records
        if self.selected in self.install_history:
            self.render_detail()
        return GLib.SOURCE_REMOVE

    def receive_install_history(self, ident, records):
        if isinstance(records, list):
            self.install_history[ident] = records
            if self.selected == ident:
                self.render_detail()
        return GLib.SOURCE_REMOVE

    def refresh_install_history(self, host):
        def work():
            try:
                records = updates.read_install_history(host)
            except Exception:
                return
            if isinstance(records, list):
                GLib.idle_add(self.receive_install_history, host["id"], records)
        threading.Thread(target=work, daemon=True).start()

    def receive_sites(self, status):
        self.site_status = status if isinstance(status, dict) else {}
        self.populate_hosts()
        return GLib.SOURCE_REMOVE

    def render_agents(self):
        if not hasattr(self, "agent_box"):
            return
        clear(self.agent_box)
        enabled = config.enabled_agents(self.configuration)
        visible = [name for name in agent_quota.NAMES if enabled.get(name)]
        if not visible:
            return
        heading = label("AGENT QUOTA", "eyebrow")
        self.agent_box.append(heading)
        row = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                          min_children_per_line=1, max_children_per_line=2,
                          column_spacing=8, row_spacing=8)
        for name in visible:
            data = self.agent_usage.get(name) or {"name": name.title(), "state": "checking",
                                                 "detail": "Checking remaining quota…", "remaining_percent": None}
            card = box(True, 6)
            card.add_css_class("card")
            title = data.get("name") or name.title()
            plan = data.get("plan")
            card.append(label(title + ((" · " + plan) if plan else ""), "eyebrow"))
            remaining = data.get("remaining_percent")
            card.append(label(f"{remaining}% left" if remaining is not None else "—", "metric"))
            bar = Gtk.ProgressBar(fraction=max(0, min(1, (remaining or 0) / 100)))
            if remaining is not None and remaining <= 20:
                bar.add_css_class("warning")
            card.append(bar)
            note = label(data.get("detail") or ("Checking remaining quota…" if data.get("state") == "checking" else "Unavailable"),
                         "warning" if (remaining is not None and remaining <= 20) or data.get("state") == "unavailable" else "muted")
            note.set_wrap(True)
            card.append(note)
            row.append(card)
        self.agent_box.append(row)

    def populate_hosts(self):
        hosts = self.configuration["hosts"]
        watched = sites.configured(self.configuration)
        online = sum(self.snapshots.get(h["id"], {}).get("status") == "online" for h in hosts)
        site_trouble = sum(1 for site in watched if sites.issues(self.site_status.get(site["id"])))
        summary = f"{online} of {len(hosts)} online"
        if watched:
            if site_trouble:
                summary += f" · {site_trouble} site" + ("s need" if site_trouble != 1 else " needs") + " attention"
            else:
                summary += f" · {len(watched)} site" + ("s" if len(watched) != 1 else "") + " checked"
        self.summary.set_text(summary)
        subtitle = f"Linux {__version__} · {online}/{len(hosts)} online"
        if site_trouble:
            subtitle += f" · {site_trouble} site alert"
        self.window_title.set_subtitle(subtitle)
        self.render_agents()
        selected_id = self.selected
        self.host_list.unselect_all()
        clear(self.host_list)
        selected_row = None
        query = self.search.get_text().casefold()
        for host in sorted(hosts, key=lambda host: not host.get("local", False)):
            if query and query not in host["name"].casefold():
                continue
            snapshot = self.snapshots.get(host["id"], {})
            trouble = issues(snapshot) + linux_update_issues(self.app_updates.get(host["id"]))
            if self.attention.get_active() and not trouble:
                continue
            row = Gtk.ListBoxRow()
            row.host_id = host["id"]
            body = box(False, 12)
            icon = Gtk.Image.new_from_icon_name("computer-symbolic" if host.get("local") else "network-server-symbolic")
            icon.set_pixel_size(24)
            online_host = snapshot.get("status") == "online"
            icon.add_css_class("good" if online_host and not trouble else "warning" if online_host else "muted")
            body.append(icon)
            names = box(True, 3)
            name = label(host["name"])
            name.set_ellipsize(3)
            names.append(name)
            state = "Local computer" if host.get("local") else snapshot.get("os", "SSH connection")
            names.append(label(state, "muted"))
            body.append(names)
            row.set_child(body)
            self.host_list.append(row)
            if host["id"] == selected_id:
                selected_row = row
        for site in watched:
            if query and query not in site["name"].casefold():
                continue
            status = self.site_status.get(site["id"], {})
            trouble = sites.issues(status)
            if self.attention.get_active() and not trouble:
                continue
            row = Gtk.ListBoxRow()
            row.host_id = site["id"]
            body = box(False, 12)
            icon = Gtk.Image.new_from_icon_name("web-browser-symbolic")
            icon.set_pixel_size(24)
            state = status.get("state")
            icon.add_css_class("good" if state == "ok" else "warning" if state else "muted")
            body.append(icon)
            names = box(True, 3)
            name = label(site["name"])
            name.set_ellipsize(3)
            names.append(name)
            names.append(label(status.get("detail") or "Website catalogue", "muted"))
            body.append(names)
            row.set_child(body)
            self.host_list.append(row)
            if site["id"] == selected_id:
                selected_row = row
        row = selected_row or self.host_list.get_row_at_index(0)
        if row:
            self.host_list.select_row(row)
        else:
            clear(self.content)
            self.content.append(Adw.StatusPage(title="No computers match", description="Change the search or attention filter.", icon_name="system-search-symbolic"))

    def select_host(self, _, row):
        if row:
            same = row.host_id == self.selected and self.content.get_first_child() is not None
            self.selected = row.host_id
            if same and self.keep_detail:
                return
            self.render_detail()

    def render_site(self, site):
        self.metric_widgets = None
        status = self.site_status.get(site["id"], {})
        trouble = sites.issues(status)
        clear(self.content)
        hero = box(False, 12)
        headings = box(True, 6)
        headings.set_hexpand(True)
        headings.append(label("WEBSITE", "eyebrow"))
        title = label(site["name"], "hero")
        title.set_wrap(True)
        headings.append(title)
        headings.append(label(site.get("url") or "", "muted"))
        hero.append(headings)
        state = status.get("state")
        badge_text = "Current" if state == "ok" else "Needs attention" if trouble else "Checking" if self.busy else "Not checked yet"
        badge = label(badge_text, "pill")
        badge.add_css_class("good" if state == "ok" else "warning")
        badge.set_valign(Gtk.Align.CENTER)
        hero.append(badge)
        self.content.append(hero)
        if trouble:
            alert = box(True, 5)
            alert.add_css_class("card")
            alert.append(label("Needs attention", "warning"))
            for message in trouble[:8]:
                line = label(message, "muted")
                line.set_wrap(True)
                alert.append(line)
            self.content.append(alert)
        card = self.section("Catalogue freshness", "Checked from this computer over HTTPS")
        generated = status.get("generated_at")
        updated = time.strftime("%a %d %b %H:%M", time.localtime(generated)) if generated else "Unknown"
        self.detail_row(card, "Last catalogue update", updated, "view-refresh-symbolic",
                        "warning" if trouble else "good" if state == "ok" else "muted")
        self.detail_row(card, "Age limit", str(site.get("max_age_hours", 4)) + " hours", "alarm-symbolic")
        if status.get("product_count") is not None:
            self.detail_row(card, "Products", str(status["product_count"]), "view-list-symbolic")
        if status.get("status"):
            self.detail_row(card, "Reported status", status["status"], "dialog-information-symbolic",
                            "warning" if trouble else "muted")
        note = label(status.get("detail") or "Press Check now to check this website.", "muted")
        note.set_wrap(True)
        card.append(note)
        footer = label("Website checks run with computer checks from this Linux app. "
                       "They do not use SSH.", "muted")
        footer.set_wrap(True)
        self.content.append(footer)

    def render_detail(self):
        self.rebuilding_detail = True
        try:
            self._render_detail_now()
        finally:
            self.rebuilding_detail = False

    def update_open_metrics(self):
        widgets = self.metric_widgets or {}
        if widgets.get("host") != self.selected:
            return
        data = self.snapshots.get(self.selected, {})
        disk = data.get("disk_percent")
        memory = data.get("memory_percent")
        widgets["disk"].set_text(f"{disk}%" if disk is not None else "—")
        widgets["disk_hint"].set_text(f"{data.get('disk_free', 0) / 1024**3:.1f} GiB free")
        widgets["disk_bar"].set_fraction(max(0, min(1, (disk or 0) / 100)))
        widgets["memory"].set_text(f"{memory}%" if memory is not None else "—")
        widgets["memory_bar"].set_fraction(max(0, min(1, (memory or 0) / 100)))
        widgets["uptime"].set_text(uptime(data.get("uptime")))
        widgets["load"].set_text(f"Load {data.get('load', '—')} · {data.get('cpus', '—')} CPUs")
        temperature = data.get("cpu_temperature")
        widgets["temperature"].set_text(f"{temperature:.1f} °C" if temperature is not None else "—")

    def _render_detail_now(self):
        self.render_batch()
        site = next((item for item in sites.configured(self.configuration) if item["id"] == self.selected), None)
        if site is not None:
            self.render_site(site)
            return
        host = next((h for h in self.configuration["hosts"] if h["id"] == self.selected), None)
        if host is None:
            return
        data = self.snapshots.get(host["id"], {})
        clear(self.content)
        hero = box(False, 12)
        headings = box(True, 6)
        headings.set_hexpand(True)
        headings.append(label("COMPUTER OVERVIEW", "eyebrow"))
        title = label(host["name"], "hero")
        title.set_wrap(True)
        headings.append(title)
        subtitle = data.get("distribution") or data.get("os") or ("This computer" if host.get("local") else "Secure Shell")
        subtitle_label = label(subtitle + "  ·  " + age(data.get("checked_at")), "muted")
        subtitle_label.set_wrap(True)
        headings.append(subtitle_label)
        hero.append(headings)
        online = data.get("status") == "online"
        status = "Online" if online else "Checking" if self.busy else "Unavailable"
        badge = label(status, "pill")
        badge.add_css_class("good" if online else "warning")
        badge.set_valign(Gtk.Align.CENTER)
        hero.append(badge)
        self.content.append(hero)
        trouble = (issues(data) if data else []) + linux_update_issues(self.app_updates.get(host["id"]))
        if trouble:
            alert = box(True, 5)
            alert.add_css_class("card")
            alert.append(label("Needs attention", "warning"))
            for message in trouble[:8]:
                line = label(message, "muted")
                line.set_wrap(True)
                alert.append(line)
            self.content.append(alert)
        metrics = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                              min_children_per_line=1, max_children_per_line=3,
                              row_spacing=12, column_spacing=12)
        metric_labels = {}
        for name, value, hint in (
            ("ROOT DISK", data.get("disk_percent"), f"{data.get('disk_free', 0) / 1024**3:.1f} GiB free" if online else "Waiting for a check"),
            ("MEMORY", data.get("memory_percent"), "Physical memory in use"),
        ):
            card = box(True, 10)
            card.add_css_class("card")
            card.set_size_request(150, -1)
            card.set_hexpand(True)
            card.append(label(name, "eyebrow"))
            value_label = label(f"{value}%" if value is not None else "—", "metric")
            card.append(value_label)
            bar = Gtk.ProgressBar(fraction=max(0, min(1, (value or 0) / 100)))
            if (value or 0) >= 90:
                bar.add_css_class("warning")
            card.append(bar)
            hint_label = label(hint, "muted")
            card.append(hint_label)
            metrics.append(card)
            key = "disk" if name == "ROOT DISK" else "memory"
            metric_labels[key] = value_label
            metric_labels[key + "_bar"] = bar
            metric_labels[key + "_hint"] = hint_label
        card = box(True, 10)
        card.add_css_class("card")
        card.set_size_request(150, -1)
        card.set_hexpand(True)
        card.append(label("UPTIME", "eyebrow"))
        uptime_label = label(uptime(data.get("uptime")) if online else "—", "metric")
        card.append(uptime_label)
        load_label = label(f"Load {data.get('load', '—')} · {data.get('cpus', '—')} CPUs", "muted")
        card.append(load_label)
        metrics.append(card)
        self.content.append(metrics)
        temperature = data.get("cpu_temperature") if online else None
        card = box(True, 10)
        card.add_css_class("card")
        card.append(label("CPU TEMPERATURE", "eyebrow"))
        temperature_label = label(f"{temperature:.1f} °C" if temperature is not None else "—", "metric")
        card.append(temperature_label)
        self.metric_widgets = {"host": host["id"], "uptime": uptime_label, "load": load_label,
                               "temperature": temperature_label, **metric_labels}
        card.append(label("Hottest CPU sensor" if temperature is not None else
                          ("Waiting for a check" if not online else "CPU sensor unavailable"), "muted"))
        metrics.append(card)
        apps = self.section("Applications", "Installed and available versions")
        self.update_row(apps, host, "cli", "Codex CLI", data.get("codex"), "utilities-terminal-symbolic")
        desktop = data.get("chatgpt", {})
        self.update_row(apps, host, "desktop", "ChatGPT", desktop.get("version"), "applications-internet-symbolic")
        if self.active_job and self.active_job["host"]["id"] == host["id"]:
            progress = Gtk.ProgressBar()
            progress.pulse()
            apps.append(progress)
            phase = label(self.active_job.get("phase", "Preparing update"), "good")
            phase.set_wrap(True)
            apps.append(phase)
        self.update_history(apps, host)
        if data.get("os") == "Linux":
            system_card = self.section("Linux updates", "Distribution packages and restart status")
            checked = self.app_updates.get(host["id"], {})
            for kind, title in (("system", "System packages"), ("restart", "Restart")):
                status = checked.get(kind, {})
                self.detail_row(system_card, title, status.get("detail", "Checking…"), "system-software-update-symbolic",
                                "warning" if status.get("state") in ("available", "protected", "unknown") else "muted")
        services = self.section("Services", "Configured system services")
        states = data.get("services", {})
        for name in host.get("services", []):
            state = states.get(name, "not checked")
            optional = name in host.get("optional_services", [])
            neutral = state == "unsupported" or (optional and state in ("inactive", "not installed"))
            self.detail_row(services, name + (" · optional" if optional else ""), state, "emblem-system-symbolic", "good" if state == "active" else "muted" if neutral else "warning")
            required = Gtk.CheckButton(label="Warn when stopped")
            required.set_active(not optional)
            required.set_sensitive(not self.demo and not self.busy and not self.update_checks_running and not self.active_job)
            required.connect("toggled", lambda button, service=name: self.service_preference(host, service, button.get_active()))
            services.append(required)
        if not host.get("services"):
            note = label("No services configured. Add systemd unit names in Settings.", "muted")
            note.set_wrap(True)
            services.append(note)
        controls = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, min_children_per_line=1,
                               max_children_per_line=4, row_spacing=8, column_spacing=8)
        terminal = Gtk.Button(label="Open terminal", icon_name="utilities-terminal-symbolic")
        terminal.set_label("Open terminal")
        terminal.connect("clicked", lambda *_: self.terminal(host))
        controls.append(terminal)
        files = Gtk.Button(label="Browse files")
        files.connect("clicked", lambda *_: self.browse(host))
        controls.append(files)
        copy = Gtk.Button(label="Copy diagnostics")
        copy.connect("clicked", lambda *_: self.copy_diagnostics(host, data))
        controls.append(copy)
        if data.get("package_manager") in actions.UPDATE_COMMANDS:
            update = Gtk.Button(label="System updates…")
            update.connect("clicked", lambda *_: self.confirm_update(host, data["package_manager"]))
            update.set_sensitive(not self.active_job and not self.update_checks_running)
            controls.append(update)
        if self.demo:
            controls.set_sensitive(False)
        self.content.append(controls)
        recent = [e for e in self.history.events if isinstance(e, dict) and e.get("host") == host["id"]][-5:]
        if recent:
            activity = self.section("Recent changes", "Saved on this computer")
            for event in reversed(recent):
                line = label(time.strftime("%H:%M", time.localtime(event["time"])) + "  ·  " + event["message"], "muted")
                line.set_wrap(True)
                activity.append(line)
        footer = label(("Local metrics every 2 seconds · " if host.get("local") else "") +
                       f"Full checks every {self.configuration.get('refresh_seconds', 60)} seconds · " +
                       (f"Last check took {data['check_ms'] / 1000:.1f}s" if data.get("check_ms") else "No verified receipt yet"), "muted")
        footer.set_wrap(True)
        self.content.append(footer)

    def section(self, title, subtitle):
        section = box(True, 12)
        section.add_css_class("card")
        section.append(label(title, "section-title"))
        section.append(label(subtitle, "muted"))
        self.content.append(section)
        return section

    def detail_row(self, parent, name, value, icon, css=None):
        row = box(False, 10)
        row.append(Gtk.Image.new_from_icon_name(icon))
        title = label(name)
        title.set_hexpand(True)
        row.append(title)
        value_label = label(value, css or "muted")
        value_label.set_wrap(True)
        row.append(value_label)
        parent.append(row)

    def update_row(self, parent, host, kind, name, installed, icon):
        row = box(False, 10)
        row.append(Gtk.Image.new_from_icon_name(icon))
        description = box(True, 4)
        description.set_hexpand(True)
        description.append(label(name))
        checked = self.app_updates.get(host["id"], {}).get(kind, {})
        state = checked.get("state", "checking" if self.update_checks_running else "unknown")
        latest = checked.get("latest")
        versions = (installed or checked.get("installed") or "Not detected") + (" → " + latest if state == "available" else "")
        description.append(label(versions, "muted"))
        detail = checked.get("detail", "Checking for updates…" if self.update_checks_running else "Press Check now to check releases")
        if state == "current":
            detail = "Up to date" + (" · " + checked["provider"] if checked.get("provider") else "")
        if state == "protected":
            detail = "Protected · " + detail
        note = label(detail, "warning" if state in ("protected", "unknown", "unsupported") else "muted")
        note.set_wrap(True)
        description.append(note)
        row.append(description)
        if state == "available":
            button = Gtk.Button(label="Update")
            button.add_css_class("suggested-action")
            button.set_valign(Gtk.Align.CENTER)
            button.set_sensitive(not self.demo and not self.busy and not self.update_checks_running and not self.active_job)
            button.connect("clicked", lambda *_: self.request_update(host, kind, checked))
            row.append(button)
        elif state == "current":
            row.append(label("Current", "good"))
        parent.append(row)

    def service_preference(self, host, name, required):
        candidate = json.loads(json.dumps(self.configuration))
        updated = next(h for h in candidate["hosts"] if h["id"] == host["id"])
        optional = set(updated.get("optional_services", []))
        optional.discard(name) if required else optional.add(name)
        updated["optional_services"] = sorted(optional)
        try:
            config.atomic_json(self.config_file or config.config_path(), config.validate(candidate))
        except (ValueError, OSError):
            self.toast("Could not save the service preference")
            self.render_detail()
            return
        self.configuration = candidate
        if host["id"] in self.snapshots:
            self.snapshots[host["id"]]["optional_services"] = updated["optional_services"]
        self.populate_hosts()
        self.render_detail()

    def check_application_updates(self):
        if self.update_checks_running or self.active_job or self.demo:
            return
        self.update_checks_running = True
        self.refresh_button.set_sensitive(False)
        self.refresh_button.set_label("Checking updates…")
        self.spinner.start()
        self.render_detail()
        def work():
            try:
                updates.check_all(self.configuration["hosts"], dict(self.snapshots),
                                  lambda ident, result: GLib.idle_add(self.receive_updates, ident, result))
            finally:
                GLib.idle_add(self.finished_updates)
        threading.Thread(target=work, daemon=True).start()

    def receive_updates(self, ident, result):
        if ident in self.pending_restarts and "restart" in result:
            result["restart"].update(state="scheduled", detail="Restart scheduled; waiting to verify a new boot")
        self.app_updates[ident] = result
        if self.dismiss_resolved_jobs({ident: result}):
            try:
                self.persist_jobs()
            except OSError:
                pass
        if self.selected == ident:
            self.render_detail()
        return GLib.SOURCE_REMOVE

    def finished_updates(self):
        self.update_checks_running = False
        self.last_update_check = time.time()
        self.refresh_button.set_label("Check now")
        self.refresh_button.set_sensitive(True)
        self.spinner.stop()
        self.render_detail()
        try:
            config.atomic_json(config.state_path().with_name("application-checks.json"), self.app_updates)
        except OSError:
            self.toast("Checks completed, but the local receipt could not be saved")
        self.maybe_auto_update()
        return GLib.SOURCE_REMOVE

    def render_batch(self):
        if not hasattr(self, "batch_buttons"):
            return
        blocked = self.demo or self.busy or self.update_checks_running or bool(self.active_job)
        for kind, button in self.batch_buttons.items():
            candidates, _ = updates.batch_candidates(self.configuration["hosts"], self.snapshots, self.app_updates, kind)
            title = "Restart required computers" if kind == "restart" else "Update all " + ACTION_NAMES[kind]
            button.set_label(title + " (" + str(len(candidates)) + ")")
            button.set_sensitive(not blocked and bool(candidates))
            button.set_tooltip_text("Run Check now to refresh available releases" if not candidates else "Review computers and start sequential updates")
        running = bool(self.batch and self.batch.get("running"))
        self.stop_batch.set_visible(running and bool(self.batch.get("pending")))
        if self.batch:
            done = len(self.batch.get("results", []))
            total = self.batch.get("total", 0)
            text = ("Automatic " if self.batch.get("automatic") else "") + ACTION_NAMES[self.batch["kind"]] + " fleet updates: " + str(done) + "/" + str(total) + " completed"
            if running and self.active_job:
                text += " · " + self.active_job["host"]["name"] + " · " + self.active_job.get("phase", "Updating")
            elif self.batch.get("stopped"):
                text += " · " + self.batch["stopped"]
            if self.pending_restarts:
                text += " · Awaiting restart verification: " + ", ".join(item["name"] for item in self.pending_restarts.values())
            self.batch_label.set_text(text)
        elif config.auto_updates_enabled(self.configuration):
            self.batch_label.set_text("Automatic updates are on · Codex CLI, ChatGPT and Linux packages install when checks find them. Computers are not restarted.")
        else:
            self.batch_label.set_text("Fleet-wide updates · only computers with available releases are included")

    def request_batch(self, kind):
        if self.demo or self.busy or self.update_checks_running or self.active_job:
            return
        pending, skipped = updates.batch_candidates(self.configuration["hosts"], self.snapshots, self.app_updates, kind)
        if not pending:
            self.toast("No eligible updates. Run Check now to refresh releases.")
            return
        title = ACTION_NAMES[kind]
        body = "Update " + title + " sequentially on these computers:\n\n"
        body += "\n".join(item["host"]["name"] + (" → " + item["checked"]["latest"] if kind in ("cli", "desktop") else " · " + item["checked"].get("detail", "")) for item in pending)
        if skipped:
            body += "\n\nSkipped: " + "; ".join(item["name"] + " (" + item["reason"] + ")" for item in skipped)
        if kind == "desktop":
            body += "\n\nChatGPT may close and reopen. Finish active work first."
            if any(item["checked"].get("provider") == "linux-pacman" for item in pending):
                body += " Arch computers require a full system upgrade, including other packages."
        if kind == "system":
            body += "\n\nThis installs all available distribution package upgrades using passwordless sudo. Services may restart during installation. Computers are not automatically rebooted."
        elif kind == "restart":
            pending.sort(key=lambda item: item["host"].get("local", False))
            body += "\n\nSave your work. Each computer will restart after a one-minute delay. The local controller is scheduled last. Fleetlight verifies a new boot when each computer returns."
        body += "\n\nThe batch stops if an update fails. You can stop remaining updates without interrupting the active installer."
        dialog = Adw.MessageDialog(transient_for=self.window, heading="Restart required computers?" if kind == "restart" else "Update all " + title + "?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("update", ("Restart " if kind == "restart" else "Update ") + str(len(pending)) + " computers")
        dialog.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _, response: self.begin_batch(kind, pending) if response == "update" else None)
        dialog.present()

    def begin_batch(self, kind, pending, automatic=False):
        if self.active_job or self.busy or self.update_checks_running:
            return
        # Recheck eligibility after the review dialog; use only the reviewed hosts.
        fresh, _ = updates.batch_candidates([item["host"] for item in pending], self.snapshots,
                    {item["host"]["id"]: {kind: item["checked"]} for item in pending}, kind)
        if not automatic and len(fresh) != len(pending):
            self.toast("The release checks expired. Check again before starting.")
            return
        if not fresh:
            return
        self.batch = {"kind": kind, "pending": fresh, "results": [], "total": len(fresh),
                      "running": True, "automatic": bool(automatic)}
        try:
            self.persist_jobs()
        except OSError:
            self.batch = None
            self.toast("Could not save the batch; no updates started")
            return
        if automatic:
            self.toast("Automatic " + ACTION_NAMES[kind] + " updates started on " + str(len(fresh)) + " computers")
        self.advance_batch()

    def maybe_auto_update(self):
        if self.demo or not config.auto_updates_enabled(self.configuration):
            return
        if self.busy or self.update_checks_running or self.active_job:
            return
        if self.batch and self.batch.get("running"):
            return
        if time.time() < self.auto_holdoff_until:
            return
        kind, pending = updates.next_auto_batch(
            self.configuration["hosts"], self.snapshots, self.app_updates, self.auto_attempted)
        if pending:
            self.begin_batch(kind, pending, automatic=True)

    def advance_batch(self):
        if self.active_job or not self.batch:
            return
        if self.batch.get("pending"):
            item = self.batch["pending"].pop(0)
            # begin_update saves the active job and remaining queue together before dispatch.
            self.begin_update(item["host"], self.batch["kind"], item["checked"])
            if not self.active_job:
                self.batch["pending"].insert(0, item)
                self.batch["running"] = False
                self.batch["stopped"] = "Could not start the next update; retry after checking"
                self.render_batch()
            return
        self.batch["running"] = False
        try:
            self.persist_jobs()
        except OSError:
            self.toast("Could not save the batch summary")
        self.render_batch()
        self.check()

    def cancel_batch(self, *_):
        if self.batch:
            previous = list(self.batch["pending"])
            self.batch["pending"] = []
            self.batch["stopped"] = "Remaining updates cancelled"
            try:
                self.persist_jobs()
            except OSError:
                self.batch["pending"] = previous
                self.batch.pop("stopped", None)
                self.toast("Could not save cancellation; remaining updates are still queued")
                self.render_batch()
                return
            if config.auto_updates_enabled(self.configuration):
                self.auto_holdoff_until = time.time() + 900
            self.render_batch()

    def request_update(self, host, kind, checked):
        if self.active_job or self.update_checks_running or self.busy or self.demo:
            return
        if checked.get("state") != "available" or time.time() - checked.get("checked_at", 0) > 1800:
            self.toast("Run Check now before updating")
            return
        if kind == "cli":
            self.begin_update(host, kind, checked)
            return
        body = "Install ChatGPT " + checked["latest"] + " on " + host["name"] + "? The app may close and reopen after verification. Finish any active work first."
        if checked.get("provider") == "linux-pacman":
            body += "\n\nArch requires a full system upgrade. Other system packages will also be updated. Locally modified ChatGPT files block this update."
        dialog = Adw.MessageDialog(transient_for=self.window, heading="Update ChatGPT?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("update", "Update and reopen")
        dialog.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _, response: self.begin_update(host, kind, checked) if response == "update" else None)
        dialog.present()

    def remember_history(self, key, widget):
        if self.rebuilding_detail or widget.get_parent() is None:
            return
        self.update_history_expanded[key] = widget.get_expanded()

    def install_records(self, host):
        active = self.active_job if (self.active_job or {}).get("host", {}).get("id") == host["id"] else None
        if self.demo:
            receipt = active or self.last_jobs.get(host["id"])
            return [receipt] if receipt else []
        records = list(self.install_history.get(host["id"]) or [])
        if not records and host.get("local"):
            records = history_report()
        os_name = (self.snapshots.get(host["id"]) or {}).get("os")
        records = updates.visible_installs(records, os_name)
        if active and active.get("id") not in {item.get("id") for item in records}:
            records = updates.visible_installs([active], os_name) + records
        if records:
            return records
        last = self.last_jobs.get(host["id"])
        return updates.visible_installs([last], os_name) if last else []

    def update_history(self, parent, host):
        records = self.install_records(host)
        if not records:
            return
        listed = []
        for record in records:
            changes = installation_changes(record)
            if changes or record.get("state") in ("succeeded", "failed"):
                listed.append((record, changes))
        if not listed:
            return
        total = sum(len(changes) for _, changes in listed)
        title = "What changed · " + str(total) if total else "What changed"
        expander = Gtk.Expander(label=title)
        history_key = (host["id"], "install-history")
        expander.set_expanded(self.update_history_expanded.get(history_key, bool(total)))
        expander.connect("notify::expanded", lambda widget, _: self.remember_history(history_key, widget))
        details = box(True, 6)
        for record, changes in listed[:12]:
            when = record.get("finished_at") or record.get("started_at")
            stamp = time.strftime("%a %d %b %H:%M", time.localtime(when)) if when else "Install"
            heading = stamp + " · " + ACTION_NAMES.get(record.get("kind"), "Update")
            if record.get("state") == "failed":
                heading += " · failed"
            line = label(heading, "warning" if record.get("state") == "failed" else "eyebrow")
            line.set_wrap(True)
            details.append(line)
            if changes:
                for item in changes:
                    change = label(item)
                    change.set_wrap(True)
                    details.append(change)
            else:
                empty = label(record.get("phase") or "No package changes recorded", "muted")
                empty.set_wrap(True)
                details.append(empty)
        latest = listed[0][0]
        if latest.get("log") and not host.get("local"):
            log_expander = Gtk.Expander(label="Installer log")
            view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
            view.get_buffer().set_text(str(latest.get("log"))[-12000:])
            scroll = Gtk.ScrolledWindow(min_content_height=120, max_content_height=200)
            scroll.set_child(view)
            log_expander.set_child(scroll)
            details.append(log_expander)
        expander.set_child(details)
        parent.append(expander)

    def dismiss_resolved_jobs(self, checks_by_host):
        changed = False
        for ident, checks in checks_by_host.items():
            last = self.last_jobs.get(ident)
            if not last or last.get("dismissed"):
                continue
            if updates.relevant_job(last, checks) is None:
                self.last_jobs[ident] = dict(last, dismissed=True)
                changed = True
        return changed

    def dismiss_update_result(self, host_id):
        previous = self.last_jobs.get(host_id)
        if not previous:
            return
        self.last_jobs[host_id] = dict(previous, dismissed=True)
        try:
            self.persist_jobs()
        except OSError:
            self.last_jobs[host_id] = previous
            self.toast("Could not save dismissal; the update log has been kept")
            return
        self.render_detail()

    def persist_jobs(self):
        config.atomic_json(self.journal_path, {"active_job": self.active_job, "last_jobs": self.last_jobs, "batch": self.batch, "pending_restarts": self.pending_restarts})

    def begin_update(self, host, kind, checked):
        if self.active_job or self.update_checks_running or self.busy:
            return
        ident = uuid.uuid4().hex
        self.active_job = {"id": ident, "host": dict(host), "kind": kind, "target": checked["latest"],
                           "boot_id": self.snapshots.get(host["id"], {}).get("boot_id"),
                           "state": "queued", "phase": "Starting " + ACTION_NAMES[kind],
                           "auto_key": list(updates.auto_target_key(host, kind, checked))}
        try:
            self.persist_jobs()
        except OSError:
            self.active_job = None
            self.toast("Update was not started: its recovery receipt could not be saved")
            return
        self.last_jobs.pop(host["id"], None)
        self.refresh_button.set_sensitive(False)
        self.spinner.start()
        self.render_detail()
        def start():
            try:
                receipt = updates.start_job(host, kind, checked, ident)
            except Exception as error:
                receipt = {"state": "failed", "phase": "Update could not start: " + str(error)}
            GLib.idle_add(self.receive_job, receipt)
        threading.Thread(target=start, daemon=True).start()

    def watch_job(self):
        if not self.active_job:
            return GLib.SOURCE_REMOVE
        job = dict(self.active_job)
        self.refresh_button.set_sensitive(False)
        self.spinner.start()
        def poll():
            receipt = updates.job_status(job["host"], job["id"])
            GLib.idle_add(self.receive_job, receipt)
        threading.Thread(target=poll, daemon=True).start()
        return GLib.SOURCE_REMOVE

    def receive_job(self, receipt):
        if not self.active_job:
            return GLib.SOURCE_REMOVE
        self.active_job.update({k:v for k,v in receipt.items() if k not in ("host", "id", "kind")})
        state = receipt.get("state")
        if state in ("succeeded", "failed", "busy", "interrupted", "unknown"):
            if state == "succeeded" and self.active_job["kind"] == "restart":
                host = self.active_job["host"]
                self.pending_restarts[host["id"]] = {"name": host["name"], "boot_id": self.active_job.get("boot_id"), "scheduled_at": time.time()}
                if host["id"] in self.app_updates:
                    self.app_updates[host["id"]]["restart"] = {"state": "scheduled", "detail": "Waiting for new boot"}
            if state != "succeeded":
                key = self.active_job.get("auto_key")
                if isinstance(key, list) and len(key) >= 2:
                    self.auto_attempted.add(tuple(key))
            if self.batch and self.batch.get("running"):
                self.batch.setdefault("results", []).append({"host": self.active_job["host"]["name"], "state": state})
                if state != "succeeded":
                    if self.batch.get("automatic"):
                        self.batch["stopped"] = "Continuing after " + self.active_job["host"]["name"] + ": " + receipt.get("phase", state)
                    else:
                        self.batch["pending"] = []
                        self.batch["stopped"] = "Stopped after " + self.active_job["host"]["name"] + ": " + receipt.get("phase", state)
            self.last_jobs[self.active_job["host"]["id"]] = dict(self.active_job)
            self.update_history_expanded[(self.active_job["host"]["id"], "install-history")] = True
            self.refresh_install_history(self.active_job["host"])
            self.toast(receipt.get("phase", "Update completed"))
            self.active_job = None
            self.spinner.stop()
            self.refresh_button.set_sensitive(True)
        try:
            self.persist_jobs()
        except OSError:
            self.toast("Could not save the latest update receipt; keep Fleetlight open")
        self.render_detail()
        if self.active_job:
            GLib.timeout_add_seconds(3 if state != "disconnected" else 10, self.watch_job)
        else:
            if self.batch and self.batch.get("running"):
                self.advance_batch()
            else:
                self.check()
        return GLib.SOURCE_REMOVE

    def close_requested(self, *_):
        if self.active_job:
            self.toast("An update is running. Keep Fleetlight open to follow progress.")
            return True
        return False

    def terminal(self, host, manager=None):
        try:
            actions.open_terminal(host, manager)
        except (OSError, RuntimeError) as error:
            self.toast(str(error))

    def browse(self, host):
        uri = Path.home().as_uri() if host.get("local") else "sftp://" + quote(host["alias"], safe="@.:-") + "/"
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error:
            self.toast("No SFTP-capable file manager found. Use Open terminal instead.")

    def copy_diagnostics(self, host, data):
        payload = {"application": "Fleetlight Linux " + __version__, "name": host["name"], **data}
        self.window.get_clipboard().set(json.dumps(payload, indent=2))
        self.toast("Diagnostics copied. Review computer names before sharing.")

    def confirm_update(self, host, manager):
        dialog = Adw.MessageDialog(transient_for=self.window, heading="Update " + host["name"] + "?",
                                  body="This opens an interactive terminal and runs:\n\n" + actions.UPDATE_COMMANDS[manager] +
                                  "\n\nIt can update all system packages and may require a restart. Package upgrades can replace local application repairs. Review the package manager’s confirmation before proceeding.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("open", "Open update terminal")
        dialog.set_response_appearance("open", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _, response: self.terminal(host, manager) if response == "open" else None)
        dialog.present()

    def add_computer(self, *_):
        dialog = Adw.Window(transient_for=self.window, modal=True, title="Add computer", default_width=430)
        body = margins(box(True, 14), 24)
        body.append(label("Add an SSH computer", "section-title"))
        entries = {}
        for key, title, hint in (("name", "Display name", "Home server"), ("alias", "SSH alias", "home-server"),
                                 ("services", "Services (comma-separated)", "tailscaled, docker")):
            body.append(label(title, "muted"))
            entries[key] = Gtk.Entry(placeholder_text=hint)
            body.append(entries[key])
        note = label("Uses your existing SSH keys and known hosts. Verify the connection in a terminal first.", "muted")
        note.set_wrap(True)
        body.append(note)
        error = label("", "warning")
        error.set_wrap(True)
        body.append(error)
        save = Gtk.Button(label="Add computer")
        save.add_css_class("suggested-action")
        save.set_sensitive(not self.demo)
        def add(*_):
            if self.busy or self.update_checks_running or self.active_job:
                error.set_text("Wait for the current check to finish")
                return
            import uuid
            host = {"id": "host-" + uuid.uuid4().hex[:8], "name": entries["name"].get_text().strip(),
                    "alias": entries["alias"].get_text().strip(),
                    "services": [s.strip() for s in entries["services"].get_text().split(",") if s.strip()]}
            candidate = {**self.configuration, "hosts": self.configuration["hosts"] + [host]}
            try:
                config.atomic_json(self.config_file or config.config_path(), config.validate(candidate))
            except (ValueError, OSError) as problem:
                error.set_text(str(problem))
                return
            self.configuration = candidate
            self.selected = host["id"]
            dialog.close()
            self.populate_hosts()
            self.check()
        save.connect("clicked", add)
        body.append(save)
        dialog.set_content(body)
        dialog.present()

    def settings(self, *_):
        dialog = Adw.Window(transient_for=self.window, modal=True, title="Fleetlight settings", default_width=620, default_height=600)
        body = margins(box(True, 12), 20)
        body.append(label("Your fleet configuration", "section-title"))
        description = label("Add or remove computers, rename them, and set systemd services. Optional websites are checked from this computer for a recent HTTPS JSON update time. Saved only in your user configuration folder.", "muted")
        description.set_wrap(True)
        body.append(description)
        body.append(label("Agent quota", "section-title"))
        quota_note = label("Show remaining Codex and Cursor allowance from this computer’s signed-in sessions. Uncheck an agent to hide it.", "muted")
        quota_note.set_wrap(True)
        body.append(quota_note)
        enabled = config.enabled_agents(self.configuration)
        agent_toggles = {}
        for name, title in (("codex", "Codex"), ("cursor", "Cursor")):
            toggle = Gtk.CheckButton(label="Show " + title)
            toggle.set_active(enabled[name])
            agent_toggles[name] = toggle
            body.append(toggle)
        body.append(label("Automatic updates", "section-title"))
        auto_note = label("When enabled, Fleetlight installs Codex CLI, ChatGPT and Linux package updates as soon as checks find them. Computers are not restarted. Keep Fleetlight open. A failed computer is skipped; the same update is not retried until you restart Fleetlight or a different set of packages appears.", "muted")
        auto_note.set_wrap(True)
        body.append(auto_note)
        auto_toggle = Gtk.CheckButton(label="Automatically install all available updates")
        auto_toggle.set_active(config.auto_updates_enabled(self.configuration))
        body.append(auto_toggle)
        editor = Gtk.TextView(monospace=True, wrap_mode=Gtk.WrapMode.NONE)
        editor.get_buffer().set_text(json.dumps(self.configuration, indent=2))
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(editor)
        body.append(scroll)
        start = Gtk.CheckButton(label="Open Fleetlight when I log in")
        start.set_active(actions.autostart_path().exists())
        body.append(start)
        error = label("", "warning")
        error.set_wrap(True)
        body.append(error)
        save = Gtk.Button(label="Save settings")
        save.add_css_class("suggested-action")
        def apply(*_):
            if self.busy or self.update_checks_running or self.active_job:
                error.set_text("Wait for the current check to finish")
                return
            buffer = editor.get_buffer()
            try:
                candidate = config.validate(json.loads(buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)))
                candidate["agents"] = {name: toggle.get_active() for name, toggle in agent_toggles.items()}
                candidate["auto_updates"] = auto_toggle.get_active()
                candidate = config.validate(candidate)
                config.atomic_json(self.config_file or config.config_path(), candidate)
                if start.get_active() != actions.autostart_path().exists():
                    actions.set_autostart(start.get_active())
            except (ValueError, OSError, RuntimeError) as problem:
                error.set_text(str(problem))
                return
            enabling = config.auto_updates_enabled(candidate) and not config.auto_updates_enabled(self.configuration)
            self.configuration = candidate
            if enabling:
                self.auto_attempted.clear()
                self.auto_holdoff_until = 0
            self.snapshots = {k:v for k,v in self.snapshots.items() if k in {h["id"] for h in candidate["hosts"]}}
            self.site_status = {k:v for k,v in self.site_status.items()
                                if k in {site["id"] for site in sites.configured(candidate)}}
            if self.timer:
                GLib.source_remove(self.timer)
            self.timer = GLib.timeout_add_seconds(candidate.get("refresh_seconds", 60), self.auto_check)
            dialog.close()
            self.populate_hosts()
            self.check()
        save.connect("clicked", apply)
        body.append(save)
        if self.demo:
            save.set_sensitive(False)
        dialog.set_content(body)
        dialog.present()

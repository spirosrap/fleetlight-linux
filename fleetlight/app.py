"""GTK4/libadwaita desktop shell. Worker threads never touch GTK widgets."""
from collections import deque
import json
import math
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
from .monitor import History, issues, linux_update_issues, probe_host, refresh
from .probe import collect_metrics
from . import sites
from .tray import Tray
from . import updates
from .update_job import installation_changes, history_report
from .widgets import AvailabilityStrip, FleetRing, MiniBars, Ring, TrendChart


ACTION_NAMES = {"cli": "Codex CLI", "claude": "Claude CLI", "desktop": "ChatGPT", "system": "Linux packages", "restart": "required restarts"}
OVERVIEW = "fleet"
WEBSITE = "https://github.com/spirosrap/fleetlight-linux"
# History chart choices: field in the saved history, title, unit and axis limit (None scales to the data).
CHART_METRICS = (("cpu", "CPU", "%", 100), ("memory", "Memory", "%", 100), ("disk", "Root disk", "%", 100),
                 ("temperature", "Temperature", "°C", None), ("ms", "Check time", "ms", None))
CHART_RANGES = (("live", "Live", 300), ("1h", "1 h", 3600), ("6h", "6 h", 6 * 3600),
                ("24h", "24 h", 86400), ("7d", "7 d", 7 * 86400))
SCHEMES = {"auto": Adw.ColorScheme.PREFER_DARK, "dark": Adw.ColorScheme.FORCE_DARK, "light": Adw.ColorScheme.FORCE_LIGHT}
LOW_QUOTA = 10

# Colours come from libadwaita's named palette so the app follows the system theme and accent.
CSS = b"""
.metric { font-size: 26px; font-weight: 800; letter-spacing: -0.02em; font-feature-settings: "tnum"; }
.stat-value { font-size: 20px; font-weight: 800; letter-spacing: -0.01em; font-feature-settings: "tnum"; }
.hero { font-size: 27px; font-weight: 800; letter-spacing: -0.02em; }
.eyebrow { font-size: 11px; font-weight: 700; letter-spacing: 0.09em; opacity: 0.66; }
.muted { opacity: 0.66; }
.small { font-size: 12px; }
.numeric { font-feature-settings: "tnum"; }
.good { color: @success_color; }
.warning { color: @warning_color; }
.bad { color: @error_color; }
.accent-text { color: @accent_color; }
.card { padding: 16px; border-radius: 16px; }
.hero-card { padding: 20px 22px; border-radius: 20px; border: 1px solid alpha(@accent_bg_color, 0.30);
  background-image: linear-gradient(115deg, alpha(@accent_bg_color, 0.30), alpha(@accent_bg_color, 0.09) 55%, alpha(@accent_bg_color, 0.03)); }
.hero-card.attention { border-color: alpha(@warning_bg_color, 0.36);
  background-image: linear-gradient(115deg, alpha(@warning_bg_color, 0.26), alpha(@warning_bg_color, 0.07) 55%, alpha(@warning_bg_color, 0.02)); }
.hero-card.offline { border-color: alpha(@error_bg_color, 0.38);
  background-image: linear-gradient(115deg, alpha(@error_bg_color, 0.28), alpha(@error_bg_color, 0.07) 55%, alpha(@error_bg_color, 0.02)); }
.hero-card.quiet { border-color: alpha(currentColor, 0.12);
  background-image: linear-gradient(115deg, alpha(currentColor, 0.09), alpha(currentColor, 0.03)); }
.stat { padding: 10px 12px; border-radius: 14px; background: alpha(currentColor, 0.06); }
.icon-tile { min-width: 38px; min-height: 38px; border-radius: 12px; background: alpha(currentColor, 0.13); }
.icon-tile.large { min-width: 56px; min-height: 56px; border-radius: 17px; }
.icon-tile.accent { color: @accent_color; }
.pill { padding: 3px 11px; border-radius: 999px; font-weight: 700; font-size: 12px; background: alpha(currentColor, 0.14); }
.chip { padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; background: alpha(currentColor, 0.08); }
.badge { padding: 1px 8px; border-radius: 999px; font-weight: 700; font-size: 11px; background: alpha(currentColor, 0.16); }
.dot { min-width: 10px; min-height: 10px; border-radius: 999px; background: alpha(currentColor, 0.28); }
.dot.good { background: @success_color; }
.dot.warning { background: @warning_color; }
.dot.bad { background: @error_color; }
progressbar trough, progressbar progress { min-height: 6px; border-radius: 3px; }
progressbar.thin trough, progressbar.thin progress { min-height: 4px; border-radius: 2px; }
progressbar.thin trough { min-width: 40px; }
progressbar progress { background: @accent_bg_color; }
progressbar.good progress { background: @success_bg_color; }
progressbar.warning progress { background: @warning_bg_color; }
progressbar.bad progress { background: @error_bg_color; }
button.host-card { padding: 0; border-radius: 16px; }
button.host-card > .card { transition: box-shadow 160ms ease-out, background-color 160ms ease-out; }
button.host-card:hover > .card { background-color: mix(@card_bg_color, @accent_bg_color, 0.10);
  box-shadow: 0 0 0 1px alpha(@accent_bg_color, 0.55), 0 8px 22px alpha(black, 0.20); }
.section-title { font-size: 16px; font-weight: 700; }
.row-title { font-weight: 600; }
.timeline-dot { min-width: 8px; min-height: 8px; border-radius: 999px; background: currentColor; }
"""

SHORTCUTS_UI = """<?xml version="1.0" encoding="UTF-8"?>
<interface>
  <object class="GtkShortcutsWindow" id="shortcuts">
    <property name="modal">1</property>
    <child>
      <object class="GtkShortcutsSection">
        <property name="section-name">shortcuts</property>
        <property name="max-height">12</property>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">Fleet</property>
            <child><object class="GtkShortcutsShortcut"><property name="accelerator">&lt;Control&gt;r F5</property><property name="title">Check all computers now</property></object></child>
            <child><object class="GtkShortcutsShortcut"><property name="accelerator">&lt;Control&gt;f</property><property name="title">Find a computer or website</property></object></child>
            <child><object class="GtkShortcutsShortcut"><property name="accelerator">&lt;Control&gt;Home</property><property name="title">Show the fleet overview</property></object></child>
            <child><object class="GtkShortcutsShortcut"><property name="accelerator">&lt;Alt&gt;1...9</property><property name="title">Open a computer by its position in the sidebar</property></object></child>
            <child><object class="GtkShortcutsShortcut"><property name="accelerator">&lt;Control&gt;n</property><property name="title">Add a computer</property></object></child>
          </object>
        </child>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">Application</property>
            <child><object class="GtkShortcutsShortcut"><property name="accelerator">&lt;Control&gt;comma</property><property name="title">Settings</property></object></child>
            <child><object class="GtkShortcutsShortcut"><property name="accelerator">&lt;Control&gt;question</property><property name="title">Keyboard shortcuts</property></object></child>
            <child><object class="GtkShortcutsShortcut"><property name="accelerator">&lt;Control&gt;q</property><property name="title">Quit</property></object></child>
          </object>
        </child>
      </object>
    </child>
  </object>
</interface>
"""


def label(text, css=None, xalign=0, wrap=False):
    widget = Gtk.Label(label=str(text), xalign=xalign)
    if css:
        for name in css.split():
            widget.add_css_class(name)
    if wrap:
        widget.set_wrap(True)
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


def attach(parent, child):
    """Append to a plain box or add to a libadwaita preferences group."""
    if isinstance(parent, Adw.PreferencesGroup):
        parent.add(child)
    else:
        parent.append(child)


def set_tone(widget, css, choices=("good", "warning", "bad", "muted")):
    """Swap a widget's status colour class without touching its other classes."""
    for name in choices:
        if name != css:
            widget.remove_css_class(name)
    if css:
        widget.add_css_class(css)


def status_dot(css=None):
    holder = Gtk.Box(width_request=16, height_request=16, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
    dot = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
    dot.add_css_class("dot")
    if css:
        dot.add_css_class(css)
    holder.append(dot)
    holder.dot = dot
    return holder


def icon_tile(icon_name, css=None, large=False):
    """Rounded square holding a symbolic icon; the tint follows the tile's status colour."""
    tile = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
    tile.add_css_class("icon-tile")
    if large:
        tile.add_css_class("large")
    if css:
        tile.add_css_class(css)
    image = Gtk.Image.new_from_icon_name(icon_name)
    image.set_pixel_size(26 if large else 18)
    image.set_hexpand(True)
    image.set_halign(Gtk.Align.CENTER)
    tile.append(image)
    # The image expands to centre itself; stop that from making the tile claim spare width.
    tile.set_hexpand(False)
    return tile


def chip(text, icon_name=None, css=None):
    holder = box(False, 6)
    holder.add_css_class("chip")
    if css:
        holder.add_css_class(css)
    if icon_name:
        image = Gtk.Image.new_from_icon_name(icon_name)
        image.set_pixel_size(13)
        holder.append(image)
    holder.append(label(text))
    return holder


def icon_button(text, icon_name):
    button = Gtk.Button()
    button.set_child(Adw.ButtonContent(label=text, icon_name=icon_name))
    return button


def fan_text(fan):
    if not fan:
        return None
    if fan.get("rpm") is not None:
        return f"fan {fan['rpm']} rpm" if fan["rpm"] else "fan stopped"
    if fan.get("percent") is not None:
        return f"fan {fan['percent']}%"
    return "fan on" if fan.get("running") else "fan off"


def usage_css(value, warn=80, bad=90):
    if value is None:
        return None
    return "bad" if value >= bad else "warning" if value >= warn else None


def cpu_share(data):
    """CPU use in percent: measured where the probe reports it, otherwise load per core."""
    value = data.get("cpu_percent")
    if isinstance(value, (int, float)):
        return max(0, min(100, round(value)))
    load, cpus = data.get("load"), data.get("cpus")
    if isinstance(load, (int, float)) and cpus:
        return max(0, min(100, round(100 * load / cpus)))
    return None


def host_icon(host, data):
    if data.get("battery"):
        return "computer-symbolic"
    return "computer-symbolic" if host.get("local") or data.get("os") == "Darwin" else "network-server-symbolic"


def system_label(data, fallback="Online"):
    """Human system name; macOS probes report only the version number."""
    distribution = data.get("distribution") or ""
    if data.get("os") == "Darwin" and distribution and not distribution.lower().startswith("mac"):
        return "macOS " + distribution
    return distribution or data.get("os") or fallback


def ago(timestamp):
    elapsed = max(0, int(time.time() - timestamp))
    if elapsed < 60:
        return f"{elapsed}s ago"
    if elapsed < 3600:
        return f"{elapsed // 60}m ago"
    if elapsed < 2 * 86400:
        return f"{elapsed // 3600}h ago"
    return f"{elapsed // 86400}d ago"


def age(timestamp):
    if not timestamp:
        return "Not checked yet"
    return "Just checked" if time.time() - timestamp < 10 else "Checked " + ago(timestamp)


def uptime(seconds):
    if seconds is None:
        return "Unavailable"
    hours = seconds // 3600
    return f"{hours // 24}d {hours % 24}h" if hours >= 24 else f"{hours}h {(seconds % 3600) // 60}m"


def size_text(amount):
    """Bytes as a short binary size."""
    if not isinstance(amount, (int, float)):
        return "—"
    for unit, scale in (("TiB", 1024**4), ("GiB", 1024**3), ("MiB", 1024**2)):
        if amount >= scale:
            value = amount / scale
            return (f"{value:.0f} " if value >= 100 else f"{value:.1f} ") + unit
    return f"{amount / 1024:.0f} KiB"


class Fleetlight(Adw.Application):
    def __init__(self, configuration=None, demo=False, background=False):
        super().__init__(application_id="io.github.fleetlight.Linux.Demo" if demo else "io.github.fleetlight.Linux", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.config_file = configuration
        self.demo = demo
        self.background = background
        self.tray = None
        self.in_tray = False
        self.tray_info = ("Fleetlight", 0)
        # What the tray shows, as a file for desktop bar widgets that draw their own icon.
        self.status_path = config.state_path().with_name("status.json")
        self.status_written = (None, 0)
        self.window = None
        self.snapshots = {}
        self.busy = False
        self.selected = None
        self.timer = None
        self.local_metrics_busy = False
        self.app_updates = {}
        self.update_checks_running = False
        self.last_update_check = 0
        self.active_jobs = {}
        self.job_polls = set()
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
        self.overview_widgets = {}
        self.auto_attempted = set()
        self.auto_holdoff_until = 0
        self.last_seen = {}
        self.live = {}
        self.chart = None
        self.chart_metric = None
        self.chart_range = None
        self.gauges = {}
        self.rendered_page = None
        self.fade = None
        self.sidebar_rows = {}
        self.sidebar_structure = None
        self.selecting = False
        self.render_pending = 0
        self.quota_warned = set()
        self.journal_path = config.state_path().with_name("update-controller.json")
        if not demo:
            try:
                journal = json.loads(self.journal_path.read_text())
                self.active_jobs = updates.restore_active_jobs(journal)
                self.batch = journal.get("batch")
                self.pending_restarts = journal.get("pending_restarts", {})
                if self.batch:
                    if self.batch["kind"] not in ("cli", "claude", "desktop", "system", "restart") or not isinstance(self.batch["pending"], list) or len(self.batch["pending"]) > 32:
                        raise ValueError("Invalid saved batch")
                    for item in self.batch["pending"]:
                        config.validate({"version": 1, "hosts": [item["host"]]})
                        if not updates.version(item["checked"].get("latest")):
                            raise ValueError("Invalid saved release")
                self.last_jobs = journal.get("last_jobs", {})
                if not isinstance(self.last_jobs, dict):
                    self.last_jobs = {}
            except (ValueError, OSError, TypeError, KeyError, AttributeError):
                self.active_jobs = {}
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
        self.agent_usage_path = config.state_path().with_name("agent-usage.json")
        if not demo:
            try:
                saved = json.loads(self.agent_usage_path.read_text())
                for name, item in (saved.items() if isinstance(saved, dict) else ()):
                    if name in agent_quota.NAMES and isinstance(item, dict) and item.get("state") == "ok":
                        self.agent_usage[name] = dict(item, stale="Checking remaining quota…")
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        # The last online result of every computer: shown until the first check finishes, and the
        # source of "last seen" facts and the Wake-on-LAN address once a computer is unreachable.
        self.last_seen_path = config.state_path().with_name("last-seen.json")
        if not demo:
            try:
                saved = json.loads(self.last_seen_path.read_text())
                self.last_seen = {ident: item for ident, item in (saved.items() if isinstance(saved, dict) else ())
                                  if isinstance(item, dict) and item.get("status") == "online"}
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        self.history = History() if not demo else History(Path("/nonexistent/fleetlight-demo"))
        if demo:
            self.journal_path = Path("/nonexistent/fleetlight-demo/update-controller.json")
        self.connect("activate", self.activate_window)
        self.connect("shutdown", self.save_state)

    def save_state(self, *_):
        """History is written every few minutes while running; write the rest on the way out."""
        if self.demo:
            return
        self.publish_status({"running": False})
        try:
            self.history.save()
            config.atomic_json(self.last_seen_path, self.last_seen, indent=None)
        except OSError:
            pass

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
            self.seed_demo_history()
        Adw.StyleManager.get_default().set_color_scheme(SCHEMES[config.appearance(self.configuration)])
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.install_actions()
        self.window = Adw.ApplicationWindow(application=self, title="Fleetlight", default_width=1180, default_height=820)
        self.window.set_icon_name("io.github.fleetlight.Linux")
        self.window.connect("close-request", self.close_requested)

        # Sidebar: fleet list with its own header bar.
        sidebar_view = Adw.ToolbarView()
        sidebar_header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title="Fleetlight", subtitle=f"Linux · {__version__}")
        sidebar_header.set_title_widget(self.window_title)
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add computer (Ctrl+N)")
        add.set_sensitive(not self.demo)
        add.connect("clicked", self.add_computer)
        sidebar_header.pack_start(add)
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Check now", "app.check")
        section.append("Fleet overview", "app.overview")
        section.append("Settings", "app.settings")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append("Keyboard shortcuts", "app.shortcuts")
        section.append("About Fleetlight", "app.about")
        section.append("Quit", "app.quit")
        menu.append_section(None, section)
        sidebar_header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Main menu", primary=True))
        sidebar_view.add_top_bar(sidebar_header)
        sidebar = box(True, 6)
        filters = margins(box(False, 6), 12)
        filters.set_margin_bottom(4)
        self.search = Gtk.SearchEntry(placeholder_text="Find a computer or site", hexpand=True)
        self.search.connect("search-changed", lambda *_: self.populate_hosts())
        filters.append(self.search)
        self.attention = Gtk.ToggleButton(icon_name="dialog-warning-symbolic", tooltip_text="Show only computers and sites that need attention")
        self.attention.connect("toggled", lambda *_: self.populate_hosts())
        filters.append(self.attention)
        sidebar.append(filters)
        self.host_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.host_list.add_css_class("navigation-sidebar")
        self.host_list.connect("row-selected", self.select_host)
        self.host_list.connect("row-activated", lambda *_: self.split_view.set_show_content(True))
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(self.host_list)
        sidebar.append(scroller)
        self.summary = margins(label("Checking computers…", "muted"), 14)
        self.summary.set_margin_top(6)
        self.summary.set_wrap(True)
        sidebar.append(self.summary)
        sidebar_view.set_content(sidebar)
        sidebar_page = Adw.NavigationPage(title="Fleetlight", child=sidebar_view, tag="sidebar")

        # Content: one page per computer, website or the fleet overview.
        self.build_fleet_widgets()
        content_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.page_title = Adw.WindowTitle(title="Fleet overview", subtitle="")
        header.set_title_widget(self.page_title)
        # Kept off screen: a spinning indicator redraws the whole window on every frame for as long
        # as a check runs. The button's label says what is happening instead.
        self.spinner = Gtk.Spinner()
        self.refresh_button = Gtk.Button(label="Check now", tooltip_text="Check every computer now (Ctrl+R)")
        self.refresh_button.add_css_class("suggested-action")
        self.refresh_button.connect("clicked", lambda *_: self.check())
        header.pack_end(self.refresh_button)
        content_view.add_top_bar(header)
        self.banner = Adw.Banner(revealed=False)
        self.banner.connect("button-clicked", self.cancel_batch)
        content_view.add_top_bar(self.banner)
        # Agent quota stays visible on every page.
        content_view.add_top_bar(Adw.Clamp(maximum_size=1180, tightening_threshold=900, child=self.agent_box))
        self.scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.content = margins(box(True, 18), 24)
        self.scroller.set_child(Adw.Clamp(maximum_size=1180, tightening_threshold=900, child=self.content))
        content_view.set_content(self.scroller)
        content_page = Adw.NavigationPage(title="Fleet overview", child=content_view, tag="content")

        layout = Adw.NavigationSplitView(min_sidebar_width=250, max_sidebar_width=330, sidebar_width_fraction=0.27)
        layout.set_sidebar(sidebar_page)
        layout.set_content(content_page)
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 960px"))
        breakpoint.add_setter(layout, "collapsed", True)
        self.window.add_breakpoint(breakpoint)
        self.split_view = layout
        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(layout)
        self.window.set_content(self.toasts)
        self.selected = OVERVIEW
        self.populate_hosts()
        self._fleet_started = False
        self.window.connect("map", self.reveal_fleet)
        self.window.connect("notify::visible", lambda *_: self.refresh_tray())
        if not self.demo:
            self.tray = Tray(self.get_dbus_connection(), "fleetlight", "Fleetlight", self.toggle_window, self.tray_changed)
            self.refresh_tray()
        if self.background and self.tray:
            # Started for the tray: stay hidden, unless no tray turns up to hold the icon.
            GLib.timeout_add_seconds(3, self.present_without_tray)
        else:
            self.window.present()
        GLib.idle_add(self.reveal_fleet)
        self.timer = GLib.timeout_add_seconds(self.configuration.get("refresh_seconds", 60), self.auto_check)
        if not self.demo:
            GLib.timeout_add_seconds(2, self.check_local_metrics)
        if self.demo:
            self.refresh_button.set_sensitive(False)
            self.app_updates = {h["id"]: {kind: updates.plan("1.0.0", "1.1.0" if kind == "cli" else "1.0.0", "standalone" if kind == "cli" else "native" if kind == "claude" else "macos-appcast") for kind in ("cli", "claude", "desktop")} for h in self.configuration["hosts"]}
            self.agent_usage = agent_quota.demo_usage()
            self.render_agents()
            self.render_detail()
        if load_error:
            self.toast("Configuration was not loaded: " + load_error)

    def install_actions(self):
        entries = (("check", lambda: self.check(), ["<Control>r", "F5"]),
                   ("search", lambda: self.search.grab_focus(), ["<Control>f"]),
                   ("overview", lambda: self.show_page(OVERVIEW), ["<Control>Home"]),
                   ("settings", lambda: self.settings(), ["<Control>comma"]),
                   ("add", lambda: self.add_computer(), ["<Control>n"]),
                   ("shortcuts", lambda: self.show_shortcuts(), ["<Control>question"]),
                   ("about", lambda: self.show_about(), []),
                   ("toggle", lambda: self.toggle_window(), []),
                   ("quit", lambda: self.quit_requested(), ["<Control>q"]))
        for name, callback, accelerators in entries:
            if self.lookup_action(name):
                continue
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_, run=callback: run())
            self.add_action(action)
            if accelerators:
                self.set_accels_for_action("app." + name, accelerators)
        for position in range(1, 10):
            name = "jump" + str(position)
            if not self.lookup_action(name):
                action = Gio.SimpleAction.new(name, None)
                action.connect("activate", lambda *_, index=position - 1: self.jump(index))
                self.add_action(action)
                self.set_accels_for_action("app." + name, ["<Alt>" + str(position)])
        if not self.lookup_action("show"):
            show = Gio.SimpleAction.new("show", GLib.VariantType.new("s"))
            show.connect("activate", self.show_from_notification)
            self.add_action(show)

    def tray_changed(self, available):
        """A tray took the icon, or the tray went away: only keep running windowless while it is shown."""
        if available == self.in_tray:
            return
        self.in_tray = available
        if available:
            self.hold()
        else:
            if self.window is not None and not self.window.get_visible():
                self.window.present()
            self.release()

    def present_without_tray(self):
        if not self.in_tray and self.window is not None:
            self.window.present()
        return GLib.SOURCE_REMOVE

    def toggle_window(self):
        if self.window is None:
            return
        if self.window.get_visible():
            self.window.set_visible(False)
        else:
            self.window.present()

    def refresh_tray(self):
        if self.tray is None or self.window is None:
            return
        summary, attention = self.tray_info
        shown = self.window.get_visible()
        state = (str(attention) + (" need attention" if attention != 1 else " needs attention")) if attention else "Everything looks healthy"
        updating = len(self.active_jobs)
        progress = "Updating " + str(updating) + (" computers…" if updating != 1 else " computer…")
        self.tray.update("Fleetlight · " + (progress if updating else summary + (" · " + state if attention else "")),
                         "updating" if updating else "attention" if attention else "normal", [
            (summary, None),
            (state, (lambda: (self.window.present(), self.show_page(OVERVIEW))) if attention else None),
            *([(progress, lambda: (self.window.present(), self.show_page(OVERVIEW)))] if updating else []),
            None,
            ("Hide Fleetlight" if shown else "Show Fleetlight", self.toggle_window),
            ("Check now", self.check),
            None,
            ("Quit", self.quit_requested)])
        self.publish_status({"running": True, "summary": summary, "attention": attention, "updating": updating, "visible": shown})

    def publish_status(self, status):
        """Write status.json when it changes, and once a minute so readers can tell a live app from a stale file."""
        now = time.time()
        if status == self.status_written[0] and now - self.status_written[1] < 60:
            return
        self.status_written = (status, now)
        try:
            config.atomic_json(self.status_path, dict(status, updated_at=int(now)), indent=None)
        except OSError:
            pass

    def quit_requested(self):
        if self.window is None:
            return
        if self.active_jobs:
            self.window.present()
            self.toast("Updates are running. Keep Fleetlight open to follow progress.")
            return
        self.quit()

    def show_from_notification(self, _action, parameter):
        if self.window is None:
            return
        self.window.present()
        self.show_page(parameter.get_string())

    def build_fleet_widgets(self):
        """Fleet-wide controls live on the overview page but keep their state across renders."""
        self.fleet_actions = box(True, 10)
        buttons = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False,
                              min_children_per_line=1, max_children_per_line=3,
                              column_spacing=8, row_spacing=6)
        self.batch_buttons = {}
        for kind, title in (("cli", "Update all Codex CLI"), ("claude", "Update all Claude CLI"), ("desktop", "Update all ChatGPT"), ("system", "Update all Linux packages"), ("restart", "Restart required computers")):
            button = Gtk.Button(label=title)
            button.connect("clicked", lambda _, selected=kind: self.request_batch(selected))
            buttons.insert(button, -1)
            self.batch_buttons[kind] = button
        self.fleet_actions.append(buttons)
        self.batch_label = label("", "muted", wrap=True)
        self.fleet_actions.append(self.batch_label)
        self.agent_box = margins(box(True, 8), 24)
        self.agent_box.set_margin_top(14)
        self.agent_box.set_margin_bottom(0)
        self.agent_box.set_visible(False)

    def seed_demo_history(self):
        """Fictional trend data for screenshots; never persisted."""
        now = time.time()
        for index, host in enumerate(self.configuration["hosts"]):
            base = self.snapshots.get(host["id"], {})
            rows = []
            for step in range(288):
                moment = now - (287 - step) * 300
                wave = math.sin(step / 9 + index * 1.7) + 0.5 * math.sin(step / 2.3 + index)
                up = 0 if index == 2 and 150 <= step < 156 else 1
                rows.append([moment, up,
                             max(1, (base.get("disk_percent") or 30) - 3 + round(3 * step / 287)) if up else None,
                             max(1, round((base.get("memory_percent") or 30) + 6 * wave)) if up else None,
                             max(1, min(100, round((base.get("cpu_percent") or 20) * (1 + 0.55 * wave)))) if up else None,
                             base.get("load") if up else None,
                             round(base["cpu_temperature"] + 4 * wave, 1) if up and base.get("cpu_temperature") else None,
                             round((base.get("check_ms") or 300) * (1 + 0.2 * wave)) if up else None])
            self.history.series[host["id"]] = rows
            if host.get("local"):
                self.live[host["id"]] = deque(
                    ((now - (149 - step) * 2, max(1, round((base.get("cpu_percent") or 20) * (1 + 0.5 * math.sin(step / 6)) + 5 * math.sin(step / 1.7))),
                      base.get("memory_percent"), base.get("cpu_temperature"), base.get("disk_percent")) for step in range(150)),
                    maxlen=160)
        names = [h["id"] for h in self.configuration["hosts"]]
        self.history.events = [
            {"time": now - 138 * 300, "host": names[2 % len(names)], "message": "SSH connection unavailable"},
            {"time": now - 132 * 300, "host": names[2 % len(names)], "message": "Connection and services healthy"},
            {"time": now - 5400, "host": names[0], "message": "docker: inactive"},
            {"time": now - 4800, "host": names[0], "message": "Connection and services healthy"}]

    def show_page(self, ident):
        if self.window is None:
            return
        self.selected = ident
        if self.search.get_text() or self.attention.get_active():
            self.search.set_text("")
            self.attention.set_active(False)
        else:
            self.populate_hosts()
        if self.split_view.get_collapsed():
            self.split_view.set_show_content(True)

    def show_shortcuts(self):
        builder = Gtk.Builder.new_from_string(SHORTCUTS_UI, -1)
        window = builder.get_object("shortcuts")
        window.set_transient_for(self.window)
        window.present()

    def show_about(self):
        about = Adw.AboutWindow(transient_for=self.window, application_name="Fleetlight",
                                application_icon="io.github.fleetlight.Linux", version=__version__,
                                developer_name="Fleetlight contributors", license_type=Gtk.License.MIT_X11,
                                website=WEBSITE, issue_url=WEBSITE + "/issues",
                                comments="Your computers, services and application versions at a glance. "
                                         "Direct SSH monitoring with a private local history.")
        about.present()

    def host_name(self, ident):
        for host in self.configuration["hosts"]:
            if host["id"] == ident:
                return host["name"]
        for site in sites.configured(self.configuration):
            if site["id"] == ident:
                return site["name"]
        return ident

    def notify_change(self, host_id, previous, current):
        """Desktop notification when a computer gains a problem or recovers. First results are silent."""
        if self.demo or not config.notifications_enabled(self.configuration):
            return
        if not previous or not previous.get("checked_at"):
            return
        before, after = issues(previous), issues(current)
        new_problems = [item for item in after if item not in before]
        if not new_problems and not (before and not after):
            return
        name = self.host_name(host_id)
        if new_problems:
            title = name + " needs attention"
            body = "; ".join(after[:3])
        else:
            title = name + " is healthy again"
            body = "Connection and services are back to normal"
        notification = Gio.Notification.new(title)
        notification.set_body(body)
        notification.set_icon(Gio.ThemedIcon.new("io.github.fleetlight.Linux"))
        notification.set_default_action_and_target("app.show", GLib.Variant.new_string(host_id))
        try:
            self.send_notification("fleetlight-" + host_id, notification)
        except GLib.Error:
            pass

    def reveal_fleet(self, *_):
        if self.window is None:
            return GLib.SOURCE_REMOVE
        self.populate_hosts()
        if self.demo or self._fleet_started:
            return GLib.SOURCE_REMOVE
        self._fleet_started = True
        if self.active_jobs:
            self.watch_job()
        if self.batch and self.batch.get("running"):
            self.advance_batch()
        elif not self.active_jobs:
            self.check()
        return GLib.SOURCE_REMOVE

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=6))

    def auto_check(self):
        if not self.demo:
            self.check(force_updates=False)
        return GLib.SOURCE_CONTINUE

    def check(self, force_updates=True):
        if self.busy or self.demo or self.update_checks_running or self.active_jobs:
            return
        self.force_update_check = force_updates
        self.busy = True
        self.refresh_button.set_sensitive(False)
        self.refresh_button.set_label("Checking…")
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
                self.live.setdefault(host["id"], deque(maxlen=160)).append(
                    (metrics["metrics_checked_at"], metrics.get("cpu_percent"), metrics.get("memory_percent"),
                     metrics.get("cpu_temperature"), metrics.get("disk_percent")))
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
                "metrics_checked_at", "uptime", "disk_percent", "disk_free", "disk_total", "memory_percent",
                "memory_total", "memory_used", "swap_total", "swap_used", "load", "cpu_percent",
                "cpu_temperature", "fan", "battery") if key in current}}
        pending = self.pending_restarts.get(snapshot["id"])
        if pending and pending.get("boot_id") and snapshot.get("status") == "online" and snapshot.get("boot_id") and snapshot["boot_id"] != pending.get("boot_id"):
            self.pending_restarts.pop(snapshot["id"], None)
            self.app_updates.setdefault(snapshot["id"], {})["restart"] = {"state": "current", "checked_at": time.time(), "detail": "Restart verified; computer is online"}
            self.toast(pending["name"] + " restarted and is back online")
            try:
                self.persist_jobs()
            except OSError:
                self.toast("Could not save restart verification")
        self.notify_change(snapshot["id"], current, snapshot)
        self.snapshots[snapshot["id"]] = snapshot
        if snapshot.get("status") == "online":
            self.last_seen[snapshot["id"]] = snapshot
        self.populate_hosts(soon=True)
        return GLib.SOURCE_REMOVE

    def finished(self, warning):
        self.busy = False
        self.refresh_button.set_sensitive(True)
        self.refresh_button.set_label("Check now")
        self.spinner.stop()
        known = {host["id"] for host in self.configuration["hosts"]}
        self.last_seen = {ident: item for ident, item in self.last_seen.items() if ident in known}
        try:
            config.atomic_json(self.last_seen_path, self.last_seen, indent=None)
        except OSError:
            pass
        self.populate_hosts()
        if warning:
            self.toast(warning)
        if self.force_update_check or time.time() - self.last_update_check > 900:
            self.check_application_updates()
        return GLib.SOURCE_REMOVE

    def receive_agents(self, usage):
        usage = usage if isinstance(usage, dict) else {}
        merged = dict(self.agent_usage)
        for name, item in usage.items():
            previous = merged.get(name)
            if isinstance(item, dict) and item.get("state") != "ok" and isinstance(previous, dict) and previous.get("state") == "ok":
                # Keep the last good reading visible and say why it is not fresh.
                merged[name] = dict(previous, stale=item.get("detail") or "Unavailable")
                continue
            if isinstance(item, dict) and item.get("state") == "ok":
                item = dict(item, checked_at=time.time())
                item.pop("stale", None)
            merged[name] = item
        self.notify_quota(self.agent_usage, merged)
        self.agent_usage = merged
        if not self.demo:
            try:
                config.atomic_json(self.agent_usage_path, {name: {k: v for k, v in item.items() if k != "stale"}
                                                           for name, item in merged.items()
                                                           if isinstance(item, dict) and item.get("state") == "ok"})
            except OSError:
                pass
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
        self.agent_box.set_visible(bool(visible))
        if not visible:
            return
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, homogeneous=True)
        for name in visible:
            data = self.agent_usage.get(name) or {"name": name.title(), "state": "checking",
                                                 "detail": "Checking remaining quota…", "remaining_percent": None}
            remaining = data.get("remaining_percent")
            tone = "accent" if remaining is None else "bad" if remaining <= LOW_QUOTA else "warning" if remaining <= 20 else "good"
            card = box(False, 12)
            card.add_css_class("card")
            card.set_hexpand(True)
            ring = self.gauge(("agent", name), remaining, f"{remaining}%" if remaining is not None else "—", tone,
                              size=50, thickness=5, text_size=9)
            ring.set_tooltip_text("Tightest remaining allowance")
            card.append(ring)
            body = box(True, 3)
            body.set_hexpand(True)
            body.set_valign(Gtk.Align.CENTER)
            heading = box(False, 8)
            heading.append(label(data.get("name") or name.title(), "row-title"))
            if data.get("plan"):
                plan = label(str(data["plan"]).upper(), "eyebrow", xalign=1)
                plan.set_hexpand(True)
                plan.set_ellipsize(3)
                heading.append(plan)
            body.append(heading)
            windows = data.get("windows") if isinstance(data.get("windows"), list) else []
            for window in windows[:4]:
                if not isinstance(window, dict) or not isinstance(window.get("remaining_percent"), (int, float)):
                    continue
                left = window["remaining_percent"]
                line = box(False, 8)
                line.append(label(str(window.get("label", "limit")), "small"))
                reset_at = window.get("reset_at")
                if isinstance(reset_at, (int, float)):
                    # Countdowns are worked out now, not when the quota was read; the weekday is enough within a week.
                    moment = time.localtime(reset_at)
                    day = time.strftime("%a %H:%M" if reset_at - time.time() < 6 * 86400 else "%a %d %b", moment)
                    parts = [agent_quota.until(reset_at), day if window.get("reset_day") else ""]
                    full = "Resets " + time.strftime("%A %d %B at %H:%M", moment) + ", in " + agent_quota.until(reset_at)
                else:
                    parts = [window.get("reset") or "", window.get("reset_day") or ""]
                    full = "Resets in " + " · ".join(part for part in parts if part)
                reset = " · ".join(part for part in parts if part)
                if reset:
                    when = label(reset, "small muted numeric", xalign=1)
                    when.set_hexpand(True)
                    when.set_ellipsize(3)
                    when.set_tooltip_text(full)
                    line.append(when)
                body.append(line)
                meter = box(False, 8)
                bar = Gtk.ProgressBar(fraction=max(0, min(1, left / 100)), hexpand=True, valign=Gtk.Align.CENTER)
                bar.add_css_class("thin")
                bar.add_css_class("bad" if left <= LOW_QUOTA else "warning" if left <= 20 else "good")
                meter.append(bar)
                amount = label(f"{left}%", "small numeric", xalign=1)
                amount.set_width_chars(4)
                meter.append(amount)
                body.append(meter)
            note = None
            if data.get("stale"):
                when = time.strftime("%H:%M", time.localtime(data["checked_at"])) if data.get("checked_at") else "earlier"
                note = label(f"Last known at {when} · " + data["stale"], "small warning", wrap=True)
            elif not body.get_first_child().get_next_sibling():
                detail = data.get("detail") or ("Checking remaining quota…" if data.get("state") == "checking" else "Unavailable")
                note = label(detail, "small " + ("warning" if data.get("state") == "unavailable" else "muted"), wrap=True)
            if note is not None:
                note.set_max_width_chars(28)
                body.append(note)
            card.append(body)
            row.append(card)
        self.agent_box.append(row)

    def populate_hosts(self, soon=False):
        """Refresh the sidebar in place and, unless a live refresh asked to keep it, the open page.

        With soon=True the page is rebuilt a moment later, so a burst of results costs one rebuild."""
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
        query = self.search.get_text().casefold()
        filtering = self.attention.get_active()
        attention_total = 0
        host_entries = []
        for host in sorted(hosts, key=lambda host: not host.get("local", False)):
            data, checked, online_host, cached, trouble, tone = self.host_state(host)
            attention_total += bool(trouble)
            if query and query not in host["name"].casefold():
                continue
            if filtering and not trouble:
                continue
            if trouble:
                detail, detail_css = trouble[0], "warning"
            elif cached:
                detail, detail_css = "Last seen " + ago(data.get("checked_at") or 0), "muted"
            elif online_host:
                detail, detail_css = system_label(data) + " · up " + uptime(data.get("uptime")), "muted"
            else:
                detail, detail_css = ("Local computer" if host.get("local") else "Waiting for first check"), "muted"
            host_entries.append({"id": host["id"], "title": host["name"], "detail": detail, "detail_css": detail_css,
                                 "tone": tone, "badge": str(len(trouble)) if online_host and trouble else None,
                                 "usage": self.usage(data) if online_host else None})
        site_entries = []
        for site in watched:
            status = self.site_status.get(site["id"], {})
            trouble = sites.issues(status)
            attention_total += bool(trouble)
            if query and query not in site["name"].casefold():
                continue
            if filtering and not trouble:
                continue
            state = status.get("state")
            site_entries.append({"id": site["id"], "title": site["name"], "detail": status.get("detail") or "Website catalogue",
                                 "detail_css": "warning" if trouble else "muted",
                                 "tone": "good" if state == "ok" else "warning" if state else None})
        entries = []
        if not query:
            entries.append({"id": OVERVIEW, "title": "Fleet overview", "detail": summary, "detail_css": "muted",
                            "icon": "view-grid-symbolic", "badge": str(attention_total) if attention_total else None})
        self.tray_info = (summary, attention_total)
        self.refresh_tray()
        if host_entries:
            entries.append({"header": "Computers"})
        entries += host_entries
        if site_entries:
            entries.append({"header": "Websites"})
        entries += site_entries
        structure = [("header", entry["header"]) if "header" in entry else ("row", entry["id"]) for entry in entries]
        before = self.selected
        visible = [ident for kind, ident in structure if kind == "row"]
        if self.selected not in visible:
            self.selected = visible[0] if visible else None
        self.selecting = True
        try:
            if structure != self.sidebar_structure:
                self.host_list.unselect_all()
                clear(self.host_list)
                self.sidebar_rows = {}
                for entry in entries:
                    if "header" in entry:
                        self.host_list.append(self.sidebar_header(entry["header"]))
                    else:
                        row = self.sidebar_row(entry)
                        self.sidebar_rows[entry["id"]] = row
                        self.host_list.append(row)
                self.sidebar_structure = structure
            else:
                for entry in entries:
                    if "header" not in entry:
                        self.update_sidebar_row(self.sidebar_rows[entry["id"]], entry)
            row = self.sidebar_rows.get(self.selected)
            if row is not None and self.host_list.get_selected_row() is not row:
                self.host_list.select_row(row)
        finally:
            self.selecting = False
        if not visible:
            self.metric_widgets = None
            self.overview_widgets = {}
            self.chart = None
            self.rendered_page = None
            clear(self.content)
            self.page_title.set_title("Fleetlight")
            self.page_title.set_subtitle("")
            self.content.append(Adw.StatusPage(title="No computers match", description="Change the search or attention filter.", icon_name="system-search-symbolic"))
        elif not (self.keep_detail and self.selected == before and self.content.get_first_child() is not None):
            if soon and self.selected == self.rendered_page:
                if not self.render_pending:
                    self.render_pending = GLib.timeout_add(300, self.render_later)
            else:
                self.render_detail()

    def render_later(self):
        self.render_pending = 0
        if self.window is not None and self.selected is not None:
            self.render_detail()
        return GLib.SOURCE_REMOVE

    def sidebar_row(self, entry):
        row = Gtk.ListBoxRow()
        row.host_id = entry["id"]
        body = box(False, 10)
        body.set_margin_top(3)
        body.set_margin_bottom(3)
        if entry.get("icon"):
            leading = Gtk.Image.new_from_icon_name(entry["icon"])
            leading.set_pixel_size(16)
            row.dot = None
        else:
            leading = status_dot()
            row.dot = leading.dot
        leading.set_valign(Gtk.Align.CENTER)
        body.append(leading)
        names = box(True, 2)
        names.set_hexpand(True)
        row.name = label("", "row-title")
        row.name.set_ellipsize(3)
        names.append(row.name)
        row.note = label("")
        row.note.set_ellipsize(3)
        names.append(row.note)
        body.append(names)
        row.bars = MiniBars((None, None, None), (None, None, None))
        row.bars.set_visible(False)
        body.append(row.bars)
        row.badge = label("", "badge warning")
        row.badge.set_valign(Gtk.Align.CENTER)
        row.badge.set_visible(False)
        body.append(row.badge)
        row.set_child(body)
        self.update_sidebar_row(row, entry)
        return row

    def sidebar_header(self, title):
        row = Gtk.ListBoxRow(selectable=False, activatable=False, can_focus=False)
        row.host_id = None
        heading = label(title.upper(), "eyebrow")
        heading.set_margin_top(10)
        heading.set_margin_bottom(2)
        heading.set_margin_start(6)
        row.set_child(heading)
        return row

    def select_host(self, _, row):
        if self.selecting or row is None or not row.get_selectable():
            return
        if row.host_id != self.selected or self.content.get_first_child() is None:
            self.selected = row.host_id
            self.render_detail()

    def render_site(self, site):
        self.metric_widgets = None
        self.overview_widgets = {}
        self.chart = None
        status = self.site_status.get(site["id"], {})
        trouble = sites.issues(status)
        clear(self.content)
        self.page_title.set_title(site["name"])
        self.page_title.set_subtitle(status.get("detail") or "Website")
        state = status.get("state")
        badge_text = "Current" if state == "ok" else "Needs attention" if trouble else "Checking" if self.busy else "Not checked yet"
        badge = label(badge_text, "pill " + ("good" if state == "ok" else "warning" if trouble else "muted"))
        tile = icon_tile("web-browser-symbolic", "good" if state == "ok" else "warning" if trouble else None, large=True)
        hero, _ = self.hero_card(tile, "WEBSITE", site["name"], [site.get("url") or ""],
                                 "attention" if trouble else None if state == "ok" else "quiet", badge)
        self.content.append(hero)
        if trouble:
            self.content.append(self.alert_card(trouble))
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
        card.add(label(status.get("detail") or "Press Check now to check this website.", "muted", wrap=True))
        self.content.append(label("Website checks run with computer checks from this Linux app. They do not use SSH.", "muted", wrap=True))

    def alert_card(self, trouble):
        alert = box(True, 6)
        alert.add_css_class("card")
        heading = box(False, 8)
        icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        icon.add_css_class("warning")
        heading.append(icon)
        heading.append(label("Needs attention", "section-title warning"))
        alert.append(heading)
        for message in trouble[:8]:
            alert.append(label(message, None, wrap=True))
        return alert

    def render_detail(self):
        if self.render_pending:
            GLib.source_remove(self.render_pending)
            self.render_pending = 0
        changed = self.selected != self.rendered_page
        if changed:
            # A new page sweeps its gauges up from zero; the agent strip keeps its place.
            self.gauges = {key: value for key, value in self.gauges.items() if key[0] == "agent"}
        self.rebuilding_detail = True
        try:
            self._render_detail_now()
        finally:
            self.rebuilding_detail = False
        self.rendered_page = self.selected
        if changed and self.window is not None:
            self.scroller.get_vadjustment().set_value(0)
            self.fade = Adw.TimedAnimation.new(self.content, 0.35, 1, 220, Adw.PropertyAnimationTarget.new(self.content, "opacity"))
            self.fade.play()

    def update_open_metrics(self):
        """Apply live readings to the widgets already on screen instead of rebuilding the page."""
        if self.selected == OVERVIEW:
            for host_id, widgets in (self.overview_widgets or {}).items():
                data = self.snapshots.get(host_id, {})
                if data.get("status") != "online":
                    continue
                for key, value, tone in self.usage(data):
                    self.move_gauge(("card", host_id, key), widgets[key], value, f"{value}%" if value is not None else "—", tone)
                foot = self.host_footnote(host_id, data)
                if widgets["foot"].get_text() != foot:
                    widgets["foot"].set_text(foot)
            return
        widgets = self.metric_widgets or {}
        if widgets.get("host") != self.selected:
            return
        data = self.snapshots.get(self.selected, {})
        for key, value, tone in self.usage(data):
            self.move_gauge(("page", self.selected, key), widgets[key], value, f"{value}%" if value is not None else "—", tone)
        hints = self.metric_hints(data)
        for key in ("cpu", "memory", "disk"):
            if widgets[key + "_hint"].get_text() != hints[key]:
                widgets[key + "_hint"].set_text(hints[key])
        temperature = data.get("cpu_temperature")
        if "temperature" in widgets and temperature is not None:
            self.move_gauge(("page", self.selected, "temperature"), widgets["temperature"], temperature,
                            f"{temperature:.0f}°", usage_css(temperature, 75, 90) or "accent")
        if "temperature_hint" in widgets:
            hint = self.temperature_hint(data)
            if widgets["temperature_hint"].get_text() != hint:
                widgets["temperature_hint"].set_text(hint)
        summary = self.host_summary(widgets["hostref"], data)
        if widgets["summary"].get_text() != summary:
            widgets["summary"].set_text(summary)
        if self.chart and self.chart.get("range") == "live":
            self.refresh_chart()

    def host_summary(self, host, data):
        parts = [system_label(data, "This computer" if host.get("local") else "Secure Shell")]
        if data.get("status") == "online" and data.get("uptime") is not None:
            parts.append("up " + uptime(data.get("uptime")))
        parts.append(("Last seen " + ago(data["checked_at"])) if data.get("cached") and data.get("checked_at") else age(data.get("checked_at")))
        return "  ·  ".join(parts)

    def temperature_hint(self, data):
        fan = fan_text(data.get("fan"))
        return "Hottest CPU sensor" + (f" · {fan}" if fan else ", °C")

    def host_footnote(self, host_id, data):
        trouble = issues(data) + linux_update_issues(self.app_updates.get(host_id))
        if trouble:
            return trouble[0]
        parts = ["Healthy", f"load {data.get('load', '—')}"]
        if data.get("cpu_temperature") is not None:
            parts.append(f"{data['cpu_temperature']:.0f} °C")
            if fan_text(data.get("fan")):
                parts.append(fan_text(data.get("fan")))
        elif data.get("cpus"):
            parts.append(f"{data['cpus']} CPUs")
        return " · ".join(parts)

    def _render_detail_now(self):
        self.render_batch()
        if self.selected == OVERVIEW:
            self.render_overview()
            return
        site = next((item for item in sites.configured(self.configuration) if item["id"] == self.selected), None)
        if site is not None:
            self.render_site(site)
            return
        host = next((h for h in self.configuration["hosts"] if h["id"] == self.selected), None)
        if host is None:
            return
        self.render_host(host)

    def render_overview(self):
        self.metric_widgets = None
        self.overview_widgets = {}
        self.chart = None
        hosts = self.configuration["hosts"]
        watched = sites.configured(self.configuration)
        clear(self.content)
        trouble_by_id = {}
        online = 0
        checked_times = []
        groups = {"good": 0, "warning": 0, "bad": 0, None: 0}
        for host in hosts:
            data, checked, online_host, _cached, trouble, tone = self.host_state(host)
            online += checked and online_host
            groups[tone] += 1
            if checked and data.get("checked_at"):
                checked_times.append(data["checked_at"])
            if trouble:
                trouble_by_id[host["id"]] = (host["name"], trouble)
        for site in watched:
            trouble = sites.issues(self.site_status.get(site["id"]))
            if trouble:
                trouble_by_id[site["id"]] = (site["name"], trouble)
        available = sum(len(updates.batch_candidates(hosts, self.snapshots, self.app_updates, kind)[0]) for kind in ("cli", "claude", "desktop", "system"))
        restarts = len(updates.batch_candidates(hosts, self.snapshots, self.app_updates, "restart")[0])
        self.page_title.set_title("Fleet overview")
        self.page_title.set_subtitle(f"{online} of {len(hosts)} online")
        if not checked_times and not self.demo:
            headline = "Checking your computers…"
        elif trouble_by_id:
            count = len(trouble_by_id)
            headline = f"{count} " + ("item needs" if count == 1 else "items need") + " attention"
        else:
            headline = "Everything looks healthy"
        parts = [f"{len(hosts)} computer" + ("s" if len(hosts) != 1 else "")]
        if watched:
            parts.append(f"{len(watched)} website" + ("s" if len(watched) != 1 else ""))
        parts.append(age(max(checked_times)) if checked_times else "Not checked yet")
        ring = FleetRing([(groups["good"], "good"), (groups["warning"], "warning"), (groups["bad"], "bad"), (groups[None], None)],
                         f"{online}/{len(hosts)}", "online")
        ring.set_tooltip_text(f"{groups['good']} healthy · {groups['warning']} need attention · {groups['bad']} unreachable")
        hero, words = self.hero_card(ring, "YOUR FLEET", headline, [" · ".join(parts)],
                                     "offline" if groups["bad"] else "attention" if trouble_by_id else None)
        stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, homogeneous=True)
        stats.set_margin_top(8)
        for icon_name, value, caption, css in (
                ("network-server-symbolic", f"{online}/{len(hosts)}", "online", "good" if online == len(hosts) else "warning"),
                ("dialog-warning-symbolic", str(len(trouble_by_id)), "need attention", "warning" if trouble_by_id else None),
                ("software-update-available-symbolic", str(available), "updates ready", "accent-text" if available else None),
                ("system-reboot-symbolic", str(restarts), "restarts pending", "warning" if restarts else None)):
            stats.append(self.stat(icon_name, value, caption, css))
        words.append(stats)
        self.content.append(hero)
        if trouble_by_id:
            group = self.section("Needs attention", "Select an item to open it")
            for ident, (name, trouble) in trouble_by_id.items():
                row = Adw.ActionRow(title=name, subtitle="; ".join(trouble[:3]), activatable=True)
                row.set_subtitle_lines(2)
                icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
                icon.add_css_class("warning")
                row.add_prefix(icon)
                row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
                row.connect("activated", lambda _, target=ident: self.show_page(target))
                group.add(row)
        heading = box(True, 2)
        heading.append(label("Computers", "section-title"))
        heading.append(label("CPU, memory and disk in use, with reachability over the last 24 hours", "muted", wrap=True))
        self.content.append(heading)
        grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                           min_children_per_line=1, max_children_per_line=2, row_spacing=12, column_spacing=12)
        for host in sorted(hosts, key=lambda host: not host.get("local", False)):
            grid.append(self.host_card(host))
        self.content.append(grid)
        fleet = box(True, 4)
        fleet.append(label("Fleet updates", "section-title"))
        fleet.append(label("Only computers with available releases are included", "muted", wrap=True))
        if self.fleet_actions.get_parent() is not None:
            self.fleet_actions.get_parent().remove(self.fleet_actions)
        fleet.append(self.fleet_actions)
        self.content.append(fleet)
        recent = [e for e in self.history.events if isinstance(e, dict)][-8:]
        if recent:
            activity = self.section("Recent activity", "Status and service changes saved on this computer")
            for event in reversed(recent):
                activity.add(self.event_row(event, self.host_name(event.get("host")), event.get("message", ""), "%a %H:%M"))
        self.content.append(label(f"Full checks every {self.configuration.get('refresh_seconds', 60)} seconds · Application release checks every 15 minutes", "muted", wrap=True))

    def host_card(self, host):
        data, checked, online, cached, trouble, tone = self.host_state(host)
        card = box(True, 12)
        card.add_css_class("card")
        top = box(False, 12)
        top.append(icon_tile(host_icon(host, data), tone))
        names = box(True, 2)
        names.set_hexpand(True)
        names.set_valign(Gtk.Align.CENTER)
        name = label(host["name"], "section-title")
        name.set_ellipsize(3)
        name.set_width_chars(9)
        names.append(name)
        if online:
            subtitle = system_label(data) + " · up " + uptime(data.get("uptime"))
        elif checked:
            subtitle = "Unreachable"
        else:
            subtitle = "Waiting for first check"
        note = label(subtitle, "muted small")
        note.set_ellipsize(3)
        names.append(note)
        top.append(names)
        widgets = {}
        for (key, value, colour), caption in zip(self.usage(data), ("CPU", "MEM", "DISK")):
            holder = box(True, 3)
            ring = self.gauge(("card", host["id"], key), value, f"{value}%" if value is not None else "—", colour,
                              size=46, thickness=5, text_size=8.2)
            holder.append(ring)
            holder.append(label(caption, "eyebrow", xalign=0.5))
            top.append(holder)
            widgets[key] = ring
        card.append(top)
        bottom = box(False, 12)
        if trouble:
            text = trouble[0]
        elif cached:
            text = "Last seen " + ago(data.get("checked_at") or 0) + " · checking now"
        elif online:
            text = self.host_footnote(host["id"], data)
        else:
            text = "—"
        foot = label(text, "small " + ("warning" if trouble else "muted"))
        foot.set_ellipsize(3)
        foot.set_hexpand(True)
        bottom.append(foot)
        now = time.time()
        strip = AvailabilityStrip(self.history.availability(host["id"], now - 86400, now, 24), now - 86400, now, height=8)
        strip.set_hexpand(False)
        strip.set_size_request(132, -1)
        strip.set_valign(Gtk.Align.CENTER)
        bottom.append(strip)
        card.append(bottom)
        widgets["foot"] = foot
        self.overview_widgets[host["id"]] = widgets
        button = Gtk.Button(child=card, tooltip_text="Open " + host["name"])
        button.add_css_class("host-card")
        button.add_css_class("flat")
        button.connect("clicked", lambda *_: self.show_page(host["id"]))
        return button

    def render_host(self, host):
        self.overview_widgets = {}
        self.metric_widgets = None
        self.chart = None
        data, checked, online, cached, trouble, tone = self.host_state(host)
        clear(self.content)
        system_name = system_label(data, "This computer" if host.get("local") else "Secure Shell")
        self.page_title.set_title(host["name"])
        self.page_title.set_subtitle(system_name + " · " + age(data.get("checked_at")))
        summary_label = label(self.host_summary(host, data), "muted", wrap=True)
        status = ("Last seen " + ago(data.get("checked_at") or 0)) if cached else "Online" if online else "Checking" if self.busy else "Unavailable" if checked else "Not checked yet"
        badge = label(status, "pill " + ("muted" if cached else "good" if online else "bad" if checked and not self.busy else "warning"))
        hero, words = self.hero_card(icon_tile(host_icon(host, data), tone, large=True),
                                     "LOCAL COMPUTER" if host.get("local") else "SSH COMPUTER", host["name"], [summary_label],
                                     "offline" if tone == "bad" else "attention" if tone == "warning" else "quiet" if tone is None else None,
                                     badge)
        if online:
            chips = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=8, min_children_per_line=1,
                                row_spacing=6, column_spacing=6, halign=Gtk.Align.START)
            chips.set_margin_top(6)
            facts = [(data.get("hostname"), "computer-symbolic"),
                     ((("Linux " if data.get("os") == "Linux" else "Darwin " if data.get("os") == "Darwin" else "") + data["kernel"]) if data.get("kernel") else None, "emblem-system-symbolic"),
                     (data.get("architecture"), "application-x-firmware-symbolic")]
            network = data.get("network") or {}
            if network.get("interface"):
                facts.append((network["interface"], "network-wireless-symbolic" if network.get("wireless") else "network-wired-symbolic"))
            battery = data.get("battery") or {}
            if battery.get("percent") is not None:
                charging = battery.get("state") in ("charging", "charged", "full", "ac attached", "finishing charge")
                facts.append((f"{battery['percent']}% · {battery.get('state') or 'battery'}",
                              "battery-good-charging-symbolic" if charging else "battery-symbolic"))
            if fan_text(data.get("fan")):
                facts.append((fan_text(data["fan"]).capitalize(), "weather-windy-symbolic"))
            for text, icon_name in facts:
                if text:
                    child = Gtk.FlowBoxChild(child=chip(str(text), icon_name), focusable=False, halign=Gtk.Align.START)
                    chips.append(child)
            words.append(chips)
        self.content.append(hero)
        if trouble:
            self.content.append(self.alert_card(trouble))
        if online:
            gauges = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, homogeneous=True)
            widgets = {"host": host["id"], "summary": summary_label, "hostref": host}
            hints = self.metric_hints(data)
            for (key, value, colour), title in zip(self.usage(data), ("CPU", "MEMORY", "ROOT DISK")):
                ring = self.gauge(("page", host["id"], key), value, f"{value}%" if value is not None else "—", colour,
                                  size=92, thickness=9, text_size=15)
                card, widgets[key + "_hint"] = self.gauge_card(title, ring, hints[key])
                widgets[key] = ring
                gauges.append(card)
            temperature = data.get("cpu_temperature")
            if temperature is not None:
                ring = self.gauge(("page", host["id"], "temperature"), temperature, f"{temperature:.0f}°",
                                  usage_css(temperature, 75, 90) or "accent", size=92, thickness=9, text_size=15)
                widgets["temperature"] = ring
                card, widgets["temperature_hint"] = self.gauge_card("TEMPERATURE", ring, self.temperature_hint(data))
                gauges.append(card)
            else:
                seconds = data.get("check_ms", 0) / 1000
                ring = self.gauge(("page", host["id"], "check"), min(100, 100 * seconds / 5) if seconds else None,
                                  f"{seconds:.1f}s" if seconds else "—", "accent" if seconds < 4 else "warning",
                                  size=92, thickness=9, text_size=15)
                gauges.append(self.gauge_card("CHECK TIME", ring, "Last full check" if seconds else "No verified receipt yet")[0])
            self.content.append(gauges)
            self.metric_widgets = widgets
        elif checked and self.last_seen.get(host["id"]):
            seen = self.last_seen[host["id"]]
            last = self.section("Last seen online", "From the most recent successful check")
            when = seen.get("checked_at")
            self.detail_row(last, "When", (time.strftime("%a %d %b %H:%M", time.localtime(when)) + " · " + ago(when)) if when else "Unknown",
                            "preferences-system-time-symbolic")
            self.detail_row(last, "System", system_label(seen, "Unknown"), "computer-symbolic")
            if seen.get("hostname"):
                self.detail_row(last, "Hostname", seen["hostname"], "network-server-symbolic")
            if (seen.get("network") or {}).get("mac") and not host.get("local"):
                row = Adw.ActionRow(title="Wake-on-LAN", subtitle="Sends a wake signal on this network. The computer must allow it in its firmware and be on the same local network.")
                row.add_prefix(Gtk.Image.new_from_icon_name("system-shutdown-symbolic"))
                wake = Gtk.Button(label="Wake", valign=Gtk.Align.CENTER)
                wake.add_css_class("suggested-action")
                wake.set_sensitive(not self.demo)
                wake.connect("clicked", lambda *_: self.wake(host))
                row.add_suffix(wake)
                last.add(row)
        if online or self.history.series.get(host["id"]):
            self.content.append(self.history_card(host))
        if online and len(data.get("disks") or []) > 1:
            storage = self.section("Storage", "Mounted local filesystems")
            for disk in data["disks"][:8]:
                if not isinstance(disk, dict) or not isinstance(disk.get("percent"), (int, float)):
                    continue
                row = Adw.ActionRow(title=GLib.markup_escape_text(str(disk.get("mount", "?"))),
                                    subtitle=size_text(disk.get("free")) + " free of " + size_text(disk.get("total")))
                row.add_prefix(Gtk.Image.new_from_icon_name("drive-harddisk-symbolic"))
                bar = Gtk.ProgressBar(fraction=max(0, min(1, disk["percent"] / 100)), valign=Gtk.Align.CENTER)
                bar.set_size_request(150, -1)
                css = usage_css(disk["percent"], 80, 90)
                if css:
                    bar.add_css_class(css)
                row.add_suffix(bar)
                amount = label(f"{disk['percent']}%", "numeric " + (css or "muted"), xalign=1)
                amount.set_width_chars(4)
                amount.set_valign(Gtk.Align.CENTER)
                row.add_suffix(amount)
                storage.add(row)
        processes = data.get("processes") if online and isinstance(data.get("processes"), dict) else None
        if processes and (processes.get("cpu") or processes.get("memory")):
            self.content.append(self.process_card(processes))
        installed = data if online else {}
        apps = self.section("Applications", "Installed and available versions")
        self.update_row(apps, host, "cli", "Codex CLI", installed.get("codex"), "utilities-terminal-symbolic")
        self.update_row(apps, host, "claude", "Claude CLI", installed.get("claude"), "utilities-terminal-symbolic")
        desktop = installed.get("chatgpt", {})
        self.update_row(apps, host, "desktop", "ChatGPT", desktop.get("version"), "web-browser-symbolic")
        active = self.job_for_host(host["id"])
        if active:
            progress = Gtk.ProgressBar()
            progress.pulse()
            apps.add(progress)
            apps.add(label(active.get("phase", "Preparing update"), "good", wrap=True))
        self.update_history(apps, host)
        if data.get("os") == "Linux":
            system_card = self.section("Linux updates", "Distribution packages and restart status")
            checked_updates = self.app_updates.get(host["id"], {})
            for kind, title, icon_name in (("system", "System packages", "software-update-available-symbolic"), ("restart", "Restart", "system-reboot-symbolic")):
                status = checked_updates.get(kind, {})
                self.detail_row(system_card, title, status.get("detail", "Checking…"), icon_name,
                                "warning" if status.get("state") in ("available", "protected", "unknown") else "muted")
        services = self.section("Services", "Configured system services")
        states = data.get("services", {}) if online else {}
        for name in host.get("services", []):
            state = states.get(name, "not checked")
            optional = name in host.get("optional_services", [])
            neutral = state in ("unsupported", "not checked") or (optional and state in ("inactive", "not installed"))
            row = Adw.ActionRow(title=name, subtitle="Optional" if optional else "Expected to run")
            row.add_prefix(status_dot("good" if state == "active" else None if neutral else "warning"))
            state_label = label(state, "good" if state == "active" else "muted" if neutral else "warning")
            state_label.set_valign(Gtk.Align.CENTER)
            row.add_suffix(state_label)
            required = Gtk.CheckButton(label="Warn when stopped")
            required.set_valign(Gtk.Align.CENTER)
            required.set_active(not optional)
            required.set_sensitive(not self.demo and not self.busy and not self.update_checks_running and not self.active_jobs)
            required.connect("toggled", lambda button, service=name: self.service_preference(host, service, button.get_active()))
            row.add_suffix(required)
            services.add(row)
        failed = data.get("failed_units") if online else None
        if isinstance(failed, list):
            row = Adw.ActionRow(title="Failed system units",
                                subtitle=GLib.markup_escape_text(", ".join(str(unit) for unit in failed)) if failed else "None; systemd reports every unit healthy")
            row.set_subtitle_lines(3)
            row.add_prefix(status_dot("warning" if failed else "good"))
            count = label(str(len(failed)), "numeric " + ("warning" if failed else "muted"))
            count.set_valign(Gtk.Align.CENTER)
            row.add_suffix(count)
            services.add(row)
        if not host.get("services") and not isinstance(failed, list):
            services.add(label("No services configured. Add systemd unit names in Settings.", "muted", wrap=True))
        controls = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, min_children_per_line=1,
                               max_children_per_line=4, row_spacing=8, column_spacing=8)
        terminal = icon_button("Open terminal", "utilities-terminal-symbolic")
        terminal.connect("clicked", lambda *_: self.terminal(host))
        controls.append(terminal)
        files = icon_button("Browse files", "folder-symbolic")
        files.connect("clicked", lambda *_: self.browse(host))
        controls.append(files)
        copy = icon_button("Copy diagnostics", "edit-copy-symbolic")
        copy.connect("clicked", lambda *_: self.copy_diagnostics(host, data))
        controls.append(copy)
        if online and data.get("package_manager") in actions.UPDATE_COMMANDS:
            update = icon_button("System updates…", "software-update-available-symbolic")
            update.connect("clicked", lambda *_: self.confirm_update(host, data["package_manager"]))
            update.set_sensitive(not self.active_jobs and not self.update_checks_running)
            controls.append(update)
        if self.demo:
            controls.set_sensitive(False)
        self.content.append(controls)
        recent = [e for e in self.history.events if isinstance(e, dict) and e.get("host") == host["id"]][-5:]
        if recent:
            activity = self.section("Recent changes", "Saved on this computer")
            for event in reversed(recent):
                activity.add(self.event_row(event, event.get("message", ""), "", "%a %d %b %H:%M"))
        self.content.append(label(("Local metrics every 2 seconds · " if host.get("local") else "") +
                                  f"Full checks every {self.configuration.get('refresh_seconds', 60)} seconds · " +
                                  (f"Last check took {data['check_ms'] / 1000:.1f}s" if data.get("check_ms") else "No verified receipt yet"), "muted", wrap=True))

    def section(self, title, subtitle):
        group = Adw.PreferencesGroup(title=title, description=subtitle)
        self.content.append(group)
        return group

    def detail_row(self, parent, name, value, icon, css=None):
        row = Adw.ActionRow(title=name)
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        value_label = label(value, css or "muted", xalign=1, wrap=True)
        value_label.set_max_width_chars(40)
        # Without this GTK prefers a squarish block and breaks short values over several lines.
        value_label.set_natural_wrap_mode(Gtk.NaturalWrapMode.NONE)
        value_label.set_justify(Gtk.Justification.RIGHT)
        value_label.set_valign(Gtk.Align.CENTER)
        row.add_suffix(value_label)
        attach(parent, row)

    def update_row(self, parent, host, kind, name, installed, icon):
        checked = self.app_updates.get(host["id"], {}).get(kind, {})
        state = checked.get("state", "checking" if self.update_checks_running else "unknown")
        latest = checked.get("latest")
        versions = (installed or checked.get("installed") or "Not detected") + (" → " + latest if state == "available" else "")
        detail = checked.get("detail", "Checking for updates…" if self.update_checks_running else "Press Check now to check releases")
        if state == "current":
            detail = "Up to date" + (" · " + checked["provider"] if checked.get("provider") else "")
        if state == "protected":
            detail = "Protected · " + detail
        row = Adw.ActionRow(title=name, subtitle=versions + "\n" + detail)
        row.set_subtitle_lines(3)
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        if state == "available":
            button = Gtk.Button(label="Update")
            button.add_css_class("suggested-action")
            button.set_valign(Gtk.Align.CENTER)
            button.set_sensitive(not self.demo and not self.busy and not self.update_checks_running and not self.active_jobs)
            button.connect("clicked", lambda *_: self.request_update(host, kind, checked))
            row.add_suffix(button)
        elif state == "current":
            current = label("Current", "good")
            current.set_valign(Gtk.Align.CENTER)
            row.add_suffix(current)
        elif state in ("protected", "unknown", "unsupported"):
            flag = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
            flag.add_css_class("warning")
            row.add_suffix(flag)
        attach(parent, row)

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
        if self.update_checks_running or self.active_jobs or self.demo:
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
        blocked = self.demo or self.busy or self.update_checks_running or bool(self.active_jobs)
        for kind, button in self.batch_buttons.items():
            candidates, _ = updates.batch_candidates(self.configuration["hosts"], self.snapshots, self.app_updates, kind)
            title = "Restart required computers" if kind == "restart" else "Update all " + ACTION_NAMES[kind]
            button.set_label(title + " (" + str(len(candidates)) + ")")
            button.set_sensitive(not blocked and bool(candidates))
            button.set_tooltip_text("Run Check now to refresh available releases" if not candidates else
                                    "Review computers and restart one at a time" if kind == "restart" else
                                    "Review computers and update up to three at a time")
        running = bool(self.batch and self.batch.get("running"))
        text = ""
        if self.batch:
            done = len(self.batch.get("results", []))
            total = self.batch.get("total", 0)
            text = ("Automatic " if self.batch.get("automatic") else "") + ACTION_NAMES[self.batch["kind"]] + " fleet updates: " + str(done) + "/" + str(total) + " completed"
            if running and self.active_jobs:
                text += " · " + str(len(self.active_jobs)) + " running: " + "; ".join(
                    job["host"]["name"] + " · " + job.get("phase", "Updating")
                    for job in self.active_jobs.values())
            elif self.batch.get("stopped"):
                text += " · " + self.batch["stopped"]
            if self.pending_restarts:
                text += " · Awaiting restart verification: " + ", ".join(item["name"] for item in self.pending_restarts.values())
            self.batch_label.set_text(text)
        elif config.auto_updates_enabled(self.configuration):
            self.batch_label.set_text("Automatic updates are on · Codex CLI, Claude CLI, ChatGPT and Linux packages install when checks find them. Computers are not restarted.")
        else:
            self.batch_label.set_text("Fleet-wide updates · only computers with available releases are included")
        if running and self.active_jobs:
            self.banner.set_title(text)
            self.banner.set_button_label("Stop queued updates" if self.batch.get("pending") else None)
            self.banner.set_revealed(True)
        elif self.active_jobs:
            job = next(iter(self.active_jobs.values()))
            self.banner.set_title("Updating " + ACTION_NAMES.get(job.get("kind"), "software") + " on " + job["host"]["name"] + " · " + job.get("phase", "Working"))
            self.banner.set_button_label(None)
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)

    def request_batch(self, kind):
        if self.demo or self.busy or self.update_checks_running or self.active_jobs:
            return
        pending, skipped = updates.batch_candidates(self.configuration["hosts"], self.snapshots, self.app_updates, kind)
        if not pending:
            self.toast("No eligible updates. Run Check now to refresh releases.")
            return
        title = ACTION_NAMES[kind]
        body = ("Restart these computers one at a time:\n\n" if kind == "restart" else
                "Update " + title + " on up to three computers at a time:\n\n")
        body += "\n".join(item["host"]["name"] + (" → " + item["checked"]["latest"] if kind in ("cli", "claude", "desktop") else " · " + item["checked"].get("detail", "")) for item in pending)
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
        body += "\n\nThe batch stops starting new updates if one fails. Updates already running will finish. You can stop queued updates without interrupting installers."
        dialog = Adw.MessageDialog(transient_for=self.window, heading="Restart required computers?" if kind == "restart" else "Update all " + title + "?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("update", ("Restart " if kind == "restart" else "Update ") + str(len(pending)) + " computers")
        dialog.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _, response: self.begin_batch(kind, pending) if response == "update" else None)
        dialog.present()

    def begin_batch(self, kind, pending, automatic=False):
        if self.active_jobs or self.busy or self.update_checks_running:
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
        if self.busy or self.update_checks_running or self.active_jobs:
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
        if not self.batch or not self.batch.get("running"):
            return
        # Restarts stay sequential so the local controller can be scheduled last.
        limit = 1 if self.batch["kind"] == "restart" else updates.MAX_PARALLEL_UPDATES
        while self.batch.get("pending") and len(self.active_jobs) < limit:
            # Never overlap installers on one computer, even in a recovered queue.
            index = next((i for i, item in enumerate(self.batch["pending"])
                          if not self.job_for_host(item["host"]["id"])), None)
            if index is None:
                break
            item = self.batch["pending"].pop(index)
            # Save the new job and the remaining queue atomically before dispatch.
            ident = self.begin_update(item["host"], self.batch["kind"], item["checked"], from_batch=True)
            if not ident:
                self.batch["pending"].insert(index, item)
                self.batch["running"] = False
                self.batch["stopped"] = "Could not start the next update; retry after checking"
                try:
                    self.persist_jobs()
                except OSError:
                    self.toast("Could not save the stopped queue")
                self.render_batch()
                return
        if self.active_jobs:
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
        if self.active_jobs or self.update_checks_running or self.busy or self.demo:
            return
        if checked.get("state") != "available" or time.time() - checked.get("checked_at", 0) > 1800:
            self.toast("Run Check now before updating")
            return
        if kind in ("cli", "claude"):
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
        active = self.job_for_host(host["id"])
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
        attach(parent, expander)

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

    def job_for_host(self, host_id):
        return next((job for job in self.active_jobs.values() if job["host"]["id"] == host_id), None)

    def persist_jobs(self):
        self.refresh_tray()
        config.atomic_json(self.journal_path, {"active_jobs": self.active_jobs, "last_jobs": self.last_jobs,
                          "batch": self.batch, "pending_restarts": self.pending_restarts})

    def begin_update(self, host, kind, checked, from_batch=False):
        if self.update_checks_running or self.busy or self.job_for_host(host["id"]):
            return
        if self.active_jobs and not from_batch:
            return
        limit = 1 if kind == "restart" else updates.MAX_PARALLEL_UPDATES
        if len(self.active_jobs) >= limit:
            return
        ident = uuid.uuid4().hex
        self.active_jobs[ident] = {"id": ident, "host": dict(host), "kind": kind, "target": checked["latest"],
                           "boot_id": self.snapshots.get(host["id"], {}).get("boot_id"),
                           "state": "queued", "phase": "Starting " + ACTION_NAMES[kind],
                           "auto_key": list(updates.auto_target_key(host, kind, checked))}
        try:
            self.persist_jobs()
        except OSError:
            self.active_jobs.pop(ident)
            self.toast("Update was not started: its recovery receipt could not be saved")
            return
        self.last_jobs.pop(host["id"], None)
        self.refresh_button.set_sensitive(False)
        self.spinner.start()
        self.render_detail()
        self.job_polls.add(ident)
        def start():
            try:
                receipt = updates.start_job(host, kind, checked, ident)
            except Exception as error:
                receipt = {"state": "failed", "phase": "Update could not start: " + str(error)}
            GLib.idle_add(self.receive_job, ident, receipt)
        threading.Thread(target=start, daemon=True).start()
        return ident

    def watch_job(self, ident=None):
        for job_id in ([ident] if ident else list(self.active_jobs)):
            if job_id not in self.active_jobs or job_id in self.job_polls:
                continue
            job = dict(self.active_jobs[job_id])
            self.refresh_button.set_sensitive(False)
            self.spinner.start()
            self.job_polls.add(job_id)
            def poll(job=job):
                try:
                    receipt = updates.job_status(job["host"], job["id"])
                except Exception:
                    receipt = {"state": "disconnected", "phase": "Status check failed; checking the existing job again"}
                GLib.idle_add(self.receive_job, job["id"], receipt)
            threading.Thread(target=poll, daemon=True).start()
        return GLib.SOURCE_REMOVE

    def receive_job(self, ident, receipt):
        self.job_polls.discard(ident)
        job = self.active_jobs.get(ident)
        if job is None:
            return GLib.SOURCE_REMOVE
        if receipt.get("id", ident) != ident:
            GLib.timeout_add_seconds(3, self.watch_job, ident)
            return GLib.SOURCE_REMOVE
        job.update({k:v for k,v in receipt.items() if k not in ("host", "id", "kind")})
        state = receipt.get("state")
        if state in ("succeeded", "failed", "busy", "interrupted", "unknown"):
            if state == "succeeded" and job["kind"] == "restart":
                host = job["host"]
                self.pending_restarts[host["id"]] = {"name": host["name"], "boot_id": job.get("boot_id"), "scheduled_at": time.time()}
                if host["id"] in self.app_updates:
                    self.app_updates[host["id"]]["restart"] = {"state": "scheduled", "detail": "Waiting for new boot"}
            if state != "succeeded":
                key = job.get("auto_key")
                if isinstance(key, list) and len(key) >= 2:
                    self.auto_attempted.add(tuple(key))
            if self.batch and self.batch.get("running"):
                self.batch.setdefault("results", []).append({"host": job["host"]["name"], "state": state})
                if state != "succeeded":
                    if self.batch.get("automatic"):
                        self.batch["stopped"] = "Continuing after " + job["host"]["name"] + ": " + receipt.get("phase", state)
                    else:
                        self.batch["pending"] = []
                        self.batch["stopped"] = "Stopped after " + job["host"]["name"] + ": " + receipt.get("phase", state)
            self.last_jobs[job["host"]["id"]] = dict(job)
            self.update_history_expanded[(job["host"]["id"], "install-history")] = True
            self.refresh_install_history(job["host"])
            self.toast(receipt.get("phase", "Update completed"))
            self.active_jobs.pop(ident)
            if not self.active_jobs:
                self.spinner.stop()
                self.refresh_button.set_sensitive(True)
        try:
            self.persist_jobs()
        except OSError:
            self.toast("Could not save the latest update receipt; keep Fleetlight open")
        self.render_detail()
        if ident in self.active_jobs:
            GLib.timeout_add_seconds(3 if state != "disconnected" else 10, self.watch_job, ident)
        if self.batch and self.batch.get("running"):
            self.advance_batch()
        elif not self.active_jobs:
            self.check()
        return GLib.SOURCE_REMOVE

    def close_requested(self, *_):
        if self.in_tray:
            # Closing only hides the window: checks, notifications and updates carry on behind the tray icon.
            self.window.set_visible(False)
            return True
        if self.active_jobs:
            self.toast("Updates are running. Keep Fleetlight open to follow progress.")
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
        dialog = Adw.Window(transient_for=self.window, modal=True, title="Add computer", default_width=500, default_height=520)
        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: dialog.close())
        header.pack_start(cancel)
        save = Gtk.Button(label="Add")
        save.add_css_class("suggested-action")
        save.set_sensitive(not self.demo)
        header.pack_end(save)
        view.add_top_bar(header)
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="SSH computer", description="Uses your existing SSH keys and known hosts. Verify the connection in a terminal first; Fleetlight never accepts unknown host keys.")
        entries = {}
        for key, title in (("name", "Display name"), ("alias", "SSH alias or user@host"), ("services", "Services, comma-separated")):
            entries[key] = Adw.EntryRow(title=title)
            group.add(entries[key])
        page.add(group)
        check_group = Adw.PreferencesGroup(title="Connection test", description="Runs a read-only check over SSH without saving anything.")
        test_line = box(False, 10)
        test = Gtk.Button(label="Test connection", halign=Gtk.Align.START)
        test.set_sensitive(not self.demo)
        test_line.append(test)
        result = label("", "muted", wrap=True)
        result.set_valign(Gtk.Align.CENTER)
        test_line.append(result)
        check_group.add(test_line)
        page.add(check_group)
        view.set_content(page)
        dialog.set_content(view)

        def host_from_form():
            return {"id": "host-" + uuid.uuid4().hex[:8], "name": entries["name"].get_text().strip(),
                    "alias": entries["alias"].get_text().strip(),
                    "services": [s.strip() for s in entries["services"].get_text().split(",") if s.strip()]}

        def show_result(text, css):
            for name in ("good", "warning", "muted"):
                result.remove_css_class(name)
            result.add_css_class(css)
            result.set_text(text)
            test.set_sensitive(True)
            return GLib.SOURCE_REMOVE

        def run_test(*_):
            host = host_from_form()
            try:
                config.validate({"version": 1, "hosts": [host]})
            except ValueError as problem:
                show_result(str(problem), "warning")
                return
            test.set_sensitive(False)
            show_result("Connecting…", "muted")
            test.set_sensitive(False)

            def work():
                snapshot = probe_host(host)
                if snapshot.get("status") == "online":
                    text = "Connected · " + system_label(snapshot, "online") + f" · {snapshot.get('check_ms', 0) / 1000:.1f}s"
                    GLib.idle_add(show_result, text, "good")
                else:
                    GLib.idle_add(show_result, snapshot.get("error") or "Connection failed", "warning")
            threading.Thread(target=work, daemon=True).start()
        test.connect("clicked", run_test)

        def add(*_):
            if self.busy or self.update_checks_running or self.active_jobs:
                show_result("Wait for the current check to finish", "warning")
                return
            host = host_from_form()
            candidate = {**self.configuration, "hosts": self.configuration["hosts"] + [host]}
            try:
                config.atomic_json(self.config_file or config.config_path(), config.validate(candidate))
            except (ValueError, OSError) as problem:
                show_result(str(problem), "warning")
                return
            self.configuration = candidate
            self.selected = host["id"]
            dialog.close()
            self.populate_hosts()
            self.check()
        save.connect("clicked", add)
        for entry in entries.values():
            entry.connect("entry-activated", add)
        dialog.present()

    def settings(self, *_):
        dialog = Adw.Window(transient_for=self.window, modal=True, title="Fleetlight settings", default_width=680, default_height=760)
        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: dialog.close())
        header.pack_start(cancel)
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        header.pack_end(save)
        view.add_top_bar(header)
        page = Adw.PreferencesPage()
        working = json.loads(json.dumps(self.configuration))
        initial = {"refresh_seconds": self.configuration.get("refresh_seconds", 60),
                   "appearance": config.appearance(self.configuration),
                   "notifications": config.notifications_enabled(self.configuration),
                   "auto_updates": config.auto_updates_enabled(self.configuration),
                   "agents": config.enabled_agents(self.configuration)}

        monitoring = Adw.PreferencesGroup(title="Monitoring", description="Full checks run for every computer on this interval. The local computer also refreshes live metrics every two seconds.")
        interval = Adw.SpinRow.new_with_range(15, 3600, 15)
        interval.set_title("Check interval")
        interval.set_subtitle("Seconds between full checks")
        interval.set_value(initial["refresh_seconds"])
        monitoring.add(interval)
        notify = Adw.SwitchRow(title="Desktop notifications", subtitle="Notify when a computer goes offline, a service stops, the problem clears, or an agent's quota runs low")
        notify.set_active(initial["notifications"])
        monitoring.add(notify)
        start = Adw.SwitchRow(title="Open Fleetlight when I log in", subtitle="Adds a user autostart entry; useful with automatic updates")
        start.set_active(actions.autostart_path().exists())
        monitoring.add(start)
        page.add(monitoring)

        looks = Adw.PreferencesGroup(title="Appearance")
        scheme = Adw.ComboRow(title="Colour scheme", subtitle="Automatic is dark unless the desktop prefers light. The accent comes from the desktop theme.",
                              model=Gtk.StringList.new(["Automatic", "Dark", "Light"]))
        scheme.set_selected(config.APPEARANCES.index(initial["appearance"]))
        looks.add(scheme)
        page.add(looks)

        automatic = Adw.PreferencesGroup(title="Automatic updates", description="A failed computer is skipped and the same update is not retried until you restart Fleetlight or a different set of packages appears. Keep Fleetlight open.")
        auto_toggle = Adw.SwitchRow(title="Automatically install all available updates", subtitle="Codex CLI, Claude CLI, ChatGPT and Linux packages. Computers are never restarted automatically.")
        auto_toggle.set_active(initial["auto_updates"])
        automatic.add(auto_toggle)
        page.add(automatic)

        agents_group = Adw.PreferencesGroup(title="Agent quota", description="Show remaining allowance from this computer’s signed-in sessions at the top of every page.")
        agent_toggles = {}
        for name, title in (("codex", "Codex"), ("cursor", "Cursor"), ("claude", "Claude")):
            toggle = Adw.SwitchRow(title="Show " + title)
            toggle.set_active(initial["agents"][name])
            agent_toggles[name] = toggle
            agents_group.add(toggle)
        page.add(agents_group)

        computers = Adw.PreferencesGroup(title="Computers", description="Remove a computer here or add one with the + button. Names and services can be edited in the configuration file below.")
        computer_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        computer_list.add_css_class("boxed-list")
        computers.add(computer_list)
        page.add(computers)
        websites = Adw.PreferencesGroup(title="Websites", description="HTTPS JSON status documents checked from this computer.")
        website_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        website_list.add_css_class("boxed-list")
        websites.add(website_list)
        page.add(websites)

        def fill_lists():
            clear(computer_list)
            for host in working["hosts"]:
                services = ", ".join(host.get("services", []))
                subtitle = ("This computer" if host.get("local") else host.get("alias", "")) + ((" · " + services) if services else "")
                row = Adw.ActionRow(title=host["name"], subtitle=subtitle)
                row.add_prefix(Gtk.Image.new_from_icon_name("computer-symbolic" if host.get("local") else "network-server-symbolic"))
                if not host.get("local"):
                    remove = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove " + host["name"], valign=Gtk.Align.CENTER)
                    remove.add_css_class("flat")
                    remove.connect("clicked", lambda _, target=host: (working["hosts"].remove(target), fill_lists()))
                    row.add_suffix(remove)
                computer_list.append(row)
            clear(website_list)
            for site in working.get("sites", []):
                row = Adw.ActionRow(title=site["name"], subtitle=site.get("url", ""))
                row.add_prefix(Gtk.Image.new_from_icon_name("web-browser-symbolic"))
                remove = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove " + site["name"], valign=Gtk.Align.CENTER)
                remove.add_css_class("flat")
                remove.connect("clicked", lambda _, target=site: (working["sites"].remove(target), fill_lists()))
                row.add_suffix(remove)
                website_list.append(row)
            websites.set_visible(bool(working.get("sites")))
        fill_lists()

        advanced = Adw.PreferencesGroup(title="Configuration file", description="The full private configuration as JSON. When edited, it replaces the lists above on save.")
        expander = Adw.ExpanderRow(title="Edit fleet.json", subtitle=str(self.config_file or config.config_path()))
        editor = Gtk.TextView(monospace=True, wrap_mode=Gtk.WrapMode.NONE)
        margins(editor, 8)
        editor.get_buffer().set_text(json.dumps(self.configuration, indent=2))
        json_edited = [False]
        editor.get_buffer().connect("changed", lambda *_: json_edited.__setitem__(0, True))
        scroll = Gtk.ScrolledWindow(min_content_height=300, vexpand=True)
        scroll.set_child(editor)
        expander.add_row(scroll)
        advanced.add(expander)
        error = label("", "warning", wrap=True)
        advanced.add(error)
        page.add(advanced)
        view.set_content(page)
        dialog.set_content(view)

        def apply(*_):
            if self.busy or self.update_checks_running or self.active_jobs:
                error.set_text("Wait for the current check to finish")
                return
            try:
                if json_edited[0]:
                    buffer = editor.get_buffer()
                    candidate = json.loads(buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True))
                    if not isinstance(candidate, dict):
                        raise ValueError("Configuration must be a JSON object")
                else:
                    candidate = working
                chosen = {"refresh_seconds": int(interval.get_value()), "notifications": notify.get_active(),
                          "appearance": config.APPEARANCES[scheme.get_selected()],
                          "auto_updates": auto_toggle.get_active(),
                          "agents": {name: toggle.get_active() for name, toggle in agent_toggles.items()}}
                for key, value in chosen.items():
                    # A JSON edit wins unless the matching control was changed in this dialog.
                    if key == "appearance" and value == initial[key] and key not in candidate:
                        continue
                    if not json_edited[0] or value != initial[key]:
                        candidate[key] = value
                candidate = config.validate(candidate)
                config.atomic_json(self.config_file or config.config_path(), candidate)
                if start.get_active() != actions.autostart_path().exists():
                    actions.set_autostart(start.get_active())
            except (ValueError, OSError, RuntimeError) as problem:
                error.set_text(str(problem))
                return
            enabling = config.auto_updates_enabled(candidate) and not config.auto_updates_enabled(self.configuration)
            self.configuration = candidate
            Adw.StyleManager.get_default().set_color_scheme(SCHEMES[config.appearance(candidate)])
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
            if self.selected not in {h["id"] for h in candidate["hosts"]} | {s["id"] for s in sites.configured(candidate)} | {OVERVIEW}:
                self.selected = OVERVIEW
            self.populate_hosts()
            self.render_agents()
            self.check()
        save.connect("clicked", apply)
        if self.demo:
            save.set_sensitive(False)
        dialog.present()

    def jump(self, index):
        """Alt+number opens the computer at that position in the sidebar."""
        hosts = sorted(self.configuration["hosts"], key=lambda host: not host.get("local", False))
        if self.window is not None and index < len(hosts):
            self.show_page(hosts[index]["id"])

    def view(self, host_id):
        """Result to display: this session's check, or the last online result until one arrives."""
        data = self.snapshots.get(host_id)
        if data:
            return data
        seen = self.last_seen.get(host_id)
        return dict(seen, cached=True) if seen else {}

    def host_state(self, host):
        """Display facts shared by the sidebar, the overview cards and the computer page."""
        data = self.view(host["id"])
        cached = bool(data.get("cached"))
        checked = bool(data) and not cached
        online = data.get("status") == "online"
        trouble = (issues(data) if checked else []) + linux_update_issues(self.app_updates.get(host["id"]))
        tone = None if not checked else "good" if online and not trouble else "warning" if online else "bad"
        return data, checked, online, cached, trouble, tone

    def usage(self, data):
        """CPU, memory and root disk as (key, percent, colour) for gauges."""
        result = []
        for key, value, warn, bad in (("cpu", cpu_share(data), 75, 92), ("memory", data.get("memory_percent"), 80, 95),
                                      ("disk", data.get("disk_percent"), 80, 90)):
            value = value if data.get("status") == "online" and isinstance(value, (int, float)) else None
            result.append((key, value, usage_css(value, warn, bad) or "accent"))
        return result

    def gauge(self, key, percent, text, tone="accent", **shape):
        """Ring that starts from the value it last showed on this page, so refreshes glide."""
        ring = Ring(None if percent is None else percent / 100, text, tone, start=self.gauges.get(key, 0.0), **shape)
        self.gauges[key] = ring.target
        return ring

    def move_gauge(self, key, ring, percent, text, tone="accent"):
        """Live reading for a gauge already on screen; it steps rather than glides to keep idle CPU low."""
        ring.set_value(None if percent is None else percent / 100, text, tone, animate=False)
        self.gauges[key] = ring.target

    def notify_quota(self, before, after):
        """One desktop notification when an agent's tightest quota window drops to the low mark."""
        for name, item in after.items():
            remaining = item.get("remaining_percent") if isinstance(item, dict) else None
            if not isinstance(remaining, (int, float)) or item.get("stale"):
                continue
            if remaining > LOW_QUOTA:
                self.quota_warned.discard(name)
                continue
            previous = (before.get(name) or {}).get("remaining_percent")
            if name in self.quota_warned or not isinstance(previous, (int, float)) or previous <= LOW_QUOTA:
                self.quota_warned.add(name)
                continue
            self.quota_warned.add(name)
            if self.demo or not config.notifications_enabled(self.configuration):
                continue
            notification = Gio.Notification.new((item.get("name") or name.title()) + " quota is running low")
            notification.set_body(item.get("detail") or f"{remaining}% left")
            notification.set_icon(Gio.ThemedIcon.new("io.github.fleetlight.Linux"))
            try:
                self.send_notification("fleetlight-quota-" + name, notification)
            except GLib.Error:
                pass

    def update_sidebar_row(self, row, entry):
        if row.name.get_text() != entry["title"]:
            row.name.set_text(entry["title"])
        if row.note.get_text() != entry["detail"]:
            row.note.set_text(entry["detail"])
        set_tone(row.note, entry["detail_css"])
        if row.dot is not None:
            set_tone(row.dot, entry.get("tone"))
        badge = entry.get("badge")
        row.badge.set_visible(bool(badge))
        if badge and row.badge.get_text() != badge:
            row.badge.set_text(badge)
        usage = entry.get("usage")
        row.bars.set_visible(bool(usage) and not badge)
        if usage:
            row.bars.update(tuple(value for _, value, _ in usage),
                            tuple(tone for _, _, tone in usage))
            row.bars.set_tooltip_text(" · ".join(
                f"{title} {value}%" for title, (_, value, _) in zip(("CPU", "Memory", "Disk"), usage) if value is not None))

    def hero_card(self, leading, eyebrow, title, lines, tone=None, trailing=None):
        """Tinted banner at the top of a page: `tone` is attention, offline or quiet."""
        hero = box(False, 18)
        hero.add_css_class("hero-card")
        if tone:
            hero.add_css_class(tone)
        hero.append(leading)
        words = box(True, 4)
        words.set_hexpand(True)
        words.set_valign(Gtk.Align.CENTER)
        words.append(label(eyebrow, "eyebrow"))
        words.append(label(title, "hero", wrap=True))
        for line in lines:
            words.append(line if isinstance(line, Gtk.Widget) else label(line, "muted", wrap=True))
        hero.append(words)
        if trailing is not None:
            trailing.set_valign(Gtk.Align.START)
            hero.append(trailing)
        return hero, words

    def metric_hints(self, data):
        """Caption under each gauge on a computer's page."""
        load, cpus = data.get("load"), data.get("cpus")
        cpu = f"Load {load} · {cpus} CPUs" if load is not None and cpus else "Waiting for a check"
        if data.get("memory_used") is not None and data.get("memory_total"):
            memory = size_text(data["memory_used"]) + " of " + size_text(data["memory_total"])
            if data.get("swap_used"):
                memory += "\n" + size_text(data["swap_used"]) + " swap in use"
        else:
            memory = "Physical memory in use"
        if data.get("disk_free") is not None:
            disk = size_text(data["disk_free"]) + " free"
            if data.get("disk_total"):
                disk += " of " + size_text(data["disk_total"])
        else:
            disk = "Waiting for a check"
        return {"cpu": cpu, "memory": memory, "disk": disk}

    def stat(self, icon_name, value, caption, css=None):
        tile = box(False, 10)
        tile.add_css_class("stat")
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(18)
        icon.add_css_class(css or "muted")
        tile.append(icon)
        words = box(True, 0)
        words.append(label(value, ("stat-value " + css) if css else "stat-value"))
        note = label(caption, "small muted")
        note.set_ellipsize(3)
        words.append(note)
        tile.append(words)
        return tile

    def event_row(self, event, title, subtitle, stamp_format):
        """History entry with a dot that is green for recoveries and amber for problems."""
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        row.set_title_lines(2)
        row.set_subtitle_lines(2)
        dot = Gtk.Box(valign=Gtk.Align.CENTER)
        dot.add_css_class("timeline-dot")
        dot.add_css_class("good" if "healthy" in str(event.get("message", "")) else "warning")
        row.add_prefix(dot)
        stamp = label(time.strftime(stamp_format, time.localtime(event.get("time", 0))), "muted small numeric")
        stamp.set_valign(Gtk.Align.CENTER)
        row.add_suffix(stamp)
        return row

    def gauge_card(self, title, ring, hint):
        card = box(True, 8)
        card.add_css_class("card")
        card.set_hexpand(True)
        card.append(label(title, "eyebrow"))
        card.append(ring)
        note = label(hint, "muted small", xalign=0.5, wrap=True)
        note.set_justify(Gtk.Justification.CENTER)
        note.set_max_width_chars(20)
        note.set_lines(2)
        note.set_ellipsize(3)
        card.append(note)
        return card, note

    def process_card(self, processes):
        """Busiest and largest programs side by side."""
        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=28, homogeneous=True)
        card.add_css_class("card")
        columns = (("TOP CPU", processes.get("cpu") or [], lambda item: f"{item[1]}%", "Share of one core during the last check"),
                   ("TOP MEMORY", processes.get("memory") or [], lambda item: size_text(item[1]), "Resident memory, summed over each program's processes"))
        for title, items, value_of, tip in columns:
            column = box(True, 7)
            heading = label(title, "eyebrow")
            heading.set_tooltip_text(tip)
            column.append(heading)
            ceiling = max((item[1] for item in items if len(item) > 1 and isinstance(item[1], (int, float))), default=0)
            for item in items[:5]:
                if not isinstance(item, list) or len(item) < 2 or not isinstance(item[1], (int, float)):
                    continue
                line = box(False, 8)
                name = label(str(item[0]) + (f"  ×{item[2]}" if len(item) > 2 and item[2] > 1 else ""))
                name.set_ellipsize(3)
                name.set_hexpand(True)
                line.append(name)
                line.append(label(value_of(item), "muted numeric", xalign=1))
                column.append(line)
                bar = Gtk.ProgressBar(fraction=max(0.0, min(1.0, item[1] / ceiling)) if ceiling else 0)
                bar.add_css_class("thin")
                column.append(bar)
            if not column.get_first_child().get_next_sibling():
                column.append(label("Nothing busy during the last check", "muted small"))
            card.append(column)
        return card

    def history_card(self, host):
        """Trend chart with metric and range pickers, plus a reachability strip."""
        card = box(True, 12)
        card.add_css_class("card")
        ranges = [item for item in CHART_RANGES if item[0] != "live" or host.get("local")]
        chosen = self.chart_range if self.chart_range in [item[0] for item in ranges] else ("live" if host.get("local") else "24h")
        metric = self.chart_metric
        if metric is None:
            # Until the user picks one, prefer CPU but fall back to a metric that already has history.
            since = time.time() - 86400
            metric = next((field for field in ("cpu", "memory") if chosen == "live" or
                           sum(isinstance(value, (int, float)) for _, value in self.history.points(host["id"], field, since)) > 1), "cpu")
        header = box(False, 10)
        titles = box(True, 1)
        titles.set_hexpand(True)
        titles.append(label("History", "section-title"))
        summary = label("", "muted small numeric")
        summary.set_ellipsize(3)
        titles.append(summary)
        header.append(titles)
        picker = Gtk.DropDown.new_from_strings([title for _, title, _, _ in CHART_METRICS])
        picker.set_selected([field for field, _, _, _ in CHART_METRICS].index(metric))
        picker.set_valign(Gtk.Align.CENTER)
        picker.connect("notify::selected", lambda dropdown, _: self.pick_chart(metric=CHART_METRICS[dropdown.get_selected()][0]))
        header.append(picker)
        toggles = box(False, 0)
        toggles.add_css_class("linked")
        toggles.set_valign(Gtk.Align.CENTER)
        first = None
        for key, title, _ in ranges:
            toggle = Gtk.ToggleButton(label=title, active=key == chosen)
            if first is None:
                first = toggle
            else:
                toggle.set_group(first)
            toggle.connect("toggled", lambda button, picked=key: self.pick_chart(span=picked) if button.get_active() else None)
            toggles.append(toggle)
        header.append(toggles)
        card.append(header)
        chart = TrendChart()
        card.append(chart)
        now = time.time()
        reach = [up for _, up in self.history.points(host["id"], "up", now - 86400) if isinstance(up, (int, float))]
        footer = box(False, 12)
        caption = label(f"Reachable {100 * sum(reach) / len(reach):.1f}% · last 24 hours" if reach else "Reachability · last 24 hours",
                        "muted small numeric")
        footer.append(caption)
        strip = AvailabilityStrip(self.history.availability(host["id"], now - 86400, now, 48), now - 86400, now, height=14)
        footer.append(strip)
        card.append(footer)
        self.chart = {"host": host["id"], "metric": metric, "range": chosen, "chart": chart, "summary": summary}
        self.refresh_chart()
        return card

    def pick_chart(self, metric=None, span=None):
        if not self.chart or self.rebuilding_detail:
            return
        if metric:
            self.chart_metric = self.chart["metric"] = metric
        if span:
            self.chart_range = self.chart["range"] = span
        self.refresh_chart()

    def refresh_chart(self):
        state = self.chart
        if not state:
            return
        field, title, unit, ceiling = next(item for item in CHART_METRICS if item[0] == state["metric"])
        seconds = next(item[2] for item in CHART_RANGES if item[0] == state["range"])
        now = time.time()
        if state["range"] == "live":
            column = {"cpu": 1, "memory": 2, "temperature": 3, "disk": 4}.get(field)
            points = [(row[0], row[column]) for row in self.live.get(state["host"], ())] if column else []
            empty = "Live readings arrive every two seconds" if column else "Check time is recorded with full checks; pick a longer range"
        else:
            points = self.history.points(state["host"], field, now - seconds)
            empty = "No history for this range yet · points appear after a few checks"
        chart = state["chart"]
        chart.set_data(points, now - seconds, now, unit, ceiling, "accent", empty)
        values = [value for _, value in chart.points]
        if values:
            state["summary"].set_text(f"{title} · average {chart.format(sum(values) / len(values))} · peak {chart.format(max(values))}")
        else:
            state["summary"].set_text(title)

    def wake(self, host):
        network = (self.last_seen.get(host["id"]) or {}).get("network") or {}
        try:
            actions.wake(network.get("mac"), network.get("broadcast"))
        except (OSError, ValueError) as error:
            self.toast(str(error))
            return
        self.toast("Wake signal sent to " + host["name"] + " · checking again in 45 seconds")
        GLib.timeout_add_seconds(45, lambda: (self.check(force_updates=False), GLib.SOURCE_REMOVE)[1])

"""GTK4/libadwaita desktop shell. Worker threads never touch GTK widgets."""
import json
from pathlib import Path
import threading
import time
from urllib.parse import quote

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import __version__
from . import actions, config
from .monitor import History, issues, refresh


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
        super().__init__(application_id="io.github.fleetlight.Linux", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.config_file = configuration
        self.demo = demo
        self.window = None
        self.snapshots = {}
        self.busy = False
        self.selected = None
        self.timer = None
        self.history = History() if not demo else History(Path("/nonexistent/fleetlight-demo"))
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
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Fleetlight", subtitle=f"Linux · {__version__}"))
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
        self.search = Gtk.SearchEntry(placeholder_text="Find a computer")
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
        self.window.present()
        self.timer = GLib.timeout_add_seconds(self.configuration.get("refresh_seconds", 60), self.auto_check)
        if self.demo:
            self.refresh_button.set_sensitive(False)
        else:
            self.check()
        if load_error:
            self.toast("Configuration was not loaded: " + load_error)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=6))

    def auto_check(self):
        if not self.demo:
            self.check()
        return GLib.SOURCE_CONTINUE

    def check(self):
        if self.busy or self.demo:
            return
        self.busy = True
        self.refresh_button.set_sensitive(False)
        self.spinner.start()
        previous = dict(self.snapshots)
        hosts = list(self.configuration["hosts"])
        def work():
            results = {}
            def received(snapshot):
                results[snapshot["id"]] = snapshot
                GLib.idle_add(self.receive, snapshot)
            refresh(hosts, received)
            warning = None
            try:
                self.history.record(results, previous)
            except OSError:
                warning = "History could not be saved; live checks are still available"
            GLib.idle_add(self.finished, warning)
        threading.Thread(target=work, daemon=True).start()

    def receive(self, snapshot):
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
        return GLib.SOURCE_REMOVE

    def populate_hosts(self):
        hosts = self.configuration["hosts"]
        online = sum(self.snapshots.get(h["id"], {}).get("status") == "online" for h in hosts)
        self.summary.set_text(f"{online} of {len(hosts)} online")
        selected_id = self.selected
        self.host_list.unselect_all()
        clear(self.host_list)
        selected_row = None
        query = self.search.get_text().casefold()
        for host in hosts:
            if query and query not in host["name"].casefold():
                continue
            snapshot = self.snapshots.get(host["id"], {})
            if self.attention.get_active() and not issues(snapshot):
                continue
            row = Gtk.ListBoxRow()
            row.host_id = host["id"]
            body = box(False, 12)
            icon = Gtk.Image.new_from_icon_name("computer-symbolic" if host.get("local") else "network-server-symbolic")
            icon.set_pixel_size(24)
            online_host = snapshot.get("status") == "online"
            icon.add_css_class("good" if online_host and not issues(snapshot) else "warning" if online_host else "muted")
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
        row = selected_row or self.host_list.get_row_at_index(0)
        if row:
            self.host_list.select_row(row)
        else:
            clear(self.content)
            self.content.append(Adw.StatusPage(title="No computers match", description="Change the search or attention filter.", icon_name="system-search-symbolic"))

    def select_host(self, _, row):
        if row:
            self.selected = row.host_id
            self.render_detail()

    def render_detail(self):
        host = next(h for h in self.configuration["hosts"] if h["id"] == self.selected)
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
        trouble = issues(data) if data else []
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
        for name, value, hint in (
            ("ROOT DISK", data.get("disk_percent"), f"{data.get('disk_free', 0) / 1024**3:.1f} GiB free" if online else "Waiting for a check"),
            ("MEMORY", data.get("memory_percent"), "Physical memory in use"),
        ):
            card = box(True, 10)
            card.add_css_class("card")
            card.set_size_request(150, -1)
            card.set_hexpand(True)
            card.append(label(name, "eyebrow"))
            card.append(label(f"{value}%" if value is not None else "—", "metric"))
            bar = Gtk.ProgressBar(fraction=max(0, min(1, (value or 0) / 100)))
            if (value or 0) >= 90:
                bar.add_css_class("warning")
            card.append(bar)
            card.append(label(hint, "muted"))
            metrics.append(card)
        card = box(True, 10)
        card.add_css_class("card")
        card.set_size_request(150, -1)
        card.set_hexpand(True)
        card.append(label("UPTIME", "eyebrow"))
        card.append(label(uptime(data.get("uptime")) if online else "—", "metric"))
        card.append(label(f"Load {data.get('load', '—')} · {data.get('cpus', '—')} CPUs", "muted"))
        metrics.append(card)
        self.content.append(metrics)
        apps = self.section("Applications", "Installed versions from this computer")
        self.detail_row(apps, "Codex CLI", data.get("codex") or "Not detected", "utilities-terminal-symbolic")
        desktop = data.get("chatgpt", {})
        self.detail_row(apps, "ChatGPT", desktop.get("version") or "Not detected", "applications-internet-symbolic")
        if desktop.get("provider"):
            note = label(desktop["provider"] + " · " + desktop.get("status", "installed"), "muted")
            note.set_wrap(True)
            apps.append(note)
        if desktop.get("provider") in ("APT", "pacman"):
            note = label("Package metadata only. Local app repairs are preserved; file integrity is not asserted.", "muted")
            note.set_wrap(True)
            apps.append(note)
        services = self.section("Services", "Configured system services")
        states = data.get("services", {})
        for name in host.get("services", []):
            state = states.get(name, "not checked")
            self.detail_row(services, name, state, "emblem-system-symbolic", "good" if state == "active" else "warning")
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
        footer = label(f"Automatic checks every {self.configuration.get('refresh_seconds', 60)} seconds · " +
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
            if self.busy:
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
        description = label("Add or remove computers, rename them, and set systemd services. Saved only in your user configuration folder.", "muted")
        description.set_wrap(True)
        body.append(description)
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
            if self.busy:
                error.set_text("Wait for the current check to finish")
                return
            buffer = editor.get_buffer()
            try:
                candidate = config.validate(json.loads(buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)))
                config.atomic_json(self.config_file or config.config_path(), candidate)
                if start.get_active() != actions.autostart_path().exists():
                    actions.set_autostart(start.get_active())
            except (ValueError, OSError, RuntimeError) as problem:
                error.set_text(str(problem))
                return
            self.configuration = candidate
            self.snapshots = {k:v for k,v in self.snapshots.items() if k in {h["id"] for h in candidate["hosts"]}}
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

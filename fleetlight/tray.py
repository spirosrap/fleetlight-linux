"""Tray icon: a StatusNotifierItem with a small D-Bus menu, using only Gio.

Desktop bars that host a tray (Omarchy, Waybar, KDE, GNOME with the AppIndicator extension) own
org.kde.StatusNotifierWatcher. Without one nothing is registered and Fleetlight behaves as before."""
from pathlib import Path

from gi.repository import Gio, GLib

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
ITEM_INTERFACE = "org.kde.StatusNotifierItem"
MENU_INTERFACE = "com.canonical.dbusmenu"
ICON = "fleetlight-symbolic"
ICON_DIRECTORY = str(Path(__file__).resolve().parent / "icons")

INTERFACES = """<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg type="s"/></signal>
  </interface>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
      <arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
    <method name="Event">
      <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/>
    </method>
    <method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
    <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
    <method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/></method>
    <signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
    <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
    <signal name="ItemActivationRequested"><arg type="i"/><arg type="u"/></signal>
  </interface>
</node>"""


def item_properties(entry):
    """D-Bus menu properties of one entry: None is a separator, otherwise (label, callback or None)."""
    if entry is None:
        return {"type": GLib.Variant("s", "separator")}
    text, callback = entry
    return {"label": GLib.Variant("s", text.replace("_", "__")), "enabled": GLib.Variant("b", callback is not None)}


def layout(entries):
    """The whole menu as the (ia{sv}av) tree GetLayout returns; entries are numbered from 1."""
    children = [GLib.Variant("(ia{sv}av)", (ident, item_properties(entry), [])) for ident, entry in enumerate(entries, 1)]
    return (0, {"children-display": GLib.Variant("s", "submenu")}, children)


class Tray:
    """One tray icon. activate runs on a click; available(bool) reports whether a tray is showing it."""

    def __init__(self, connection, ident, title, activate, available):
        self.connection = connection
        self.ident = ident
        self.title = title
        self.activate = activate
        self.available = available
        self.tooltip = title
        self.attention = False
        self.entries = []
        self.revision = 1
        self.registered = False
        node = Gio.DBusNodeInfo.new_for_xml(INTERFACES)
        self.objects = [connection.register_object(ITEM_PATH, node.lookup_interface(ITEM_INTERFACE), self.item_call, self.item_property, None),
                        connection.register_object(MENU_PATH, node.lookup_interface(MENU_INTERFACE), self.menu_call, self.menu_property, None)]
        self.watch = Gio.bus_watch_name_on_connection(connection, WATCHER, Gio.BusNameWatcherFlags.NONE, self.watcher_appeared, self.watcher_vanished)

    def close(self):
        Gio.bus_unwatch_name(self.watch)
        for ident in self.objects:
            self.connection.unregister_object(ident)
        self.registered = False

    def watcher_appeared(self, *_):
        self.connection.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                             GLib.Variant("(s)", (self.connection.get_unique_name(),)), None, Gio.DBusCallFlags.NONE, -1, None, self.registration_finished)

    def registration_finished(self, connection, result):
        try:
            connection.call_finish(result)
        except GLib.Error:
            return
        self.registered = True
        self.available(True)

    def watcher_vanished(self, *_):
        if self.registered:
            self.registered = False
            self.available(False)

    def update(self, tooltip, attention, entries):
        """Replace the hover text, the attention state and the menu: None or (label, callback or None)."""
        labels = [entry and entry[0] for entry in entries]
        changed = labels != [entry and entry[0] for entry in self.entries] or [bool(entry and entry[1]) for entry in entries] != [bool(entry and entry[1]) for entry in self.entries]
        self.entries = list(entries)
        if changed:
            self.revision += 1
            self.emit(MENU_PATH, MENU_INTERFACE, "LayoutUpdated", GLib.Variant("(ui)", (self.revision, 0)))
        if tooltip != self.tooltip:
            self.tooltip = tooltip
            self.emit(ITEM_PATH, ITEM_INTERFACE, "NewToolTip", None)
        if attention != self.attention:
            self.attention = attention
            self.emit(ITEM_PATH, ITEM_INTERFACE, "NewStatus", GLib.Variant("(s)", (self.status(),)))

    def emit(self, path, interface, name, parameters):
        try:
            self.connection.emit_signal(None, path, interface, name, parameters)
        except GLib.Error:
            pass

    def status(self):
        return "NeedsAttention" if self.attention else "Active"

    def item_property(self, _connection, _sender, _path, _interface, name):
        if name in ("IconPixmap", "OverlayIconPixmap", "AttentionIconPixmap"):
            return GLib.Variant("a(iiay)", [])
        if name == "ToolTip":
            return GLib.Variant("(sa(iiay)ss)", (ICON, [], self.tooltip, ""))
        if name == "WindowId":
            return GLib.Variant("i", 0)
        if name == "ItemIsMenu":
            return GLib.Variant("b", False)
        if name == "Menu":
            return GLib.Variant("o", MENU_PATH)
        return GLib.Variant("s", {"Category": "ApplicationStatus", "Id": self.ident, "Title": self.title, "Status": self.status(),
                                  "IconThemePath": ICON_DIRECTORY, "IconName": ICON, "AttentionIconName": ICON}.get(name, ""))

    def item_call(self, _connection, _sender, _path, _interface, method, _parameters, invocation):
        invocation.return_value(None)
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(self.run, self.activate)

    def menu_property(self, _connection, _sender, _path, _interface, name):
        if name == "Version":
            return GLib.Variant("u", 3)
        if name == "IconThemePath":
            return GLib.Variant("as", [])
        return GLib.Variant("s", "ltr" if name == "TextDirection" else "normal")

    def menu_call(self, _connection, _sender, _path, _interface, method, parameters, invocation):
        values = parameters.unpack()
        if method == "GetLayout":
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, layout(self.entries))))
        elif method == "GetGroupProperties":
            wanted = values[0] or range(1, len(self.entries) + 1)
            invocation.return_value(GLib.Variant("(a(ia{sv}))", ([(ident, item_properties(self.entries[ident - 1])) for ident in wanted if 0 < ident <= len(self.entries)],)))
        elif method == "GetProperty":
            found = item_properties(self.entries[values[0] - 1]).get(values[1]) if 0 < values[0] <= len(self.entries) else None
            if found is None:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.InvalidArgs", "No such menu property")
            else:
                invocation.return_value(GLib.Variant("(v)", (found,)))
        elif method == "Event":
            invocation.return_value(None)
            self.clicked(values[0], values[1])
        elif method == "EventGroup":
            invocation.return_value(GLib.Variant("(ai)", ([],)))
            for ident, event, _data, _timestamp in values[0]:
                self.clicked(ident, event)
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        else:
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))

    def clicked(self, ident, event):
        if event == "clicked" and 0 < ident <= len(self.entries) and self.entries[ident - 1] and self.entries[ident - 1][1]:
            GLib.idle_add(self.run, self.entries[ident - 1][1])

    @staticmethod
    def run(callback):
        callback()
        return GLib.SOURCE_REMOVE

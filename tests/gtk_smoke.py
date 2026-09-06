"""Exercise the native widget tree with fictional data under Xvfb."""
from pathlib import Path
import sys
import traceback
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fleetlight.app import Fleetlight, GLib, Gtk

app = Fleetlight(demo=True)
failures = []


def verify():
    try:
        assert app.window is not None
        assert app.summary.get_text() == "4 of 4 online"
        assert "4/4 online" in app.window_title.get_subtitle()
        assert app.selected == "local"
        app.search.set_text("Studio")
        app.populate_hosts()
        assert app.selected == "studio"
        app.search.set_text("")
        app.populate_hosts()
        app.attention.set_active(True)
        assert app.host_list.get_row_at_index(0) is None
        app.attention.set_active(False)
        assert app.host_list.get_row_at_index(0) is not None
        parent = Gtk.Box()
        app.update_row(parent, app.configuration["hosts"][0], "cli", "Codex CLI", "1.0.0", "utilities-terminal-symbolic")
        button = parent.get_first_child().get_last_child()
        assert isinstance(button, Gtk.Button) and button.get_label() == "Update"
        assert not button.get_sensitive()
        assert app.app_updates["local"]["cli"]["state"] == "available"
        app.settings()
        app.add_computer()
        assert len(app.get_windows()) >= 1
        print("GTK smoke test passed: window, host selection, filters, settings, add dialog")
    except Exception:
        failures.append(traceback.format_exc())
    finally:
        app.quit()
    return GLib.SOURCE_REMOVE


GLib.timeout_add_seconds(2, verify)
app.run([sys.argv[0]])
if failures:
    print("\n".join(failures), file=sys.stderr)
    sys.exit(1)

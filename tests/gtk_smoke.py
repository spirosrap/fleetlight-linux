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
        assert "Update all Codex CLI" in app.batch_buttons["cli"].get_label()
        assert "Update all ChatGPT" in app.batch_buttons["desktop"].get_label()
        assert "Update all Linux packages" in app.batch_buttons["system"].get_label()
        assert "Restart required computers" in app.batch_buttons["restart"].get_label()
        assert not app.batch_buttons["cli"].get_sensitive()
        # Drive the real queue state machine with fake jobs: no SSH or installation.
        from fleetlight import updates
        pending, _ = updates.batch_candidates(app.configuration["hosts"], app.snapshots, app.app_updates, "cli")
        launched = []
        app.persist_jobs = lambda: None
        app.check = lambda *a, **k: None
        def fake_start(host, kind, checked):
            launched.append(host["id"])
            app.active_job = {"id": "a" * 32, "host": host, "kind": kind, "state": "running"}
        app.begin_update = fake_start
        app.begin_batch("cli", pending)
        assert len(launched) == 1
        # A new controller restores both the running job and remaining queue.
        import json, os, tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "fleetlight"
            state.mkdir()
            (state / "update-controller.json").write_text(json.dumps({"active_job": app.active_job, "batch": app.batch, "last_jobs": {}}))
            with patch.dict(os.environ, {"XDG_STATE_HOME": directory}):
                recovered = Fleetlight()
            assert recovered.active_job["host"]["id"] == launched[0]
            assert len(recovered.batch["pending"]) == len(pending) - 1
        app.receive_job({"state": "succeeded", "phase": "Verified"})
        assert len(launched) == 2
        app.receive_job({"state": "failed", "phase": "Fixture failure"})
        assert len(launched) == 2 and not app.batch["running"]
        assert not app.batch["pending"] and "Fixture failure" in app.batch["stopped"]
        app.begin_batch("cli", pending)
        app.cancel_batch()
        assert app.active_job is not None and not app.batch["pending"]
        app.receive_job({"state": "succeeded", "phase": "Verified"})
        assert len(launched) == 3 and not app.batch["running"]
        host_id = app.configuration["hosts"][0]["id"]
        app.pending_restarts[host_id] = {"name": "Fixture", "boot_id": "old-boot"}
        snapshot = dict(app.snapshots[host_id], id=host_id, boot_id="old-boot", status="online")
        app.receive(snapshot)
        assert host_id in app.pending_restarts
        app.receive(dict(snapshot, boot_id="new-boot"))
        assert host_id not in app.pending_restarts
        assert app.app_updates[host_id]["restart"]["detail"].startswith("Restart verified")
        app.settings()
        app.add_computer()
        assert len(app.get_windows()) >= 1
        print("GTK smoke test passed: UI, batch sequencing, failure stop, cancellation and recovery")
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

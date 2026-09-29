"""Exercise the native widget tree with fictional data under Xvfb."""
from pathlib import Path
import sys
import traceback
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fleetlight.app import Fleetlight, GLib, Gtk

app = Fleetlight(demo=True)
failures = []


def find_widget(widget, predicate):
    if predicate(widget):
        return widget
    child = widget.get_first_child()
    while child:
        found = find_widget(child, predicate)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None


def host_rows():
    rows, index = [], 0
    while True:
        row = app.host_list.get_row_at_index(index)
        if row is None:
            return rows
        if row.get_selectable() and row.host_id != "fleet":
            rows.append(row.host_id)
        index += 1


def verify():
    try:
        assert app.window is not None
        assert app.summary.get_text() == "4 of 4 online"
        assert "4/4 online" in app.window_title.get_subtitle()
        # The fleet overview is the landing page and lists every computer as a card.
        assert app.selected == "fleet"
        assert app.page_title.get_title() == "Fleet overview"
        assert set(app.overview_widgets) == {h["id"] for h in app.configuration["hosts"]}
        assert host_rows() == ["local", "studio", "server", "lab"]
        app.show_page("local")
        assert app.selected == "local"
        assert app.page_title.get_title() == "This Computer"
        # Live metrics preserve full receipts and leave remote hosts alone.
        local = app.configuration["hosts"][0]
        from fleetlight.probe import collect_metrics
        metrics = collect_metrics()
        original = dict(app.snapshots["local"])
        remote = dict(app.snapshots["studio"])
        app.receive_local_metrics([local], metrics)
        assert app.snapshots["local"]["metrics_checked_at"] == metrics["metrics_checked_at"]
        assert app.snapshots["local"]["services"] == original["services"]
        assert app.snapshots["studio"] == remote
        app.receive(original)
        assert app.snapshots["local"]["metrics_checked_at"] == metrics["metrics_checked_at"]
        app.receive_local_metrics([local], dict(metrics, metrics_checked_at=0))
        assert app.snapshots["local"]["metrics_checked_at"] == metrics["metrics_checked_at"]
        app.search.set_text("Studio")
        app.populate_hosts()
        assert app.selected == "studio"
        app.search.set_text("")
        app.populate_hosts()
        app.attention.set_active(True)
        assert host_rows() == []
        assert app.selected == "fleet"
        app.attention.set_active(False)
        assert host_rows()
        parent = Gtk.Box()
        app.update_row(parent, app.configuration["hosts"][0], "cli", "Codex CLI", "1.0.0", "utilities-terminal-symbolic")
        button = find_widget(parent, lambda w: isinstance(w, Gtk.Button) and w.get_label() == "Update")
        assert button is not None
        assert not button.get_sensitive()
        assert app.app_updates["local"]["cli"]["state"] == "available"
        assert "Update all Codex CLI" in app.batch_buttons["cli"].get_label()
        assert "Update all ChatGPT" in app.batch_buttons["desktop"].get_label()
        assert "Update all Linux packages" in app.batch_buttons["system"].get_label()
        assert "Restart required computers" in app.batch_buttons["restart"].get_label()
        assert not app.batch_buttons["cli"].get_sensitive()
        # Old failures are collapsed history, and dismissal survives a restart without deleting logs.
        import json, os, tempfile
        from unittest.mock import patch
        receipt = {"id": "b" * 32, "kind": "desktop", "state": "failed", "phase": "Old package failure", "log": "Original failure log"}
        app.last_jobs["local"] = receipt
        history = Gtk.Box()
        app.update_history(history, local)
        expander = history.get_first_child()
        assert isinstance(expander, Gtk.Expander) and not expander.get_expanded()
        assert expander.get_label() == "What changed"
        # Real local metric refreshes rebuild the detail pane every two seconds.
        def find_history(widget):
            return find_widget(widget, lambda w: isinstance(w, Gtk.Expander))
        app.selected = "local"
        app.render_detail()
        opened = find_history(app.content)
        opened.set_expanded(True)
        app.receive_local_metrics([local], dict(metrics, metrics_checked_at=metrics["metrics_checked_at"] + 10))
        refreshed = find_history(app.content)
        assert refreshed is opened and refreshed.get_expanded()
        app.metric_widgets["disk"].set_text("1%")
        app.receive_local_metrics([local], dict(metrics, disk_percent=42, metrics_checked_at=metrics["metrics_checked_at"] + 20))
        assert app.metric_widgets["disk"].get_text() == "42%"
        refreshed.set_expanded(False)
        app.receive_updates("local", app.app_updates["local"])
        assert not find_history(app.content).get_expanded()
        find_history(app.content).set_expanded(True)
        app.last_jobs["studio"] = dict(receipt)
        app.selected = "studio"
        app.render_detail()
        assert not find_history(app.content).get_expanded()
        app.selected = "local"
        app.render_detail()
        assert find_history(app.content).get_expanded()
        app.last_jobs["local"] = dict(receipt, id="c" * 32)
        app.render_detail()
        assert find_history(app.content).get_expanded()
        app.last_jobs["local"] = receipt
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "fleetlight"
            state.mkdir()
            app.journal_path = state / "update-controller.json"
            app.dismiss_update_result("local")
            with patch.dict(os.environ, {"XDG_STATE_HOME": directory}):
                restored = Fleetlight()
            assert restored.last_jobs["local"]["dismissed"] is True
            assert restored.last_jobs["local"]["log"] == receipt["log"]
            assert restored.last_jobs["local"]["state"] == "failed"
            history = Gtk.Box()
            app.update_history(history, local)
            assert history.get_first_child().get_label() == "What changed"
            app.last_jobs["local"] = receipt
            with patch.object(app, "persist_jobs", side_effect=OSError("fixture")):
                app.dismiss_update_result("local")
            assert not app.last_jobs["local"].get("dismissed")
        app.last_jobs.clear()
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
        launched.clear()
        app.begin_batch("cli", pending, automatic=True)
        assert len(launched) == 1 and app.batch.get("automatic")
        app.receive_job({"state": "failed", "phase": "Auto fixture failure"})
        assert len(launched) == 2
        assert "Continuing after" in app.batch["stopped"]
        app.cancel_batch()
        app.receive_job({"state": "succeeded", "phase": "Verified"})
        assert len(launched) == 2 and not app.batch["pending"]
        app.configuration["auto_updates"] = True
        before = list(launched)
        app.maybe_auto_update()
        assert launched == before
        host_id = app.configuration["hosts"][0]["id"]
        app.pending_restarts[host_id] = {"name": "Fixture", "boot_id": "old-boot"}
        snapshot = dict(app.snapshots[host_id], id=host_id, boot_id="old-boot", status="online")
        app.receive(snapshot)
        assert host_id in app.pending_restarts
        app.receive(dict(snapshot, boot_id="new-boot"))
        assert host_id not in app.pending_restarts
        assert app.app_updates[host_id]["restart"]["detail"].startswith("Restart verified")
        # Live local metrics also update the overview cards in place.
        app.show_page("fleet")
        assert app.selected == "fleet"
        app.receive_local_metrics([local], dict(metrics, disk_percent=57, metrics_checked_at=metrics["metrics_checked_at"] + 30))
        assert app.overview_widgets["local"]["disk"].get_text() == "57%"
        # A failed quota refresh keeps the last good reading and explains why it is stale.
        app.receive_agents({"claude": {"id": "claude", "name": "Claude", "state": "ok", "remaining_percent": 88, "detail": "88% weekly"}})
        assert app.agent_usage["claude"]["remaining_percent"] == 88 and "stale" not in app.agent_usage["claude"]
        app.receive_agents({"claude": {"id": "claude", "name": "Claude", "state": "unavailable", "detail": "rate limited"}})
        assert app.agent_usage["claude"]["remaining_percent"] == 88 and app.agent_usage["claude"]["stale"] == "rate limited"
        app.settings()
        app.add_computer()
        app.show_shortcuts()
        app.show_about()
        assert len(app.get_windows()) >= 1
        print("GTK smoke test passed: overview, UI, batch sequencing, failure stop, cancellation and recovery")
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

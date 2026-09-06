# Fleetlight for Linux

A native GTK4/libadwaita dashboard for your computers. Monitor Linux and macOS hosts from a Linux desktop, using the SSH access you already have.

![Fleetlight with fictional demo data](docs/screenshot.png)

## Version 0.2.0

- A Wayland-native desktop application with a searchable fleet sidebar, attention filter and automatic checks.
- Direct, concurrent SSH monitoring. The local computer is checked without SSH.
- Root disk, memory, uptime, load, configured systemd services, Codex CLI and ChatGPT package versions.
- Installed/latest application versions and in-app Codex CLI and ChatGPT update buttons, including remote Apple Silicon Macs.
- Durable update jobs with progress, verification and reconnect recovery.
- Per-service **Warn when stopped** preferences for optional services.
- A local history of status and service transitions, with recent changes on each computer's page.
- Explicit terminal, SFTP and copy-diagnostics actions.
- System package updates in an interactive terminal after confirmation. Arch uses a full `pacman -Syu`; APT and DNF are also supported. Fleetlight never answers the package manager's confirmation prompt.
- Editable private configuration, an add-computer dialog and optional start at login.
- A read-only JSON CLI for diagnostics, plus a fictional demo mode for screenshots.

The Linux edition does not yet provide tray integration, Android controller pairing, wake-on-LAN or fleet-wide update batches. The Linux app monitors hosts directly and does not depend on a Mac controller.

## Install

Requires Python 3.10+, GTK4, libadwaita 1.4+ and OpenSSH. Remote computers require Python 3.9+ and a working, non-interactive SSH connection. Tested on Arch/Omarchy under Hyprland; CI uses Ubuntu 24.04 with Xvfb.

Arch / Omarchy:

```sh
sudo pacman -S --needed python python-gobject gtk4 libadwaita openssh
```

Ubuntu 24.04+ / Debian with libadwaita 1.4+:

```sh
sudo apt install python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 openssh-client
```

Then:

```sh
git clone https://github.com/spirosrap/fleetlight-linux.git
cd fleetlight-linux
bash scripts/install.sh
~/.local/bin/fleetlight
```

The installer does not need sudo. It installs the app, icon and desktop launcher under `~/.local/`. Existing user configuration stays intact; the previous installed source is retained in `~/.local/share/fleetlight.previous` after an upgrade. Choose **Fleetlight** in your desktop application launcher. A terminal such as foot, kitty, GNOME Terminal or Konsole is needed for terminal actions; SFTP browsing requires a compatible file manager.

For development, run `python3 -m fleetlight` from the checkout. Use your distribution's system Python so it can find PyGObject; an isolated pip virtual environment will not normally include the system GI bindings. See the [PyGObject installation guide](https://pygobject.gnome.org/getting_started.html) and [libadwaita documentation](https://gnome.pages.gitlab.gnome.org/libadwaita/doc/main/).

## Configure your fleet

The public app starts with **This Computer only**. It never imports or probes your SSH hosts automatically. Add machines with the **+** button, or edit **Settings**. Configuration lives in `$XDG_CONFIG_HOME/fleetlight/fleet.json` (normally `~/.config/fleetlight/fleet.json`):

```json
{
  "version": 1,
  "refresh_seconds": 60,
  "hosts": [
    {"id": "local", "name": "This Computer", "local": true, "services": []},
    {"id": "server", "name": "Home Server", "alias": "home-server", "services": ["tailscaled", "docker"]}
  ]
}
```

SSH aliases are resolved by OpenSSH using your normal `~/.ssh/config`. Verify a new host's fingerprint and establish key-based authentication in your terminal before adding it. Fleetlight uses `BatchMode=yes` and `StrictHostKeyChecking=yes`; it never bypasses unknown or changed keys. Connection errors are shown separately from service failures.

Service names refer to system-level systemd units. macOS service checks are currently unsupported. A missing configured service is reported as **not installed**, not healthy. Host probes run at most eight at once, time out after 25 seconds and never require sudo. Codex version lookup follows common system, mise and nvm paths. Application release checks run separately and refresh package metadata.

Services are expected to run unless you turn off **Warn when stopped**. Optional services remain visible; inactive or uninstalled optional services are neutral, while an actual failed service still needs attention. Settings stores these preferences in each host's `optional_services` list.

System updates can overwrite local application repairs. The update dialog explains this; its terminal leaves package selection, authentication and final confirmation to you. No system update runs during monitoring or installation.

History is stored locally in `$XDG_STATE_HOME/fleetlight/history.json`, capped at 2,048 samples / 24 hours and 100 events. It contains computer IDs, metrics and state changes. Diagnostics copied to the clipboard include computer names and should be reviewed before sharing.

## Application updates

**Check now** checks installed and available releases. Host monitoring runs every minute by default; automatic application release checks run every 15 minutes. Select a computer and press **Update** beside an available application release. ChatGPT asks for confirmation because it may close and reopen the app; Arch also requires a full system upgrade to avoid unsupported partial upgrades.

- **Codex CLI, Linux and macOS:** uses the active installation's standalone updater, npm or mise. Stable versions come from the official npm registry. Unknown installation methods require a manual update.
- **ChatGPT on Apple Silicon macOS:** checks OpenAI's official appcast, verifies the downloaded bundle's identity, version and OpenAI signing team, and restores the previous bundle if replacement verification fails.
- **ChatGPT on Arch:** refreshes isolated package metadata, checks package integrity and runs a full `pacman -Syu` after confirmation. Modified packages are protected from this action.
- **ChatGPT on APT:** validates the supported official repository and installed package, refreshes metadata and upgrades the package. Linux installation requires existing passwordless sudo permission; Fleetlight does not collect passwords or configure sudo.

Updates run one at a time in Fleetlight. Jobs continue after an SSH interruption or controller crash, and Fleetlight resumes watching the saved job when reopened. It verifies the active version before reporting success. Job state and logs are retained on each target at `~/.local/state/fleetlight/update-jobs`; controller receipts are in the local state folder. Do not remove these while a job is running. Package-manager locks also apply; do not run competing manual updates.

Fleetlight itself can be updated by pulling this repository and rerunning `bash scripts/install.sh` after closing the app. Its own updater is not yet integrated.

## Command line

```sh
python3 -m fleetlight --check
python3 -m fleetlight --config /path/to/private-config.json --check
python3 -m fleetlight --demo
python3 -m fleetlight --version
```

`--check` returns status 0 only when every configured host returns a verified probe receipt. It does not assert that every service is healthy. Demo mode performs no network checks and uses fictional data.

## Test

```sh
python3 -m unittest discover -s tests -v
python3 scripts/privacy_check.py
dbus-run-session -- xvfb-run -a python3 tests/gtk_smoke.py
```

The native smoke test needs Xvfb and a session D-Bus (`xvfb` and `dbus-x11` on Ubuntu). It exercises window construction, filtering, selection and configuration dialogs. The tests use isolated fake installers to exercise job locking, reconnect recovery and receipt verification; they do not install application updates.

## Privacy and security

No analytics, cloud accounts, API keys or bundled private fleet. Host connections go only to the computers you configure. Application checks also contact the official npm registry, OpenAI appcast and configured package repositories. SSH handles keys; Fleetlight does not read private-key contents. Probes use a fixed read-only collector and validate a per-request receipt. Host aliases and service names are validated, and update operations use a fixed allowlist of commands. Remote output is rendered as text, never executed as an action or treated as markup.

Keep personal `fleet.json`, history, keys and screenshots out of public commits. The public privacy check scans tracked source. See [SECURITY.md](SECURITY.md) for reporting concerns.

## Uninstall

Close Fleetlight and disable **Open Fleetlight when I log in** in Settings. Remove the `fleetlight` launcher from `~/.local/bin`, the `fleetlight` and `fleetlight.previous` app directories from `~/.local/share`, and the Fleetlight desktop/icon files. Keep `~/.config/fleetlight` and `~/.local/state/fleetlight` if you want to retain configuration and history.

## Related projects

[Fleetlight for macOS](https://github.com/spirosrap/fleetlight-macos) · [Fleetlight for Android](https://github.com/spirosrap/fleetlight-android)

MIT licensed. See [LICENSE](LICENSE).

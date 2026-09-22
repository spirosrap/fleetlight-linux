"""Self-contained Linux package/restart operations for explicit Fleetlight jobs."""
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def run(args, env=None):
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          env=dict(os.environ, LC_ALL='C', **(env or {})))


def reboot_status():
    if platform.system() != 'Linux':
        return {'required': False, 'reason': 'Not Linux'}
    marker = Path.home() / '.local/state/fleetlight/system-restart.json'
    try:
        recorded = json.loads(marker.read_text())
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if recorded.get('boot_id') == boot:
            return {'required': True, 'reason': 'Core system packages were updated; restart recommended'}
    except (OSError, ValueError):
        pass
    if Path('/run/reboot-required').exists():
        return {'required': True, 'reason': 'The operating system requests a restart'}
    # On Arch, a replaced running kernel loses its modules directory.
    if shutil.which('pacman') and Path('/usr/lib/modules').is_dir():
        if not (Path('/usr/lib/modules') / platform.release()).exists():
            return {'required': True, 'reason': 'The running kernel was replaced'}
    if shutil.which('needs-restarting'):
        result = run(['needs-restarting', '-r'])
        if result.returncode == 1:
            return {'required': True, 'reason': 'needs-restarting requests a restart'}
        if result.returncode != 0:
            return {'required': None, 'reason': 'Restart check failed'}
    return {'required': False, 'reason': 'No restart requirement detected'}


def linux_manager():
    if shutil.which('omarchy-update') and shutil.which('pacman'):
        return 'omarchy'
    return next((m for m in ('pacman', 'apt', 'dnf') if shutil.which(m)), None)


def aur_updates():
    if not shutil.which('yay'):
        return []
    foreign = run(['pacman', '-Qem'])
    if foreign.returncode != 0 or not foreign.stdout.strip():
        return []
    completed = run(['yay', '-Qua'])
    if completed.returncode not in (0, 1):
        return []
    return [line.strip() for line in completed.stdout.splitlines() if ' -> ' in line]


def cursor_platform():
    machine = platform.machine()
    if machine in ('aarch64', 'arm64'):
        return 'linux-arm64'
    return 'linux-x64'


def cursor_update():
    """Official Cursor desktop vs installed cursor-bin. Omarchy's repo lags, so pacman will not list it."""
    installed = run(['pacman', '-Q', 'cursor-bin'])
    if installed.returncode != 0 or len(installed.stdout.split()) < 2:
        return []
    installed_ver = installed.stdout.split()[1].rsplit('-', 1)[0]
    url = 'https://cursor.com/api/download?platform=' + cursor_platform() + '&releaseTrack=stable'
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'Fleetlight'})
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode())
    except (OSError, ValueError, urllib.error.URLError, TimeoutError):
        return []
    official = data.get('version') if isinstance(data, dict) else None
    if not official:
        return []
    compared = run(['vercmp', installed_ver, official])
    try:
        if compared.returncode == 0 and int(compared.stdout.strip()) < 0:
            return [('cursor:official', 'cursor:official ' + installed_ver + ' → ' + official)]
    except ValueError:
        return []
    return []


def omarchy_extra_updates():
    names = []
    details = []
    if shutil.which('mise'):
        completed = run(['mise', 'outdated', '--json'], {'MISE_MINIMUM_RELEASE_AGE': '0'})
        if completed.returncode in (0, 1) and completed.stdout.strip().startswith('{'):
            try:
                data = json.loads(completed.stdout)
            except ValueError:
                data = {}
            if isinstance(data, dict):
                for name, value in data.items():
                    if isinstance(value, dict) and value.get('current') != value.get('latest'):
                        label = 'mise:' + name
                        names.append(label)
                        current = value.get('current') or ''
                        latest = value.get('latest') or ''
                        details.append(label + ' ' + current + ' → ' + latest if current and latest else label)
    if shutil.which('omarchy-migrate'):
        completed = run(['omarchy-migrate', '--pending'],
                        {'OMARCHY_PATH': os.environ.get('OMARCHY_PATH', '/usr/share/omarchy')})
        if completed.returncode <= 1 and completed.stdout.strip():
            names.append('omarchy:migrations')
            scripts = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
            details.append('Omarchy migrations: ' + ', '.join(scripts[:8]))
    for name, detail in cursor_update():
        if name not in names:
            names.append(name)
            details.append(detail)
    return names, details


def sidecar_updates():
    """Snap and Flatpak updates counted by the macOS/Android companion."""
    names = []
    details = []
    if shutil.which('snap'):
        completed = run(['snap', 'refresh', '--list'])
        if completed.returncode == 0 and 'All snaps up to date' not in completed.stdout:
            lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
            if lines and lines[0].split()[0].lower() == 'name':
                lines = lines[1:]
            for line in lines:
                parts = line.split()
                if not parts:
                    continue
                name = 'snap:' + parts[0]
                names.append(name)
                details.append(name + ' ' + parts[1] if len(parts) > 1 else name)
    if shutil.which('flatpak'):
        for scope in ('--user', '--system'):
            completed = run(['flatpak', 'remote-ls', '--updates', scope, '--columns=application,version'])
            if completed.returncode:
                continue
            for line in completed.stdout.splitlines():
                parts = line.split()
                if not parts or parts[0].lower() == 'application':
                    continue
                name = 'flatpak:' + parts[0]
                if name in names:
                    continue
                names.append(name)
                details.append(name + ' ' + parts[1] if len(parts) > 1 else name)
    return names, details


def install_sidecars():
    code = 0
    if shutil.which('snap') and subprocess.call(['sudo', '-n', 'snap', 'refresh']):
        code = 1
    if shutil.which('flatpak'):
        if subprocess.call(['flatpak', 'update', '-y', '--noninteractive', '--user']):
            code = 1
        if subprocess.call(['sudo', '-n', 'flatpak', 'update', '-y', '--noninteractive', '--system']):
            code = 1
    return code


def check():
    manager = linux_manager()
    result = {'manager': manager, 'packages': [], 'state': 'unknown', 'restart': reboot_status()}
    if platform.system() != 'Linux' or not manager:
        result.update(state='unsupported', detail='Unsupported operating system or package manager')
        return result
    if manager in ('pacman', 'omarchy'):
        if not shutil.which('checkupdates'):
            result['detail'] = 'Install pacman-contrib to check updates'
            return result
        with tempfile.TemporaryDirectory(prefix='fleetlight-system-check-') as directory:
            completed = run(['checkupdates', '--nocolor'], {'CHECKUPDATES_DB': directory + '/db'})
        if completed.returncode not in (0, 2):
            result['detail'] = 'Package metadata refresh failed'
            return result
        changes = [line.strip() for line in completed.stdout.splitlines() if ' -> ' in line]
        packages = [line.split()[0] for line in changes]
        protected = bool(packages) and run(['pacman', '-Q', 'openai-codex-desktop']).returncode == 0 and run(['pacman', '-Qkk', 'openai-codex-desktop']).returncode != 0
        if manager == 'omarchy' and not protected:
            for line in aur_updates():
                name = line.split()[0]
                if name not in packages:
                    packages.append(name)
                    changes.append(line)
            extra, extra_changes = omarchy_extra_updates()
            for name, detail in zip(extra, extra_changes):
                if name not in packages:
                    packages.append(name)
                    changes.append(detail)
    elif manager == 'apt':
        for attempt in range(3):
            # Keep this comfortably inside the controller's remote-check timeout.
            # `timeout` runs as root so it can also stop the apt child cleanly.
            # Without explicit bounds, apt can wait indefinitely for a stale lock or
            # a repository that accepts a connection but never responds.
            completed = run([
                'sudo', '-n', 'timeout', '--signal=TERM', '--kill-after=5s', '90s',
                'env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', '-q',
                '-o', 'APT::Update::Error-Mode=any',
                '-o', 'Acquire::Languages=none',
                '-o', 'Acquire::Retries=2',
                '-o', 'Acquire::http::Timeout=15',
                '-o', 'Acquire::https::Timeout=15',
                '-o', 'DPkg::Lock::Timeout=0',
                'update',
            ])
            output = completed.stdout.lower()
            if completed.returncode in (124, 137):
                result['detail'] = 'Package metadata refresh timed out; check repository, network, or package-manager activity and retry'
                result['error_output'] = completed.stdout[-4000:]
                return result
            locked = any(message in output for message in (
                'could not get lock', 'unable to acquire', 'held by process'))
            if not completed.returncode or not locked or attempt == 2:
                break
            time.sleep(3)
        if completed.returncode:
            if locked:
                result['detail'] = 'Another package operation is running; retry after it finishes'
            elif 'sudo:' in output and any(message in output for message in (
                    'password is required', 'not allowed', 'not in the sudoers')):
                result['detail'] = 'Passwordless sudo is required for package metadata refresh'
            else:
                result['detail'] = 'Package metadata refresh failed; check repository or network errors'
            result['error_output'] = completed.stdout[-4000:]
            return result
        completed = run(['apt-get', '-s', 'upgrade'])
        if completed.returncode:
            result['detail'] = 'Package upgrade planning failed'
            return result
        packages = []
        changes = []
        for line in completed.stdout.splitlines():
            if not line.startswith('Inst '):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            packages.append(parts[1])
            old = parts[2].strip('[]') if len(parts) > 2 and parts[2].startswith('[') else ''
            new = line.split('(', 1)[1].split()[0] if '(' in line else ''
            change = parts[1]
            if old or new:
                change += ' ' + old + ' → ' + new
            changes.append(change.strip())
        installed = run(['dpkg-query', '-W', '-f=${Status}', 'chatgpt']) if packages else None
        verification = run(['dpkg', '--verify', 'chatgpt']) if installed is not None and installed.stdout.strip() == 'install ok installed' else None
        protected = verification is not None and (verification.returncode != 0 or bool(verification.stdout.strip()))
    else:
        completed = run(['dnf', '-q', 'check-update', '--refresh'])
        if completed.returncode not in (0, 100):
            result['detail'] = 'Package metadata refresh failed'
            return result
        packages = []
        changes = []
        for line in completed.stdout.splitlines():
            parts = line.split()
            if len(parts) == 3 and '.' in parts[0]:
                packages.append(parts[0])
                changes.append(parts[0] + ' ' + parts[1])
        protected = False
    side_names, side_details = sidecar_updates()
    for name, detail in zip(side_names, side_details):
        if name not in packages:
            packages.append(name)
            changes.append(detail)
    result.update(packages=packages, changes=changes, state='protected' if protected else 'available' if packages else 'current',
                  detail='ChatGPT has local modifications; review system upgrades manually' if protected else str(len(packages)) + ' package updates')
    return result


def omarchy_env():
    env = dict(os.environ, LC_ALL='C', OMARCHY_UPDATE_LOGGED='1')
    env.setdefault('OMARCHY_PATH', '/usr/share/omarchy')
    env['PATH'] = env['OMARCHY_PATH'] + '/bin:' + env.get('PATH', '/usr/bin:/bin')
    return env


def update():
    print('FLEETLIGHT_SYSTEM_UPDATE\nPHASE:Refreshing and checking system packages', flush=True)
    checked = check()
    if checked['state'] not in ('available', 'current'):
        print('UPDATE:' + ('installation-invalid' if checked['state'] == 'protected' else 'refresh-failed'))
        print('VERIFY:failed')
        return 1
    if checked['packages']:
        print('PHASE:Installing system package updates', flush=True)
        for change in (checked.get('changes') or checked['packages'])[:80]:
            print('CHANGED:' + str(change).replace('\n', ' ')[:180], flush=True)
        # No timeout: interrupting a package manager can leave the system broken.
        # Skip omarchy-update's `script` PTY wrapper so gum cannot wait on a job with no operator.
        distro = [package for package in checked['packages'] if not package.startswith(('snap:', 'flatpak:'))]
        sidecars = [package for package in checked['packages'] if package.startswith(('snap:', 'flatpak:'))]
        if distro:
            if checked['manager'] == 'omarchy':
                if run(['sudo', '-n', 'true']).returncode != 0:
                    print('UPDATE:permission-required\nVERIFY:failed')
                    return 1
                code = subprocess.call(['omarchy-update', '-y'], env=omarchy_env())
            else:
                commands = {'pacman': ['sudo', '-n', 'env', 'OMARCHY_ALLOW_DIRECT_PACMAN=1', 'pacman', '-Syu', '--noconfirm'],
                            'apt': ['sudo', '-n', 'env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', '-y', '-o', 'Dpkg::Options::=--force-confold', 'upgrade'],
                            'dnf': ['sudo', '-n', 'dnf', '-y', 'upgrade']}
                code = subprocess.call(commands[checked['manager']])
            if code != 0:
                print('UPDATE:install-failed\nVERIFY:failed')
                return 1
        if sidecars:
            print('PHASE:Installing snap and Flatpak updates', flush=True)
            if install_sidecars():
                print('UPDATE:install-failed\nVERIFY:failed')
                return 1
    core = {'glibc', 'systemd', 'dbus', 'libc6', 'linux', 'linux-lts', 'linux-zen', 'linux-hardened'}
    if any(package.split('.')[0] in core for package in checked['packages']):
        marker = Path.home() / '.local/state/fleetlight/system-restart.json'
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}))
    print('PHASE:Checking the installed package state', flush=True)
    verified = check()
    if verified['state'] != 'current':
        print('UPDATE:verification-failed\nVERIFY:failed')
        return 1
    print('REBOOT:' + ('required' if verified['restart']['required'] else 'unknown' if verified['restart']['required'] is None else 'not-required'))
    print('VERIFY:ok')
    return 0


def restart():
    print('FLEETLIGHT_RESTART\nPHASE:Checking restart requirement', flush=True)
    if reboot_status()['required'] is not True:
        print('UPDATE:restart-not-required\nVERIFY:failed')
        return 1
    # Delay leaves time to persist the receipt, including when restarting this controller.
    completed = run(['sudo', '-n', 'shutdown', '-r', '+1'])
    if completed.returncode:
        print('UPDATE:permission-required\nVERIFY:failed')
        return 1
    print('VERIFY:ok')
    return 0


if __name__ == '__main__':
    operation = sys.argv[1] if len(sys.argv) > 1 else 'check'
    if operation == 'check':
        print('FLEETLIGHT_SYSTEM_CHECK=' + json.dumps(check()))
    elif operation == 'update':
        sys.exit(update())
    elif operation == 'restart':
        sys.exit(restart())
    else:
        sys.exit(2)

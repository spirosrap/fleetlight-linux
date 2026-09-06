"""Self-contained Linux package/restart operations for explicit Fleetlight jobs."""
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile


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


def check():
    manager = next((m for m in ('pacman', 'apt', 'dnf') if shutil.which(m)), None)
    result = {'manager': manager, 'packages': [], 'state': 'unknown', 'restart': reboot_status()}
    if platform.system() != 'Linux' or not manager:
        result.update(state='unsupported', detail='Unsupported operating system or package manager')
        return result
    if manager == 'pacman':
        if not shutil.which('checkupdates'):
            result['detail'] = 'Install pacman-contrib to check updates'
            return result
        with tempfile.TemporaryDirectory(prefix='fleetlight-system-check-') as directory:
            completed = run(['checkupdates', '--nocolor'], {'CHECKUPDATES_DB': directory + '/db'})
        if completed.returncode not in (0, 2):
            result['detail'] = 'Package metadata refresh failed'
            return result
        packages = [line.split()[0] for line in completed.stdout.splitlines() if ' -> ' in line]
        protected = 'openai-codex-desktop' in packages and run(['pacman', '-Qkk', 'openai-codex-desktop']).returncode != 0
    elif manager == 'apt':
        completed = run(['sudo', '-n', 'apt-get', 'update'])
        if completed.returncode:
            result['detail'] = 'Package metadata refresh needs passwordless sudo or repository repair'
            return result
        completed = run(['apt-get', '-s', 'upgrade'])
        if completed.returncode:
            result['detail'] = 'Package upgrade planning failed'
            return result
        packages = [line.split()[1] for line in completed.stdout.splitlines() if line.startswith('Inst ')]
        verification = run(['dpkg', '--verify', 'chatgpt']) if 'chatgpt' in packages else None
        protected = verification is not None and (verification.returncode != 0 or bool(verification.stdout.strip()))
    else:
        completed = run(['dnf', '-q', 'check-update', '--refresh'])
        if completed.returncode not in (0, 100):
            result['detail'] = 'Package metadata refresh failed'
            return result
        packages = [line.split()[0] for line in completed.stdout.splitlines() if len(line.split()) == 3 and '.' in line.split()[0]]
        protected = False
    result.update(packages=packages, state='protected' if protected else 'available' if packages else 'current',
                  detail='ChatGPT has local modifications and would be replaced' if protected else str(len(packages)) + ' package updates')
    return result


def update():
    print('FLEETLIGHT_SYSTEM_UPDATE\nPHASE:Refreshing and checking system packages', flush=True)
    checked = check()
    if checked['state'] not in ('available', 'current'):
        print('UPDATE:' + ('installation-invalid' if checked['state'] == 'protected' else 'refresh-failed'))
        print('VERIFY:failed')
        return 1
    if checked['packages']:
        print('PHASE:Installing system package updates', flush=True)
        commands = {'pacman': ['sudo', '-n', 'pacman', '-Syu', '--noconfirm'],
                    'apt': ['sudo', '-n', 'env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', '-y', '-o', 'Dpkg::Options::=--force-confold', 'upgrade'],
                    'dnf': ['sudo', '-n', 'dnf', '-y', 'upgrade']}
        # No timeout: interrupting a package manager can leave the system broken.
        if subprocess.call(commands[checked['manager']]) != 0:
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

"""Self-contained Linux package/restart operations for explicit Fleetlight jobs."""
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


def run(args, env=None):
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          env=dict(os.environ, LC_ALL='C', **(env or {})))


def current_boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip() or None
    except OSError:
        return None


def reboot_status():
    if platform.system() != 'Linux':
        return {'required': False, 'reason': 'Not Linux'}
    # The verdict is tied to the boot it was observed on, so a later boot can retire it.
    status = reboot_requirement()
    status['boot_id'] = current_boot_id()
    return status


def running_kernel_replaced(modules=Path('/usr/lib/modules'), release=None, owned=None, installed=None):
    """On Arch, the running kernel's modules directory either vanishes or, with kernel-modules-hook,
    is kept as an unowned copy; both mean the package now holds a different kernel."""
    directory = modules / (release or platform.release())
    if not directory.exists():
        return True
    try:
        pkgbase = (directory / 'pkgbase').read_text().strip()
    except OSError:
        return False  # not a packaged kernel (custom build): no verdict
    owned = owned or (lambda path: run(['pacman', '-Qqo', str(path)]).returncode == 0)
    installed = installed or (lambda name: run(['pacman', '-Qq', name]).returncode == 0)
    return bool(pkgbase) and installed(pkgbase) and not owned(directory / 'vmlinuz')


def reboot_requirement():
    marker = Path.home() / '.local/state/fleetlight/system-restart.json'
    try:
        recorded = json.loads(marker.read_text())
        if recorded.get('boot_id') == current_boot_id():
            return {'required': True, 'reason': 'Core system packages were updated; restart recommended'}
    except (OSError, ValueError):
        pass
    if Path('/run/reboot-required').exists():
        return {'required': True, 'reason': 'The operating system requests a restart'}
    if shutil.which('pacman') and Path('/usr/lib/modules').is_dir() and running_kernel_replaced():
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


def upgrade_lines(text):
    """Real pacman/yay upgrades only. Yay errors such as '-> 1 error occurred:' are not packages."""
    found = []
    for raw in str(text or "").splitlines():
        match = re.match(r"^(?:[A-Za-z0-9@._+-]+/)?([A-Za-z0-9@._+-]+)\s+(\S+)\s+->\s+(\S+)", raw.strip())
        if not match:
            continue
        name, old, new = match.group(1), match.group(2), match.group(3).rstrip(",")
        if not any(character.isdigit() for character in old + new):
            continue
        found.append((name, name + " " + old + " -> " + new))
    return found


def aur_updates():
    if not shutil.which('yay'):
        return []
    foreign = run(['pacman', '-Qem'])
    if foreign.returncode != 0 or not foreign.stdout.strip():
        return []
    completed = run(['yay', '-Qua'])
    if completed.returncode not in (0, 1):
        return []
    return [description for _, description in upgrade_lines(completed.stdout)]


AUR_RPC = 'https://aur.archlinux.org/rpc/v5/'
STATE = Path.home() / '.local/state/fleetlight'
HELD_FILE = STATE / 'held-packages.json'
# How long an AUR package that failed to build waits for its next try. A new AUR version is tried at once.
HOLD_DELAYS = (86400, 2 * 86400, 4 * 86400, 7 * 86400)
PACKAGE_NAME = r'[A-Za-z0-9@_+][A-Za-z0-9@._+-]*'


def aur_rpc(path, query=''):
    """Results of one AUR request; None when the AUR cannot be reached or answers nonsense."""
    try:
        request = urllib.request.Request(AUR_RPC + path + ('?' + query if query else ''), headers={'User-Agent': 'Fleetlight'})
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode())
    except (OSError, ValueError, urllib.error.URLError, TimeoutError):
        return None
    results = data.get('results') if isinstance(data, dict) else None
    return results if isinstance(results, list) else None


def missing_dependencies(names, dbpath=None):
    """{AUR package: [dependencies that nothing can supply]} for packages about to be upgraded.

    A dependency counts as available when it is installed, when a repository configured on this computer has
    it (or something providing it), or when the AUR does. The usual cause of a gap: the AUR follows Arch,
    while Omarchy's stable repositories trail it by days or weeks. Anything unknown counts as available,
    so the installer still gets to decide."""
    info = aur_rpc('info', urllib.parse.urlencode([('arg[]', name) for name in names]))
    if not info:
        return {}
    wanted = {}
    for item in info:
        if isinstance(item, dict) and item.get('Name') in names:
            wanted[item['Name']] = [dep for field in ('Depends', 'MakeDepends', 'CheckDepends')
                                    for dep in item.get(field) or [] if isinstance(dep, str) and dep.strip()]
    every = sorted({dep for deps in wanted.values() for dep in deps})
    if not every:
        return {}
    # pacman -T prints the dependencies no installed package satisfies.
    absent = [line.strip() for line in run(['pacman', '-T'] + every).stdout.splitlines() if line.strip() in every]
    database = ['--dbpath', dbpath] if dbpath else []
    absent = [dep for dep in absent
              if run(['pacman'] + database + ['-Sddp', '--print-format', '%n', '--noconfirm', dep]).returncode != 0]
    if not absent:
        return {}
    plain = {dep: re.split(r'[<>=]', dep)[0] for dep in absent}
    found = aur_rpc('info', urllib.parse.urlencode([('arg[]', name) for name in sorted(set(plain.values()))]))
    if found is None:
        return {}
    in_aur = {item.get('Name') for item in found if isinstance(item, dict)}
    gone = []
    for dep in absent:
        if plain[dep] in in_aur:
            continue
        providers = aur_rpc('search/' + urllib.parse.quote(plain[dep], safe='') , 'by=provides')
        if providers is None:
            return {}
        if not providers:
            gone.append(dep)
    return {name: [dep for dep in deps if dep in gone] for name, deps in wanted.items() if any(dep in gone for dep in deps)}


def load_holds():
    try:
        data = json.loads(HELD_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return {name: item for name, item in data.items() if isinstance(item, dict)} if isinstance(data, dict) else {}


def save_holds(holds):
    try:
        HELD_FILE.parent.mkdir(parents=True, exist_ok=True)
        HELD_FILE.write_text(json.dumps(holds))
    except OSError:
        pass


def held_updates(lines, dbpath=None, now=None):
    """Split pending AUR upgrades ("name old -> new") into the installable ones and the ones held back.

    Held back: a package whose dependencies nothing can supply yet (checked afresh every time, so it is
    released the moment they appear), and a package that recently failed to build (see hold_failed).
    One such package would otherwise fail every system update of this computer at its AUR step."""
    now = time.time() if now is None else now
    pending = {line.split()[0]: line for line in lines}
    holds = load_holds()
    # A hold belongs to one version of one pending package.
    kept = {name: item for name, item in holds.items() if name in pending and item.get('version') == pending[name].split()[-1]}
    if lines and kept != holds:
        save_holds(kept)
    held = [{'name': name, 'change': pending[name], 'reason': str(item.get('reason') or 'did not build')}
            for name, item in kept.items() if item.get('after', 0) > now]
    names = {item['name'] for item in held}
    missing = missing_dependencies([name for name in pending if name not in names], dbpath) if len(pending) > len(names) else {}
    for name, deps in missing.items():
        held.append({'name': name, 'change': pending[name],
                     'reason': 'needs ' + ', '.join(deps) + ', which the repositories here do not have yet'})
        names.add(name)
    return [line for name, line in pending.items() if name not in names], held


AUR_FAILURE = re.compile(r'No AUR package found for|error making:|error downloading sources:|exit status \d+')


def aur_failures(text):
    """{package: reason} for the AUR packages yay names as the cause of a failed run."""
    text = re.sub(r'\x1b\[[0-9;]*m', '', str(text or ''))
    found = {}
    for dep, owners in re.findall(r'No AUR package found for (\S+) \(required by: ([^)]+)\)', text):
        for name in re.split(r'[,\s]+', owners.strip()):
            if name:
                found.setdefault(name, 'needs ' + dep + ', which the repositories here do not have yet')
    for name in re.findall(r'error downloading sources: (' + PACKAGE_NAME + ')', text):
        found.setdefault(name, 'its sources could not be downloaded')
    for name in re.findall(r'(?:error making: |^\s*)(' + PACKAGE_NAME + r'?)\s?-\s?exit status \d+', text, re.M):
        found.setdefault(name, 'did not build')
    return found


def hold_failed(blocked, checked, now=None):
    """Set aside the pending AUR packages a failed run blamed, each for longer than the last time. Returns their names."""
    now = time.time() if now is None else now
    versions = {change.split()[0]: change.split()[-1] for change in checked.get('changes') or [] if change.split()}
    names = [name for name in blocked if name in (checked.get('aur') or []) and name in versions]
    if not names:
        return []
    holds = load_holds()
    for name in names:
        previous = holds.get(name, {})
        tries = previous.get('tries', 0) if previous.get('version') == versions[name] else 0
        holds[name] = {'version': versions[name], 'reason': blocked[name], 'tries': tries + 1,
                       'after': now + HOLD_DELAYS[min(tries, len(HOLD_DELAYS) - 1)]}
    save_holds(holds)
    return names


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
                    # Omarchy 4.5 declares lazy agent CLIs at "latest" in /etc/mise/conf.d; one
                    # that was never run has no current version and is not an update to install.
                    if isinstance(value, dict) and value.get('current') and value.get('current') != value.get('latest'):
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
            # The commit comes before the version because runtimes have no version. It tells one build from
            # the next: without it every later build looked like the one already installed automatically.
            completed = run(['flatpak', 'remote-ls', '--updates', scope, '--columns=application,commit,version'])
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
                details.append(' '.join([name] + parts[2:3] + parts[1:2]))
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
    held = []
    if platform.system() != 'Linux' or not manager:
        result.update(state='unsupported', detail='Unsupported operating system or package manager')
        return result
    if manager in ('pacman', 'omarchy'):
        if not shutil.which('checkupdates'):
            result['detail'] = 'Install pacman-contrib to check updates'
            return result
        scratch = tempfile.TemporaryDirectory(prefix='fleetlight-system-check-')
        try:
            for attempt in range(3):
                # A single mirror stall must not fail the check; retry with a clean database.
                database = scratch.name + '/db' + str(attempt)
                completed = run(['checkupdates', '--nocolor'], {'CHECKUPDATES_DB': database})
                if completed.returncode in (0, 2) or attempt == 2:
                    break
                time.sleep(3)
            if completed.returncode not in (0, 2):
                result['detail'] = 'Package metadata refresh failed'
                return result
            upgrades = upgrade_lines(completed.stdout)
            changes = [description for _, description in upgrades]
            packages = [name for name, _ in upgrades]
            protected = bool(packages) and run(['pacman', '-Q', 'openai-codex-desktop']).returncode == 0 and run(['pacman', '-Qkk', 'openai-codex-desktop']).returncode != 0
            if manager == 'omarchy' and not protected:
                # The database checkupdates just downloaded knows about packages the system's own copy may not list yet.
                installable, held = held_updates(aur_updates(), database)
                result['aur'] = []
                for line in installable:
                    name = line.split()[0]
                    if name not in packages:
                        packages.append(name)
                        changes.append(line)
                        result['aur'].append(name)
                extra, extra_changes = omarchy_extra_updates()
                for name, detail in zip(extra, extra_changes):
                    if name not in packages:
                        packages.append(name)
                        changes.append(detail)
        finally:
            scratch.cleanup()
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
        completed = run(['apt-get', '-s', 'full-upgrade'])
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
    if held:
        result['held'] = held
        if not protected:
            result['detail'] += ' · ' + str(len(held)) + ' held back'
    return result


# Download and mirror failures from pacman, apt and dnf: the update can simply be retried later.
NETWORK_FAILURE = re.compile(
    r"failed retrieving file|failed to synchronize all databases|Operation too slow|Could not resolve host"
    r"|Connection timed out|Failed to connect to|Temporary failure resolving|Could not connect to|Failed to fetch"
    r"|Curl error|Cannot download", re.I)


# A downloaded package that fails its checksum or signature: usually the repository replaced the file (a rebuild
# under the same version, a mirror in mid-sync). pacman deletes the bad file, so running the installer again fetches a fresh copy.
CORRUPT_DOWNLOAD = re.compile(r"invalid or corrupted (package|database)|Hash Sum mismatch", re.I)


def call_watching(argv, env=None, blocked=None):
    """Run an installer with its output passed through unchanged; also report a retryable failure it hit:
    'network' (download or mirror), 'corrupt' (a download failed its integrity check) or '' for neither.
    `blocked`, when given, collects {AUR package: reason} for the packages yay could not build."""
    sys.stdout.flush()
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    network = corrupt = False
    suspicious = []
    with process.stdout:
        for raw in iter(process.stdout.readline, b''):
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()
            line = raw.decode('utf-8', 'replace')
            network = network or bool(NETWORK_FAILURE.search(line))
            corrupt = corrupt or bool(CORRUPT_DOWNLOAD.search(line))
            if blocked is not None and AUR_FAILURE.search(line):
                suspicious.append(line)
    code = process.wait()
    if blocked is not None and code != 0:
        blocked.update(aur_failures(''.join(suspicious)))
    return code, 'network' if network else 'corrupt' if corrupt else ''


def install_distribution(manager, held=(), blocked=None):
    """Run the distribution's upgrade; a corrupted download gets one immediate second run, which re-downloads it.
    On Omarchy the AUR packages in `held` are left out, and `blocked` collects the ones that failed to build."""
    if manager == 'omarchy':
        options = {} if blocked is None else {'blocked': blocked}
        if not held:
            return install_with_one_redownload(['omarchy-update', '-y'], dict(options, env=omarchy_env()))
        with tempfile.TemporaryDirectory(prefix='yay-', dir=state_directory()) as directory:
            return install_with_one_redownload(['omarchy-update', '-y'], dict(options, env=omarchy_env(held, directory)))
    else:
        argv, options = {'pacman': ['sudo', '-n', 'env', 'OMARCHY_ALLOW_DIRECT_PACMAN=1', 'pacman', '-Syu', '--noconfirm'],
                         'apt': ['sudo', '-n', 'env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', '-y', '-o', 'Dpkg::Options::=--force-confold', 'full-upgrade'],
                         'dnf': ['sudo', '-n', 'dnf', '-y', 'upgrade']}[manager], {}
    return install_with_one_redownload(argv, options)


def install_with_one_redownload(argv, options):
    code, failure = call_watching(argv, **options)
    if code != 0 and failure == 'corrupt':
        print('PHASE:Downloading a corrupted package again', flush=True)
        code, failure = call_watching(argv, **options)
    return code, failure


def state_directory():
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        return str(STATE)
    except OSError:
        return None


def omarchy_env(held=(), directory=None):
    env = dict(os.environ, LC_ALL='C', OMARCHY_UPDATE_LOGGED='1')
    env.setdefault('OMARCHY_PATH', '/usr/share/omarchy')
    env['PATH'] = env['OMARCHY_PATH'] + '/bin:' + env.get('PATH', '/usr/bin:/bin')
    yay = shutil.which('yay', path=env['PATH']) if held and directory else None
    if yay:
        # omarchy-update cannot be told to skip an AUR package, and one that cannot be built fails the whole
        # update before its boot check. For this run only, yay is reached through a wrapper that ignores them.
        wrapper = Path(directory) / 'yay'
        wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(yay) + ' "$@" --ignore ' + shlex.quote(','.join(held)) + '\n')
        wrapper.chmod(0o700)
        env['PATH'] = directory + ':' + env['PATH']
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
            if checked['manager'] == 'omarchy' and run(['sudo', '-n', 'true']).returncode != 0:
                print('UPDATE:permission-required\nVERIFY:failed')
                return 1
            held = [item['name'] for item in checked.get('held') or []]
            blocked = {}
            code, failure = install_distribution(checked['manager'], held, blocked)
            aside = hold_failed(blocked, checked) if code != 0 else []
            if aside:
                # The rest of the update, its boot check included, must not wait for one AUR package.
                for name in aside:
                    print('HELD:' + name + ' ' + blocked[name], flush=True)
                print('PHASE:Finishing the update without ' + ', '.join(aside), flush=True)
                code, failure = install_distribution(checked['manager'], held + aside)
            if code != 0:
                print('UPDATE:' + {'network': 'network-failed', 'corrupt': 'package-corrupted'}.get(failure, 'install-failed') + '\nVERIFY:failed')
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
    if not installed_everything(checked, verified):
        print('UPDATE:verification-failed\nVERIFY:failed')
        return 1
    for change in (verified.get('changes') or verified.get('packages') or [])[:80]:
        print('NEW:' + str(change).replace('\n', ' ')[:180], flush=True)
    print('REBOOT:' + ('required' if verified['restart']['required'] else 'unknown' if verified['restart']['required'] is None else 'not-required'))
    print('VERIFY:ok')
    return 0


def installed_everything(checked, verified):
    """The packages that were pending at the start are now current.

    A release published while the installer ran is a new finding for the next check,
    not a failure of this one: the first update of the day used to be blamed for it,
    and the controller then stopped retrying that package name altogether.
    """
    if verified.get('state') == 'current':
        return True
    if verified.get('state') != 'available':
        return False
    remaining = set(verified.get('packages') or [])
    return not any(package in remaining for package in checked.get('packages') or [])

def restart(required=True):
    print('FLEETLIGHT_RESTART\nPHASE:Checking restart requirement', flush=True)
    # A restart asked for by hand on one computer does not need the system to request it.
    if required and reboot_status()['required'] is not True:
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
    elif operation == 'reboot':
        sys.exit(restart(required=False))
    else:
        sys.exit(2)

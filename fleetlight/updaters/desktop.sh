# Adapted from Fleetlight macOS (MIT). Keep package integrity and signed bundle checks.
case "${FLEETLIGHT_EXPECTED_VERSION:-}" in
  ''|*[!0-9.]*) printf 'UPDATE:target-invalid\nVERIFY:failed\n'; exit 2 ;;
esac
verify_reviewed_target() {
  if [ "$target_version" != "$FLEETLIGHT_EXPECTED_VERSION" ]; then
    printf 'UPDATE:target-changed\nVERIFY:failed\n'; exit 3
  fi
}
printf 'PHASE:Validating installation and update source\n'
printf 'FLEETLIGHT_CODEX_APP_UPDATE
'
platform=$(uname -s 2>/dev/null)
if [ "$platform" = Linux ]; then
  printf 'PLATFORM:linux
'
  if command -v pacman >/dev/null 2>&1 &&
     command -v vercmp >/dev/null 2>&1 &&
     pacman -Q openai-codex-desktop >/dev/null 2>&1; then
    package=openai-codex-desktop
    before_package_version=$(pacman -Q "$package" 2>/dev/null | awk 'NF >= 2 {print $2; exit}')
    before_version=${before_package_version#*:}
    before_version=${before_version%-*}
    printf 'BEFORE_VERSION:%s
' "$before_version"
    verify_chatgpt_pacman_installation() {
  expected_package_version=$1
  expected_app_version=$2
  installed_package_version=$(pacman -Q "$package" 2>/dev/null | awk 'NF >= 2 {print $2; exit}')
  [ "$installed_package_version" = "$expected_package_version" ] || return 1
  [ -x /usr/lib/chatgpt/ChatGPT ] && [ -x /usr/bin/chatgpt ] || return 1
  [ "$(pacman -Qoq /usr/lib/chatgpt/ChatGPT 2>/dev/null)" = "$package" ] || return 1
  [ "$(pacman -Qoq /usr/bin/chatgpt 2>/dev/null)" = "$package" ] || return 1
  install_metadata=/usr/lib/chatgpt/resources/linux-package-metadata.json
  [ "$(pacman -Qoq "$install_metadata" 2>/dev/null)" = "$package" ] || return 1
  install_metadata_version=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$install_metadata" 2>/dev/null | head -n 1)
  [ "$install_metadata_version" = "$expected_app_version" ] || return 1
  grep -Eq '"codexAppBrand"[[:space:]]*:[[:space:]]*"chatgpt"' "$install_metadata" 2>/dev/null || return 1
  grep -Eq '"codexBuildFlavor"[[:space:]]*:[[:space:]]*"prod"' "$install_metadata" 2>/dev/null || return 1
  pacman -Qkk "$package" >/dev/null 2>&1
}
    if ! verify_chatgpt_pacman_installation "$before_package_version" "$before_version"; then
      printf 'UPDATE:installation-invalid
VERIFY:failed
'
      exit 3
    fi
    printf 'PROVIDER:linux-pacman
SYSTEM_UPGRADE:1
'
    if ! command -v checkupdates >/dev/null 2>&1 || ! command -v fakeroot >/dev/null 2>&1; then
      printf 'UPDATE:source-missing
VERIFY:failed
'
      exit 3
    fi
    pacman_work=$(mktemp -d "${TMPDIR:-/tmp}/fleetlight-chatgpt-pacman-update.XXXXXX") || {
      printf 'UPDATE:staging-failed
VERIFY:failed
'
      exit 3
    }
    pacman_check_log="$pacman_work/check.log"
    pacman_update_log="$pacman_work/update.log"
    cleanup_pacman_update() { rm -rf "$pacman_work"; }
    trap cleanup_pacman_update EXIT HUP INT TERM
    CHECKUPDATES_DB="$pacman_work/db" checkupdates --nocolor >"$pacman_check_log" 2>&1
    pacman_refresh_status=$?
    if [ "$pacman_refresh_status" -ne 0 ] && [ "$pacman_refresh_status" -ne 2 ]; then
      tail -n 10 "$pacman_check_log" 2>/dev/null || true
      printf 'UPDATE:refresh-failed
VERIFY:failed
'
      exit 3
    fi
    target_package_version=$(pacman -Si --dbpath "$pacman_work/db" "$package" 2>/dev/null | awk -F: '/^Version[[:space:]]*:/ {value=$2; sub(/^[[:space:]]+/, "", value); print value; exit}')
    if [ -z "$target_package_version" ]; then
      printf 'UPDATE:candidate-missing
VERIFY:failed
'
      exit 3
    fi
    target_version=${target_package_version#*:}
    target_version=${target_version%-*}
    printf 'TARGET_VERSION:%s
AVAILABLE_VERSION:%s
' "$target_version" "$target_version"
    verify_reviewed_target
    comparison=$(vercmp "$before_package_version" "$target_package_version" 2>/dev/null || true)
    if [ -z "$comparison" ]; then
      printf 'UPDATE:candidate-invalid
VERIFY:failed
'
      exit 3
    elif [ "$comparison" -ge 0 ]; then
      printf 'AFTER_VERSION:%s
RELAUNCH:not-needed
UPDATE:current
VERIFY:current
' "$before_version"
      exit 0
    fi
    if ! sudo -n true >/dev/null 2>&1; then
      printf 'UPDATE:permission-required
VERIFY:failed
'
      exit 3
    fi

    main_pattern='^/usr/lib/chatgpt/ChatGPT([[:space:]]|$)'
    was_running=0
    if pgrep -f "$main_pattern" >/dev/null 2>&1; then was_running=1; fi
    printf 'PHASE:Installing the Arch system update\n'
    if ! sudo -n env OMARCHY_ALLOW_DIRECT_PACMAN=1 pacman -Syu --noconfirm >"$pacman_update_log" 2>&1; then
      tail -n 12 "$pacman_update_log" 2>/dev/null || true
      printf 'UPDATE:install-failed
VERIFY:failed
'
      exit 3
    fi
    after_package_version=$(pacman -Q "$package" 2>/dev/null | awk 'NF >= 2 {print $2; exit}')
    after_version=${after_package_version#*:}
    after_version=${after_version%-*}
    installed_comparison=$(vercmp "$after_package_version" "$target_package_version" 2>/dev/null || true)
    if [ -z "$installed_comparison" ] || [ "$installed_comparison" -lt 0 ] ||
       ! verify_chatgpt_pacman_installation "$after_package_version" "$after_version"; then
      printf 'AFTER_VERSION:%s
UPDATE:post-install-invalid
VERIFY:failed
' "$after_version"
      exit 3
    fi

    relaunch=not-needed
    if [ "$was_running" -eq 1 ]; then
      relaunch=failed
      main_pids=$(pgrep -f "$main_pattern" 2>/dev/null || true)
      if [ -n "$main_pids" ]; then kill -TERM $main_pids >/dev/null 2>&1 || true; fi
      stop_attempt=0
      while pgrep -f "$main_pattern" >/dev/null 2>&1 && [ "$stop_attempt" -lt 20 ]; do
        sleep 1
        stop_attempt=$((stop_attempt + 1))
      done
      if ! pgrep -f "$main_pattern" >/dev/null 2>&1 &&
         command -v systemd-run >/dev/null 2>&1 &&
         systemctl --user show-environment 2>/dev/null | grep -Eq '^(DISPLAY|WAYLAND_DISPLAY)='; then
        launch_unit=fleetlight-chatgpt-$(date +%s)-$$
        if systemd-run --user --collect --no-block --unit="$launch_unit" /usr/bin/chatgpt >/dev/null 2>&1; then
          launch_attempt=0
          while [ "$launch_attempt" -lt 15 ]; do
            if pgrep -f "$main_pattern" >/dev/null 2>&1; then relaunch=ok; break; fi
            sleep 1
            launch_attempt=$((launch_attempt + 1))
          done
        fi
      fi
    fi
    printf 'AFTER_VERSION:%s
RELAUNCH:%s
UPDATE:ok
VERIFY:updated
' "$after_version" "$relaunch"
    exit 0
  fi

  package=chatgpt
  source_file=/etc/apt/sources.list.d/chatgpt.sources
  keyring=/usr/share/keyrings/chatgpt-archive-keyring.gpg
  expected_repo=https://persistent.oaistatic.com/codex-app-prod/linux/deb
  expected_fingerprint=3BFA0E4AE8B8CC16A2D9BA684A3B4A566C4660E4

  package_status=$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)
  if [ "$package_status" != 'install ok installed' ]; then
    printf 'UPDATE:missing
VERIFY:failed
'
    exit 2
  fi
  before_version=$(dpkg-query -W -f='${Version}' "$package" 2>/dev/null)
  printf 'BEFORE_VERSION:%s
' "$before_version"
  verify_chatgpt_installation() {
  expected_install_version=$1
  installed_status=$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)
  [ "$installed_status" = 'install ok installed' ] || return 1
  [ -x /usr/lib/chatgpt/ChatGPT ] || return 1
  install_metadata=/usr/lib/chatgpt/resources/linux-package-metadata.json
  install_metadata_version=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$install_metadata" 2>/dev/null | head -n 1)
  [ "$install_metadata_version" = "$expected_install_version" ] || return 1
  grep -Eq '"codexAppBrand"[[:space:]]*:[[:space:]]*"chatgpt"' "$install_metadata" 2>/dev/null || return 1
  grep -Eq '"codexBuildFlavor"[[:space:]]*:[[:space:]]*"prod"' "$install_metadata" 2>/dev/null || return 1
  install_verify_output=$(dpkg -V "$package" 2>&1)
  install_verify_status=$?
  [ "$install_verify_status" -eq 0 ] && [ -z "$install_verify_output" ]
}

  if [ ! -r "$source_file" ] || [ ! -r "$keyring" ] || ! command -v gpg >/dev/null 2>&1; then
    printf 'UPDATE:source-missing
VERIFY:failed
'
    exit 3
  fi
  secure_root_file() {
  secure_path=$1
  [ -f "$secure_path" ] && [ ! -L "$secure_path" ] || return 1
  secure_owner=$(stat -c %u -- "$secure_path" 2>/dev/null) || return 1
  secure_mode=$(stat -c %a -- "$secure_path" 2>/dev/null) || return 1
  [ "$secure_owner" = 0 ] || return 1
  [ $((0$secure_mode & 0022)) -eq 0 ] || return 1
}
  if ! secure_root_file "$source_file" || ! secure_root_file "$keyring"; then
    printf 'UPDATE:source-invalid
VERIFY:failed
'
    exit 3
  fi
  source_schema=$(awk '
function finish_stanza() {
  if (in_stanza) { stanzas++; in_stanza = 0 }
}
BEGIN {
  valid = 1
  allowed["types"] = allowed["uris"] = allowed["suites"] = 1
  allowed["components"] = allowed["architectures"] = allowed["signed-by"] = 1
  allowed["x-repolib-name"] = 1
}
{
  line = $0
  sub(/\r$/, "", line)
  if (line ~ /^[[:space:]]*$/) { finish_stanza(); next }
  if (line ~ /^[[:space:]]*#/) { next }
  if (line ~ /^[ \t]/) { valid = 0; next }
  separator = index(line, ":")
  if (separator <= 1) { valid = 0; next }
  field = tolower(substr(line, 1, separator - 1))
  value = substr(line, separator + 1)
  gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
  if (!(field in allowed) || value == "" || seen[field]++) { valid = 0; next }
  in_stanza = 1
}
END {
  finish_stanza()
  complete = seen["types"] && seen["uris"] && seen["suites"] &&
             seen["components"] && seen["architectures"] && seen["signed-by"]
  print (valid && stanzas == 1 && complete) ? "valid" : "invalid"
}' "$source_file" 2>/dev/null)
  source_types=$(sed -n 's/^Types:[[:space:]]*//p' "$source_file")
  source_uri=$(sed -n 's/^URIs:[[:space:]]*//p' "$source_file")
  source_suites=$(sed -n 's/^Suites:[[:space:]]*//p' "$source_file")
  source_components=$(sed -n 's/^Components:[[:space:]]*//p' "$source_file")
  source_architectures=$(sed -n 's/^Architectures:[[:space:]]*//p' "$source_file")
  source_keyring=$(sed -n 's/^Signed-By:[[:space:]]*//p' "$source_file")
  primary_fingerprints=$(gpg --batch --no-default-keyring --keyring "$keyring" --with-colons --fingerprint 2>/dev/null | awk -F: '$1 == "pub" { primary = 1; next } $1 == "sub" { primary = 0; next } $1 == "fpr" && primary { print $10; primary = 0 }')
  if [ "$source_schema" != valid ] ||
     [ "$source_types" != deb ] ||
     [ "$source_uri" != "$expected_repo" ] ||
     [ "$source_suites" != stable ] ||
     [ "$source_components" != main ] ||
     [ "$source_architectures" != amd64 ] ||
     [ "$source_keyring" != "$keyring" ] ||
     [ "$primary_fingerprints" != "$expected_fingerprint" ]; then
    printf 'UPDATE:source-invalid
VERIFY:failed
'
    exit 3
  fi
  printf 'PROVIDER:linux-apt
'
  if ! sudo -n true >/dev/null 2>&1; then
    printf 'UPDATE:permission-required
VERIFY:failed
'
    exit 3
  fi

  update_log=$(mktemp "${TMPDIR:-/tmp}/fleetlight-chatgpt-update.XXXXXX") || {
    printf 'UPDATE:staging-failed
VERIFY:failed
'
    exit 3
  }
  cleanup_linux_update() { rm -f "$update_log"; }
  trap cleanup_linux_update EXIT HUP INT TERM
  if ! sudo -n env DEBIAN_FRONTEND=noninteractive apt-get -q -o APT::Update::Error-Mode=any update     -o Dir::Etc::sourcelist="$source_file"     -o Dir::Etc::sourceparts=-     -o APT::Get::List-Cleanup=0     -o Acquire::Languages=none     -o Acquire::Retries=2     -o Acquire::http::Timeout=15     -o Acquire::https::Timeout=15     -o DPkg::Lock::Timeout=30 >"$update_log" 2>&1; then
    tail -n 10 "$update_log" 2>/dev/null || true
    printf 'UPDATE:refresh-failed
VERIFY:failed
'
    exit 3
  fi

  target_version=$(apt-cache     -o Dir::Etc::sourcelist="$source_file"     -o Dir::Etc::sourceparts=-     policy "$package" 2>/dev/null | awk '/Candidate:/ {print $2; exit}')
  if [ -z "$target_version" ] || [ "$target_version" = '(none)' ]; then
    printf 'UPDATE:candidate-missing
VERIFY:failed
'
    exit 3
  fi
  printf 'TARGET_VERSION:%s
AVAILABLE_VERSION:%s
' "$target_version" "$target_version"
  verify_reviewed_target
  if ! dpkg --compare-versions "$before_version" lt "$target_version"; then
    if ! verify_chatgpt_installation "$before_version"; then
      printf 'AFTER_VERSION:%s
RELAUNCH:not-needed
UPDATE:installation-invalid
VERIFY:failed
' "$before_version"
      exit 3
    fi
    printf 'AFTER_VERSION:%s
RELAUNCH:not-needed
UPDATE:current
VERIFY:current
' "$before_version"
    exit 0
  fi

  was_running=0
  if pgrep -f '^/usr/lib/chatgpt/ChatGPT([[:space:]]|$)' >/dev/null 2>&1; then was_running=1; fi
  printf 'PHASE:Installing the ChatGPT package\n'
  if ! sudo -n env DEBIAN_FRONTEND=noninteractive apt-get -y     --only-upgrade     --no-install-recommends     -o Dir::Etc::sourcelist="$source_file"     -o Dir::Etc::sourceparts=-     -o APT::Get::List-Cleanup=0     -o Acquire::Languages=none     -o DPkg::Lock::Timeout=60     install "$package=$target_version" >"$update_log" 2>&1; then
    tail -n 12 "$update_log" 2>/dev/null || true
    printf 'UPDATE:install-failed
VERIFY:failed
'
    exit 3
  fi

  after_version=$(dpkg-query -W -f='${Version}' "$package" 2>/dev/null || true)
  if [ "$after_version" != "$target_version" ] ||
     ! verify_chatgpt_installation "$target_version"; then
    printf 'AFTER_VERSION:%s
UPDATE:post-install-invalid
VERIFY:failed
' "$after_version"
    exit 3
  fi

  relaunch=not-needed
  if [ "$was_running" -eq 1 ]; then
    relaunch=failed
    main_pids=$(pgrep -f '^/usr/lib/chatgpt/ChatGPT([[:space:]]|$)' 2>/dev/null || true)
    if [ -n "$main_pids" ]; then kill -TERM $main_pids >/dev/null 2>&1 || true; fi
    stop_attempt=0
    while pgrep -f '^/usr/lib/chatgpt/ChatGPT([[:space:]]|$)' >/dev/null 2>&1 && [ "$stop_attempt" -lt 20 ]; do
      sleep 1
      stop_attempt=$((stop_attempt + 1))
    done
    if ! pgrep -f '^/usr/lib/chatgpt/ChatGPT([[:space:]]|$)' >/dev/null 2>&1 &&
       command -v systemd-run >/dev/null 2>&1 &&
       systemctl --user show-environment 2>/dev/null | grep -Eq '^(DISPLAY|WAYLAND_DISPLAY)='; then
      launch_unit=fleetlight-chatgpt-$(date +%s)-$$
      if systemd-run --user --collect --no-block --unit="$launch_unit" /usr/bin/chatgpt >/dev/null 2>&1; then
        launch_attempt=0
        while [ "$launch_attempt" -lt 15 ]; do
          if pgrep -f '^/usr/lib/chatgpt/ChatGPT([[:space:]]|$)' >/dev/null 2>&1; then
            relaunch=ok
            break
          fi
          sleep 1
          launch_attempt=$((launch_attempt + 1))
        done
      fi
    fi
  fi
  printf 'AFTER_VERSION:%s
RELAUNCH:%s
UPDATE:ok
VERIFY:updated
' "$after_version" "$relaunch"
  exit 0
fi
if [ "$platform" != Darwin ]; then
  printf 'UPDATE:unsupported
VERIFY:failed
'
  exit 2
fi
printf 'PLATFORM:macos
PROVIDER:macos-appcast
'

if [ "$(uname -m)" != arm64 ]; then printf 'UPDATE:unsupported-architecture\nVERIFY:failed\n'; exit 2; fi
app=/Applications/ChatGPT.app
plist="$app/Contents/Info.plist"
if [ ! -r "$plist" ]; then
  printf 'UPDATE:missing
VERIFY:failed
'
  exit 2
fi
bundle_id=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$plist" 2>/dev/null)
if [ "$bundle_id" != com.openai.codex ]; then
  printf 'UPDATE:wrong-app
VERIFY:failed
'
  exit 2
fi

read_version() { /usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$1/Contents/Info.plist" 2>/dev/null; }
read_build() { /usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$1/Contents/Info.plist" 2>/dev/null; }
before_version=$(read_version "$app")
before_build=$(read_build "$app")
printf 'BEFORE_VERSION:%s
BEFORE_BUILD:%s
' "$before_version" "$before_build"

feed=https://persistent.oaistatic.com/codex-app-prod/appcast.xml
work=$(/usr/bin/mktemp -d /tmp/fleetlight-codex-update.XXXXXX) || {
  printf 'UPDATE:staging-failed
VERIFY:failed
'
  exit 3
}
archive="$work/ChatGPT.zip"
staged="$work/ChatGPT.app"
feed_file="$work/appcast.xml"
cleanup() { /bin/rm -rf "$work"; }
trap cleanup EXIT HUP INT TERM

if ! /usr/bin/curl -fsSL --connect-timeout 15 --max-time 60 --retry 2 "$feed" -o "$feed_file"; then
  printf 'UPDATE:feed-failed
VERIFY:failed
'
  exit 3
fi

target_version=$(/usr/bin/xmllint --xpath 'string((//*[local-name()="item"])[1]/*[local-name()="shortVersionString"])' "$feed_file" 2>/dev/null)
target_build=$(/usr/bin/xmllint --xpath 'string((//*[local-name()="item"])[1]/*[local-name()="version"])' "$feed_file" 2>/dev/null)
target_url=$(/usr/bin/xmllint --xpath 'string((//*[local-name()="item"])[1]/*[local-name()="enclosure" and not(ancestor::*[local-name()="deltas"])][1]/@url)' "$feed_file" 2>/dev/null)
case "$target_version:$target_build" in
  *[!0-9.:]*|:*|*:) printf 'UPDATE:feed-invalid
VERIFY:failed
'; exit 3 ;;
esac
case "$target_url" in
  https://persistent.oaistatic.com/codex-app-prod/ChatGPT-darwin-arm64-*.zip) ;;
  *) printf 'UPDATE:feed-invalid
VERIFY:failed
'; exit 3 ;;
esac
printf 'TARGET_VERSION:%s
TARGET_BUILD:%s
' "$target_version" "$target_build"

verify_reviewed_target
if [ "$target_build" != "${FLEETLIGHT_EXPECTED_BUILD:-}" ]; then
  printf 'UPDATE:target-changed\nVERIFY:failed\n'; exit 3
fi
if [ "$before_build" = "$target_build" ] && [ "$before_version" = "$target_version" ]; then
  printf 'AFTER_VERSION:%s
AFTER_BUILD:%s
UPDATE:current
VERIFY:current
' "$before_version" "$before_build"
  exit 0
fi
case "$before_build:$target_build" in
  *[!0-9:]*|:*|*:) printf 'UPDATE:feed-invalid
VERIFY:failed
'; exit 3 ;;
esac
if [ "$before_build" -gt "$target_build" ]; then
  printf 'AFTER_VERSION:%s
AFTER_BUILD:%s
UPDATE:current
VERIFY:current
' "$before_version" "$before_build"
  exit 0
fi

printf 'PHASE:Downloading the signed macOS application\n'
if ! /usr/bin/curl -fL --connect-timeout 15 --retry 3 --retry-delay 2 "$target_url" -o "$archive"; then
  printf 'UPDATE:download-failed
VERIFY:failed
'
  exit 3
fi
if ! /usr/bin/ditto -x -k "$archive" "$work" || [ ! -d "$staged" ]; then
  printf 'UPDATE:archive-invalid
VERIFY:failed
'
  exit 3
fi
printf 'PHASE:Verifying the macOS application signature\n'
staged_plist="$staged/Contents/Info.plist"
staged_id=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$staged_plist" 2>/dev/null)
staged_version=$(read_version "$staged")
staged_build=$(read_build "$staged")
staged_team=$(/usr/bin/codesign -dv --verbose=4 "$staged" 2>&1 | /usr/bin/sed -n 's/^TeamIdentifier=//p')
if [ "$staged_id" != com.openai.codex ] || [ "$staged_version" != "$target_version" ] || [ "$staged_build" != "$target_build" ] || [ "$staged_team" != 2DC432GLL2 ] || ! /usr/bin/codesign --verify --deep --strict "$staged" >/dev/null 2>&1; then
  printf 'UPDATE:signature-invalid
VERIFY:failed
'
  exit 3
fi

printf 'PHASE:Replacing ChatGPT and keeping a rollback copy\n'
/usr/bin/pkill -TERM -x ChatGPT >/dev/null 2>&1 || true
stop_attempt=0
while /usr/bin/pgrep -x ChatGPT >/dev/null 2>&1 && [ "$stop_attempt" -lt 20 ]; do
  sleep 1
  stop_attempt=$((stop_attempt + 1))
done
if /usr/bin/pgrep -x ChatGPT >/dev/null 2>&1; then
  printf 'UPDATE:app-busy
VERIFY:failed
'
  exit 3
fi

backup=/Applications/.Fleetlight-ChatGPT-backup.$$
restore_previous_app() {
  /bin/rm -rf "$app"
  /bin/mv "$backup" "$app" >/dev/null 2>&1
}
if ! /bin/mv "$app" "$backup"; then
  printf 'UPDATE:backup-failed
VERIFY:failed
'
  exit 3
fi
# The staged app and /Applications live on the macOS data volume, so
# moving the verified bundle makes the replacement nearly instant.
# This avoids a long partial-copy window for large Electron releases.
if ! /bin/mv "$staged" "$app"; then
  if restore_previous_app; then
    printf 'UPDATE:replace-failed
VERIFY:failed
'
  else
    printf 'UPDATE:rollback-failed
VERIFY:failed
'
  fi
  exit 3
fi
after_version=$(read_version "$app")
after_build=$(read_build "$app")
after_team=$(/usr/bin/codesign -dv --verbose=4 "$app" 2>&1 | /usr/bin/sed -n 's/^TeamIdentifier=//p')
if [ "$after_version" != "$target_version" ] || [ "$after_build" != "$target_build" ] || [ "$after_team" != 2DC432GLL2 ] || ! /usr/bin/codesign --verify --deep --strict "$app" >/dev/null 2>&1; then
  if restore_previous_app; then
    printf 'UPDATE:post-install-invalid
VERIFY:failed
'
  else
    printf 'UPDATE:rollback-failed
VERIFY:failed
'
  fi
  exit 3
fi
/bin/rm -rf "$backup"
printf 'PHASE:Reopening ChatGPT\n'
relaunch_ok=0
launch_attempt=1
while [ "$launch_attempt" -le 3 ] && [ "$relaunch_ok" -eq 0 ]; do
  case "$launch_attempt" in
    1) /usr/bin/open -gj "$app" >/dev/null 2>&1 || true ;;
    2) /bin/launchctl asuser "$(id -u)" /usr/bin/open -gj "$app" >/dev/null 2>&1 || true ;;
    3) /usr/bin/open -gj -b com.openai.codex >/dev/null 2>&1 || true ;;
  esac
  launch_wait=0
  while [ "$launch_wait" -lt 10 ]; do
    if /usr/bin/pgrep -x ChatGPT >/dev/null 2>&1; then
      relaunch_ok=1
      break
    fi
    sleep 1
    launch_wait=$((launch_wait + 1))
  done
  launch_attempt=$((launch_attempt + 1))
done
if [ "$relaunch_ok" -eq 1 ]; then
  relaunch=ok
else
  relaunch=failed
fi
printf 'AFTER_VERSION:%s
AFTER_BUILD:%s
RELAUNCH:%s
UPDATE:ok
VERIFY:updated
' "$after_version" "$after_build" "$relaunch"

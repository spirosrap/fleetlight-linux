# Install the checked Grok Bot release on a Mac from x.ai's official download, keeping a rollback copy.
# On Linux the grok-bot package comes with the system packages instead.
printf 'FLEETLIGHT_GROKBOT_UPDATE\nPHASE:Validating installation and update source\n'
target_version=${FLEETLIGHT_EXPECTED_VERSION:-}
if ! printf '%s\n' "$target_version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  printf 'UPDATE:target-invalid\nVERIFY:failed\n'; exit 2
fi
if [ "$(uname -s 2>/dev/null)" != Darwin ]; then printf 'UPDATE:unsupported\nVERIFY:failed\n'; exit 2; fi
case "$(uname -m)" in
  arm64) arch=arm64 ;;
  x86_64) arch=x64 ;;
  *) printf 'UPDATE:unsupported-architecture\nVERIFY:failed\n'; exit 2 ;;
esac
# x.ai builds the app on Cursor's platform: its identifier, signing team and update service are Anysphere's.
bundle=com.anysphere.sand
team=DCNK4UB866
read_key() { /usr/libexec/PlistBuddy -c "Print :$2" "$1/Contents/Info.plist" 2>/dev/null; }
read_team() { /usr/bin/codesign -dv --verbose=4 "$1" 2>&1 | /usr/bin/sed -n 's/^TeamIdentifier=//p'; }
reached() {
  python3 -c 'import re,sys; v=lambda x: tuple(map(int,x.split("."))); sys.exit(0 if all(re.fullmatch(r"\d+\.\d+\.\d+",x) for x in sys.argv[1:]) and v(sys.argv[1])>=v(sys.argv[2]) else 1)' "$1" "$target_version"
}
app=""
for folder in /Applications "$HOME/Applications"; do
  if [ -z "$app" ] && [ "$(read_key "$folder/Grok Bot.app" CFBundleIdentifier)" = "$bundle" ]; then app="$folder/Grok Bot.app"; fi
done
if [ -z "$app" ]; then printf 'UPDATE:missing\nVERIFY:failed\n'; exit 2; fi
before_version=$(read_key "$app" CFBundleShortVersionString)
printf 'BEFORE_VERSION:%s\nTARGET_VERSION:%s\n' "$before_version" "$target_version"
if reached "$before_version"; then
  printf 'AFTER_VERSION:%s\nRELAUNCH:not-needed\nUPDATE:current\nVERIFY:current\n' "$before_version"; exit 0
fi
parent=$(dirname "$app")
if [ ! -w "$parent" ]; then printf 'UPDATE:permission-required\nVERIFY:failed\n'; exit 2; fi

work=$(/usr/bin/mktemp -d /tmp/fleetlight-grokbot-update.XXXXXX) || { printf 'UPDATE:staging-failed\nVERIFY:failed\n'; exit 3; }
image="$work/GrokBot.dmg"
mount="$work/mount"
staged="$work/Grok Bot.app"
cleanup() {
  /usr/bin/hdiutil detach "$mount" -force >/dev/null 2>&1
  /bin/rm -rf "$work"
}
trap cleanup EXIT HUP INT TERM

if ! /usr/bin/curl -fsSL -A Fleetlight/0.2 --connect-timeout 15 --max-time 60 --retry 2 \
    "https://api2.cursor.sh/updates/api/download/stable/darwin-$arch/sand" -o "$work/release.json"; then
  printf 'UPDATE:feed-failed\nVERIFY:failed\n'; exit 3
fi
feed_version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("version") or "")' "$work/release.json" 2>/dev/null)
url=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("downloadUrl") or "")' "$work/release.json" 2>/dev/null)
if [ "$feed_version" != "$target_version" ]; then printf 'UPDATE:target-changed\nVERIFY:failed\n'; exit 3; fi
case "$url" in
  *[!A-Za-z0-9._/:-]*) printf 'UPDATE:feed-failed\nVERIFY:failed\n'; exit 3 ;;
  https://downloads.cursor.com/grokbot/*.dmg) ;;
  *) printf 'UPDATE:feed-failed\nVERIFY:failed\n'; exit 3 ;;
esac

printf 'PHASE:Downloading the signed macOS application\n'
if ! /usr/bin/curl -fsSL --connect-timeout 15 --retry 3 --retry-delay 2 "$url" -o "$image"; then
  printf 'UPDATE:download-failed\nVERIFY:failed\n'; exit 3
fi
/bin/mkdir "$mount"
if ! /usr/bin/hdiutil attach "$image" -nobrowse -readonly -noautoopen -mountpoint "$mount" >/dev/null 2>&1 \
    || [ ! -d "$mount/Grok Bot.app" ] || ! /usr/bin/ditto "$mount/Grok Bot.app" "$staged"; then
  printf 'UPDATE:download-failed\nVERIFY:failed\n'; exit 3
fi
/usr/bin/hdiutil detach "$mount" -force >/dev/null 2>&1

printf 'PHASE:Verifying the macOS application signature\n'
# The release must be the checked one, signed by the same developer and notarized by Apple.
genuine() {
  [ "$(read_key "$1" CFBundleIdentifier)" = "$bundle" ] && [ "$(read_key "$1" CFBundleShortVersionString)" = "$target_version" ] \
    && [ "$(read_team "$1")" = "$team" ] && /usr/bin/codesign --verify --deep --strict "$1" >/dev/null 2>&1 \
    && /usr/sbin/spctl --assess --type execute "$1" >/dev/null 2>&1
}
if ! genuine "$staged"; then printf 'UPDATE:signature-invalid\nVERIFY:failed\n'; exit 3; fi

printf 'PHASE:Replacing Grok Bot and keeping a rollback copy\n'
was_running=0
if /usr/bin/pgrep -x 'Grok Bot' >/dev/null 2>&1; then
  was_running=1
  /usr/bin/pkill -TERM -x 'Grok Bot' >/dev/null 2>&1 || true
  waited=0
  while /usr/bin/pgrep -x 'Grok Bot' >/dev/null 2>&1 && [ "$waited" -lt 20 ]; do
    sleep 1
    waited=$((waited + 1))
  done
  if /usr/bin/pgrep -x 'Grok Bot' >/dev/null 2>&1; then printf 'UPDATE:app-busy\nVERIFY:failed\n'; exit 3; fi
fi
backup="$parent/.Fleetlight-GrokBot-backup.$$"
restore_previous_app() {
  /bin/rm -rf "$app"
  /bin/mv "$backup" "$app" >/dev/null 2>&1
}
reopen() {
  relaunch=not-needed
  if [ "$was_running" -eq 1 ]; then
    relaunch=failed
    /usr/bin/open -gj "$app" >/dev/null 2>&1 || /bin/launchctl asuser "$(id -u)" /usr/bin/open -gj "$app" >/dev/null 2>&1 || true
    waited=0
    while [ "$waited" -lt 15 ] && [ "$relaunch" = failed ]; do
      if /usr/bin/pgrep -x 'Grok Bot' >/dev/null 2>&1; then relaunch=ok; else sleep 1; fi
      waited=$((waited + 1))
    done
  fi
}
if ! /bin/mv "$app" "$backup"; then printf 'UPDATE:backup-failed\nVERIFY:failed\n'; exit 3; fi
# The staged app and the Applications folder are on the same volume, so the swap is a rename.
if ! /bin/mv "$staged" "$app" || ! genuine "$app"; then
  if restore_previous_app; then
    reopen
    printf 'UPDATE:post-install-invalid\nVERIFY:failed\n'
  else
    printf 'UPDATE:rollback-failed\nVERIFY:failed\n'
  fi
  exit 3
fi
/bin/rm -rf "$backup"
if [ "$was_running" -eq 1 ]; then printf 'PHASE:Reopening Grok Bot\n'; fi
reopen
printf 'AFTER_VERSION:%s\nRELAUNCH:%s\nUPDATE:ok\nVERIFY:updated\n' "$(read_key "$app" CFBundleShortVersionString)" "$relaunch"

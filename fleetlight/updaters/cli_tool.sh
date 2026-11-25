# Update a command-line agent that is already installed to at least the checked release.
# The controller puts `tool=agy` or `tool=opencode` on a line before this script.
case "${tool:-}" in
  agy)
    label='Antigravity CLI'; marker=FLEETLIGHT_AGY_UPDATE; mise_name=antigravity-cli
    native_home=.local/bin/agy; shells='-lc -ic'; programs='agy antigravity' ;;
  opencode)
    label=OpenCode; marker=FLEETLIGHT_OPENCODE_UPDATE; mise_name=opencode
    # OpenCode's installer adds itself to PATH in the interactive shell's file only.
    native_home=.opencode/bin/opencode; shells='-ic -lc'; programs='opencode bin/opencode node_modules/.bin/opencode' ;;
  *) printf 'UPDATE:target-invalid\nVERIFY:failed\n'; exit 2 ;;
esac
printf '%s\nPHASE:Inspecting %s\n' "$marker" "$label"
target_version=${FLEETLIGHT_EXPECTED_VERSION:-}
if ! printf '%s\n' "$target_version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  printf 'UPDATE:target-invalid\nVERIFY:failed\n'; exit 2
fi
shell_bin=${SHELL:-/bin/sh}
active_path=""
for flag in $shells; do
  if [ ! -x "$active_path" ]; then
    active_path=$("$shell_bin" "$flag" "command -v $tool" 2>/dev/null | tail -n 1 | tr -d '\r')
  fi
done
if [ ! -x "$active_path" ]; then
  active_path=$(command -v "$tool" 2>/dev/null || true)
fi
if [ ! -x "$active_path" ] && [ -x "$HOME/$native_home" ]; then active_path="$HOME/$native_home"; fi
if [ ! -x "$active_path" ]; then printf 'UPDATE:missing\nVERIFY:failed\n'; exit 2; fi
real_path=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$active_path")
native_path=$(python3 -c 'import os,sys; print(os.path.realpath(os.path.join(os.path.expanduser("~"), sys.argv[1])))' "$native_home")
# Only sniff text scripts for a mise launcher; the program itself may contain the word.
launcher=""
if [ "$(head -c 2 "$active_path" 2>/dev/null)" = '#!' ]; then
  launcher=$(head -c 500 "$active_path" 2>/dev/null)
fi
if printf '%s\n' "$active_path:$real_path" | grep -q '/mise/' || [ "$(basename "$real_path")" = mise ] || printf '%s\n' "$launcher" | grep -q 'mise '; then
  mode=mise
elif [ "$real_path" = "$native_path" ]; then
  mode=native
else
  printf 'UPDATE:unsupported-installation\nVERIFY:failed\n'; exit 2
fi
# mise lists these tools as lazy on Omarchy: running a shim would download whatever release is newest.
# The newest release in mise's own folder is the installed one.
mise_version() {
  python3 -c '
import os, re, sys
root = os.path.join(os.environ.get("MISE_DATA_DIR") or os.path.expanduser("~/.local/share/mise"), "installs", sys.argv[1])
found = []
try:
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if re.fullmatch(r"\d+\.\d+\.\d+", name) and not os.path.islink(path) and any(
                os.access(os.path.join(path, item), os.X_OK) for item in sys.argv[2].split()):
            found.append(tuple(map(int, name.split("."))))
except OSError:
    pass
print(".".join(map(str, max(found))) if found else "")' "$mise_name" "$programs"
}
version_now() {
  if [ "$mode" = mise ]; then
    mise_version
  else
    "$active_path" --version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1
  fi
}
reached() {
  python3 -c 'import re,sys; v=lambda x: tuple(map(int,x.split("."))); sys.exit(0 if all(re.fullmatch(r"\d+\.\d+\.\d+",x) for x in sys.argv[1:]) and v(sys.argv[1])>=v(sys.argv[2]) else 1)' "$1" "$target_version"
}
before_version=$(version_now)
printf 'BEFORE_VERSION:%s\nTARGET_VERSION:%s\n' "$before_version" "$target_version"
if reached "$before_version"; then
  printf 'ACTIVE_VERSION:%s\nUPDATE:current\nVERIFY:ok\n' "$before_version"; exit 0
fi
printf 'UPDATE_MODE:%s\nPHASE:Installing %s using %s\n' "$mode" "$label" "$mode"
# The interpolated version is restricted to digits and dots above.
case "$mode" in
  mise)
    "$shell_bin" -ic "MISE_MINIMUM_RELEASE_AGE=0 mise use --global --yes $mise_name@$target_version && mise reshim"
    status=$?
    if [ "$status" -ne 0 ]; then
      # mise's versions host can lag a new release by hours and answers 404 for it.
      printf 'PHASE:Installing %s using mise, directly from the publisher\n' "$label"
      "$shell_bin" -ic "MISE_USE_VERSIONS_HOST=false MISE_MINIMUM_RELEASE_AGE=0 mise use --global --yes $mise_name@$target_version && mise reshim" \
        || { printf 'UPDATE:download-failed\nVERIFY:failed\n'; exit 1; } # Automatic updates retry this later.
      status=0
    fi
    ;;
  native)
    # Each program's own updater downloads, verifies and swaps the release in place.
    case "$tool" in
      # `agy update` takes no version, so the result is checked against the release Fleetlight saw.
      agy) "$active_path" update </dev/null ;;
      opencode) "$active_path" upgrade "$target_version" </dev/null ;;
    esac
    status=$?
    ;;
esac
printf 'PHASE:Verifying the active %s version\n' "$label"
after=$(version_now)
printf 'ACTIVE_VERSION:%s\n' "$after"
if [ "$status" -eq 0 ] && reached "$after"; then
  if [ "$tool" = agy ] && [ "$mode" = native ]; then
    # `agy update` leaves the replaced program (over 200 MB) beside the new one until agy is next started.
    rm -f "$active_path".[0-9]*.old
  fi
  printf 'UPDATE:ok\nVERIFY:ok\n'; exit 0
fi
printf 'UPDATE:verification-failed\nVERIFY:failed\n'; exit 1

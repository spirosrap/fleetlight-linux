# Native Codex updater; based on Fleetlight's MIT-licensed macOS implementation.
printf 'FLEETLIGHT_CODEX_UPDATE\nPHASE:Inspecting Codex installation\n'
target_version=${FLEETLIGHT_EXPECTED_VERSION:-}
if ! printf '%s\n' "$target_version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  printf 'UPDATE:target-invalid\nVERIFY:failed\n'; exit 2
fi
shell_bin=${SHELL:-/bin/sh}
# Follow the interactive user's active installation, rather than an unrelated global npm copy.
active_path=$("$shell_bin" -ic 'command -v codex' 2>/dev/null | tail -n 1 | tr -d '\r')
if [ ! -x "$active_path" ]; then
  active_path=$(command -v codex 2>/dev/null || true)
fi
if [ ! -x "$active_path" ]; then printf 'UPDATE:missing\nVERIFY:failed\n'; exit 2; fi
before_version=$("$active_path" --version 2>/dev/null | sed -n 's/^codex-cli //p' | tail -n 1)
printf 'BEFORE_VERSION:%s\nTARGET_VERSION:%s\n' "$before_version" "$target_version"
if python3 -c 'import re,sys; v=lambda x: tuple(map(int,x.split("."))); sys.exit(0 if all(re.fullmatch(r"\d+\.\d+\.\d+",x) for x in sys.argv[1:]) and v(sys.argv[1])>=v(sys.argv[2]) else 1)' "$before_version" "$target_version"; then
  printf 'ACTIVE_VERSION:%s\nUPDATE:current\nVERIFY:ok\n' "$before_version"; exit 0
fi
real_path=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$active_path")
case "$active_path:$real_path" in
  *'/mise/shims/'*|*'/mise/installs/codex/'*) mode=mise ;;
  *'/node_modules/@openai/codex/'*) mode=npm ;;
  *'/.codex/packages/standalone/'*) mode=standalone ;;
  *) printf 'UPDATE:unsupported-installation\nVERIFY:failed\n'; exit 2 ;;
esac
printf 'UPDATE_MODE:%s\nPHASE:Installing Codex using %s\n' "$mode" "$mode"
# All interpolated versions are restricted to digits and dots above.
case "$mode" in
  mise)
    "$shell_bin" -ic "mise use --global --yes codex@$target_version && mise reshim"
    status=$?
    ;;
  npm)
    "$shell_bin" -ic "npm install --global @openai/codex@$target_version --registry=https://registry.npmjs.org"
    status=$?
    ;;
  standalone)
    # `codex update` follows the standalone channel, which can lag the npm release Fleetlight checked.
    curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 CODEX_RELEASE="$target_version" sh
    status=$?
    ;;
esac
printf 'PHASE:Verifying the active Codex version\n'
after=$("$shell_bin" -ic 'codex --version' 2>/dev/null | sed -n 's/^codex-cli //p' | tail -n 1 | tr -d '\r')
if [ -z "$after" ]; then after=$("$active_path" --version 2>/dev/null | sed -n 's/^codex-cli //p' | tail -n 1); fi
printf 'ACTIVE_VERSION:%s\n' "$after"
# The requested version is pinned for standalone installs. Never report an older version as success.
if [ "$status" -eq 0 ] && python3 -c 'import re,sys; v=lambda x: tuple(map(int,x.split("."))); sys.exit(0 if all(re.fullmatch(r"\d+\.\d+\.\d+",x) for x in sys.argv[1:]) and v(sys.argv[1])>=v(sys.argv[2]) else 1)' "$after" "$target_version"; then
  printf 'UPDATE:ok\nVERIFY:ok\n'; exit 0
fi
printf 'UPDATE:verification-failed\nVERIFY:failed\n'; exit 1

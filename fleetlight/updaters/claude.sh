# Install or update the official Claude Code CLI to the checked release.
printf 'FLEETLIGHT_CLAUDE_UPDATE\nPHASE:Inspecting Claude CLI\n'
target_version=${FLEETLIGHT_EXPECTED_VERSION:-}
if ! printf '%s\n' "$target_version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  printf 'UPDATE:target-invalid\nVERIFY:failed\n'; exit 2
fi
shell_bin=${SHELL:-/bin/sh}
export PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:$PATH"
active_path=$("$shell_bin" -lc 'command -v claude' 2>/dev/null | tail -n 1 | tr -d '\r')
if [ ! -x "$active_path" ]; then
  active_path=$(command -v claude 2>/dev/null || true)
fi
version_of() {
  "$1" --version 2>/dev/null | awk 'NR == 1 { print $1 }' | tr -d '\r'
}
before_version=""
if [ -x "$active_path" ]; then
  before_version=$(version_of "$active_path")
fi
printf 'BEFORE_VERSION:%s\nTARGET_VERSION:%s\n' "$before_version" "$target_version"
if [ -n "$before_version" ] && python3 -c 'import re,sys; v=lambda x: tuple(map(int,x.split("."))); sys.exit(0 if all(re.fullmatch(r"\d+\.\d+\.\d+",x) for x in sys.argv[1:]) and v(sys.argv[1])>=v(sys.argv[2]) else 1)' "$before_version" "$target_version"; then
  printf 'ACTIVE_VERSION:%s\nUPDATE:current\nVERIFY:ok\n' "$before_version"; exit 0
fi
if [ ! -x "$active_path" ]; then
  mode=native
else
  real_path=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$active_path")
  # Omarchy's ~/.local/bin/claude is a mise launcher, not the native binary.
  if printf '%s\n' "$active_path:$real_path" | grep -q '/mise/' || grep -q 'mise ' "$active_path" 2>/dev/null; then
    mode=mise
  else
    case "$active_path:$real_path" in
      *'node_modules/@anthropic-ai/claude-code/'*) mode=npm ;;
      *'/.local/share/claude/'*|*'/.local/bin/claude'*) mode=native ;;
      *) printf 'UPDATE:unsupported-installation\nVERIFY:failed\n'; exit 2 ;;
    esac
  fi
fi
printf 'UPDATE_MODE:%s\nPHASE:Installing Claude CLI using %s\n' "$mode" "$mode"
case "$mode" in
  mise)
    "$shell_bin" -ic "MISE_MINIMUM_RELEASE_AGE=0 mise use --global --yes claude@$target_version && mise reshim"
    status=$?
    ;;
  npm)
    "$shell_bin" -lc "npm install --global @anthropic-ai/claude-code@$target_version --registry=https://registry.npmjs.org"
    status=$?
    ;;
  native)
    curl -fsSL https://claude.ai/install.sh | bash -s "$target_version"
    status=$?
    ;;
esac
printf 'PHASE:Verifying the active Claude CLI version\n'
after=$("$shell_bin" -lc 'claude --version' 2>/dev/null | awk 'NR == 1 { print $1 }' | tr -d '\r')
if [ -z "$after" ] && [ -x "$HOME/.local/bin/claude" ]; then after=$(version_of "$HOME/.local/bin/claude"); fi
if [ -z "$after" ] && [ -x "$active_path" ]; then after=$(version_of "$active_path"); fi
printf 'ACTIVE_VERSION:%s\n' "$after"
if [ "$status" -eq 0 ] && python3 -c 'import re,sys; v=lambda x: tuple(map(int,x.split("."))); sys.exit(0 if all(re.fullmatch(r"\d+\.\d+\.\d+",x) for x in sys.argv[1:]) and v(sys.argv[1])>=v(sys.argv[2]) else 1)' "$after" "$target_version"; then
  printf 'UPDATE:ok\nVERIFY:ok\n'; exit 0
fi
printf 'UPDATE:verification-failed\nVERIFY:failed\n'; exit 1

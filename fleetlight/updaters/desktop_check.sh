# Adapted from Fleetlight macOS (MIT). Checks refresh metadata, never install packages.
printf 'FLEETLIGHT_CODEX_APP_RELEASE_CHECK
'
platform=$(uname -s 2>/dev/null)
if [ "$platform" = Darwin ]; then
  printf 'PLATFORM:macos
PROVIDER:macos-appcast
CHECK:external
VERIFY:delegated
'
  exit 0
fi
if [ "$platform" != Linux ]; then
  printf 'CHECK:unsupported
VERIFY:failed
'
  exit 2
fi
printf 'PLATFORM:linux
'

if command -v pacman >/dev/null 2>&1 &&
   command -v vercmp >/dev/null 2>&1 &&
   pacman -Q openai-codex-desktop >/dev/null 2>&1; then
  package=openai-codex-desktop
  installed_package_version=$(pacman -Q "$package" 2>/dev/null | awk 'NF >= 2 {print $2; exit}')
  installed_version=${installed_package_version#*:}
  installed_version=${installed_version%-*}
  printf 'INSTALLED_VERSION:%s
' "$installed_version"
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
  return 0
}
  if ! verify_chatgpt_pacman_installation "$installed_package_version" "$installed_version"; then
    printf 'CHECK:installation-invalid
VERIFY:failed
'
    exit 3
  fi
  printf 'PROVIDER:linux-pacman
'
  if ! pacman -Qkk "$package" >/dev/null 2>&1; then
    printf 'INSTALLATION:modified
'
  fi
  if ! command -v checkupdates >/dev/null 2>&1 || ! command -v fakeroot >/dev/null 2>&1; then
    printf 'CHECK:source-missing
VERIFY:failed
'
    exit 3
  fi
  pacman_check_db=$(mktemp -d "${TMPDIR:-/tmp}/fleetlight-chatgpt-pacman.XXXXXX") || {
  printf 'CHECK:staging-failed
VERIFY:failed
'
  exit 3
}
pacman_check_log="$pacman_check_db/check.log"
cleanup_pacman_check() { rm -rf "$pacman_check_db"; }
trap cleanup_pacman_check EXIT HUP INT TERM
CHECKUPDATES_DB="$pacman_check_db/db" checkupdates --nocolor >"$pacman_check_log" 2>&1
pacman_refresh_status=$?
if [ "$pacman_refresh_status" -ne 0 ] && [ "$pacman_refresh_status" -ne 2 ]; then
  tail -n 8 "$pacman_check_log" 2>/dev/null || true
  printf 'CHECK:refresh-failed
VERIFY:failed
'
  exit 3
fi
pacman_db_path="$pacman_check_db/db"
printf 'METADATA:refreshed
'
  if [ -n "$pacman_db_path" ]; then
    candidate_package_version=$(pacman -Si --dbpath "$pacman_db_path" "$package" 2>/dev/null | awk -F: '/^Version[[:space:]]*:/ {value=$2; sub(/^[[:space:]]+/, "", value); print value; exit}')
  else
    candidate_package_version=$(pacman -Si "$package" 2>/dev/null | awk -F: '/^Version[[:space:]]*:/ {value=$2; sub(/^[[:space:]]+/, "", value); print value; exit}')
  fi
  if [ -z "$candidate_package_version" ]; then
    printf 'CHECK:candidate-missing
VERIFY:failed
'
    exit 3
  fi
  available_version=${candidate_package_version#*:}
  available_version=${available_version%-*}
  printf 'AVAILABLE_VERSION:%s
' "$available_version"
  comparison=$(vercmp "$installed_package_version" "$candidate_package_version" 2>/dev/null || true)
  if [ -z "$comparison" ]; then
    printf 'CHECK:candidate-invalid
VERIFY:failed
'
    exit 3
  elif [ "$comparison" -lt 0 ]; then
    printf 'UPDATE_AVAILABLE:1
CHECK:update-available
VERIFY:ok
'
  else
    printf 'UPDATE_AVAILABLE:0
CHECK:current
VERIFY:ok
'
  fi
  exit 0
fi

package=chatgpt
source_file=/etc/apt/sources.list.d/chatgpt.sources
keyring=/usr/share/keyrings/chatgpt-archive-keyring.gpg
expected_repo=https://persistent.oaistatic.com/codex-app-prod/linux/deb
expected_fingerprint=3BFA0E4AE8B8CC16A2D9BA684A3B4A566C4660E4

package_status=$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)
if [ "$package_status" != 'install ok installed' ]; then
  printf 'CHECK:missing
VERIFY:ok
'
  exit 0
fi
installed_version=$(dpkg-query -W -f='${Version}' "$package" 2>/dev/null)
printf 'INSTALLED_VERSION:%s
' "$installed_version"
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
if ! verify_chatgpt_installation "$installed_version"; then
  printf 'CHECK:installation-invalid
VERIFY:failed
'
  exit 3
fi

if [ ! -r "$source_file" ] || [ ! -r "$keyring" ] || ! command -v gpg >/dev/null 2>&1; then
  printf 'CHECK:source-missing
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
  printf 'CHECK:source-invalid
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
  printf 'CHECK:source-invalid
VERIFY:failed
'
  exit 3
fi
printf 'PROVIDER:linux-apt
'
if ! sudo -n true >/dev/null 2>&1; then
  printf 'CHECK:permission-required
VERIFY:failed
'
  exit 3
fi
check_log=$(mktemp "${TMPDIR:-/tmp}/fleetlight-chatgpt-check.XXXXXX") || {
  printf 'CHECK:staging-failed
VERIFY:failed
'
  exit 3
}
cleanup() { rm -f "$check_log"; }
trap cleanup EXIT HUP INT TERM
if ! sudo -n env DEBIAN_FRONTEND=noninteractive apt-get -q -o APT::Update::Error-Mode=any update \
  -o Dir::Etc::sourcelist="$source_file" \
  -o Dir::Etc::sourceparts=- \
  -o APT::Get::List-Cleanup=0 \
  -o Acquire::Languages=none \
  -o Acquire::Retries=2 \
  -o Acquire::http::Timeout=15 \
  -o Acquire::https::Timeout=15 \
  -o DPkg::Lock::Timeout=30 >"$check_log" 2>&1; then
  tail -n 8 "$check_log" 2>/dev/null || true
  printf 'CHECK:refresh-failed
VERIFY:failed
'
  exit 3
fi
printf 'METADATA:refreshed
'

available_version=$(apt-cache   -o Dir::Etc::sourcelist="$source_file"   -o Dir::Etc::sourceparts=-   policy "$package" 2>/dev/null | awk '/Candidate:/ {print $2; exit}')
if [ -z "$available_version" ] || [ "$available_version" = '(none)' ]; then
  printf 'CHECK:candidate-missing
VERIFY:failed
'
  exit 3
fi
printf 'AVAILABLE_VERSION:%s
' "$available_version"
if dpkg --compare-versions "$installed_version" lt "$available_version"; then
  printf 'UPDATE_AVAILABLE:1
CHECK:update-available
VERIFY:ok
'
else
  printf 'UPDATE_AVAILABLE:0
CHECK:current
VERIFY:ok
'
fi

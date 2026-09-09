#!/usr/bin/env bash
# Grant only the extracted browser AppArmor userns access on disposable CI runners.
set -euo pipefail
export LC_ALL=C

fail() {
  printf 'prepare-ci-sandbox: %s\n' "$*" >&2
  exit 1
}

read_optional_uint() {
  local path="$1" value
  if [ ! -e "$path" ] && [ ! -L "$path" ]; then
    return 0
  fi
  [ -f "$path" ] && [ -r "$path" ] || fail "cannot read kernel setting: $path"
  value="$(cat -- "$path")" || fail "cannot read kernel setting: $path"
  case "$value" in
    ''|*[!0-9]*) fail "invalid kernel setting: $path" ;;
  esac
  printf '%s' "$value"
}

validate_path() {
  # A conservative allowlist excludes AppArmor patterns, variables and quoting syntax.
  case "$1" in
    ''|*[!a-zA-Z0-9_./\ +-]*)
      fail "unsafe executable path; only ASCII letters, digits, spaces and /._+- are allowed" ;;
  esac
}

if [ "$#" -ne 1 ]; then
  printf 'usage: prepare-ci-sandbox.sh /path/to/extracted/chromix/chrome\n' >&2
  exit 2
fi

USERNS_CLONE="$(read_optional_uint /proc/sys/kernel/unprivileged_userns_clone)"
case "$USERNS_CLONE" in
  ''|1) ;;
  0) fail "kernel.unprivileged_userns_clone=0 blocks the browser sandbox; global sysctls will not be changed" ;;
  *) fail "unexpected kernel.unprivileged_userns_clone value: $USERNS_CLONE" ;;
esac
MAX_USERNS="$(read_optional_uint /proc/sys/user/max_user_namespaces)"
if [[ "$MAX_USERNS" =~ ^0+$ ]]; then
  fail "user.max_user_namespaces=0 blocks the browser sandbox; global sysctls will not be changed"
fi

RESTRICT_USERNS="$(read_optional_uint /proc/sys/kernel/apparmor_restrict_unprivileged_userns)"
case "$RESTRICT_USERNS" in
  ''|0)
    printf '==> AppArmor userns restriction is absent or disabled; no profile needed.\n'
    exit 0 ;;
  1) ;;
  *) fail "unexpected kernel.apparmor_restrict_unprivileged_userns value: $RESTRICT_USERNS" ;;
esac
APPARMOR_ENABLED_PATH=/sys/module/apparmor/parameters/enabled
[ -f "$APPARMOR_ENABLED_PATH" ] && [ -r "$APPARMOR_ENABLED_PATH" ] || \
  fail "cannot read AppArmor enabled state: $APPARMOR_ENABLED_PATH"
APPARMOR_ENABLED="$(cat -- "$APPARMOR_ENABLED_PATH")" || \
  fail "cannot read AppArmor enabled state: $APPARMOR_ENABLED_PATH"
case "$APPARMOR_ENABLED" in
  N) printf '==> AppArmor is disabled; no profile needed.\n'; exit 0 ;;
  Y) ;;
  *) fail "unexpected AppArmor enabled state: $APPARMOR_ENABLED" ;;
esac

[ "${GITHUB_ACTIONS:-}" = true ] || \
  fail "AppArmor userns restriction is enabled; profile changes require GITHUB_ACTIONS=true and are restricted to GitHub Actions"

validate_path "$1"
# NUL termination preserves control characters in symlink targets for validation.
BROWSER=''
if ! IFS= read -r -d '' BROWSER < <(readlink -e -z -- "$1"); then
  fail "cannot resolve executable path: $1"
fi
validate_path "$BROWSER"
[ -f "$BROWSER" ] && [ -x "$BROWSER" ] || fail "not a regular executable: $BROWSER"
[ ! -u "$BROWSER" ] && [ ! -g "$BROWSER" ] || fail "executable must not be setuid or setgid: $BROWSER"

PARSER="$(type -P apparmor_parser || true)"
[ -n "$PARSER" ] || fail "apparmor_parser is missing from PATH; install apparmor-utils in CI before running this helper"
SUDO="$(type -P sudo || true)"
[ -n "$SUDO" ] || fail "sudo is missing from PATH; noninteractive sudo is required to load the CI profile"

PATH_HASH="$(printf '%s' "$BROWSER" | sha256sum)"
PROFILE_NAME="chromix-ci-userns-${PATH_HASH%% *}"
# Load from stdin without persistent policy files or parser cache writes.
if ! printf 'profile "%s" "%s" flags=(unconfined) {\n  userns,\n}\n' \
  "$PROFILE_NAME" "$BROWSER" | "$SUDO" -n "$PARSER" --replace --skip-cache; then
  fail "failed to load CI AppArmor userns profile for $BROWSER"
fi
printf '==> Loaded CI AppArmor profile %s for %s.\n' "$PROFILE_NAME" "$BROWSER"
printf '==> Run the native browser smoke test to verify the sandbox at runtime.\n'

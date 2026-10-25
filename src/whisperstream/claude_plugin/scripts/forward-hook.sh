#!/bin/zsh
# Whisperstream Claude Code plugin hook forwarder for macOS.
# Every failure emits exact empty JSON and exits successfully so Claude keeps
# its native decision path. Request and response bodies exist only in an
# owner-private temporary directory that is removed on every handled exit.

setopt NO_UNSET PIPE_FAIL
umask 077

request_timeout_seconds=125
max_request_bytes=1048576
max_response_bytes=65536
max_discovery_bytes=4096
provider="${1:-claude}"
[[ "${provider}" == "claude" || "${provider}" == "codex" ]] || { /bin/echo '{}'; exit 0; }
# Unattended Claude runs (`claude -p`, SDK automation) have no person to speak to.
[[ "${provider}" == "claude" && "${CLAUDE_CODE_SESSION_ATTENDED:-}" == "0" ]] && { /bin/echo '{}'; exit 0; }
work_root=""

finish() {
  if [[ -n "${work_root}" && -d "${work_root}" ]]; then
    /bin/rm -f -- "${work_root}/request.json" "${work_root}/response.json" "${work_root}/proof.json" >/dev/null 2>&1
    /bin/rmdir -- "${work_root}" >/dev/null 2>&1
  fi
}

fail_open() {
  finish
  /bin/echo '{}'
  exit 0
}

trap finish EXIT
trap fail_open HUP INT TERM

work_root=$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/whisperstream-claude-hook.XXXXXX" 2>/dev/null) || fail_open
request_path="${work_root}/request.json"
response_path="${work_root}/response.json"

/usr/bin/head -c 1048577 > "${request_path}" 2>/dev/null || fail_open
request_bytes=$(/usr/bin/stat -f '%z' "${request_path}" 2>/dev/null) || fail_open
[[ "${request_bytes}" -gt 0 && "${request_bytes}" -le "${max_request_bytes}" ]] || fail_open
/usr/bin/plutil -convert json -o - -- "${request_path}" >/dev/null 2>&1 || fail_open
first_request=$(/usr/bin/sed -n 's/^[[:space:]]*\([{]\).*$/\1/p' "${request_path}" | /usr/bin/head -n 1)
last_request=$(/usr/bin/tr -d '[:space:]' < "${request_path}" | /usr/bin/tail -c 1)
[[ "${first_request}" == '{' && "${last_request}" == '}' ]] || fail_open

# Bind to the owner-marked cached plugin's channel. Never fall back to a
# shared port or inherit a different app-data target from the invoking shell.
plugin_root="${0:A:h:h}"
target_path="${plugin_root}/runtime-target.json"
[[ -f "${target_path}" && ! -L "${target_path}" ]] || fail_open
target_bytes=$(/usr/bin/stat -f '%z' "${target_path}" 2>/dev/null) || fail_open
[[ "${target_bytes}" -gt 0 && "${target_bytes}" -le "${max_discovery_bytes}" ]] || fail_open
data_root=$(/usr/bin/plutil -extract data_dir raw -o - -- "${target_path}" 2>/dev/null) || fail_open
[[ "${data_root}" == /* ]] || fail_open
discovery_path="${data_root}/${provider}-hud-port.json"
[[ -f "${discovery_path}" && ! -L "${discovery_path}" ]] || fail_open
discovery_bytes=$(/usr/bin/stat -f '%z' "${discovery_path}" 2>/dev/null) || fail_open
[[ "${discovery_bytes}" -gt 0 && "${discovery_bytes}" -le "${max_discovery_bytes}" ]] || fail_open
port=$(/usr/bin/plutil -extract port raw -o - -- "${discovery_path}" 2>/dev/null) || fail_open
hook_token=$(/usr/bin/plutil -extract token raw -o - -- "${discovery_path}" 2>/dev/null) || fail_open
[[ "${port}" == <-> && "${port}" -ge 1 && "${port}" -le 65535 ]] || fail_open
[[ ${#hook_token} -eq 64 && "${hook_token}" != *[^a-f0-9]* ]] || fail_open
headers=(--header "x-whisperstream-hook-token: ${hook_token}")
/usr/bin/curl --silent --fail --noproxy '*' --max-time 3 --max-filesize 4096 \
  "${headers[@]}" --output "${work_root}/proof.json" \
  "http://127.0.0.1:${port}/api/agent-hook/verify" >/dev/null 2>&1 || fail_open
proof=$(/usr/bin/plutil -extract ok raw -o - -- "${work_root}/proof.json" 2>/dev/null) || fail_open
[[ "${proof}" == true ]] || fail_open

if [[ -n "${WHISPERSTREAM_SESSION_TOKEN:-}" ]]; then
  # A header is never allowed to introduce a second header line.
  [[ "${WHISPERSTREAM_SESSION_TOKEN}" != *$'\r'* && "${WHISPERSTREAM_SESSION_TOKEN}" != *$'\n'* ]] || fail_open
  headers+=(--header "x-whisperstream-managed-session-token: ${WHISPERSTREAM_SESSION_TOKEN}")
fi


/usr/bin/curl --silent --show-error --fail --noproxy '*' \
  "${headers[@]}" --max-filesize "${max_response_bytes}" \
  --max-time "${request_timeout_seconds}" \
  --header 'Content-Type: application/json; charset=utf-8' \
  --request POST \
  --data-binary "@${request_path}" \
  --output "${response_path}" \
  "http://127.0.0.1:${port}/api/${provider}-hook" >/dev/null 2>&1 || fail_open

response_bytes=$(/usr/bin/stat -f '%z' "${response_path}" 2>/dev/null) || fail_open
[[ "${response_bytes}" -gt 0 && "${response_bytes}" -le "${max_response_bytes}" ]] || fail_open
/usr/bin/plutil -convert json -o - -- "${response_path}" >/dev/null 2>&1 || fail_open
first_response=$(/usr/bin/sed -n 's/^[[:space:]]*\([{]\).*$/\1/p' "${response_path}" | /usr/bin/head -n 1)
last_response=$(/usr/bin/tr -d '[:space:]' < "${response_path}" | /usr/bin/tail -c 1)
[[ "${first_response}" == '{' && "${last_response}" == '}' ]] || fail_open

/bin/cat "${response_path}"
/bin/echo
finish
exit 0

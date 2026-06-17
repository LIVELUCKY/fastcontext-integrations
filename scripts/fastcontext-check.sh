#!/usr/bin/env bash
# fastcontext-check.sh — verify a FastContext integration is ready.
#
# Connection is configured via MCP server ARGS, so this script checks:
#   1. the `fastcontext` CLI is on PATH
#   2. the MCP server's protocol handshake works (no endpoint needed)
#   3. (optional) a live exploration, if you pass a repo and connection flags
#
# Usage:
#   ./scripts/fastcontext-check.sh
#   ./scripts/fastcontext-check.sh /path/to/repo --base-url http://localhost:1234/v1 --model your-model

set -u
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
server="$here/fastcontext_mcp.py"
fail=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '  [ OK ] %s\n' "$*"; }
bad()  { printf '  [FAIL] %s\n' "$*"; fail=1; }
warn() { printf '  [WARN] %s\n' "$*"; }

repo="${1:-}"; shift || true   # remaining args ($@) are passed to the server (e.g. --base-url/--model)

say "1) fastcontext CLI on PATH"
if command -v fastcontext >/dev/null 2>&1; then
  ok "found: $(command -v fastcontext)"
else
  bad "fastcontext not found — see docs/SETUP.md"
fi

say "2) MCP server handshake (no endpoint needed)"
if command -v python3 >/dev/null 2>&1; then
  handshake="$(printf '%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{}}}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
    | python3 "$server" --model check 2>/dev/null)"
  if printf '%s' "$handshake" | grep -q '"fastcontext_explore"'; then
    ok "server responded and advertises fastcontext_explore"
  else
    bad "server did not advertise the tool — check $server"
  fi
else
  bad "python3 not found"
fi

say "3) live exploration"
if [ -z "$repo" ]; then
  warn "skipped — pass a repo and connection flags to run a real query:"
  warn "  ./scripts/fastcontext-check.sh /path/to/repo --base-url http://localhost:1234/v1 --model your-model"
elif [ ! -d "$repo" ]; then
  bad "not a directory: $repo"
elif [ "$#" -eq 0 ]; then
  warn "skipped — no connection flags given (need at least --model). Example:"
  warn "  ./scripts/fastcontext-check.sh $repo --base-url http://localhost:1234/v1 --model your-model"
else
  say "  driving the MCP server with: $* (repo: $repo)"
  req="$(printf '%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{}}}' \
    "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"fastcontext_explore\",\"arguments\":{\"query\":\"list the entry points of this project\",\"repo_path\":\"$repo\",\"max_turns\":4}}}")"
  resp="$(printf '%s\n' "$req" | python3 "$server" "$@" 2>/dev/null)"
  if printf '%s' "$resp" | grep -q '"isError":true'; then
    bad "exploration returned an error — see docs/99-troubleshooting.md"
    printf '%s\n' "$resp" | python3 -c "import sys,json
for ln in sys.stdin:
    ln=ln.strip()
    if not ln: continue
    o=json.loads(ln)
    if o.get('id')==2: print('       ', o['result']['content'][0]['text'][:200])" 2>/dev/null
  elif printf '%s' "$resp" | grep -q '"fastcontext_explore"\|"content"'; then
    ok "exploration completed"
  else
    bad "no usable response from the server"
  fi
fi

say ""
if [ "$fail" -eq 0 ]; then say "All required checks passed."; else say "Some checks failed — see notes above."; fi
exit "$fail"

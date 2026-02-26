#!/usr/bin/env bash
# Manage API keys in users.json — changes take effect immediately (no restart needed).
#
# Usage:
#   ./users.sh list
#   ./users.sh add   [username]
#   ./users.sh remove <username>

set -euo pipefail

USERS_FILE="$(cd "$(dirname "$0")" && pwd)/users.json"

[[ -f "$USERS_FILE" ]] || echo '{}' > "$USERS_FILE"

gen_key() {
  openssl rand -hex 16 2>/dev/null | sed 's/^/sk-/' \
    || python3 -c "import secrets; print('sk-' + secrets.token_hex(16))"
}

cmd="${1:-help}"

case "$cmd" in

  list)
    echo ""
    python3 - "$USERS_FILE" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
if not data:
    print("  (no users)")
else:
    col = max(len(v) for v in data.values())
    for key, user in sorted(data.items(), key=lambda x: x[1]):
        print(f"  {user:<{col}}  {key}")
print()
PY
    ;;

  add)
    user="${2:-}"
    if [[ -z "$user" ]]; then
      read -rp "Username: " user
    fi
    if [[ -z "$user" ]]; then echo "Username required." >&2; exit 1; fi
    key="$(gen_key)"
    python3 - "$USERS_FILE" "$key" "$user" <<'PY'
import json, sys
f, key, user = sys.argv[1], sys.argv[2], sys.argv[3]
data = json.load(open(f))
if key in data:
    print(f"Key already exists for user '{data[key]}'. Remove it first.")
    sys.exit(1)
data[key] = user
json.dump(data, open(f, "w"), indent=2)
print(f"Added: {user}  {key}")
PY
    ;;

  remove)
    user="${2:-}"
    if [[ -z "$user" ]]; then
      read -rp "Username to remove: " user
    fi
    if [[ -z "$user" ]]; then echo "Username required." >&2; exit 1; fi
    python3 - "$USERS_FILE" "$user" <<'PY'
import json, sys
f, user = sys.argv[1], sys.argv[2]
data = json.load(open(f))
removed = [k for k, v in data.items() if v == user]
if not removed:
    print(f"No user '{user}' found.")
    sys.exit(1)
for k in removed:
    del data[k]
json.dump(data, open(f, "w"), indent=2)
print(f"Removed: {user} ({len(removed)} key{'s' if len(removed)>1 else ''})")
PY
    ;;

  *)
    echo ""
    echo "  Usage: $0 <command>"
    echo ""
    echo "  Commands:"
    echo "    list              List all users and their API keys"
    echo "    add [user]        Add a user with an auto-generated API key (prompts if omitted)"
    echo "    remove [user]     Remove all keys for a user (prompts if omitted)"
    echo ""
    ;;
esac

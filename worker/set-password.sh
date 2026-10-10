#!/bin/sh
# Set or change the dashboard password.
#
# Sessions are signed with a key derived from the password, so changing it ends
# every existing session everywhere. Sign in once afterwards.
#
#     ./set-password.sh

set -e
cd "$(dirname "$0")"

printf 'New dashboard password: '
stty -echo 2>/dev/null || true
read -r password
stty echo 2>/dev/null || true
printf '\n'

if [ -z "$password" ]; then
    echo "Nothing entered. Unchanged." >&2
    exit 1
fi
if [ ${#password} -lt 12 ]; then
    echo "Too short. Twelve characters or more, please: this is the only gate on" >&2
    echo "a page whose address is public." >&2
    exit 1
fi

printf '%s' "$password" | npx wrangler@4 secret put DASHBOARD_PASSWORD
echo
echo "Done. Every existing session has ended; sign in again at the dashboard."

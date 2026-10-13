#!/bin/bash
# Pull the newest mirror from the "mirror-data" release into this codespace.
# Runs automatically on codespace start (once per UTC day).
#
#   bash .devcontainer/refresh.sh            # skip if already refreshed today
#   bash .devcontainer/refresh.sh --force    # always pull the newest

MARKER="$HOME/.deadlock_last_refresh"
TODAY=$(date -u +%Y-%m-%d)
TAG="mirror-data"
PATTERN="mirror-deadlock.tar.part*"

cd "$(dirname "$0")/.." || exit 1

if [ "$1" != "--force" ] && [ -f "$MARKER" ] && [ "$(cat "$MARKER")" = "$TODAY" ]; then
    echo "[refresh] Mirror already updated today ($TODAY). Use --force to pull again."
    exit 0
fi

dir=$(mktemp -d)
if gh release download "$TAG" --pattern "$PATTERN" --dir "$dir" 2>/dev/null \
   && ls "$dir"/$PATTERN >/dev/null 2>&1; then
    echo "[refresh] Downloaded $(du -sh "$dir" | cut -f1), unpacking..."
    if cat "$dir"/$PATTERN | tar -x; then
        echo "$TODAY" > "$MARKER"
        head -n 8 deadlock_mirror/status.md 2>/dev/null
        echo "[refresh] Done. If port 8000 doesn't load: bash .devcontainer/start.sh   (status at /_status)"
    else
        echo "[refresh] Unpack failed (an upload may have been in progress); try again in a few minutes."
    fi
else
    echo "[refresh] No mirror on the release yet. Run deadlock_mirror.py --publish somewhere that can reach the wiki."
fi
rm -rf "$dir"

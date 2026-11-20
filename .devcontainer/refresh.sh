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
# Unpack beside the live copy (same disk, so the swap is a quick rename), then
# replace it, so pages removed or turned into redirects on the wiki go away.
mkdir -p deadlock_mirror
rm -rf deadlock_mirror/.incoming.*   # left by an earlier failed swap; this run downloads afresh
new=$(mktemp -d -p deadlock_mirror .incoming.XXXXXX)
if gh release download "$TAG" --pattern "$PATTERN" --dir "$dir" 2>/dev/null \
   && ls "$dir"/$PATTERN >/dev/null 2>&1; then
    echo "[refresh] Downloaded $(du -sh "$dir" | cut -f1), unpacking..."
    if cat "$dir"/$PATTERN | tar -x -C "$new" && [ -d "$new/deadlock_mirror/site" ]; then
        rm -rf "$dir"
        failed=0
        for f in "$new"/deadlock_mirror/*; do
            name=$(basename "$f")
            [ "$name" = "server.py" ] && continue      # the server comes from git
            rm -rf "deadlock_mirror/$name.old"
            if [ -e "deadlock_mirror/$name" ] && ! mv "deadlock_mirror/$name" "deadlock_mirror/$name.old"; then
                echo "[refresh] Could not move deadlock_mirror/$name aside; kept the current copy." >&2
                failed=1; continue
            fi
            if mv "$f" "deadlock_mirror/$name"; then
                rm -rf "deadlock_mirror/$name.old"
            else  # put the live copy back, so a failed swap never leaves the server without it
                [ -e "deadlock_mirror/$name.old" ] && mv "deadlock_mirror/$name.old" "deadlock_mirror/$name"
                echo "[refresh] Could not swap in the new deadlock_mirror/$name; kept the current copy." >&2
                failed=1
            fi
        done
        if [ "$failed" -ne 0 ]; then
            echo "[refresh] Refresh incomplete (disk full or permissions?). Today is not marked as refreshed;" \
                 "the new copy stays in $new until the next run. Retry: bash .devcontainer/refresh.sh --force" >&2
            exit 1
        fi
        echo "$TODAY" > "$MARKER"
        head -n 8 deadlock_mirror/status.md 2>/dev/null
        echo "[refresh] Done. If port 8000 doesn't load: bash .devcontainer/start.sh   (status at /_status)"
    else
        echo "[refresh] Unpack failed (an upload may have been in progress); try again in a few minutes."
    fi
else
    echo "[refresh] No mirror on the release yet. Run deadlock_mirror.py --publish somewhere that can reach the wiki."
fi
rm -rf "$dir" "$new"

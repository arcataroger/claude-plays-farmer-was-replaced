# Sourced by install_macos.sh and redeploy_macos.sh. Keeps a patched copy of the game.
#
# The hardened runtime on Steam's app drops DYLD_INSERT_LIBRARIES, so Doorstop can't
# load. We never touch that app. We clone it and re-sign only the clone ad hoc, so
# Steam's Play button and Verify keep working on the original.
BRIDGE_APP="TheFarmerWasReplaced-Bridge.app"

# Prints a fingerprint of the Steam app: every file's mtime, size and path.
source_fingerprint() {
    (cd "$1" && find TheFarmerWasReplaced.app -type f -print0 | sort -z |
        xargs -0 stat -f '%m %z %N' | shasum | cut -d' ' -f1)
}

# Brings the clone up to date. Call it only while the game is stopped.
# Prints one line when it rebuilds the clone, and nothing when the clone is current.
refresh_clone() {
    local game_dir="$1" stamp="$1/BepInEx/bridge_clone.fingerprint"
    local src="$1/TheFarmerWasReplaced.app" clone="$1/$BRIDGE_APP" tmp="$1/$BRIDGE_APP.tmp"
    local want
    want=$(source_fingerprint "$game_dir")
    if [[ -d "$clone" && -f "$stamp" && "$(cat "$stamp")" == "$want" ]]; then
        return 0
    fi
    rm -rf "$tmp"
    # An APFS clone shares blocks with the original, so it costs almost no disk.
    cp -cR "$src" "$tmp" 2> /dev/null || { rm -rf "$tmp"; cp -R "$src" "$tmp"; }
    codesign --force --deep --sign - "$tmp" 2> /dev/null
    rm -rf "$clone"
    mv "$tmp" "$clone"
    echo "$want" > "$stamp"
    echo "rebuilt $BRIDGE_APP from Steam's app"
}

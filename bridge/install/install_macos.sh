#!/usr/bin/env bash
# Installs BepInEx 5, builds the TFWR Bridge plugin and installs it into the macOS game folder.
# Usage: bridge/install/install_macos.sh [GAME_DIR]
# GAME_DIR is the folder that holds TheFarmerWasReplaced.app.
#
# Steps:
#   1. Find the game and the .NET SDK.
#   2. Download BepInEx, check its SHA-256 and unpack it into the game (skipped when present).
#   3. Copy BepInEx/core into bridge/vendor/BepInEx/core: the build references it.
#   4. Build the plugin against the game's own DLLs.
#   5. Configure BepInEx, re-sign the app ad hoc and copy the plugin in.
set -euo pipefail

BEPINEX_TAG="v5.4.23.6"
BEPINEX_ZIP="BepInEx_macos_universal_5.4.23.6.zip"
BEPINEX_SHA256="2e711e21f6570da964fce4654b1976500dbe043f61778bce0008500078564638"

here="$(cd "$(dirname "$0")/.." && pwd)"
game_dir="${1:-$HOME/Library/Application Support/Steam/steamapps/common/The Farmer Was Replaced}"
plugin="$here/plugin/bin/Release/TfwrBridge.dll"
vendor="$here/vendor/BepInEx/core"

# 1. The game and the SDK.
if [ ! -d "$game_dir/TheFarmerWasReplaced.app" ]; then
    echo "No TheFarmerWasReplaced.app in: $game_dir" >&2
    echo "Pass the folder that holds it: bridge/install/install_macos.sh \"/path/to/The Farmer Was Replaced\"" >&2
    exit 1
fi
if ! command -v dotnet > /dev/null && [ -x "$HOME/.dotnet/dotnet" ]; then
    export DOTNET_ROOT="$HOME/.dotnet" PATH="$HOME/.dotnet:$PATH"
fi
if ! command -v dotnet > /dev/null; then
    echo "The build needs the .NET SDK 8 or later: https://dotnet.microsoft.com/download" >&2
    exit 1
fi

# 2. BepInEx, checksum-verified.
if [ ! -d "$game_dir/BepInEx/core" ]; then
    tmp="$(mktemp -d)"
    curl -fsSL -o "$tmp/$BEPINEX_ZIP" \
        "https://github.com/BepInEx/BepInEx/releases/download/$BEPINEX_TAG/$BEPINEX_ZIP"
    echo "$BEPINEX_SHA256  $tmp/$BEPINEX_ZIP" | shasum -a 256 -c -
    unzip -q -o "$tmp/$BEPINEX_ZIP" -d "$game_dir"
    rm -rf "$tmp"
    # Point the launcher at the game bundle.
    sed -i '' 's|^executable_name=""|executable_name="TheFarmerWasReplaced.app"|' "$game_dir/run_bepinex.sh"
    chmod +x "$game_dir/run_bepinex.sh"
    echo "Installed BepInEx $BEPINEX_TAG"
fi

# 3. The build references BepInEx.dll from the vendor folder (gitignored).
mkdir -p "$vendor"
cp "$game_dir/BepInEx/core/"*.dll "$vendor/"

# 4. Build. The shared compiler server would outlive the build, so we turn it off.
dotnet build "$here/plugin/TfwrBridge.csproj" -c Release -nologo -v q \
    -p:UseSharedCompilation=false -p:GameDir="$game_dir"
[ -f "$plugin" ] || { echo "The build left no $plugin" >&2; exit 1; }

# 5. Steam must know the app ID when we launch outside the Steam client's own wrapper.
[ -f "$game_dir/steam_appid.txt" ] || echo 2060160 > "$game_dir/steam_appid.txt"

# Load the chainloader from a MonoBehaviour hook; it proved reliable on arm64 macOS.
cfg="$game_dir/BepInEx/config/BepInEx.cfg"
mkdir -p "$(dirname "$cfg")"
if [ -f "$cfg" ] && grep -q '^\[Preloader.Entrypoint\]' "$cfg"; then
    sed -i '' '/^\[Preloader.Entrypoint\]/,/^\[/ s/^Type = .*/Type = MonoBehaviour/' "$cfg"
else
    printf '\n[Preloader.Entrypoint]\nAssembly = UnityEngine.CoreModule.dll\nType = MonoBehaviour\nMethod = .cctor\n' >> "$cfg"
fi

# The hardened runtime drops DYLD_INSERT_LIBRARIES, so Doorstop can't load.
# An ad-hoc signature (no hardened runtime) fixes that. We back up the original first;
# Steam's "Verify integrity of game files" restores it too.
app="$game_dir/TheFarmerWasReplaced.app"
backup="$HOME/.cache/tfwr-bridge/original-signature"
if codesign -d --verbose=2 "$app" 2>&1 | grep -q 'flags=.*runtime'; then
    mkdir -p "$backup"
    cp -R "$app/Contents/_CodeSignature" "$backup/" 2>/dev/null || true
    cp "$app/Contents/MacOS/TheFarmerWasReplaced" "$backup/"
    codesign --force --deep --sign - "$app"
    echo "Re-signed the app ad hoc (backup in $backup)"
fi

mkdir -p "$game_dir/BepInEx/plugins/TfwrBridge"
cp "$plugin" "$game_dir/BepInEx/plugins/TfwrBridge/"
echo "Installed plugin -> $game_dir/BepInEx/plugins/TfwrBridge/TfwrBridge.dll"
echo
echo "Set this Steam launch option (Properties > General > Launch Options):"
echo "  \"$game_dir/run_bepinex.sh\" %command%"

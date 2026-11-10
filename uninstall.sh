#!/usr/bin/env bash
# Removes what install.sh created. Your settings are kept unless you delete them yourself.
set -euo pipefail
data="${XDG_DATA_HOME:-$HOME/.local/share}"
rm -rf "$data/art-gui"
rm -f "$data/applications/art-gui.desktop"
update-desktop-database "$data/applications" 2>/dev/null || true
echo "Removed. Settings (if any) are in ${XDG_CONFIG_HOME:-$HOME/.config}/art-gui/"

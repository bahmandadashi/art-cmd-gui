#!/usr/bin/env bash
# Installs the GUI for the current user (no root needed).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
data="${XDG_DATA_HOME:-$HOME/.local/share}"
dest="$data/art-gui"
apps="$data/applications"

command -v python3 >/dev/null || { echo "python3 is required."; exit 1; }

if ! python3 -c "import tkinter" 2>/dev/null; then
    echo "Python's tkinter is missing. Install it first:"
    echo "  Fedora / Rocky / Alma:  sudo dnf install python3-tkinter"
    echo "  Debian / Ubuntu:        sudo apt install python3-tk"
    echo "  Arch:                   sudo pacman -S tk"
    exit 1
fi

for tool in ffmpeg ffprobe; do
    command -v "$tool" >/dev/null || echo "WARNING: $tool not found. It is required to run conversions."
done

mkdir -p "$dest" "$apps"
install -m 755 "$here/art_gui.py" "$dest/art_gui.py"

cat > "$apps/art-gui.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=ART GUI (unofficial)
Comment=Batch converter GUI for ARRI Reference Tool CMD
Exec=python3 $dest/art_gui.py
Icon=video-x-generic
Terminal=false
Categories=AudioVideo;Video;
DESKTOP

update-desktop-database "$apps" 2>/dev/null || true

echo "Installed to $dest"
echo "Start it from your application menu ('ART GUI'), or run: python3 $dest/art_gui.py"
echo
echo "This program needs ARRI's art-cmd, which is NOT included."
echo "Download it from ARRI, read its license, then select it in the app's Settings."

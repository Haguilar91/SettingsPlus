#!/bin/sh
# Installs Settings Plus for the current user (no root needed).
set -e
cd "$(dirname "$0")" || exit 1

PY=
for cand in /usr/bin/python3 "$(command -v python3 2>/dev/null)"; do
    [ -n "$cand" ] && [ -x "$cand" ] || continue
    if "$cand" -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1")' 2>/dev/null; then
        PY=$cand
        break
    fi
done
if [ -z "$PY" ]; then
    echo "No python3 with PyGObject, GTK 4 and libadwaita found." >&2
    echo "Install python3-gi (Debian/Ubuntu), python-gobject (Arch) or python3-gobject (Fedora)," >&2
    echo "plus the GTK 4 and libadwaita typelibs (gir1.2-gtk-4.0, gir1.2-adw-1)." >&2
    exit 1
fi

d="$HOME/.local/share/settings-plus"
apps="$HOME/.local/share/applications"
icons="$HOME/.local/share/icons/hicolor/scalable/apps"
sym="$HOME/.local/share/icons/hicolor/symbolic/apps"
meta="$HOME/.local/share/metainfo"
mkdir -p "$d" "$apps" "$icons" "$sym"
cp settings_plus.py "$d/"
cp icons/io.github.settingsplus.svg "$icons/"
cp icons/io.github.settingsplus-symbolic.svg "$sym/"
rm -f "$apps/settings-plus.desktop"
sed -e "s|%h|$HOME|" -e "s|@PYTHON@|$PY|" io.github.settingsplus.desktop \
    > "$apps/io.github.settingsplus.desktop"
[ -f data/io.github.settingsplus.metainfo.xml ] && { mkdir -p "$meta" && cp data/io.github.settingsplus.metainfo.xml "$meta/" || true; }
gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
update-desktop-database "$apps" 2>/dev/null || true
echo "Installed using $PY. Launch 'Settings Plus' from the app grid."

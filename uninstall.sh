#!/bin/sh
# Removes exactly what install.sh adds.
cd "$(dirname "$0")"
apps="$HOME/.local/share/applications"
rm -rf "$HOME/.local/share/settings-plus"
rm -f "$apps/io.github.settingsplus.desktop" \
      "$apps/settings-plus.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/io.github.settingsplus.svg" \
      "$HOME/.local/share/icons/hicolor/symbolic/apps/io.github.settingsplus-symbolic.svg"
gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
update-desktop-database "$apps" 2>/dev/null || true
echo "Settings Plus removed."

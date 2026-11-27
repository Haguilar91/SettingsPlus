# Settings Plus

A GTK 4 / libadwaita app that exposes GNOME settings the stock Settings app and
Tweaks leave out, in the spirit of KDE System Settings modules: lid and power
key behaviour, experimental mutter features, extra touchpad / pointing stick /
keyboard options, wallpaper and text rendering, window and workspace options,
regional formats, NTP, GDM login screen settings, and more.

Only stock GNOME components are used. Schemas, keys and enum values that your
GNOME version does not have are skipped, and empty groups and pages are hidden.

## Requirements

- GNOME 46 or newer
- PyGObject (`python3-gi` / `python-gobject` / `python3-gobject`)
- GTK 4.10+ and libadwaita 1.5+ (the app exits with a message otherwise)
- Optional: systemd-logind and polkit (`pkexec`) for lid / power key settings,
  `timedatectl` for the NTP switch, `dconf` and GDM for the Login Screen page,
  GnomeDesktop typelib for nicer locale names, Nautilus / Tracker for their groups

## Install / uninstall

    ./install.sh      # per-user, no root; uses the python3 that has PyGObject
    ./uninstall.sh    # removes exactly what install.sh added

Run directly with `python3 settings_plus.py`.

## System-level settings

The lid and hardware power key actions (a drop-in in
`/etc/systemd/logind.conf.d/`), the Login Screen page (files in `/etc/dconf/`)
and the NTP switch change system state. Each change is applied as soon as you
change the row and asks for your administrator password through polkit; if you
cancel or it fails, the row reverts. These rows are marked with a lock icon and
are hidden or disabled when the needed tools (polkit, systemd-logind, GDM,
dconf, timedatectl) are missing.

## Author

Hector Aguilar (@haguilar91), built with AI assistance (Claude).

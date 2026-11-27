#!/usr/bin/env python3
"""Render Settings Plus pages offscreen to docs/screenshots/*.png.

Draws the app's own window through GTK's renderer; no settings are changed.
Run with: /usr/bin/python3 tools/screenshots.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, GLib, Graphene, Gtk  # noqa: E402

import settings_plus as s  # noqa: E402

OUT = os.path.join(ROOT, "docs", "screenshots")
PAGES = ["power", "display", "input", "shortcuts", "appearance",
         "windows-and-desktop", "date-and-region", "login-screen",
         "privacy-and-files", "accessibility"]
# (page, scheme, filename)
JOBS = [(p, "light", p + ".png") for p in PAGES]
JOBS.append(("power", "dark", "power-dark.png"))


def render(win, path):
    w, h = win.get_width(), win.get_height()
    snap = Gtk.Snapshot()
    Gtk.WidgetPaintable.new(win).snapshot(snap, w, h)
    node = snap.to_node()
    tex = win.get_native().get_renderer().render_texture(
        node, Graphene.Rect().init(0, 0, w, h))
    tex.save_to_png(path)
    print("wrote", path)


class ShotApp(s.App):
    def __init__(self):
        Adw.Application.__init__(self, application_id="io.github.settingsplus.screens")

    def do_activate(self):
        win = s.MainWindow(self)
        win.set_default_size(1100, 780)
        win.present()
        jobs = list(JOBS)
        names = [n for n, _t in win.pages]
        mgr = Adw.StyleManager.get_default()

        def step():
            if not jobs:
                self.quit()
                return False
            page, scheme, fname = jobs.pop(0)
            if page not in names:
                print("skip (page not present):", page)
                GLib.idle_add(step)
                return False
            mgr.set_color_scheme(Adw.ColorScheme.FORCE_DARK if scheme == "dark"
                                 else Adw.ColorScheme.FORCE_LIGHT)
            win.sidebar.select_row(win.sidebar.get_row_at_index(names.index(page)))

            def capture():
                render(win, os.path.join(OUT, fname))
                GLib.idle_add(step)
                return False
            GLib.timeout_add(500, capture)
            return False

        GLib.timeout_add(800, step)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    sys.exit(ShotApp().run([sys.argv[0]]))

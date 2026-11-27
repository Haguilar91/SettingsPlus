#!/usr/bin/python3
"""Settings Plus - the GNOME settings that Tweaks and Settings leave out.

Requires GNOME 46+ (GTK >= 4.10, libadwaita >= 1.5, PyGObject).

Pages (sidebar layout, collapses on narrow widths):
  Power            lid close, hardware power key, power button, idle, screen lock
  Display          experimental mutter features, Xwayland, night light
  Input            mouse, touchpad, pointing stick, keyboard, key remapping
  Shortcuts        custom shortcut editor, window/system shortcuts (key capture)
  Appearance       wallpaper, themes, text and fonts, sounds, effects, notifications
  Windows and Desktop  titlebar, focus, switchers, workspaces and their names, session
  Startup Apps     autostart entries
  Date and Region  automatic time zone, NTP, regional formats, calendar
  Login Screen     GDM greeter settings written to /etc/dconf (applied immediately, needs polkit)
  Privacy and Files    privacy, removable media, Files, file chooser, search, disk space
  Accessibility    seeing, typing aids, pointing
  Default Apps     default application per content type

Portability: schemas, keys and enum values that are missing are skipped, groups
and pages that end up empty are hidden, and system-level features (logind, NTP,
GDM) are only offered when pkexec/systemctl/timedatectl/dconf/gdm are present.
System changes (logind drop-in, GDM dconf files, NTP) are never made unless the
user presses the corresponding button or switch.
"""
import base64
import glob
import os
import re
import subprocess
import sys
import traceback

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402
except (ImportError, ValueError) as _e:
    sys.stderr.write(
        f"Settings Plus needs PyGObject with GTK 4 and libadwaita ({_e}).\n"
        "Install it with your package manager: python3-gi (Debian/Ubuntu), "
        "python-gobject (Arch), python3-gobject (Fedora/openSUSE), plus "
        "gir1.2-gtk-4.0 and gir1.2-adw-1 where packaged separately.\n")
    sys.exit(1)

try:  # optional: nicer locale names
    gi.require_version("GnomeDesktop", "4.0")
    from gi.repository import GnomeDesktop  # noqa: E402
except (ImportError, ValueError):
    GnomeDesktop = None

DEBUG = os.environ.get("SETTINGS_PLUS_DEBUG") == "1"


def dbg(*args):
    """Debug log to stderr, enabled with SETTINGS_PLUS_DEBUG=1."""
    if DEBUG:
        print("[settings-plus]", *args, file=sys.stderr, flush=True)


MIN_ADW = (1, 5)
MIN_GTK_MINOR = 10


def check_versions():
    if (Adw.get_major_version(), Adw.get_minor_version()) < MIN_ADW \
            or Gtk.get_minor_version() < MIN_GTK_MINOR:
        sys.stderr.write("Settings Plus requires libadwaita >= 1.5 (GNOME 46+)\n")
        sys.exit(1)


APP_ID = "io.github.settingsplus"
VERSION = "0.1.0"
GLib.set_prgname(APP_ID)
LOGIND_DROPIN = "/etc/systemd/logind.conf.d/50-settings-plus.conf"
USER_AUTOSTART = os.path.join(GLib.get_user_config_dir(), "autostart")
SYSTEM_AUTOSTART = "/etc/xdg/autostart"
GDM_PROFILE = "/etc/dconf/profile/gdm"
GDM_DB_DIR = "/etc/dconf/db/gdm.d"
GDM_CONF = GDM_DB_DIR + "/50-settings-plus"
GDM_GREETER_DEFAULTS = "/usr/share/gdm/greeter-dconf-defaults"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def have(tool):
    return GLib.find_program_in_path(tool) is not None


def has_logind():
    return os.path.isdir("/run/systemd/system") and have("pkexec") \
        and have("systemctl")


def has_gdm():
    return any(os.path.isdir(p) for p in ("/usr/share/gdm", "/etc/gdm", "/etc/gdm3"))


def get_settings(schema, key=None):
    """Return Gio.Settings if the schema (and key) exist, else None."""
    src = Gio.SettingsSchemaSource.get_default()
    sch = src.lookup(schema, True)
    if sch is None or (key and not sch.has_key(key)):
        return None
    return Gio.Settings.new(schema)


def enum_values(schema, key):
    """Allowed values of an enum key, or None if the key is not an enum."""
    sch = Gio.SettingsSchemaSource.get_default().lookup(schema, True)
    if sch is None or not sch.has_key(key):
        return None
    kind, vals = sch.get_key(key).get_range().unpack()
    return list(vals) if kind == "enum" else None


def combo_row(title, schema, key, options, subtitle=None):
    """ComboRow bound to a string GSettings key. options = [(value, label)].

    Options the installed schema does not allow are dropped; a current value
    outside the list is appended as 'Current: <value>'. None if nothing is left.
    """
    st = get_settings(schema, key)
    if st is None:
        return None
    allowed = enum_values(schema, key)
    if allowed is not None:
        options = [(v, l) for v, l in options if v in allowed]
    cur = st.get_string(key)
    options = list(options)
    if options and cur not in [v for v, _ in options]:
        options.append((cur, f"Current: {cur}"))
    if not options:
        return None
    row = Adw.ComboRow(title=title, subtitle=subtitle or "")
    row.set_model(Gtk.StringList.new([lbl for _, lbl in options]))
    values = [v for v, _ in options]
    row.set_selected(values.index(cur))
    def on_sel(r, _p):
        st.set_string(key, values[r.get_selected()])
    row.connect("notify::selected", on_sel)
    return row


def switch_row(title, schema, key, subtitle=None, invert=False):
    st = get_settings(schema, key)
    if st is None:
        return None
    row = Adw.SwitchRow(title=title, subtitle=subtitle or "")
    flags = Gio.SettingsBindFlags.DEFAULT
    if invert:
        flags |= Gio.SettingsBindFlags.INVERT_BOOLEAN
    st.bind(key, row, "active", flags)
    return row


def multi_switch_row(title, bindings, subtitle=None):
    """SwitchRow controlling several boolean keys at once.

    bindings = [(schema, key), ...]; missing schemas/keys are skipped.
    Row is ON only if all existing keys are true. Returns None if none exist.
    """
    bound = []
    for schema, key in bindings:
        st = get_settings(schema, key)
        if st is not None:
            bound.append((st, key))
    if not bound:
        return None
    row = Adw.SwitchRow(title=title, subtitle=subtitle or "")
    updating = [False]

    def read_all():
        return all(st.get_boolean(k) for st, k in bound)

    def sync(*_a):
        if updating[0]:
            return
        updating[0] = True
        try:
            row.set_active(read_all())
        finally:
            updating[0] = False

    def on_toggled(r, _p):
        if updating[0]:
            return
        updating[0] = True
        try:
            for st, k in bound:
                st.set_boolean(k, r.get_active())
        finally:
            updating[0] = False

    row.set_active(read_all())
    row.connect("notify::active", on_toggled)
    for st, k in bound:
        st.connect("changed::" + k, sync)
    return row


def spin_row(title, schema, key, lo, hi, step=1, subtitle=None, digits=0):
    st = get_settings(schema, key)
    if st is None:
        return None
    row = Adw.SpinRow.new_with_range(lo, hi, step)
    row.set_digits(digits)
    row.set_title(title)
    if subtitle:
        row.set_subtitle(subtitle)
    st.bind(key, row, "value", Gio.SettingsBindFlags.DEFAULT)
    return row


def entry_row(title, schema, key, subtitle=None):
    """EntryRow for a string key; the value is written on the apply signal."""
    st = get_settings(schema, key)
    if st is None:
        return None
    row = Adw.EntryRow(title=title, show_apply_button=True,
                       text=st.get_string(key))
    row.connect("apply", lambda r: st.set_string(key, r.get_text()))
    return row


def disabled_row(title, subtitle):
    row = Adw.ActionRow(title=title, subtitle=subtitle, sensitive=False)
    return row


def basename_of(uri_or_path):
    if not uri_or_path:
        return ""
    f = (Gio.File.new_for_uri(uri_or_path) if "://" in uri_or_path
         else Gio.File.new_for_path(uri_or_path))
    return f.get_basename() or ""


class FileChooserRow(Adw.ActionRow):
    """Row with a 'Choose...' button (and optional clear) for an image file.

    value is a URI or path as stored by the caller; on_change(value) is called
    with the new value ('' when cleared). to_value(Gio.File) -> stored string.
    """

    def __init__(self, title, value, on_change, to_value, filters=None):
        super().__init__(title=title)
        self._value, self._on_change = value, on_change
        self._to_value, self._filters = to_value, filters
        btn = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        btn.connect("clicked", self._choose)
        self._clear = Gtk.Button(icon_name="edit-clear-symbolic",
                                 valign=Gtk.Align.CENTER, tooltip_text="Clear")
        self._clear.add_css_class("flat")
        self._clear.connect("clicked", lambda _b: self.set_value(""))
        self.add_suffix(self._clear)
        self.add_suffix(btn)
        self.set_activatable_widget(btn)
        self._refresh()

    def _refresh(self):
        name = basename_of(self._value)
        self.set_subtitle(GLib.markup_escape_text(name) if name else "None")
        self._clear.set_visible(bool(self._value))

    def set_value(self, value, notify=True):
        self._value = value
        self._refresh()
        if notify:
            self._on_change(value)

    def _choose(self, _btn):
        dlg = Gtk.FileDialog(title=self.get_title())
        if self._filters is not None:
            dlg.set_filters(self._filters)
            dlg.set_default_filter(self._filters.get_item(0))
        dlg.open(self.get_root(), None, self._chosen)

    def _chosen(self, dlg, res):
        try:
            f = dlg.open_finish(res)
        except GLib.Error:
            return  # dismissed
        if f is not None:
            self.set_value(self._to_value(f))


def image_filters():
    flt = Gtk.FileFilter(name="Images")
    flt.add_mime_type("image/*")
    store = Gio.ListStore.new(Gtk.FileFilter)
    store.append(flt)
    return store


def uri_row(title, schema, key):
    """FileChooserRow editing a GSettings string key holding a file:// URI."""
    st = get_settings(schema, key)
    if st is None:
        return None
    row = FileChooserRow(title, st.get_string(key),
                         lambda v: st.set_string(key, v),
                         lambda f: f.get_uri(), image_filters())
    st.connect("changed::" + key, lambda s, k: row.set_value(s.get_string(k), False))
    return row


def color_row(title, schema, key):
    """ColorDialogButton row storing '#rrggbb'."""
    st = get_settings(schema, key)
    if st is None:
        return None
    row = Adw.ActionRow(title=title)
    btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False),
                                valign=Gtk.Align.CENTER)
    rgba = Gdk.RGBA()
    if rgba.parse(st.get_string(key)):
        btn.set_rgba(rgba)
    def changed(b, _p):
        c = b.get_rgba()
        st.set_string(key, "#%02x%02x%02x" % tuple(
            round(x * 255) for x in (c.red, c.green, c.blue)))
    btn.connect("notify::rgba", changed)
    row.add_suffix(btn)
    return row


def group(title, rows, description=None):
    """PreferencesGroup of the non-None rows; None when no row survives."""
    rows = [r for r in rows if r is not None]
    if not rows:
        return None
    g = Adw.PreferencesGroup(title=title or "")
    if description:
        g.set_description(description)
    for r in rows:
        g.add(r)
    return g


def note_group(title, description):
    """Text-only group (no rows)."""
    return Adw.PreferencesGroup(title=title, description=description)


class Page(Adw.PreferencesPage):
    """PreferencesPage whose add() ignores None (empty groups) and counts groups."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.n_groups = 0
        self.groups = []

    def add(self, grp):
        if grp is not None:
            super().add(grp)
            self.n_groups += 1
            self.groups.append(grp)



# --------------------------------------------------------------------------
# Power page (logind settings need root -> pkexec)
# --------------------------------------------------------------------------
LID_ACTIONS = [
    ("suspend", "Suspend"),
    ("hibernate", "Hibernate"),
    ("hybrid-sleep", "Hybrid sleep"),
    ("suspend-then-hibernate", "Suspend, then hibernate"),
    ("lock", "Lock screen"),
    ("ignore", "Do nothing"),
    ("poweroff", "Power off"),
]
KEY_ACTIONS = [
    ("ignore", "Do nothing"),
    ("poweroff", "Power off"),
    ("reboot", "Restart"),
    ("suspend", "Suspend"),
    ("hibernate", "Hibernate"),
    ("lock", "Lock screen"),
]
LID_KEYS = [
    ("HandleLidSwitch", "On battery", "suspend"),
    ("HandleLidSwitchExternalPower", "On AC power", "suspend"),
    ("HandleLidSwitchDocked", "Docked / external display", "ignore"),
]
# GNOME overrides HandlePowerKey, HandleSuspendKey and IdleAction while a
# session runs, so only the long-press action is managed.
POWER_KEYS = [
    ("HandlePowerKeyLongPress", "Hold the power button", "ignore"),
]
LOGIND_KEYS = LID_KEYS + POWER_KEYS
LOGIND_APPLY_SCRIPT = (
    "mkdir -p /etc/systemd/logind.conf.d && "
    f"cat > {LOGIND_DROPIN} && "
    "systemctl kill -s HUP systemd-logind"
)


def _parse_keys(text, into):
    for line in text.splitlines():
        line = line.strip()
        if "=" in line and not line.startswith(("#", ";", "[")):
            k, v = line.split("=", 1)
            into[k.strip()] = v.strip()


def _logind_conf_text():
    """Merged logind configuration text, lowest to highest precedence."""
    try:
        out = subprocess.run(["systemd-analyze", "cat-config", "systemd/logind.conf"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout:
            return out.stdout
    except (OSError, subprocess.SubprocessError):
        pass
    # Fallback without systemd-analyze: drop-ins override the main file, and
    # /etc drop-ins override same-named ones in /usr/lib.
    paths = ["/etc/systemd/logind.conf"]
    dropins = {}
    for d in ("/usr/lib/systemd/logind.conf.d", "/etc/systemd/logind.conf.d"):
        for p in glob.glob(os.path.join(d, "*.conf")):
            dropins[os.path.basename(p)] = p
    paths += [dropins[n] for n in sorted(dropins)]
    chunks = []
    for p in paths:
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                chunks.append(fh.read())
        except OSError:
            pass
    return "\n".join(chunks)


def read_logind(keys=LOGIND_KEYS):
    """Effective logind values for keys = [(key, label, default)]."""
    vals = {k: d for k, _, d in keys}
    seen = {}
    _parse_keys(_logind_conf_text(), seen)
    # Per systemd: ExternalPower inherits the battery value when unset.
    if "HandleLidSwitch" in seen and "HandleLidSwitch" in vals:
        vals["HandleLidSwitchExternalPower"] = seen["HandleLidSwitch"]
    for k in vals:
        if k in seen:
            vals[k] = seen[k]
    return vals


def logind_conf(values):
    """Drop-in file content for {key: action} (only managed keys, fixed order)."""
    lines = ["[Login]"] + [f"{k}={values[k]}" for k, _, _ in LOGIND_KEYS if k in values]
    return "\n".join(lines) + "\n"


def classify_exit(status, err):
    """('ok'|'cancelled'|'error', message) for a pkexec/polkit-backed command."""
    err = (err or "").strip()
    if status == 0:
        return "ok", ""
    if status in (126, 127) or "uthenticat" in err or "dismissed" in err:
        return "cancelled", err
    return "error", err or "unknown error"


def run_admin(script, stdin_text, callback):
    """Run `pkexec sh -c script` asynchronously, feeding stdin_text.

    callback(kind, message) with kind 'ok', 'cancelled' or 'error'. User text
    must only travel through stdin, never inside script.
    """
    try:
        proc = Gio.Subprocess.new(
            ["pkexec", "sh", "-c", script],
            Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
        )
    except GLib.Error as e:
        callback("error", f"could not run pkexec: {e.message}")
        return

    def done(p, res):
        try:
            _ok, _out, err = p.communicate_utf8_finish(res)
        except GLib.Error as e:
            callback("error", e.message)
            return
        callback(*classify_exit(p.get_exit_status(), err))
    proc.communicate_utf8_async(stdin_text, None, done)


class AdminWriter:
    """Applies admin-backed UI state right away, one pkexec at a time.

    build() -> (script, stdin_text, snapshot) from the current UI state.
    Changes arriving while a run is pending are coalesced into a single
    follow-up run with the latest state. On success on_ok(snapshot) records
    the applied state; on cancel/failure on_fail() must revert the UI to it
    (without calling request() again) and any queued follow-up is dropped.
    """

    def __init__(self, window, build, on_ok, on_fail):
        self.window, self.build = window, build
        self.on_ok, self.on_fail = on_ok, on_fail
        self.busy = self.queued = False

    def request(self):
        if self.busy:
            self.queued = True
            return
        script, stdin_text, snapshot = self.build()
        self.busy = True
        self._spawn(script, stdin_text,
                    lambda kind, msg: self._finished(kind, msg, snapshot))

    def _spawn(self, script, stdin_text, callback):  # overridden in tests
        run_admin(script, stdin_text, callback)

    def _finished(self, kind, msg, snapshot):
        self.busy = False
        if kind == "ok":
            self.on_ok(snapshot)
            if self.queued:
                self.queued = False
                self.request()
            else:
                self.window.toast("Applied")
            return
        self.queued = False
        self.on_fail()
        self.window.toast("Authentication cancelled \u2014 change reverted"
                          if kind == "cancelled"
                          else f"Failed: {msg} \u2014 change reverted")


def admin_icon():
    return Gtk.Image(icon_name="changes-allow-symbolic",
                     tooltip_text="Requires administrator password")


def admin(row):
    """Mark a row as needing the administrator password."""
    if row is not None:
        row.add_prefix(admin_icon())
    return row


class PowerPage(Page):
    def __init__(self, window):
        super().__init__(title="Power", icon_name="battery-symbolic")
        self.window = window
        self.logind_rows = {}
        self.logind_actions = {}
        self.lid_rows = {}
        self._quiet = False  # True while rows are changed by code, not the user
        if has_logind():
            self._build_logind_groups()
        else:
            self.add(group("Lid and power key", [disabled_row(
                "Lid and hardware power key actions",
                "Requires systemd-logind and polkit")]))

        P = "org.gnome.settings-daemon.plugins.power"
        idle_opts = [("nothing", "Do nothing"), ("suspend", "Suspend"),
                     ("hibernate", "Hibernate"), ("blank", "Blank screen"),
                     ("shutdown", "Power off"), ("logout", "Log out")]
        self.add(group("Power button", [
            combo_row("Power button action", P, "power-button-action", [
                ("interactive", "Ask what to do"), ("suspend", "Suspend"),
                ("hibernate", "Hibernate"), ("nothing", "Do nothing")]),
        ]))
        self.add(group("Automatic actions when idle", [
            combo_row("On battery power", P, "sleep-inactive-battery-type", idle_opts),
            spin_row("Battery timeout (seconds)", P,
                     "sleep-inactive-battery-timeout", 60, 86400, 60),
            combo_row("On AC power", P, "sleep-inactive-ac-type", idle_opts),
            spin_row("AC timeout (seconds)", P,
                     "sleep-inactive-ac-timeout", 60, 86400, 60),
            switch_row("Dim screen when idle", P, "idle-dim"),
            spin_row("Dimmed brightness (%)", P, "idle-brightness", 0, 100, 5),
            switch_row("Automatic brightness", P, "ambient-enabled"),
            switch_row("Power saver on low battery", P,
                       "power-saver-profile-on-low-battery"),
        ]))
        S = "org.gnome.desktop.session"
        L = "org.gnome.desktop.screensaver"
        self.add(group("Screen blank and lock", [
            spin_row("Blank screen after (seconds, 0 = never)", S,
                     "idle-delay", 0, 7200, 30),
            switch_row("Lock screen when blanked", L, "lock-enabled"),
            spin_row("Lock delay after blank (seconds)", L, "lock-delay", 0, 3600, 5),
        ]))

    def _logind_row(self, key, label, actions):
        cur = self.logind_values[key]
        actions = list(actions)
        values = [v for v, _ in actions]
        if cur not in values:
            actions.append((cur, f"Current: {cur}"))
            values.append(cur)
        row = Adw.ComboRow(title=label)
        row.set_model(Gtk.StringList.new([l for _, l in actions]))
        row.set_selected(values.index(cur))
        row.connect("notify::selected", self.on_row_changed)
        self.logind_rows[key] = row
        self.logind_actions[key] = actions
        return admin(row)

    def _build_logind_groups(self):
        self.logind_values = read_logind()
        self.applied = dict(self.logind_values)
        self.writer = AdminWriter(self.window, self._build_job,
                                  self._on_applied, self._revert)
        self._lock_st = get_settings("org.gnome.desktop.screensaver", "lock-enabled")
        note = "Changes ask for your administrator password and apply immediately."
        quick = Adw.PreferencesGroup(
            title="Lid quick options",
            description="Shortcuts that fill in the lid actions below. " + note)
        vals0 = [self.logind_values[k] for k, _, _ in LID_KEYS]
        self.keep_row = Adw.SwitchRow(
            title="Keep running when the lid is closed",
            subtitle="The machine stays on instead of suspending",
            active=all(v in ("ignore", "lock") for v in vals0))
        self.lock_row = Adw.SwitchRow(
            title="Lock the screen when the lid closes / on wake",
            subtitle="Require your password after closing and reopening the lid",
            active=self._lock_st.get_boolean("lock-enabled") if self._lock_st
            else any(v == "lock" for v in vals0))
        self.keep_row.connect("notify::active", self.on_quick)
        self.lock_row.connect("notify::active", self.on_quick)
        quick.add(admin(self.keep_row))
        quick.add(self.lock_row)
        self.add(quick)

        lid_group = Adw.PreferencesGroup(
            title="When the laptop lid is closed",
            description="Applied system-wide through systemd-logind. " + note)
        for key, label, _d in LID_KEYS:
            row = self._logind_row(key, label, LID_ACTIONS)
            self.lid_rows[key] = row
            lid_group.add(row)
        self.add(lid_group)

        key_group = Adw.PreferencesGroup(
            title="Hardware power key",
            description="What holding the power button does. A short press is "
                        "handled by GNOME (see Power button below). " + note)
        for key, label, _d in POWER_KEYS:
            key_group.add(self._logind_row(key, label, KEY_ACTIONS))
        self.add(key_group)

    # -- state ----------------------------------------------------------------
    def current_values(self):
        out = {}
        for key, row in self.logind_rows.items():
            i = row.get_selected()
            if i != Gtk.INVALID_LIST_POSITION:
                out[key] = self.logind_actions[key][i][0]
        return out

    def _set_rows(self, values):
        """Set rows from values without triggering writes."""
        self._quiet = True
        try:
            for key, row in self.logind_rows.items():
                vals = [v for v, _ in self.logind_actions[key]]
                if values.get(key) in vals:
                    row.set_selected(vals.index(values[key]))
            lid = [values.get(k) for k, _, _ in LID_KEYS]
            self.keep_row.set_active(all(v in ("ignore", "lock") for v in lid))
        finally:
            self._quiet = False

    def _build_job(self):
        values = self.current_values()
        return LOGIND_APPLY_SCRIPT, logind_conf(values), values

    def _on_applied(self, snapshot):
        self.applied = snapshot

    def _revert(self):
        self._set_rows(self.applied)

    # -- events ---------------------------------------------------------------
    def on_row_changed(self, _row, _p):
        if not self._quiet:
            self.writer.request()

    def _select_all(self, action_for):
        values = [v for v, _ in LID_ACTIONS]
        for key, row in self.lid_rows.items():
            act = action_for(key)
            if act in values:
                row.set_selected(values.index(act))

    def on_quick(self, row, _p):
        if self._quiet:
            return
        keep, lock = self.keep_row.get_active(), self.lock_row.get_active()
        if self._lock_st is not None and row is self.lock_row:
            self._lock_st.set_boolean("lock-enabled", lock)
        before = self.current_values()
        self._quiet = True  # batch: several combos change, write only once
        try:
            if keep:
                act = "lock" if lock else "ignore"
                self._select_all(lambda k: act)
            else:
                self._select_all(
                    lambda k: "ignore" if k.endswith("Docked") else "suspend")
        finally:
            self._quiet = False
        if self.current_values() != before:
            self.writer.request()


# --------------------------------------------------------------------------
# Startup apps page
# --------------------------------------------------------------------------
def load_entry(path):
    kf = GLib.KeyFile.new()
    try:
        kf.load_from_file(path, GLib.KeyFileFlags.KEEP_TRANSLATIONS)
    except GLib.Error:
        return None
    return kf


def kf_get(kf, key, default=""):
    try:
        return kf.get_string("Desktop Entry", key)
    except GLib.Error:
        return default


def kf_enabled(kf):
    if kf_get(kf, "Hidden").lower() == "true":
        return False
    return kf_get(kf, "X-GNOME-Autostart-enabled", "true").lower() != "false"


class StartupPage(Page):
    def __init__(self, window):
        super().__init__(title="Startup Apps", icon_name="system-run-symbolic")
        self.window = window
        self.rows = []
        self.grp = Adw.PreferencesGroup(
            title="Applications run at login",
            description="Entries from ~/.config/autostart and system-wide defaults.",
        )
        add_btn = Gtk.Button(label="Add…")
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", self.on_add)
        self.grp.set_header_suffix(add_btn)
        self.add(self.grp)
        self.refresh()

    # -- listing ------------------------------------------------------------
    def entries(self):
        found = {}
        for d, system in ((SYSTEM_AUTOSTART, True), (USER_AUTOSTART, False)):
            if not os.path.isdir(d):
                continue
            for fn in sorted(os.listdir(d)):
                if not fn.endswith(".desktop"):
                    continue
                kf = load_entry(os.path.join(d, fn))
                if kf is None:
                    continue
                prev = found.get(fn)
                found[fn] = (kf, system or (prev[2] if prev else False),
                             not system)
        return found

    def refresh(self):
        for r in self.rows:
            self.grp.remove(r)
        self.rows = []
        items = self.entries()
        for fn, (kf, has_system, is_user) in sorted(
                items.items(), key=lambda kv: kf_get(kv[1][0], "Name", kv[0]).lower()):
            if kf_get(kf, "NoDisplay").lower() == "true" and has_system and not is_user:
                continue  # hide plumbing entries unless the user touched them
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(kf_get(kf, "Name", fn)),
                subtitle=GLib.markup_escape_text(kf_get(kf, "Exec")),
            )
            sw = Gtk.Switch(valign=Gtk.Align.CENTER, active=kf_enabled(kf))
            sw.connect("state-set", self.on_toggle, fn)
            row.add_suffix(sw)
            if is_user and not has_system:
                rm = Gtk.Button(icon_name="user-trash-symbolic",
                                valign=Gtk.Align.CENTER, tooltip_text="Remove")
                rm.add_css_class("flat")
                rm.connect("clicked", self.on_remove, fn)
                row.add_suffix(rm)
            elif has_system:
                row.set_tooltip_text("System entry: can be disabled, not removed")
            self.grp.add(row)
            self.rows.append(row)

    # -- actions ------------------------------------------------------------
    def on_toggle(self, sw, state, fn):
        src = os.path.join(USER_AUTOSTART, fn)
        if not os.path.exists(src):
            src = os.path.join(SYSTEM_AUTOSTART, fn)
        kf = load_entry(src)
        if kf is None:
            return False
        kf.set_string("Desktop Entry", "X-GNOME-Autostart-enabled",
                      "true" if state else "false")
        if state and kf_get(kf, "Hidden").lower() == "true":
            kf.set_string("Desktop Entry", "Hidden", "false")
        os.makedirs(USER_AUTOSTART, exist_ok=True)
        kf.save_to_file(os.path.join(USER_AUTOSTART, fn))
        return False

    def on_remove(self, _btn, fn):
        try:
            os.remove(os.path.join(USER_AUTOSTART, fn))
        except OSError as e:
            self.window.toast(str(e))
        self.refresh()

    def on_add(self, _btn):
        apps = [a for a in Gio.AppInfo.get_all() if a.should_show()]
        apps.sort(key=lambda a: a.get_display_name().lower())

        dlg = Adw.AlertDialog(heading="Add startup application")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        dd = Gtk.DropDown.new_from_strings(
            ["Custom command"] + [a.get_display_name() for a in apps])
        dd.set_enable_search(True)
        name = Gtk.Entry(placeholder_text="Name")
        cmd = Gtk.Entry(placeholder_text="Command, e.g. /usr/bin/myapp --flag")
        for w in (dd, name, cmd):
            box.append(w)

        def on_pick(d, _p):
            i = d.get_selected()
            if i > 0:
                a = apps[i - 1]
                name.set_text(a.get_display_name())
                cmd.set_text(
                    (a.get_commandline() or "").replace("%U", "").replace(
                        "%u", "").replace("%F", "").replace("%f", "").strip())
        dd.connect("notify::selected", on_pick)

        dlg.set_extra_child(box)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("add", "Add")
        dlg.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)

        def done(d, resp):
            if resp != "add":
                return
            n, c = name.get_text().strip(), cmd.get_text().strip()
            if not n or not c:
                self.window.toast("Name and command are required")
                return
            kf = GLib.KeyFile.new()
            kf.set_string("Desktop Entry", "Type", "Application")
            kf.set_string("Desktop Entry", "Name", n)
            kf.set_string("Desktop Entry", "Exec", c)
            kf.set_string("Desktop Entry", "X-GNOME-Autostart-enabled", "true")
            safe = "".join(ch if ch.isalnum() else "-" for ch in n.lower()).strip("-")
            os.makedirs(USER_AUTOSTART, exist_ok=True)
            path = os.path.join(USER_AUTOSTART, f"{safe or 'app'}.desktop")
            kf.save_to_file(path)
            self.refresh()
        dlg.connect("response", done)
        dlg.present(self.window)


# --------------------------------------------------------------------------
# Windows and Desktop page
# --------------------------------------------------------------------------
class WorkspaceNamesGroup(Adw.PreferencesGroup):
    """One EntryRow per workspace (up to 12); saved on the apply signal."""

    MAX = 12

    def __init__(self, schema):
        super().__init__(title="Workspace names",
                         description="Leave a name empty to use the default.")
        self.st = get_settings(schema, "workspace-names")
        self.count_st = get_settings(schema, "num-workspaces")
        self.rows = []
        self.count_st.connect("changed::num-workspaces", lambda *_: self.rebuild())
        self.rebuild()

    @classmethod
    def create(cls, schema):
        if get_settings(schema, "workspace-names") is None \
                or get_settings(schema, "num-workspaces") is None:
            return None
        return cls(schema)

    def rebuild(self):
        for r in self.rows:
            self.remove(r)
        names = list(self.st.get_strv("workspace-names"))
        n = max(1, min(self.count_st.get_int("num-workspaces"), self.MAX))
        self.rows = []
        for i in range(n):
            row = Adw.EntryRow(title=f"Workspace {i + 1}", show_apply_button=True,
                               text=names[i] if i < len(names) else "")
            row.connect("apply", self.save)
            self.add(row)
            self.rows.append(row)

    def save(self, _row):
        names = list(self.st.get_strv("workspace-names"))
        names += [""] * (len(self.rows) - len(names))
        for i, row in enumerate(self.rows):
            names[i] = row.get_text().strip()
        while names and not names[-1]:
            names.pop()
        self.st.set_strv("workspace-names", names)


class DesktopPage(Page):
    def __init__(self, window):
        super().__init__(title="Windows and Desktop", icon_name="preferences-desktop-symbolic")
        W = "org.gnome.desktop.wm.preferences"
        M = "org.gnome.mutter"
        I = "org.gnome.desktop.interface"
        T = "org.gnome.desktop.peripherals.touchpad"

        self.add(group("Titlebar buttons", [self._buttons_row(W)]))
        tb = [("toggle-maximize", "Toggle maximize"), ("minimize", "Minimize"),
              ("lower", "Lower"), ("menu", "Window menu"), ("none", "Nothing"),
              ("toggle-maximize-horizontally", "Maximize horizontally"),
              ("toggle-maximize-vertically", "Maximize vertically"),
              ("toggle-shade", "Toggle roll-up")]
        self.add(group("Titlebar actions", [
            combo_row("Double-click", W, "action-double-click-titlebar", tb),
            combo_row("Middle-click", W, "action-middle-click-titlebar", tb),
            combo_row("Right-click", W, "action-right-click-titlebar", tb),
        ]))
        self.add(group("Window behaviour", [
            combo_row("Focus mode", W, "focus-mode", [
                ("click", "Click to focus"), ("sloppy", "Focus on hover"),
                ("mouse", "Focus on hover (strict)")]),
            switch_row("Edge tiling", M, "edge-tiling",
                       "Drag windows to screen edges to tile them"),
            switch_row("Auto-maximize large windows", M, "auto-maximize"),
            switch_row("Center new windows", M, "center-new-windows"),
            switch_row("Attach modal dialogs", M, "attach-modal-dialogs"),
            spin_row("Draggable border width (px)", M, "draggable-border-width",
                     0, 64, 1, "Invisible resize area around windows"),
            switch_row("Change focus when the pointer rests", M,
                       "focus-change-on-pointer-rest",
                       "In hover focus modes, focus follows the pointer after it stops"),
            spin_row("Not-responding timeout (ms)", M, "check-alive-timeout",
                     0, 60000, 1000,
                     'How long before "not responding" appears; 0 disables'),
            combo_row("Overview key", M, "overlay-key", [
                ("Super_L", "Left Super"), ("Super_R", "Right Super"),
                ("", "Disabled")], "Key that opens the Activities overview"),
            combo_row("Locate pointer key", M, "locate-pointer-key", [
                ("Control_L", "Left Ctrl"), ("Control_R", "Right Ctrl")],
                "Used when Locate pointer with Ctrl is on"),
        ]))
        KB = "org.gnome.desktop.wm.keybindings"
        self.add(group("Window switching and mouse", [
            self._alt_tab_row(KB),
            combo_row("Window switcher shows", "org.gnome.shell.window-switcher",
                      "app-icon-mode", [("thumbnail-only", "Window thumbnails"),
                                        ("app-icon-only", "App icons"),
                                        ("both", "Thumbnails and icons")]),
            multi_switch_row("Alt+Tab: current workspace only",
                             [("org.gnome.shell.window-switcher", "current-workspace-only"),
                              ("org.gnome.shell.app-switcher", "current-workspace-only")],
                             "Applies to both the window and app switchers"),
            switch_row("Raise window on click", W, "raise-on-click"),
            switch_row("Auto-raise on hover", W, "auto-raise"),
            spin_row("Auto-raise delay (ms)", W, "auto-raise-delay", 0, 5000, 50),
            combo_row("New window focus", W, "focus-new-windows",
                      [("smart", "Smart"), ("strict", "Strict")]),
            combo_row("Move/resize modifier", W, "mouse-button-modifier", [
                ("<Super>", "Super"), ("<Alt>", "Alt"), ("<Control>", "Ctrl"),
                ("disabled", "Disabled")],
                "Hold with left-drag to move, right-drag to resize"),
            switch_row("Resize with right mouse button", W, "resize-with-right-button"),
        ]))
        self.add(group("Workspaces", [
            spin_row("Number of workspaces (static mode)", W, "num-workspaces", 1, 36, 1,
                     "Applies when dynamic workspaces is off"),
            switch_row("Dynamic workspaces", M, "dynamic-workspaces"),
            switch_row("Workspaces on primary display only", M,
                       "workspaces-only-on-primary"),
        ]))
        self.add(WorkspaceNamesGroup.create(W))
        LD = "org.gnome.desktop.lockdown"
        SS = "org.gnome.desktop.screensaver"
        self.add(group("Session", [
            switch_row("Always show Log Out in the system menu", "org.gnome.shell",
                       "always-show-log-out",
                       "Show Log Out even with a single user and session type"),
            switch_row("Allow Log Out", LD, "disable-log-out",
                       "Admin lockdown: when off, Log Out is removed entirely",
                       invert=True),
            switch_row("Show Log Out button on the lock screen", SS, "logout-enabled"),
            spin_row("Lock-screen log out delay (seconds)", SS, "logout-delay",
                     0, 7200, 30),
            switch_row("Show Switch User", LD, "disable-user-switching",
                       invert=True),
            switch_row("Show Restart button on the lock screen", SS, "restart-enabled"),
            switch_row("Confirm before logging out / powering off",
                       "org.gnome.SessionManager", "logout-prompt"),
            switch_row("Restore previous session", "org.gnome.desktop.session",
                       "save-restore", "Experimental in GNOME"),
        ]))
        self.add(group("Top bar and clock", [
            switch_row("Hot corner", I, "enable-hot-corners"),
            switch_row("Show battery percentage", I, "show-battery-percentage"),
            combo_row("Clock format", I, "clock-format",
                      [("24h", "24-hour"), ("12h", "12-hour")]),
            switch_row("Show seconds", I, "clock-show-seconds"),
            switch_row("Show date", I, "clock-show-date"),
            switch_row("Show weekday", I, "clock-show-weekday"),
        ]))
        self.add(group("Touchpad extras", [
            combo_row("Click method", T, "click-method", [
                ("fingers", "Two-finger right-click"),
                ("areas", "Bottom-corner right-click"), ("none", "None")]),
            combo_row("Pointer acceleration", T, "accel-profile", [
                ("default", "Default"), ("flat", "Flat"),
                ("adaptive", "Adaptive")]),
            switch_row("Tap and drag", T, "tap-and-drag"),
            switch_row("Disable while typing", T, "disable-while-typing"),
            switch_row("Edge scrolling", T, "edge-scrolling-enabled"),
        ]))

    @staticmethod
    def _alt_tab_row(schema):
        st = get_settings(schema, "switch-windows")
        if st is None:
            return None
        row = Adw.ComboRow(title="Alt+Tab switches between",
                           subtitle="Windows lists every window; Applications groups them")
        row.set_model(Gtk.StringList.new(["Applications", "Windows"]))
        row.set_selected(1 if "<Alt>Tab" in st.get_strv("switch-windows") else 0)
        def changed(r, _p):
            if r.get_selected() == 1:
                st.set_strv("switch-windows", ["<Alt>Tab"])
                st.set_strv("switch-windows-backward", ["<Shift><Alt>Tab"])
                st.set_strv("switch-applications", ["<Super>Tab"])
                st.set_strv("switch-applications-backward", ["<Shift><Super>Tab"])
            else:
                st.set_strv("switch-windows", [])
                st.set_strv("switch-windows-backward", [])
                st.set_strv("switch-applications", ["<Super>Tab", "<Alt>Tab"])
                st.set_strv("switch-applications-backward",
                            ["<Shift><Super>Tab", "<Shift><Alt>Tab"])
        row.connect("notify::selected", changed)
        return row

    @staticmethod
    def _buttons_row(schema):
        st = get_settings(schema, "button-layout")
        if st is None:
            return None
        presets = [
            ("appmenu:close", "Close only"),
            ("appmenu:minimize,maximize,close", "Minimize, maximize, close (right)"),
            ("close,minimize,maximize:appmenu", "macOS style (left)"),
            ("appmenu:maximize,close", "Maximize, close"),
        ]
        cur = st.get_string("button-layout")
        if cur not in [p for p, _ in presets]:
            presets.append((cur, f"Custom ({cur})"))
        row = combo_row("Layout", schema, "button-layout", presets)
        return row


# --------------------------------------------------------------------------
# Mouse & keyboard page
# --------------------------------------------------------------------------
XKB = "org.gnome.desktop.input-sources"


def xkb_row(title, key_prefixes, options, subtitle=None):
    """ComboRow editing one family of xkb-options (e.g. Caps Lock behaviour)."""
    st = get_settings(XKB, "xkb-options")
    if st is None:
        return None
    values = [v for v, _ in options]
    def family(opt):
        return opt.startswith(key_prefixes)
    cur = [o for o in st.get_strv("xkb-options") if family(o)]
    row = Adw.ComboRow(title=title, subtitle=subtitle or "")
    row.set_model(Gtk.StringList.new([l for _, l in options]))
    row.set_selected(values.index(cur[0]) if cur and cur[0] in values else 0)
    def changed(r, _p):
        v = values[r.get_selected()]
        opts = [o for o in st.get_strv("xkb-options") if not family(o)]
        if v:
            opts.append(v)
        st.set_strv("xkb-options", opts)
    row.connect("notify::selected", changed)
    return row


class InputPage(Page):
    def __init__(self, window):
        super().__init__(title="Input", icon_name="input-keyboard-symbolic")
        Mo = "org.gnome.desktop.peripherals.mouse"
        Kb = "org.gnome.desktop.peripherals.keyboard"
        Tp = "org.gnome.desktop.peripherals.touchpad"
        self.add(group("Mouse", [
            spin_row("Pointer speed", Mo, "speed", -1.0, 1.0, 0.1, digits=1),
            combo_row("Acceleration profile", Mo, "accel-profile", [
                ("default", "Default"), ("flat", "Flat (no acceleration)"),
                ("adaptive", "Adaptive")]),
            switch_row("Natural scrolling", Mo, "natural-scroll"),
            switch_row("Left-handed (swap buttons)", Mo, "left-handed"),
            switch_row("Middle-click emulation", Mo, "middle-click-emulation",
                       "Press both buttons together to middle-click"),
            spin_row("Double-click time (ms)", Mo, "double-click", 100, 2000, 50),
            spin_row("Drag threshold (px)", Mo, "drag-threshold", 1, 100, 1),
            spin_row("Scroll wheel emulation button", Mo, "scroll-wheel-emulation-button",
                     0, 24, 1, "Hold this button while moving a trackball or "
                     "pointing stick to scroll; 0 = off"),
            switch_row("Lock scroll wheel emulation button", Mo,
                       "scroll-wheel-emulation-button-lock",
                       "Press once to start scrolling, again to stop"),
        ]))
        self.add(group("Touchpad", [
            spin_row("Pointer speed", Tp, "speed", -1.0, 1.0, 0.1, digits=1),
            switch_row("Natural scrolling", Tp, "natural-scroll"),
            switch_row("Tap to click", Tp, "tap-to-click"),
            switch_row("Two-finger scrolling", Tp, "two-finger-scrolling-enabled"),
            combo_row("Primary button", Tp, "left-handed", [
                ("right", "Right-handed"), ("left", "Left-handed"),
                ("mouse", "Follow mouse")]),
            switch_row("Middle-click emulation", Tp, "middle-click-emulation",
                       "Press both buttons together to middle-click"),
            spin_row("Scroll speed", Tp, "scroll-speed", 0.1, 5.0, 0.1, digits=1),
            combo_row("Touchpad", Tp, "send-events", [
                ("enabled", "Always enabled"), ("disabled", "Always disabled"),
                ("disabled-on-external-mouse",
                 "Disable touchpad when a mouse is plugged in")],
                "Choose when the touchpad is active"),
            combo_row("Tap button map", Tp, "tap-button-map", [
                ("default", "Default"), ("lrm", "One, two, three fingers: "
                 "left, right, middle"), ("lmr", "One, two, three fingers: "
                 "left, middle, right")]),
            switch_row("Tap-and-drag lock", Tp, "tap-and-drag-lock",
                       "Keep dragging after lifting the finger briefly"),
            spin_row("Disable while typing timeout (ms)", Tp,
                     "disable-while-typing-timeout", 100, 5000, 100),
        ]))
        PS = "org.gnome.desktop.peripherals.pointingstick"
        self.add(group("Pointing stick (TrackPoint)", [
            spin_row("Pointer speed", PS, "speed", -1.0, 1.0, 0.1, digits=1),
            combo_row("Acceleration profile", PS, "accel-profile", [
                ("default", "Default"), ("flat", "Flat (no acceleration)"),
                ("adaptive", "Adaptive")]),
            combo_row("Scroll method", PS, "scroll-method", [
                ("default", "Default"), ("none", "Disabled"),
                ("on-button-down", "Scroll while the middle button is held")]),
        ]))
        self.add(group("Keyboard", [
            switch_row("Repeat keys when held", Kb, "repeat"),
            spin_row("Repeat delay (ms)", Kb, "delay", 100, 2000, 50),
            spin_row("Repeat interval (ms)", Kb, "repeat-interval", 10, 500, 5),
            switch_row("Remember Num Lock state", Kb, "remember-numlock-state"),
            switch_row("Num Lock on", Kb, "numlock-state"),
            entry_row("Keyboard model (xkb)", XKB, "xkb-model"),
        ]))
        self.add(group("Input sources", [
            switch_row("Different source for each window", XKB, "per-window"),
            switch_row("Show all input sources", XKB, "show-all-sources"),
        ]))
        self.add(group("Key remapping", [
            xkb_row("Caps Lock behaves as", ("caps:", "ctrl:nocaps"), [
                ("", "Caps Lock (default)"), ("ctrl:nocaps", "Ctrl"),
                ("caps:escape", "Escape"), ("caps:swapescape", "Swap with Escape"),
                ("caps:backspace", "Backspace"), ("caps:none", "Disabled")]),
            xkb_row("Compose key", ("compose:",), [
                ("", "None"), ("compose:ralt", "Right Alt"),
                ("compose:rctrl", "Right Ctrl"), ("compose:menu", "Menu key"),
                ("compose:caps", "Caps Lock"), ("compose:rwin", "Right Super")]),
            xkb_row("Alt/Win keys", ("altwin:",), [
                ("", "Default"), ("altwin:swap_alt_win", "Swap Alt and Super"),
                ("altwin:ctrl_alt_win", "Ctrl+Alt+Super as modifiers")]),
        ]))


# --------------------------------------------------------------------------
# Appearance page (KDE-style theme controls GNOME hides)
# --------------------------------------------------------------------------
def list_themes(kind):
    """Installed theme names. kind: 'icon' | 'cursor' | 'gtk' | 'sound'."""
    home = GLib.get_home_dir()
    data = [GLib.get_user_data_dir()] + list(GLib.get_system_data_dirs())
    names = set()
    for d in data:
        sub = {"gtk": "themes", "sound": "sounds"}.get(kind, "icons")
        roots = [os.path.join(d, sub)]
        if kind != "sound":
            roots.append(os.path.join(home, ".themes" if kind == "gtk" else ".icons"))
        for root in roots:
            if not os.path.isdir(root):
                continue
            for n in os.listdir(root):
                base = os.path.join(root, n)
                if kind == "cursor" and os.path.isdir(os.path.join(base, "cursors")):
                    names.add(n)
                elif kind == "sound" and os.path.exists(os.path.join(base, "index.theme")):
                    names.add(n)
                elif kind == "icon" and os.path.exists(os.path.join(base, "index.theme")) \
                        and not os.path.isdir(os.path.join(base, "cursors")):
                    names.add(n)
                elif kind == "gtk" and (os.path.isdir(os.path.join(base, "gtk-4.0"))
                                        or os.path.isdir(os.path.join(base, "gtk-3.0"))):
                    names.add(n)
    return sorted(names, key=str.lower)


def theme_row(title, schema, key, kind, extra=()):
    st = get_settings(schema, key)
    if st is None:
        return None
    names = list(extra) + [n for n in list_themes(kind) if n not in extra]
    cur = st.get_string(key)
    if cur not in names:
        names.insert(0, cur)
    return combo_row(title, schema, key, [(n, n) for n in names])


def font_row(title, schema, key):
    st = get_settings(schema, key)
    if st is None:
        return None
    row = Adw.ActionRow(title=title)
    btn = Gtk.FontDialogButton(dialog=Gtk.FontDialog(), valign=Gtk.Align.CENTER)
    btn.set_font_desc(Pango.FontDescription.from_string(st.get_string(key)))
    btn.connect("notify::font-desc",
                lambda b, _p: st.set_string(key, b.get_font_desc().to_string()))
    row.add_suffix(btn)
    return row


class AppearancePage(Page):
    def __init__(self, window):
        super().__init__(title="Appearance", icon_name="applications-graphics-symbolic")
        I = "org.gnome.desktop.interface"
        W = "org.gnome.desktop.wm.preferences"
        BG = "org.gnome.desktop.background"
        self.add(group("Wallpaper", [
            uri_row("Wallpaper", BG, "picture-uri"),
            uri_row("Wallpaper (dark style)", BG, "picture-uri-dark"),
            combo_row("Picture options", BG, "picture-options", [
                ("none", "None"), ("wallpaper", "Tiled"), ("centered", "Centered"),
                ("scaled", "Scaled"), ("stretched", "Stretched"),
                ("zoom", "Zoom"), ("spanned", "Spanned")]),
            color_row("Primary color", BG, "primary-color"),
            color_row("Secondary color", BG, "secondary-color"),
            combo_row("Color shading", BG, "color-shading-type", [
                ("solid", "Solid"), ("vertical", "Vertical gradient"),
                ("horizontal", "Horizontal gradient")],
                "Blends the two colors; visible behind or without a picture"),
        ]))
        self.add(group("Style", [
            combo_row("Color scheme", I, "color-scheme", [
                ("default", "System default"), ("prefer-light", "Light"),
                ("prefer-dark", "Dark")]),
            combo_row("Accent color", I, "accent-color", [
                ("blue", "Blue"), ("teal", "Teal"), ("green", "Green"),
                ("yellow", "Yellow"), ("orange", "Orange"), ("red", "Red"),
                ("pink", "Pink"), ("purple", "Purple"), ("slate", "Slate")]),
            theme_row("Legacy GTK theme", I, "gtk-theme", "gtk", extra=("Adwaita",)),
            theme_row("Icon theme", I, "icon-theme", "icon"),
            theme_row("Cursor theme", I, "cursor-theme", "cursor"),
            spin_row("Cursor size", I, "cursor-size", 16, 128, 4),
        ]))
        self.add(group("Text and fonts", [
            font_row("Interface font", I, "font-name"),
            font_row("Document font", I, "document-font-name"),
            font_row("Monospace font", I, "monospace-font-name"),
            font_row("Window title font", W, "titlebar-font"),
            switch_row("Titlebar uses interface font", W, "titlebar-uses-system-font"),
            spin_row("Text scaling factor", I, "text-scaling-factor", 0.5, 3.0, 0.05,
                     digits=2),
            combo_row("Font hinting", I, "font-hinting", [
                ("none", "None"), ("slight", "Slight"), ("medium", "Medium"),
                ("full", "Full")]),
            combo_row("Font antialiasing", I, "font-antialiasing", [
                ("none", "None"), ("grayscale", "Grayscale"),
                ("rgba", "Subpixel (LCD)")]),
            combo_row("Subpixel order", I, "font-rgba-order", [
                ("rgb", "RGB"), ("bgr", "BGR"), ("vrgb", "Vertical RGB"),
                ("vbgr", "Vertical BGR"), ("rgba", "RGBA")],
                "Used with subpixel antialiasing"),
            switch_row("Blinking text cursor", I, "cursor-blink"),
            spin_row("Cursor blink time (ms)", I, "cursor-blink-time", 100, 2500, 100),
            switch_row("Middle-click paste", I, "gtk-enable-primary-paste",
                       "Paste the selected text with the middle mouse button"),
        ]))
        SND = "org.gnome.desktop.sound"
        self.add(group("Effects and sounds", [
            switch_row("Animations", I, "enable-animations"),
            switch_row("Overlay scrollbars", I, "overlay-scrolling"),
            switch_row("Locate pointer with Ctrl", I, "locate-pointer"),
            theme_row("Sound theme", SND, "theme-name", "sound",
                      extra=("freedesktop",)),
            switch_row("System event sounds", SND, "event-sounds"),
            switch_row("Input feedback sounds", SND, "input-feedback-sounds"),
            switch_row("Allow volume above 100%", SND, "allow-volume-above-100-percent"),
            switch_row("Visual bell", W, "visual-bell"),
            combo_row("Visual bell style", W, "visual-bell-type", [
                ("fullscreen-flash", "Flash the screen"),
                ("frame-flash", "Flash the window title")]),
            switch_row("Audible bell", W, "audible-bell"),
        ]))
        NO = "org.gnome.desktop.notifications"
        self.add(group("Notifications", [
            switch_row("Show notification banners", NO, "show-banners"),
            switch_row("Show notifications on the lock screen", NO,
                       "show-in-lock-screen"),
        ]))


# --------------------------------------------------------------------------
# Shortcuts page
# --------------------------------------------------------------------------
MEDIA_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM_SCHEMA = MEDIA_KEYS + ".custom-keybinding"
CUSTOM_BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
KEYBINDING_SCHEMAS = (
    "org.gnome.desktop.wm.keybindings", "org.gnome.mutter.keybindings",
    "org.gnome.mutter.wayland.keybindings", "org.gnome.shell.keybindings", MEDIA_KEYS)

SHELL_KB = "org.gnome.shell.keybindings"
# friendly names for conflict messages: (schema, key) -> title
SHORTCUT_TITLES = {
    (SHELL_KB, "show-screenshot-ui"): "Take a screenshot (select area)",
    (SHELL_KB, "screenshot"): "Screenshot the whole screen",
    (SHELL_KB, "screenshot-window"): "Screenshot a window",
    (SHELL_KB, "show-screen-recording-ui"): "Record the screen",
}

_M = Gdk.ModifierType
# modifier keyval -> modifier mask bit (ints, so they can be combined freely)
MOD_KEYVALS = {
    Gdk.KEY_Shift_L: _M.SHIFT_MASK, Gdk.KEY_Shift_R: _M.SHIFT_MASK,
    Gdk.KEY_Control_L: _M.CONTROL_MASK, Gdk.KEY_Control_R: _M.CONTROL_MASK,
    Gdk.KEY_Alt_L: _M.ALT_MASK, Gdk.KEY_Alt_R: _M.ALT_MASK,
    Gdk.KEY_Super_L: _M.SUPER_MASK, Gdk.KEY_Super_R: _M.SUPER_MASK,
    Gdk.KEY_Meta_L: _M.META_MASK, Gdk.KEY_Meta_R: _M.META_MASK,
    Gdk.KEY_Hyper_L: _M.HYPER_MASK, Gdk.KEY_Hyper_R: _M.HYPER_MASK}
MOD_KEYVALS = {k: int(v) for k, v in MOD_KEYVALS.items()}
# keys that never form a combination on their own (lock/level keys)
NEUTRAL_KEYVALS = {Gdk.KEY_ISO_Level3_Shift, Gdk.KEY_ISO_Level5_Shift,
                   Gdk.KEY_Caps_Lock, Gdk.KEY_Shift_Lock, Gdk.KEY_Num_Lock,
                   Gdk.KEY_Mode_switch}
MOD_NAMES = ((_M.SUPER_MASK, "Super"), (_M.HYPER_MASK, "Hyper"),
             (_M.META_MASK, "Meta"), (_M.CONTROL_MASK, "Ctrl"),
             (_M.ALT_MASK, "Alt"), (_M.SHIFT_MASK, "Shift"))
# printable-looking or navigation keys that need a real modifier
NEEDS_MODIFIER = {Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab, Gdk.KEY_Return,
                  Gdk.KEY_KP_Enter, Gdk.KEY_space, Gdk.KEY_Delete, Gdk.KEY_Home,
                  Gdk.KEY_End, Gdk.KEY_Page_Up, Gdk.KEY_Page_Down, Gdk.KEY_Left,
                  Gdk.KEY_Right, Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_KP_Delete,
                  Gdk.KEY_BackSpace, Gdk.KEY_Escape}
KEYCAP_CSS = """
.keycap { min-width: 1.2em; padding: 4px 10px; border-radius: 6px;
  background: alpha(currentColor, 0.08); border: 1px solid alpha(currentColor, 0.25);
  border-bottom-width: 3px; font-weight: bold; }
.keycap-big { font-size: 1.8em; padding: 8px 16px; }
"""
_css_loaded = False


def ensure_keycap_css():
    global _css_loaded
    if _css_loaded or Gdk.Display.get_default() is None:
        return
    prov = Gtk.CssProvider()
    try:
        prov.load_from_string(KEYCAP_CSS)
    except AttributeError:  # GTK < 4.12
        prov.load_from_data(KEYCAP_CSS.encode())
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _css_loaded = True


def mod_mask():
    return int(Gtk.accelerator_get_default_mod_mask())


def parse_accel(accel):
    """(keyval, mods) with a lower-case keyval and filtered mods, or None."""
    if not accel:
        return None
    ok, keyval, mods = Gtk.accelerator_parse(accel)
    if not ok or not keyval:
        return None
    return Gdk.keyval_to_lower(keyval), int(mods) & mod_mask()


def accel_keys(keyval, mods):
    """Labels of the keycaps for a combination (modifiers first)."""
    keys = [name for bit, name in MOD_NAMES if int(mods) & int(bit)]
    if keyval:
        keys.append(Gtk.accelerator_get_label(keyval, Gdk.ModifierType(0)))
    return keys


def keycap_box(keys, placeholder=None, big=False):
    box = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
    if not keys and placeholder:
        lbl = Gtk.Label(label=placeholder)
        lbl.add_css_class("dim-label")
        box.append(lbl)
    for k in keys:
        lbl = Gtk.Label(label=k)
        lbl.add_css_class("keycap")
        if big:
            lbl.add_css_class("keycap-big")
        box.append(lbl)
    return box


def accel_problem(keyval, mods):
    """None if keyval+mods is a usable shortcut, otherwise an error message."""
    mods = int(mods) & mod_mask()
    keyval = Gdk.keyval_to_lower(keyval)
    # GTK refuses Tab, but GNOME uses e.g. <Super>Tab; the modifier rule covers it
    if keyval not in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab) \
            and not Gtk.accelerator_valid(keyval, Gdk.ModifierType(mods)):
        return "This key combination can't be used"
    if mods & ~int(_M.SHIFT_MASK):
        return None
    if Gdk.keyval_to_unicode(keyval) != 0 or keyval in NEEDS_MODIFIER:
        return "Use a modifier such as Ctrl, Alt or Super"
    return None


def _setting_accels(gs, key):
    """Accelerator strings held by an 'as' or 's' key (empty for others)."""
    t = gs.get_value(key).get_type_string()
    if t == "as":
        return list(gs.get_strv(key))
    if t == "s":
        v = gs.get_string(key)
        return [v] if v else []
    return []


def find_conflicts(accel, exclude=None):
    """Other actions using the same accelerator.

    exclude is (Gio.Settings, key) for the key being edited. Returns a list of
    (name, remove) where remove() drops that accelerator from the other action.
    """
    target = parse_accel(accel)
    if target is None:
        return []
    skip = None
    if exclude:
        est, ekey = exclude
        skip = (est.get_property("schema-id"), est.get_property("path"), ekey)
    found = []

    def check(gs, key, name):
        sid, path = gs.get_property("schema-id"), gs.get_property("path")
        if skip == (sid, path, key):
            return
        accels = _setting_accels(gs, key)
        if not any(parse_accel(a) == target for a in accels):
            return
        if gs.get_value(key).get_type_string() == "as":
            def remove(gs=gs, key=key, accels=accels):
                gs.set_strv(key, [a for a in accels if parse_accel(a) != target])
        else:
            def remove(gs=gs, key=key):
                gs.set_string(key, "")
        found.append((SHORTCUT_TITLES.get((sid, key), name), remove))

    src = Gio.SettingsSchemaSource.get_default()
    for schema in KEYBINDING_SCHEMAS:
        sch = src.lookup(schema, True)
        if sch is None:
            continue
        gs = Gio.Settings.new(schema)
        for key in sch.list_keys():
            if key.endswith("-static") or key == "custom-keybindings":
                continue
            try:
                check(gs, key, key)
            except Exception:
                pass
    cust = get_settings(MEDIA_KEYS, "custom-keybindings")
    if cust and src.lookup(CUSTOM_SCHEMA, True) is not None:
        for path in cust.get_strv("custom-keybindings"):
            gs = Gio.Settings.new_with_path(CUSTOM_SCHEMA, path)
            check(gs, "binding", gs.get_string("name") or path)
    return found


class ShortcutCaptureDialog(Adw.AlertDialog):
    """Capture a key combination with live feedback, validation and conflict check.

    callback(result, removals): result is an accelerator string, '' (disabled)
    or None (cancelled); removals are the callables that drop the accelerator
    from conflicting actions (the caller runs them when it commits the change).
    exclude: (Gio.Settings, key) of the setting being edited, ignored when
    looking for conflicts.
    """

    def __init__(self, name, callback, exclude=None):
        super().__init__(heading="Set shortcut",
                         body=f"Press the keys for “{name}”")
        ensure_keycap_css()
        self.callback, self.exclude = callback, exclude
        self.pending = None   # (keyval, mods) of the captured combination
        self.conflicts = []
        self.result = None
        self.removals = []
        self._done = False
        self.held_mods, self.held_key = 0, 0
        self.keys_box = Gtk.Box(halign=Gtk.Align.CENTER, height_request=44,
                                focusable=True)
        self.msg = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        hint = Gtk.Label(label="Press a key combination, then press Set or Enter to save"
                         " · Esc cancels · Backspace disables", wrap=True,
                         justify=Gtk.Justification.CENTER)
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for w in (self.keys_box, self.msg, hint):
            box.append(w)
        self.set_extra_child(box)
        self.add_response("cancel", "Cancel")
        self.add_response("set", "Set")
        self.set_response_appearance("set", Adw.ResponseAppearance.SUGGESTED)
        self.set_response_enabled("set", False)
        self.set_default_response(None)
        self.set_close_response("cancel")
        ctl = Gtk.EventControllerKey()
        ctl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        ctl.connect("key-pressed", self.on_pressed)
        ctl.connect("key-released", self.on_released)
        self.add_controller(ctl)
        self.connect("response", self.on_response)
        self.connect("closed", self.on_closed)
        self.connect("map", self.on_map)
        self._surface = None
        self.render()

    # -- display ---------------------------------------------------------
    def set_message(self, text, css=None):
        for c in ("error", "warning"):
            self.msg.remove_css_class(c)
        if css:
            self.msg.add_css_class(css)
        self.msg.set_label(text)

    def render(self):
        child = self.keys_box.get_first_child()
        while child:
            self.keys_box.remove(child)
            child = self.keys_box.get_first_child()
        if self.held_mods or self.held_key:
            keys = accel_keys(self.held_key, self.held_mods)
        elif self.pending:
            keys = accel_keys(*self.pending)
        else:
            keys = []
        self.keys_box.append(keycap_box(keys, "Waiting for keys…",
                                        big=bool(self.pending or keys)))

    # -- key handling ----------------------------------------------------
    def base_keyval(self, ctl, keyval, keycode):
        """Unshifted keyval for the hardware key (Shift+1 gives 1, not !)."""
        try:
            event = ctl.get_current_event()
            group = event.get_layout() if hasattr(event, "get_layout") else 0
            ok, kv, *_ = Gdk.Display.get_default().translate_key(keycode, 0, group)
            if ok and kv:
                return Gdk.keyval_to_lower(kv)
        except Exception:
            pass
        return Gdk.keyval_to_lower(keyval)

    def on_pressed(self, ctl, keyval, keycode, state):
        try:
            return self._on_pressed(ctl, keyval, keycode, state)
        except Exception:
            if DEBUG:
                traceback.print_exc()
            return True

    def _on_pressed(self, ctl, keyval, keycode, state):
        mods = int(state) & mod_mask()
        dbg("capture pressed", Gdk.keyval_name(keyval), "keycode", keycode,
            "raw_state", hex(int(state)), "mods", hex(mods),
            "base", Gdk.keyval_name(self.base_keyval(ctl, keyval, keycode)))
        if keyval in MOD_KEYVALS:
            self.held_mods = mods | MOD_KEYVALS[keyval]
            self.render()
            return True
        if keyval in NEUTRAL_KEYVALS:
            return True
        self.held_mods = mods
        kv = self.base_keyval(ctl, keyval, keycode)
        self.held_key = kv
        if not mods and kv == Gdk.KEY_Escape:
            dbg("capture: Escape -> cancel")
            self.close()
        elif not mods and kv == Gdk.KEY_BackSpace:
            dbg("capture: BackSpace -> disable")
            self.result = ""
            self.deliver("backspace")
            self.close()
        elif not mods and kv in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and self.pending:
            dbg("capture: Return with pending", self.pending, "-> commit")
            self.commit()
            self.deliver("enter")
            self.close()
        else:
            err = accel_problem(kv, mods)
            dbg("capture accel_problem:", err)
            if err:
                self.set_message(err, "error")
            else:
                self.pending = (kv, mods)
                dbg("capture pending =", self.pending)
                self.show_pending()
        self.render()
        return True

    def on_released(self, ctl, keyval, keycode, state):
        mods = int(state) & mod_mask()
        dbg("capture released", Gdk.keyval_name(keyval), "keycode", keycode,
            "raw_state", hex(int(state)), "mods", hex(mods))
        if keyval in MOD_KEYVALS:
            mods &= ~MOD_KEYVALS[keyval]
        else:
            self.held_key = 0
        self.held_mods = mods
        self.render()

    def show_pending(self):
        kv, mods = self.pending
        accel = Gtk.accelerator_name(kv, Gdk.ModifierType(mods))
        self.conflicts = find_conflicts(accel, self.exclude)
        dbg("capture accel", accel, "conflicts", [n for n, _ in self.conflicts])
        self.set_response_enabled("set", True)
        self.set_default_response("set")
        if self.conflicts:
            names = ", ".join(sorted({n for n, _ in self.conflicts}))
            self.set_message(f"Already used by {names}", "warning")
            self.set_response_label("set", "Replace")
        else:
            self.set_message("")
            self.set_response_label("set", "Set")

    # -- result ----------------------------------------------------------
    def commit(self):
        if not self.pending or self.result:
            return
        self.removals = [remove for _name, remove in self.conflicts]
        kv, mods = self.pending
        self.result = Gtk.accelerator_name(kv, Gdk.ModifierType(mods))
        dbg("capture commit result =", repr(self.result))

    def deliver(self, path):
        """Invoke the callback exactly once with the decided outcome."""
        if self._done:
            dbg("capture deliver skipped (already done), path", path)
            return
        self._done = True
        result = self.result
        removals = self.removals if result else []
        dbg("capture deliver via", path, "result =", repr(result),
            "removals", len(removals))
        self.callback(result, removals)

    def on_response(self, _d, resp):
        dbg("capture response", resp)
        if self._done:
            return
        if resp == "set":
            self.commit()
            self.deliver("response-set")
        else:
            self.result, self.removals = None, []
            self.deliver("response-" + str(resp))

    def on_closed(self, _d):
        if self._surface is not None:
            try:
                self._surface.restore_system_shortcuts()
            except Exception:
                pass
            self._surface = None
        dbg("capture closed, done =", self._done)
        if not self._done:
            def fallback():
                if not self._done:
                    self.result, self.removals = None, []
                    self.deliver("closed-fallback")
                return False
            GLib.idle_add(fallback)

    def on_map(self, _w):
        """Ask the compositor to deliver Super/Alt+Tab style combos to us."""
        try:
            root = self.get_root()
            surface = root.get_surface() if root else None
            has = surface is not None and hasattr(surface, "inhibit_system_shortcuts")
            dbg("capture map: root", type(root).__name__, "surface",
                type(surface).__name__, "has inhibit_system_shortcuts", has)
            if has:
                surface.inhibit_system_shortcuts(None)
                self._surface = surface
                dbg("capture map: inhibit_system_shortcuts called")
        except Exception as e:
            dbg("capture map: inhibit failed:", repr(e))
            self._surface = None
        # make sure keys are delivered inside this dialog, not to the parent
        self.keys_box.grab_focus()
        GLib.idle_add(lambda: (self.keys_box.grab_focus(), False)[1])
        dbg("capture map: focus widget", type(self.get_focus()).__name__
            if hasattr(self, "get_focus") and self.get_focus() else None)


class ShortcutRow(Adw.ActionRow):
    """Row showing an accelerator; click the button to capture a new one.

    strv=True edits the first entry of a string-array key (keeping the rest);
    strv=False edits a plain string key (custom shortcuts, which have no
    default to reset to: Backspace in the capture dialog disables them).
    """

    def __init__(self, title, st, key, strv=True, subtitle=None):
        super().__init__(title=GLib.markup_escape_text(title),
                         subtitle=GLib.markup_escape_text(subtitle or ""))
        self.st, self.key, self.strv, self.name = st, key, strv, title
        self.base_subtitle = subtitle or ""
        self.button = Gtk.Button(valign=Gtk.Align.CENTER)
        self.button.connect("clicked", self.on_click)
        self.reset_btn = None
        if strv:
            self.reset_btn = Gtk.Button(icon_name="edit-undo-symbolic",
                                        valign=Gtk.Align.CENTER,
                                        tooltip_text="Reset to default")
            self.reset_btn.add_css_class("flat")
            self.reset_btn.connect("clicked", lambda *_: st.reset(key))
            self.add_suffix(self.reset_btn)
        self.add_suffix(self.button)
        self.set_activatable_widget(self.button)
        st.connect(f"changed::{key}", lambda *_: self.refresh())
        self.refresh()

    def accels(self):
        if self.strv:
            return list(self.st.get_strv(self.key))
        v = self.st.get_string(self.key)
        return [v] if v else []

    def refresh(self):
        accels = self.accels()
        ok, keyval, mods = (Gtk.accelerator_parse(accels[0])
                            if accels else (False, 0, 0))
        self.button.set_label(Gtk.accelerator_get_label(keyval, mods)
                              if ok else "Disabled")
        # only the first accelerator is editable: show the others
        extra = []
        for a in accels[1:]:
            eok, ekv, emods = Gtk.accelerator_parse(a)
            extra.append(Gtk.accelerator_get_label(ekv, emods) if eok else a)
        parts = ([self.base_subtitle] if self.base_subtitle else []) + (
            ["Also: " + ", ".join(extra)] if extra else [])
        self.set_subtitle(GLib.markup_escape_text(" · ".join(parts)))
        if self.reset_btn:
            self.reset_btn.set_visible(self.st.get_user_value(self.key) is not None)

    def store(self, accel):
        """Set the first accelerator (None clears it)."""
        dbg("row", self.name, "store", repr(accel))
        if self.strv:
            rest = self.accels()[1:]
            self.st.set_strv(self.key, ([accel] if accel else []) + rest)
        else:
            self.st.set_string(self.key, accel or "")

    def on_click(self, _btn):
        def done(result, removals):
            dbg("row", self.name, "done result =", repr(result), "removals", len(removals))
            if result is not None:
                for remove in removals:
                    remove()
                self.store(result)
        ShortcutCaptureDialog(self.name, done, (self.st, self.key)).present(self.get_root())


def shortcut_row(title, schema, key):
    st = get_settings(schema, key)
    return ShortcutRow(title, st, key) if st else None


class ActionItem(GObject.Object):
    """One entry of the shortcut action picker.

    kind: 'custom' (free command), 'builtin' (GNOME action = existing
    GSettings key) or 'app' (installed application)."""
    label = GObject.Property(type=str, default="")
    kind = GObject.Property(type=str, default="")
    detail = GObject.Property(type=str, default="")

    def __init__(self, kind, label, detail="", icon=None, schema=None, key=None,
                 command=None):
        super().__init__(kind=kind, label=label, detail=detail)
        self.icon, self.schema, self.key, self.command = icon, schema, key, command


# (label, schema, key): built-in GNOME actions, listed only when the key exists
_KB = "org.gnome.desktop.wm.keybindings"
BUILTIN_ACTIONS = [
    ("Take a screenshot (select area)", SHELL_KB, "show-screenshot-ui"),
    ("Screenshot whole screen", SHELL_KB, "screenshot"),
    ("Screenshot window", SHELL_KB, "screenshot-window"),
    ("Record screen", SHELL_KB, "show-screen-recording-ui"),
    ("Lock screen", MEDIA_KEYS, "screensaver"),
    ("Log out", MEDIA_KEYS, "logout"),
    ("Open file manager (Home)", MEDIA_KEYS, "home"),
    ("Web browser", MEDIA_KEYS, "www"),
    ("Email", MEDIA_KEYS, "email"),
    ("Calculator", MEDIA_KEYS, "calculator"),
    ("Search", MEDIA_KEYS, "search"),
    ("Show the overview", SHELL_KB, "toggle-overview"),
    ("Show all apps", SHELL_KB, "toggle-application-view"),
    ("Show notifications", SHELL_KB, "toggle-message-tray"),
    ("Close window", _KB, "close"),
    ("Maximize", _KB, "toggle-maximized"),
    ("Minimize", _KB, "minimize"),
    ("Toggle fullscreen", _KB, "toggle-fullscreen"),
    ("Switch to workspace left", _KB, "switch-to-workspace-left"),
    ("Switch to workspace right", _KB, "switch-to-workspace-right"),
    ("Move window to workspace left", _KB, "move-to-workspace-left"),
    ("Move window to workspace right", _KB, "move-to-workspace-right"),
]


def app_launch_command(app):
    """Command that starts a Gio.AppInfo: gtk-launch when available, else its
    commandline without desktop-entry field codes."""
    app_id = app.get_id()
    if app_id and GLib.find_program_in_path("gtk-launch"):
        return "gtk-launch " + (app_id[:-8] if app_id.endswith(".desktop") else app_id)
    cmd = app.get_commandline() or ""
    return re.sub(r"\s+", " ", re.sub(r"%[UuFfick]", "", cmd)).strip()


def build_action_items(include_builtin=True):
    """Items of the action picker: built-in actions, then applications."""
    items = []
    if include_builtin:
        built = []
        for label, schema, key in BUILTIN_ACTIONS:
            if get_settings(schema, key):
                built.append(ActionItem(
                    "builtin", label, "GNOME action",
                    "preferences-desktop-keyboard-shortcuts-symbolic", schema, key))
        items += sorted(built, key=lambda i: i.label.casefold())
    apps = [a for a in Gio.AppInfo.get_all() if a.should_show()]
    for a in sorted(apps, key=lambda a: (a.get_name() or "").casefold()):
        items.append(ActionItem("app", a.get_name() or a.get_id(), "Application",
                                a.get_icon(), command=app_launch_command(a)))
    return items


def add_builtin_accel(schema, key, accel, writer=None):
    """Add accel to a built-in action's key ('as': appended, no duplicates;
    's': replaced). Returns (key, new value); writer(st, key, value) stores it
    (default: write to GSettings)."""
    st = get_settings(schema, key)
    if st.get_value(key).get_type_string() == "as":
        cur = list(st.get_strv(key))
        new = cur if parse_accel(accel) in [parse_accel(a) for a in cur] else cur + [accel]
    else:
        new = accel
    if writer is None:
        def writer(st, key, value):
            (st.set_strv if isinstance(value, list) else st.set_string)(key, value)
    writer(st, key, new)
    return key, new


def action_factory(show_kind):
    """List factory showing icon + label (+ dim kind) for ActionItem rows."""
    f = Gtk.SignalListItemFactory()

    def setup(_f, li):
        box = Gtk.Box(spacing=8)
        box.append(Gtk.Image())
        box.append(Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END))
        if show_kind:
            kind = Gtk.Label()
            kind.add_css_class("dim-label")
            kind.add_css_class("caption")
            box.append(kind)
        li.set_child(box)

    def bind(_f, li):
        item, box = li.get_item(), li.get_child()
        img, lbl = box.get_first_child(), box.get_first_child().get_next_sibling()
        if isinstance(item.icon, str):
            img.set_from_icon_name(item.icon)
        elif item.icon is not None:
            img.set_from_gicon(item.icon)
        else:
            img.set_from_icon_name("application-x-executable-symbolic")
        lbl.set_label(item.label)
        if show_kind:
            lbl.get_next_sibling().set_label(item.detail)
    f.connect("setup", setup)
    f.connect("bind", bind)
    return f


def filter_rows(groups, text):
    """Filter the preference rows inside groups by title/subtitle (case-
    insensitive substring); groups without a visible row are hidden and an
    empty text restores everything. Returns the number of visible rows."""
    def rows(w):
        c = w.get_first_child()
        while c:
            if isinstance(c, Adw.PreferencesRow):
                yield c
            else:
                yield from rows(c)
            c = c.get_next_sibling()
    q = (text or "").strip().casefold()
    total = 0
    for g in groups:
        shown = 0
        for r in rows(g):
            hay = " ".join(filter(None, (getattr(r, "get_title", lambda: "")(),
                                         getattr(r, "get_subtitle", lambda: "")())))
            hay = re.sub(r"&amp;|&lt;|&gt;", lambda m: {"&amp;": "&", "&lt;": "<",
                                                        "&gt;": ">"}[m.group()], hay)
            ok = not q or q in hay.casefold()
            r.set_visible(ok)
            shown += ok
        g.set_visible(shown > 0 or not q)
        total += shown
    return total


class CustomShortcutsGroup(Adw.PreferencesGroup):
    """Editor for GNOME custom keyboard shortcuts."""

    def __init__(self, window):
        super().__init__(title="Custom shortcuts",
                         description="Run your own commands with a key combination.")
        self.window = window
        self.st = get_settings(MEDIA_KEYS, "custom-keybindings")
        self.rows = []
        self.on_refresh = None  # called after the rows are rebuilt
        add_btn = Gtk.Button(label="Add…")
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", lambda *_: self.edit_dialog(None))
        self.set_header_suffix(add_btn)
        self.refresh()

    @staticmethod
    def entry(path):
        src = Gio.SettingsSchemaSource.get_default()
        if src.lookup(CUSTOM_SCHEMA, True) is None:
            return None
        return Gio.Settings.new_with_path(CUSTOM_SCHEMA, path)

    def refresh(self):
        for r in self.rows:
            self.remove(r)
        self.rows = []
        for path in self.st.get_strv("custom-keybindings"):
            est = self.entry(path)
            if est is None:
                continue
            row = ShortcutRow(est.get_string("name") or path, est, "binding",
                              strv=False, subtitle=est.get_string("command"))
            for icon, tip, cb in (("document-edit-symbolic", "Edit", self.on_edit),
                                  ("user-trash-symbolic", "Delete", self.on_delete)):
                btn = Gtk.Button(icon_name=icon, valign=Gtk.Align.CENTER,
                                 tooltip_text=tip)
                btn.add_css_class("flat")
                btn.connect("clicked", cb, path)
                row.add_suffix(btn)
            self.add(row)
            self.rows.append(row)
        if self.on_refresh:
            self.on_refresh()

    def on_delete(self, _btn, path):
        est = self.entry(path)
        for k in ("name", "command", "binding"):
            est.reset(k)
        self.st.set_strv("custom-keybindings",
                         [p for p in self.st.get_strv("custom-keybindings") if p != path])
        self.refresh()

    def on_edit(self, _btn, path):
        self.edit_dialog(path)

    builtin_writer = None  # stub hook for tests; None writes to GSettings

    def edit_dialog(self, path):
        """Add (path None) or edit a custom shortcut: action, name, command, binding."""
        est = self.entry(path) if path else None
        state = {"binding": est.get_string("binding") if est else "",
                 "removals": []}  # removals: conflicting bindings, run on Save only
        dlg = Adw.AlertDialog(
            heading="Edit custom shortcut" if est else "Add custom shortcut")
        items = build_action_items(include_builtin=est is None)
        model = Gio.ListStore.new(ActionItem)
        for i in items:
            model.append(i)
        combo = Adw.ComboRow(title="Action", model=model, enable_search=True)
        combo.set_expression(Gtk.PropertyExpression.new(ActionItem, None, "label"))
        try:  # default is prefix matching; typing 'screen' must find 'Take a screenshot'
            combo.set_search_match_mode(Gtk.StringFilterMatchMode.SUBSTRING)
        except (AttributeError, TypeError):
            pass  # older libadwaita without the property
        combo.set_factory(action_factory(False))
        combo.set_list_factory(action_factory(True))
        combo_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        combo_list.add_css_class("boxed-list")
        combo_list.append(combo)
        note = Gtk.Label(label="Adds this shortcut to GNOME’s built-in action",
                         xalign=0, wrap=True, visible=False)
        note.add_css_class("dim-label")
        note.add_css_class("caption")
        name = Gtk.Entry(placeholder_text="Name",
                         text=est.get_string("name") if est else "")
        cmd = Gtk.Entry(placeholder_text="Command, e.g. nautilus",
                        text=est.get_string("command") if est else "")
        sc_btn = Gtk.Button(tooltip_text="Choose the shortcut")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        add_cmd = Gtk.Button(halign=Gtk.Align.START,
                             child=Adw.ButtonContent(icon_name="utilities-terminal-symbolic",
                                                     label="Add command instead"))
        add_cmd.add_css_class("flat")
        choose = Gtk.Button(halign=Gtk.Align.START,
                            child=Adw.ButtonContent(icon_name="view-list-symbolic",
                                                    label="Choose from list"))
        choose.add_css_class("flat")
        box.set_size_request(380, -1)
        for w in (combo_list, note, name, cmd, add_cmd, choose, sc_btn):
            box.append(w)
        dlg.set_extra_child(box)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("save", "Save" if est else "Add")
        dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response(None)  # Enter must never save by accident
        dlg.set_close_response("cancel")
        dlg.parts = dict(combo=combo, name=name, cmd=cmd, state=state, items=items,
                         note=note, combo_list=combo_list, add_cmd=add_cmd,
                         choose=choose, dlg=dlg)
        touched = {"name": bool(est)}   # user edited the name: stop auto-filling
        filling = {"on": False}

        def current():
            return combo.get_selected_item()

        def show_binding():
            parsed = parse_accel(state["binding"])
            sc_btn.set_child(keycap_box(accel_keys(*parsed) if parsed else [],
                                        "Set shortcut…"))

        def validate(*_):
            it = current()
            if state["cmd_mode"]:
                ok = bool(name.get_text().strip() and cmd.get_text().strip())
            elif it is None:
                ok = False
            elif it.kind == "builtin":
                ok = bool(state["binding"])
            else:
                ok = bool(name.get_text().strip())
            dlg.set_response_enabled("save", ok)

        def set_name(text):
            filling["on"] = True
            name.set_text(text)
            filling["on"] = False

        def update_view():
            it, cm = current(), state["cmd_mode"]
            builtin = not cm and it is not None and it.kind == "builtin"
            combo_list.set_visible(not cm)
            add_cmd.set_visible(not cm)
            note.set_visible(builtin)
            name.set_visible(not builtin)
            cmd.set_visible(cm)
            choose.set_visible(cm)
            validate()

        def on_select(*_):
            it = current()
            if it is not None and it.kind == "app" and not touched["name"]:
                set_name(it.label)
            update_view()

        def to_cmd_mode(*_):
            state["cmd_mode"] = True
            if not touched["name"]:
                set_name("")   # drop an auto-filled app name
            update_view()
            cmd.grab_focus()

        def to_picker_mode(*_):
            state["cmd_mode"] = False
            on_select()

        def on_name(*_):
            if not filling["on"]:
                touched["name"] = True
            validate()

        def captured(result, new_removals):
            dbg("edit captured() result =", repr(result), "removals", len(new_removals))
            if result is not None:
                state["binding"], state["removals"] = result, new_removals
                show_binding()
                validate()

        def pick(_b):
            dbg("edit: opening capture dialog")
            it = current()
            exclude = (est, "binding") if est else None
            if not state["cmd_mode"] and it is not None and it.kind == "builtin":
                exclude = (get_settings(it.schema, it.key), it.key)
            title = (name.get_text().strip() if state["cmd_mode"] or it is None
                     else it.label) or "the new shortcut"
            ShortcutCaptureDialog(title, captured, exclude).present(self.window)

        def done(_d, resp):
            dbg("edit dialog response", resp, "binding =", repr(state["binding"]))
            if resp != "save":
                return
            it = current()
            n, binding = name.get_text().strip(), state["binding"]
            for remove in state["removals"]:
                remove()
            if not state["cmd_mode"] and it.kind == "builtin":
                dlg.result = add_builtin_accel(it.schema, it.key, binding,
                                               self.builtin_writer)
                return
            c = cmd.get_text().strip() if state["cmd_mode"] else it.command
            dlg.result = (n, c, binding)
            if est:
                self.save(est, n, c, binding)
                self.refresh()
            else:
                self.create(n, c, binding)
        sc_btn.connect("clicked", pick)
        name.connect("changed", on_name)
        cmd.connect("changed", validate)
        combo.connect("notify::selected", on_select)
        add_cmd.connect("clicked", to_cmd_mode)
        choose.connect("clicked", to_picker_mode)
        dlg.connect("response", done)
        state["cmd_mode"] = False
        if est:  # app launch command -> picker with that app, else command mode
            c = est.get_string("command")
            match = [i for i, it in enumerate(items)
                     if it.kind == "app" and it.command == c]
            if match:
                combo.set_selected(match[0])
            else:
                state["cmd_mode"] = True
        show_binding()
        update_view()
        dlg.present(self.window)
        return dlg

    @staticmethod
    def save(est, name, command, binding):
        dbg("save name", repr(name), "command", repr(command), "binding", repr(binding))
        est.set_string("name", name)
        est.set_string("command", command)
        est.set_string("binding", binding)

    def create(self, name, command, binding):
        paths = list(self.st.get_strv("custom-keybindings"))
        n = 0
        while f"{CUSTOM_BASE}custom{n}/" in paths:
            n += 1
        path = f"{CUSTOM_BASE}custom{n}/"
        self.save(self.entry(path), name, command, binding)
        self.st.set_strv("custom-keybindings", paths + [path])
        self.refresh()


class ShortcutsPage(Page):
    def __init__(self, window):
        super().__init__(title="Shortcuts",
                         icon_name="preferences-desktop-keyboard-shortcuts-symbolic")
        KB = "org.gnome.desktop.wm.keybindings"
        MK = MEDIA_KEYS
        search_group = Adw.PreferencesGroup()
        self.search = Gtk.SearchEntry(placeholder_text="Search shortcuts")
        search_group.add(self.search)
        self.add(search_group)
        first = len(self.groups)
        self.refilter = lambda: filter_rows(self.groups[first:], self.search.get_text())
        self.search.connect("search-changed", lambda e: self.refilter())
        if get_settings(MK, "custom-keybindings"):
            custom = CustomShortcutsGroup(window)
            self.add(custom)
            custom.on_refresh = self.refilter
        self.add(group("Screenshots", [
            shortcut_row(title, SHELL_KB, key)
            for (sch, key), title in SHORTCUT_TITLES.items() if sch == SHELL_KB
        ]))
        self.add(group("Windows and workspaces", [
            shortcut_row("Close window", KB, "close"),
            shortcut_row("Toggle maximize", KB, "toggle-maximized"),
            shortcut_row("Minimize window", KB, "minimize"),
            shortcut_row("Switch to workspace on the left", KB, "switch-to-workspace-left"),
            shortcut_row("Switch to workspace on the right", KB, "switch-to-workspace-right"),
            shortcut_row("Move window to workspace on the left", KB, "move-to-workspace-left"),
            shortcut_row("Move window to workspace on the right", KB, "move-to-workspace-right"),
        ]))
        self.add(group("System and launchers", [
            shortcut_row("Lock screen", MK, "screensaver"),
            shortcut_row("Log out", MK, "logout"),
            shortcut_row("Web browser", MK, "www"),
            shortcut_row("Email client", MK, "email"),
            shortcut_row("Home folder", MK, "home"),
            shortcut_row("Calculator", MK, "calculator"),
            shortcut_row("Search", MK, "search"),
            spin_row("Volume step (%)", MK, "volume-step", 1, 100, 1),
        ]))


# --------------------------------------------------------------------------
# Privacy and Files page
# --------------------------------------------------------------------------
ZOOM3 = [("small", "Small"), ("medium", "Medium"), ("large", "Large")]
ALWAYS_LOCAL = [("always", "Always"), ("local-only", "Local files only"),
                ("never", "Never")]


class PrivacyPage(Page):
    def __init__(self, window):
        super().__init__(title="Privacy and Files", icon_name="security-high-symbolic")
        P = "org.gnome.desktop.privacy"
        MH = "org.gnome.desktop.media-handling"
        N = "org.gnome.nautilus.preferences"
        FC = "org.gtk.gtk4.Settings.FileChooser"
        TR = "org.freedesktop.Tracker3.Miner.Files"
        FTS = "org.freedesktop.Tracker3.FTS"
        self.add(group("History and cleanup", [
            switch_row("Remember recent files", P, "remember-recent-files"),
            spin_row("Forget recent files after (days, -1 = never)", P,
                     "recent-files-max-age", -1, 3650, 1),
            switch_row("Remember app usage", P, "remember-app-usage"),
            switch_row("Automatically delete old trash files", P, "remove-old-trash-files"),
            switch_row("Automatically delete old temporary files", P,
                       "remove-old-temp-files"),
            spin_row("Delete files older than (days)", P, "old-files-age", 0, 365, 1),
        ]))
        self.add(group("Devices and identity", [
            switch_row("Report technical problems", P, "report-technical-problems"),
            switch_row("Send software usage statistics", P, "send-software-usage-stats"),
            switch_row("USB protection", P, "usb-protection",
                       "Block new USB devices while the screen is locked"),
            combo_row("USB protection level", P, "usb-protection-level", [
                ("lockscreen", "When the screen is locked"), ("always", "Always")]),
            switch_row("Disable camera", P, "disable-camera"),
            switch_row("Disable microphone", P, "disable-microphone"),
            switch_row("Privacy screen", P, "privacy-screen",
                       "Only on laptops with a built-in privacy screen"),
            switch_row("Disable sound output", P, "disable-sound-output"),
            switch_row("Hide identity", P, "hide-identity"),
            switch_row("Show full name in top bar", P, "show-full-name-in-top-bar"),
        ]))
        self.add(group("Removable media", [
            switch_row("Automount removable media", MH, "automount"),
            switch_row("Open folder for automounted media", MH, "automount-open"),
            switch_row("Never prompt or autorun programs", MH, "autorun-never"),
        ]))
        self.add(group("Files", [
            combo_row("Open items with", N, "click-policy",
                      [("double", "Double click"), ("single", "Single click")]),
            combo_row("Default view", N, "default-folder-viewer",
                      [("list-view", "List"), ("icon-view", "Grid")]),
            switch_row("Show hidden files", N, "show-hidden-files"),
            switch_row("Always use location entry", N, "always-use-location-entry"),
            switch_row("Show Delete Permanently", N, "show-delete-permanently"),
            switch_row("Show Create Link", N, "show-create-link"),
            combo_row("Sort by", N, "default-sort-order", [
                ("name", "Name"), ("size", "Size"), ("type", "Type"),
                ("mtime", "Modified"), ("atime", "Accessed"), ("btime", "Created"),
                ("starred", "Starred")]),
            switch_row("Reverse sort order", N, "default-sort-in-reverse-order"),
            combo_row("Search in subfolders", N, "recursive-search", ALWAYS_LOCAL),
            combo_row("Show directory item counts", N, "show-directory-item-counts",
                      ALWAYS_LOCAL),
            combo_row("Show thumbnails", N, "show-image-thumbnails", ALWAYS_LOCAL),
            combo_row("Date format", N, "date-time-format",
                      [("simple", "Simple"), ("detailed", "Detailed")]),
            switch_row("Open folder when dragging over it", N, "open-folder-on-dnd-hover"),
            switch_row("Use mouse back and forward buttons", N, "mouse-use-extra-buttons"),
            combo_row("List view zoom", "org.gnome.nautilus.list-view",
                      "default-zoom-level", ZOOM3),
            switch_row("List view: expandable folders", "org.gnome.nautilus.list-view",
                       "use-tree-view"),
            combo_row("Grid view zoom", "org.gnome.nautilus.icon-view",
                      "default-zoom-level", [
                          ("small", "Small"), ("small-plus", "Small plus"),
                          ("medium", "Medium"), ("large", "Large"),
                          ("extra-large", "Extra large")]),
        ]))
        self.add(group("File chooser dialogs", [
            switch_row("Show hidden files", FC, "show-hidden"),
            switch_row("Sort folders before files", FC, "sort-directories-first"),
            combo_row("Start in", FC, "startup-mode",
                      [("recent", "Recent files"), ("cwd", "Current folder")]),
            combo_row("Location", FC, "location-mode",
                      [("path-bar", "Path bar"), ("filename-entry", "Filename entry")]),
            combo_row("Clock format", FC, "clock-format",
                      [("24h", "24-hour"), ("12h", "12-hour")]),
        ]))
        self.add(group("Search and indexing", [
            switch_row("Disable external search providers",
                       "org.gnome.desktop.search-providers", "disable-external"),
            switch_row("Index while on battery", TR, "index-on-battery"),
            switch_row("Index removable devices", TR, "index-removable-devices"),
            switch_row("Watch folders for changes", TR, "enable-monitors"),
            switch_row("Match word variants (stemming)", FTS, "enable-stemmer"),
            switch_row("Ignore numbers in file contents", FTS, "ignore-numbers"),
        ]))
        self.add(group("Low disk space", [
            spin_row("Notify when free space is below (%)",
                     "org.gnome.settings-daemon.plugins.housekeeping",
                     "free-percent-notify", 0, 1, 0.01, digits=2),
        ]))


# --------------------------------------------------------------------------
# Accessibility page
# --------------------------------------------------------------------------
class AccessibilityPage(Page):
    def __init__(self, window):
        super().__init__(title="Accessibility",
                         icon_name="preferences-desktop-accessibility-symbolic")
        A = "org.gnome.desktop.a11y"
        AI = A + ".interface"
        AK = A + ".keyboard"
        AM = A + ".mouse"
        self.add(group("Seeing", [
            switch_row("Always show universal access menu", A,
                       "always-show-universal-access-status"),
            switch_row("Always show text caret", A, "always-show-text-caret"),
            switch_row("High contrast", AI, "high-contrast"),
            switch_row("Show on/off status shapes in switches", AI, "show-status-shapes"),
            combo_row("Animations", AI, "reduced-motion", [
                ("no-preference", "Normal"), ("reduce", "Reduced")]),
        ]))
        self.add(group("Typing", [
            switch_row("Sticky keys", AK, "stickykeys-enable",
                       "Press modifier keys one at a time"),
            switch_row("Turn sticky keys off when two keys are pressed together", AK,
                       "stickykeys-two-key-off"),
            switch_row("Slow keys", AK, "slowkeys-enable"),
            spin_row("Slow keys delay (ms)", AK, "slowkeys-delay", 0, 3000, 50),
            switch_row("Bounce keys", AK, "bouncekeys-enable"),
            spin_row("Bounce keys delay (ms)", AK, "bouncekeys-delay", 0, 3000, 50),
            switch_row("Toggle keys", AK, "togglekeys-enable",
                       "Beep when Caps Lock or Num Lock changes"),
            switch_row("Mouse keys", AK, "mousekeys-enable",
                       "Control the pointer with the numeric keypad"),
        ]))
        self.add(group("Pointing and clicking", [
            switch_row("Hover click", AM, "dwell-click-enabled"),
            spin_row("Hover click delay (s)", AM, "dwell-time", 0.2, 3.0, 0.1, digits=1),
            spin_row("Hover click motion threshold (px)", AM, "dwell-threshold",
                     1, 30, 1),
            switch_row("Simulated secondary click", AM, "secondary-click-enabled"),
            spin_row("Secondary click delay (s)", AM, "secondary-click-time",
                     0.5, 3.0, 0.1, digits=1),
        ]))


# --------------------------------------------------------------------------
# Default apps page
# --------------------------------------------------------------------------
DEFAULT_APP_TYPES = [
    # title, mime type, extra types set together with it
    ("Web browser", "x-scheme-handler/http",
     ("x-scheme-handler/https", "text/html")),
    ("Email", "x-scheme-handler/mailto", ()),
    ("Text", "text/plain", ()),
    ("Video", "video/mp4", ()),
    ("Music", "audio/mpeg", ()),
    ("Images", "image/png", ()),
    ("PDF", "application/pdf", ()),
    ("File manager", "inode/directory", ()),
]


def default_app_row(window, title, mime, extra=()):
    apps, seen = [], set()
    for a in (Gio.AppInfo.get_all_for_type(mime)
              + Gio.AppInfo.get_recommended_for_type(mime)):
        if a.get_id() not in seen:
            seen.add(a.get_id())
            apps.append(a)
    if not apps:
        return None
    cur = Gio.AppInfo.get_default_for_type(mime, False)
    row = Adw.ComboRow(title=title)
    row.set_model(Gtk.StringList.new([a.get_display_name() for a in apps]))
    ids = [a.get_id() for a in apps]
    if cur is not None and cur.get_id() in ids:
        row.set_selected(ids.index(cur.get_id()))
    else:
        row.set_selected(Gtk.INVALID_LIST_POSITION)

    def changed(r, _p):
        i = r.get_selected()
        if i == Gtk.INVALID_LIST_POSITION:
            return
        try:
            for t in (mime,) + tuple(extra):
                apps[i].set_as_default_for_type(t)
        except GLib.Error as e:
            window.toast(e.message)
    row.connect("notify::selected", changed)
    return row


class DefaultAppsPage(Page):
    def __init__(self, window):
        super().__init__(title="Default Apps", icon_name="application-x-executable-symbolic")
        self.add(group("Default applications", [
            default_app_row(window, t, m, x) for t, m, x in DEFAULT_APP_TYPES]))


# --------------------------------------------------------------------------
# Display page
# --------------------------------------------------------------------------
def strv_toggle_row(title, schema, key, feature, subtitle=None):
    """SwitchRow adding/removing one entry of a string-array key (feature list)."""
    st = get_settings(schema, key)
    if st is None:
        return None
    row = Adw.SwitchRow(title=title, subtitle=subtitle or "",
                        active=feature in st.get_strv(key))
    busy = [False]

    def toggled(r, _p):
        if busy[0]:
            return
        feats = [f for f in st.get_strv(key) if f != feature]
        if r.get_active():
            feats.append(feature)
        st.set_strv(key, feats)

    def synced(*_a):
        busy[0] = True
        try:
            row.set_active(feature in st.get_strv(key))
        finally:
            busy[0] = False
    row.connect("notify::active", toggled)
    st.connect("changed::" + key, synced)
    return row


def mutter_feature_row(title, feature, subtitle=None):
    """Experimental mutter feature: boolean key in org.gnome.mutter.experimental
    (newer GNOME) or an entry of org.gnome.mutter experimental-features (older)."""
    return (switch_row(title, "org.gnome.mutter.experimental", feature, subtitle)
            or strv_toggle_row(title, "org.gnome.mutter", "experimental-features",
                               feature, subtitle))


class DisplayPage(Page):
    def __init__(self, window):
        super().__init__(title="Display", icon_name="video-display-symbolic")
        X = "org.gnome.mutter.wayland"
        NL = "org.gnome.settings-daemon.plugins.color"
        exp_rows = [
            mutter_feature_row("Fractional scaling", "scale-monitor-framebuffer",
                               "Allow non-integer display scales such as 125%"),
            mutter_feature_row("Variable refresh rate", "variable-refresh-rate",
                               "Let compatible monitors adapt their refresh rate"),
            mutter_feature_row("Native Xwayland scaling", "xwayland-native-scaling",
                               "Scale X11 apps without blurring on HiDPI"),
            mutter_feature_row("Close Xwayland when unused", "autoclose-xwayland",
                               "Stop the X11 server after the last X11 app exits"),
            mutter_feature_row("Allow screen tearing", "tearing",
                               "Let apps present frames without waiting for vsync"),
            mutter_feature_row("Tearing for fullscreen windows",
                               "auto-fullscreen-tearing",
                               "Allow tearing for every fullscreen window"),
            mutter_feature_row("Always use KMS modifiers", "kms-modifiers",
                               "Allocate scanout buffers with explicit modifiers"),
        ]
        if any(r is not None for r in exp_rows):
            self.add(note_group(
                "Display settings",
                "Changes on this page may require logging out to take effect."))
        self.add(group("Experimental features", exp_rows,
                       "Experimental mutter options. They can be removed or "
                       "behave differently between GNOME versions."))
        self.add(group("X11 applications", [
            switch_row("Allow X11 apps to grab keyboard", X, "xwayland-allow-grabs",
                       "Lets X11 apps capture all keyboard input"),
        ]))
        self.add(group("Night light", [
            switch_row("Night light", NL, "night-light-enabled"),
            spin_row("Temperature (K)", NL, "night-light-temperature", 1700, 4700, 100),
            switch_row("Automatic schedule (sunset to sunrise)", NL,
                       "night-light-schedule-automatic"),
            spin_row("Manual start hour", NL, "night-light-schedule-from", 0, 24, 0.5,
                     digits=1),
            spin_row("Manual end hour", NL, "night-light-schedule-to", 0, 24, 0.5,
                     digits=1),
        ]))


# --------------------------------------------------------------------------
# Date and Region page
# --------------------------------------------------------------------------
def read_ntp():
    """True/False from timedatectl, or None if unavailable."""
    if not have("timedatectl"):
        return None
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTP", "--value"],
                             capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return {"yes": True, "no": False}.get(out)


def ntp_row(window):
    """'Set time automatically' switch driving `timedatectl set-ntp` (polkit)."""
    cur = read_ntp()
    if cur is None:
        return None
    row = Adw.SwitchRow(title="Set time automatically (NTP)",
                        subtitle="Keep the clock synchronised with network time servers",
                        active=cur)
    reverting = [False]

    def revert(state):
        reverting[0] = True
        try:
            row.set_active(state)
        finally:
            reverting[0] = False

    def toggled(r, _p):
        if reverting[0]:
            return
        want = r.get_active()
        try:
            proc = Gio.Subprocess.new(
                ["timedatectl", "set-ntp", "true" if want else "false"],
                Gio.SubprocessFlags.STDERR_PIPE)
        except GLib.Error as e:
            revert(not want)
            window.toast(f"Could not run timedatectl: {e.message}")
            return
        r.set_sensitive(False)

        def done(p, res):
            r.set_sensitive(True)
            try:
                _ok, _out, err = p.communicate_utf8_finish(res)
                kind, msg = classify_exit(p.get_exit_status(), err)
            except GLib.Error as e:
                kind, msg = "error", e.message
            if kind == "ok":
                window.toast("Applied")
                return
            revert(not want)
            window.toast("Authentication cancelled \u2014 change reverted"
                         if kind == "cancelled"
                         else f"Failed: {msg} \u2014 change reverted")
        proc.communicate_utf8_async(None, None, done)
    row.connect("notify::active", toggled)
    return admin(row)


def list_regions():
    """[(locale, label)] of installed UTF-8 locales as xx_YY.UTF-8, or None."""
    if not have("locale"):
        return None
    try:
        out = subprocess.run(["locale", "-a"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = set()
    for name in out.split():
        base, _, enc = name.rpartition(".")
        if base and "_" in base and enc.lower().replace("-", "") == "utf8":
            found.add(base + ".UTF-8")
    result = []
    for loc in sorted(found):
        label = loc
        if GnomeDesktop is not None:
            lang = GnomeDesktop.get_language_from_locale(loc, None)
            country = GnomeDesktop.get_country_from_locale(loc, None)
            if lang:
                label = f"{lang} ({country})" if country else lang
                label = f"{label} - {loc}"
        result.append((loc, label))
    return result or None


def region_row():
    regions = list_regions()
    if regions is None:
        return None
    return combo_row("Formats", "org.gnome.system.locale", "region",
                     [("", "Same as language")] + regions,
                     "Dates, numbers, currency and measurements; "
                     "applies after logging in again")


class DateRegionPage(Page):
    def __init__(self, window):
        super().__init__(title="Date and Region",
                         icon_name="preferences-system-time-symbolic")
        CAL = "org.gnome.desktop.calendar"
        self.add(group("Date and time", [
            switch_row("Set time zone automatically", "org.gnome.desktop.datetime",
                       "automatic-timezone", "Uses your location"),
            ntp_row(window),
        ]))
        self.add(group("Regional formats", [region_row()]))
        self.add(group("Calendar", [
            switch_row("Show week numbers", CAL, "show-weekdate"),
            combo_row("Week starts on", CAL, "week-start-day", [
                ("default", "Locale default"), ("monday", "Monday"),
                ("tuesday", "Tuesday"), ("wednesday", "Wednesday"),
                ("thursday", "Thursday"), ("friday", "Friday"),
                ("saturday", "Saturday"), ("sunday", "Sunday")]),
        ]))


# --------------------------------------------------------------------------
# Login Screen page (GDM greeter settings via /etc/dconf, needs root)
# --------------------------------------------------------------------------
LS = "org.gnome.login-screen"
TP = "org.gnome.desktop.peripherals.touchpad"
IF = "org.gnome.desktop.interface"
# (dconf section, key, kind, user schema used for the default value)
GDM_KEYS = [
    ("org/gnome/login-screen", "disable-user-list", bool, LS),
    ("org/gnome/login-screen", "disable-restart-buttons", bool, LS),
    ("org/gnome/login-screen", "banner-message-enable", bool, LS),
    ("org/gnome/login-screen", "banner-message-text", str, LS),
    ("org/gnome/login-screen", "logo", str, LS),
    ("org/gnome/login-screen", "enable-fingerprint-authentication", bool, LS),
    ("org/gnome/login-screen", "enable-smartcard-authentication", bool, LS),
    ("org/gnome/desktop/peripherals/touchpad", "tap-to-click", bool, TP),
    ("org/gnome/desktop/interface", "clock-format", str, IF),
    ("org/gnome/desktop/interface", "show-battery-percentage", bool, IF),
]
GDM_APPLY_SCRIPT = (
    "set -e; umask 022; IFS= read -r p; IFS= read -r c; "
    f"mkdir -p /etc/dconf/profile {GDM_DB_DIR}; "
    f'if [ ! -e {GDM_PROFILE} ]; then printf %s "$p" | base64 -d > {GDM_PROFILE}; fi; '
    f'printf %s "$c" | base64 -d > {GDM_CONF}; '
    "dconf update"
)
GDM_RESET_SCRIPT = f"set -e; rm -f {GDM_CONF}; dconf update"


def gvariant_text(value):
    """GVariant text form of a bool or str (strings single-quoted, escaped)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    out = []
    for ch in value:
        if ch in "\\'":
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7f:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    return "'" + "".join(out) + "'"


def gdm_keyfile(state):
    """dconf keyfile text for {key: value}; empty 'logo' and unknown keys are omitted."""
    lines = ["# Written by Settings Plus. Remove with its 'Reset login screen "
             "settings' button."]
    sections = {}
    for section, key, _kind, _schema in GDM_KEYS:
        if key in state and not (key == "logo" and not state[key]):
            sections.setdefault(section, []).append(
                f"{key}={gvariant_text(state[key])}")
    for section, entries in sections.items():
        lines += ["", f"[{section}]"] + entries
    return "\n".join(lines) + "\n"


def gdm_profile(include_defaults=None):
    if include_defaults is None:
        include_defaults = os.path.exists(GDM_GREETER_DEFAULTS)
    text = "user-db:user\nsystem-db:gdm\n"
    if include_defaults:
        text += f"file-db:{GDM_GREETER_DEFAULTS}\n"
    return text


def parse_gdm_conf(text):
    """{key: value} read back from a file written by gdm_keyfile()."""
    kf = GLib.KeyFile.new()
    try:
        kf.load_from_data(text, len(text.encode()), GLib.KeyFileFlags.NONE)
    except GLib.Error:
        return {}
    found = {}
    for section, key, kind, _schema in GDM_KEYS:
        try:
            val = GLib.Variant.parse(None, kf.get_value(section, key)).unpack()
        except GLib.Error:
            continue
        if isinstance(val, kind):
            found[key] = val
    return found


def initial_gdm_state():
    """Values from our drop-in if present, else the user's own settings."""
    try:
        with open(GDM_CONF, encoding="utf-8") as fh:
            saved = parse_gdm_conf(fh.read())
    except OSError:
        saved = {}
    state = {}
    for _section, key, kind, schema in GDM_KEYS:
        st = get_settings(schema, key)
        if st is None:
            continue
        if key in saved:
            state[key] = saved[key]
        else:
            state[key] = st.get_boolean(key) if kind is bool else st.get_string(key)
    return state


class LoginPage(Page):
    """GDM greeter settings; every change is written (with polkit) right away."""

    def __init__(self, window):
        super().__init__(title="Login Screen", icon_name="system-users-symbolic")
        self.window = window
        if not (has_gdm() and have("dconf") and have("pkexec")):
            return
        self.state = initial_gdm_state()
        if not self.state:
            return
        self.applied = dict(self.state)
        self._quiet = False
        self.setters = {}  # key -> function(value) updating its row silently
        self.writer = AdminWriter(window, self._build_job, self._on_applied,
                                  self._revert)

        main = group("Login screen", [
            self._switch("disable-user-list", "Hide the user list",
                         "Ask for a username instead of listing users"),
            self._switch("disable-restart-buttons",
                         "Hide the restart and power-off buttons"),
            self._switch("banner-message-enable", "Show a banner message"),
            self._entry("banner-message-text", "Banner message"),
            self._logo(),
            self._switch("enable-fingerprint-authentication",
                         "Allow fingerprint login"),
            self._switch("enable-smartcard-authentication", "Allow smartcard login"),
        ], "Takes effect next time the login screen appears. Changes ask for "
           "your administrator password and apply immediately.")
        extras = group("Greeter extras", [
            self._switch("tap-to-click", "Tap to click on the login screen"),
            self._clock(),
            self._switch("show-battery-percentage", "Show battery percentage"),
        ], "Also written to the login screen. Changes ask for your "
           "administrator password.")
        self.add(main)
        self.add(extras)

        reset = Gtk.Button(label="Reset", valign=Gtk.Align.CENTER)
        reset.add_css_class("destructive-action")
        reset.connect("clicked", self.on_reset)
        reset_row = Adw.ActionRow(
            title="Reset login screen settings",
            subtitle=f"Removes {GDM_CONF} and leaves the GDM profile in place")
        reset_row.add_prefix(admin_icon())
        reset_row.add_suffix(reset)
        self.add(group(None, [reset_row]))

    # -- rows -----------------------------------------------------------------
    def _commit(self, key, value):
        if self._quiet:
            return
        self.state[key] = value
        self.writer.request()

    def _switch(self, key, title, subtitle=None):
        if key not in self.state:
            return None
        row = Adw.SwitchRow(title=title, subtitle=subtitle or "",
                            active=self.state[key])
        row.connect("notify::active", lambda r, _p: self._commit(key, r.get_active()))
        self.setters[key] = row.set_active
        return admin(row)

    def _entry(self, key, title):
        if key not in self.state:
            return None
        row = Adw.EntryRow(title=title, text=self.state[key], show_apply_button=True)
        row.connect("apply", lambda r: self._commit(key, r.get_text()))
        self.setters[key] = row.set_text
        return admin(row)

    def _logo(self):
        if "logo" not in self.state:
            return None
        row = FileChooserRow("Logo", self.state["logo"],
                             lambda v: self._commit("logo", v),
                             lambda f: f.get_path() or "", image_filters())
        self.setters["logo"] = lambda v: row.set_value(v, notify=False)
        return admin(row)

    def _clock(self):
        key = "clock-format"
        if key not in self.state:
            return None
        fmts = [("24h", "24-hour"), ("12h", "12-hour")]
        allowed = enum_values(IF, key)
        if allowed is not None:
            fmts = [f for f in fmts if f[0] in allowed]
        vals = [v for v, _ in fmts]
        if not fmts or self.state[key] not in vals:
            return None
        row = Adw.ComboRow(title="Clock format")
        row.set_model(Gtk.StringList.new([l for _, l in fmts]))
        row.set_selected(vals.index(self.state[key]))
        row.connect("notify::selected",
                    lambda r, _p: self._commit(key, vals[r.get_selected()]))
        self.setters[key] = lambda v: row.set_selected(vals.index(v)) \
            if v in vals else None
        return admin(row)

    # -- applying -------------------------------------------------------------
    def _build_job(self):
        enc = lambda t: base64.b64encode(t.encode()).decode()  # noqa: E731
        stdin = enc(gdm_profile()) + "\n" + enc(gdm_keyfile(self.state)) + "\n"
        return GDM_APPLY_SCRIPT, stdin, dict(self.state)

    def _on_applied(self, snapshot):
        self.applied = snapshot

    def _revert(self):
        self._quiet = True
        try:
            self.state = dict(self.applied)
            for key, setter in self.setters.items():
                setter(self.state[key])
        finally:
            self._quiet = False

    def on_reset(self, _btn):
        dlg = Adw.AlertDialog(
            heading="Reset login screen settings?",
            body=f"This removes {GDM_CONF} and runs dconf update. "
                 "The GDM dconf profile is left in place.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("reset", "Reset")
        dlg.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)

        def done(_d, resp):
            if resp != "reset":
                return
            def result(kind, msg):
                self.window.toast("Login screen settings reset" if kind == "ok" else
                                  "Authentication cancelled" if kind == "cancelled"
                                  else f"Failed: {msg}")
            run_admin(GDM_RESET_SCRIPT, "", result)
        dlg.connect("response", done)
        dlg.present(self.window)


# --------------------------------------------------------------------------
# Window / app
# --------------------------------------------------------------------------
PAGES = (PowerPage, DisplayPage, InputPage, ShortcutsPage, AppearancePage,
         DesktopPage, StartupPage, DateRegionPage, LoginPage, PrivacyPage,
         AccessibilityPage, DefaultAppsPage)


class MainWindow(Adw.ApplicationWindow):
    """Sidebar of pages (collapses to a single pane on narrow widths)."""

    def __init__(self, app):
        super().__init__(application=app, title="Settings Plus",
                         default_width=900, default_height=720)
        self.overlay = Adw.ToastOverlay()
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.sidebar = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.sidebar.add_css_class("navigation-sidebar")
        self.pages = []
        for p_cls in PAGES:
            pg = p_cls(self)
            if pg.n_groups == 0:
                continue  # nothing supported on this system
            name = pg.get_title().lower().replace(" ", "-")
            self.stack.add_named(pg, name)
            self.pages.append((name, pg.get_title()))
            row = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=12, margin_top=10, margin_bottom=10,
                          margin_start=6, margin_end=6)
            box.append(Gtk.Image(icon_name=pg.get_icon_name()))
            box.append(Gtk.Label(label=pg.get_title(), xalign=0))
            row.set_child(box)
            self.sidebar.append(row)

        side_view = Adw.ToolbarView()
        side_header = Adw.HeaderBar(title_widget=Adw.WindowTitle(title="Settings Plus"))
        menu = Gio.Menu()
        menu.append("About Settings Plus", "app.about")
        side_header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic",
                                            menu_model=menu, primary=True))
        side_view.add_top_bar(side_header)
        side_view.set_content(Gtk.ScrolledWindow(child=self.sidebar, hscrollbar_policy=Gtk.PolicyType.NEVER))
        content_view = Adw.ToolbarView(content=self.stack)
        content_view.add_top_bar(Adw.HeaderBar())
        self.content_page = Adw.NavigationPage(title="Settings Plus", child=content_view)
        self.split = Adw.NavigationSplitView(
            sidebar=Adw.NavigationPage(title="Settings Plus", child=side_view),
            content=self.content_page,
            min_sidebar_width=200, max_sidebar_width=260)
        self.sidebar.connect("row-selected", self.on_selected)
        self.sidebar.connect("row-activated", lambda *_: self.split.set_show_content(True))
        self.sidebar.select_row(self.sidebar.get_row_at_index(0))

        self.overlay.set_child(self.split)
        self.set_content(self.overlay)
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 640sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)

    def on_selected(self, _lb, row):
        if row is None:
            return
        name, title = self.pages[row.get_index()]
        self.stack.set_visible_child_name(name)
        self.content_page.set_title(title)

    def toast(self, msg):
        self.overlay.add_toast(Adw.Toast(title=msg))


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_startup(self):
        Adw.Application.do_startup(self)
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self.on_about)
        self.add_action(about)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Primary>q"])

    def on_about(self, *_):
        dialog = Adw.AboutDialog(
            application_name="Settings Plus",
            application_icon=APP_ID,
            version=VERSION,
            developer_name="Hector Aguilar (@haguilar91)",
            developers=["Hector Aguilar (@haguilar91)"],
            comments="The GNOME settings that Settings and Tweaks leave out "
                     "\u2014 bringing GNOME closer to KDE Plasma.")
        # TODO: set website, issue_url and license_type once the user chooses them.
        dialog.add_credit_section("Built with", ["AI assistance (Claude by Anthropic)"])
        dialog.present(self.props.active_window)

    def do_activate(self):
        win = self.props.active_window or MainWindow(self)
        win.present()


if __name__ == "__main__":
    check_versions()
    sys.exit(App().run(sys.argv))

"""Entry point of the settings-plus zipapp."""
import os
import sys

if any(a in ("--version", "-V") for a in sys.argv[1:]):
    # Answer without importing GTK: read VERSION from the module source
    # without executing it (works from inside the zipapp).
    import importlib.util
    import re

    _spec = importlib.util.find_spec("settings_plus")
    _src = _spec.loader.get_data(_spec.origin).decode("utf-8")
    _m = re.search(r'^VERSION = "([^"]+)"', _src, re.M)
    print(f"Settings Plus {_m.group(1) if _m else 'unknown'}")
    sys.exit(0)

try:
    import gi  # noqa: F401
except ImportError:
    # `#!/usr/bin/env python3` may pick a python without PyGObject (linuxbrew,
    # pyenv, venv); retry with the system interpreter.
    SYSTEM_PY = "/usr/bin/python3"
    if os.path.exists(SYSTEM_PY) and \
            os.path.realpath(SYSTEM_PY) != os.path.realpath(sys.executable):
        os.execv(SYSTEM_PY, [SYSTEM_PY] + sys.argv)

import settings_plus  # noqa: E402

sys.exit(settings_plus.main())

"""Entry point of the settings-plus zipapp."""
import os
import sys

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

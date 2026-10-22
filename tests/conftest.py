import logging
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "py_modules"))
sys.path.insert(0, ROOT)

# Stand-in for the loader's `decky` module so main.py imports outside Decky.
if "decky" not in sys.modules:
    decky = types.ModuleType("decky")
    decky.DECKY_PLUGIN_SETTINGS_DIR = ""  # tests point Plugin.settings at tmp_path
    decky.logger = logging.getLogger("wqs-test")
    sys.modules["decky"] = decky

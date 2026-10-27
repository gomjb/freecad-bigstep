"""BigStep preferences (stored under Preferences/Mod/BigStep)."""
import os

import FreeCAD as App

PARAM_PATH = "User parameter:BaseApp/Preferences/Mod/BigStep"
ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
ENGINE_SCRIPT = os.path.join(ADDON_DIR, "engine", "bigstep_engine.py")

DEFAULTS = {
    "EnginePython": "",
    "Jobs": max(1, (os.cpu_count() or 2) - 2),
    "Deflection": 0.002,
    "Angular": 20.0,
    "PartTimeout": 1200.0,
    "AutoLoad": True,
    "FastTransfer": True,
    "TuneViewer": True,
    "FastPreview": True,
    "OutputRoot": "",
}


PREFS_VERSION = 2


def params():
    p = App.ParamGet(PARAM_PATH)
    if p.GetInt("PrefsVersion", 1) < PREFS_VERSION:
        # v2: workers heal with the file's tolerances, which takes longer on
        # heavy parts; lift a timeout still at an older default.
        if p.GetFloat("PartTimeout", 0.0) in (300.0, 900.0):
            p.SetFloat("PartTimeout", DEFAULTS["PartTimeout"])
        p.SetInt("PrefsVersion", PREFS_VERSION)
    return p


def get(key):
    p, d = params(), DEFAULTS[key]
    if isinstance(d, bool):
        return p.GetBool(key, d)
    if isinstance(d, int):
        return p.GetInt(key, d)
    if isinstance(d, float):
        return p.GetFloat(key, d)
    return p.GetString(key, d)


def set(key, value):  # noqa: A001 - mirrors get()
    p, d = params(), DEFAULTS[key]
    if isinstance(d, bool):
        p.SetBool(key, bool(value))
    elif isinstance(d, int):
        p.SetInt(key, int(value))
    elif isinstance(d, float):
        p.SetFloat(key, float(value))
    else:
        p.SetString(key, str(value))


VIEW_PARAM_PATH = "User parameter:BaseApp/Preferences/View"
# FreeCAD's defaults re-send every triangle to the GPU each frame (no VBO) and
# rebuild the scene traversal per object; on assemblies with thousands of
# instances that makes the view unusable (see README, "Smooth 3D view").
VIEW_TUNING = {"UseVBO": True, "RenderCache": 1}   # 1 = Distributed


def tune_viewer():
    """Apply VIEW_TUNING to FreeCAD's 3D view preferences (live views follow).
    Returns the names of the settings that were changed."""
    if not get("TuneViewer"):
        return []
    p, changed = App.ParamGet(VIEW_PARAM_PATH), []
    for key, value in VIEW_TUNING.items():
        if isinstance(value, bool):
            if p.GetBool(key, False) != value:
                p.SetBool(key, value)
                changed.append(key)
        elif p.GetInt(key, 0) != value:
            p.SetInt(key, value)
            changed.append(key)
    return changed


def engine_venv_dir():
    """Where BigStep ▸ Settings ▸ Install engine puts its Python environment.
    Outside the addon folder, so updating the addon keeps the engine."""
    return os.path.join(App.getUserAppDataDir(), "BigStep", "engine-venv")


def venv_python(venv):
    if os.name == "nt":
        return os.path.join(venv, "Scripts", "python.exe")
    return os.path.join(venv, "bin", "python")


def engine_candidates():
    """Places the OCCT 8 Python env is looked for when no path is configured."""
    real = os.path.realpath(ADDON_DIR)
    return [
        os.environ.get("BIGSTEP_PYTHON", ""),
        venv_python(engine_venv_dir()),
        venv_python(os.path.join(os.path.dirname(real), ".venv-occt8")),   # setup_engine.sh
    ]


def engine_python():
    configured = get("EnginePython")
    if configured:
        return configured
    for c in engine_candidates():
        if c and os.access(c, os.X_OK):
            return c
    return ""


def output_dir_for(step_path):
    stem = os.path.splitext(os.path.basename(step_path))[0]
    root = get("OutputRoot")
    if not root:
        root = os.path.dirname(os.path.abspath(step_path))
        if not os.access(root, os.W_OK):
            root = os.path.expanduser("~/.cache/bigstep")
    return os.path.join(root, stem + ".bigstep")

"""Run the BigStep engine as a child process of FreeCAD via QProcess.

The engine uses its own Python with OCCT 8, so it must not inherit the
FreeCAD/AppImage interpreter settings (PYTHONHOME, LD_LIBRARY_PATH, ...).
"""
import json
import shutil
import sys

from PySide import QtCore

import BigStepPrefs as Prefs

# Set by FreeCAD's AppImage/conda launchers; they would break a foreign Python.
_STRIP_ENV = ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "LD_LIBRARY_PATH", "PREFIX",
              "PATH_TO_FREECAD_LIBDIR", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH")


def clean_environment():
    env = QtCore.QProcessEnvironment.systemEnvironment()
    for key in _STRIP_ENV:
        env.remove(key)
    return env


class EngineProcess(QtCore.QObject):
    """Emits `event(dict)` per protocol line and `finished(exit_code, crashed)`."""

    event = QtCore.Signal(object)
    stderr = QtCore.Signal(str)
    finished = QtCore.Signal(int, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc = QtCore.QProcess(self)
        self.proc.setProcessEnvironment(clean_environment())
        self.proc.readyReadStandardOutput.connect(self._read_stdout)
        self.proc.readyReadStandardError.connect(self._read_stderr)
        self.proc.finished.connect(self._finished)
        self.proc.errorOccurred.connect(self._error)
        self._buf = b""

    def start(self, args):
        py = Prefs.engine_python()
        if not py:
            self.event.emit({"ev": "fatal", "msg": "OCCT 8 engine not found. Use BigStep ▸ "
                             "Settings ▸ Install engine…, or set its Python there."})
            self.finished.emit(-1, False)
            return False
        self.proc.start(py, ["-E", "-s", Prefs.ENGINE_SCRIPT] + list(args))
        return True

    def run_step(self, step, out_dir):
        return self.start(["run", step, out_dir,
                           "--jobs", str(Prefs.get("Jobs")),
                           "--deflection", repr(Prefs.get("Deflection")),
                           "--angular", repr(Prefs.get("Angular")),
                           "--part-timeout", repr(Prefs.get("PartTimeout")),
                           "--transfer", "fast" if Prefs.get("FastTransfer") else "full"])

    def cancel(self):
        if self.proc.state() != QtCore.QProcess.NotRunning:
            self.proc.terminate()                 # engine kills its workers on SIGTERM
            if not self.proc.waitForFinished(5000):
                self.proc.kill()

    def running(self):
        return self.proc.state() != QtCore.QProcess.NotRunning

    def _read_stdout(self):
        self._buf += bytes(self.proc.readAllStandardOutput())
        *lines, self._buf = self._buf.split(b"\n")
        for line in lines:
            if line.strip():
                try:
                    self.event.emit(json.loads(line))
                except ValueError:
                    self.stderr.emit(line.decode(errors="replace"))

    def _read_stderr(self):
        self.stderr.emit(bytes(self.proc.readAllStandardError()).decode(errors="replace"))

    def _finished(self, code, status):
        self._read_stdout()
        self.finished.emit(code, status == QtCore.QProcess.CrashExit)

    def _error(self, err):
        if err == QtCore.QProcess.FailedToStart:
            self.event.emit({"ev": "fatal", "msg": f"Cannot start engine: {Prefs.engine_python()}"})
            self.finished.emit(-1, False)


def check_engine(timeout_ms=60000):
    """Synchronously ask the engine for its Python/OCCT versions."""
    py = Prefs.engine_python()
    if not py:
        return None, "engine Python not configured"
    p = QtCore.QProcess()
    p.setProcessEnvironment(clean_environment())
    p.start(py, ["-E", "-s", Prefs.ENGINE_SCRIPT, "check"])
    if not p.waitForFinished(timeout_ms):
        p.kill()
        return None, "engine check timed out"
    out = bytes(p.readAllStandardOutput()).decode(errors="replace")
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("ev") == "check":
            return ev, None
    return None, (out + bytes(p.readAllStandardError()).decode(errors="replace")).strip()


# --- one-click engine installation ---------------------------------------------

OCP_REQUIREMENT = "cadquery-ocp==8.0.1.1.0"     # OCCT 8.0.1 bindings
SUPPORTED_PY = ((3, 11), (3, 14))               # wheel availability, inclusive


def base_python_candidates():
    """Interpreters on PATH that can host the engine venv (not FreeCAD's own)."""
    names = [f"python3.{m}" for m in range(SUPPORTED_PY[1][1], SUPPORTED_PY[0][1] - 1, -1)]
    names += ["python3", "python"]
    out = []
    if sys.platform == "win32" and shutil.which("py"):
        out += [["py", f"-3.{m}"] for m in range(SUPPORTED_PY[1][1], SUPPORTED_PY[0][1] - 1, -1)]
    for n in names:
        path = shutil.which(n)
        if path and "FreeCAD" not in path and [path] not in out:
            out.append([path])
    return out


def _py_version(cmd):
    p = QtCore.QProcess()
    p.setProcessEnvironment(clean_environment())
    p.start(cmd[0], cmd[1:] + ["-c", "import sys; print(sys.version_info[0], sys.version_info[1])"])
    if not p.waitForFinished(15000) or p.exitCode() != 0:
        return None
    try:
        major, minor = bytes(p.readAllStandardOutput()).decode().split()
        return int(major), int(minor)
    except ValueError:
        return None


def find_base_python():
    for cmd in base_python_candidates():
        v = _py_version(cmd)
        if v and SUPPORTED_PY[0] <= v <= SUPPORTED_PY[1]:
            return cmd, v
    return None, None


class EngineInstaller(QtCore.QObject):
    """Create Prefs.engine_venv_dir() and install OCP into it, step by step.
    Emits `log(str)` and `finished(ok, message)`."""

    log = QtCore.Signal(str)
    finished = QtCore.Signal(bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc = QtCore.QProcess(self)
        self.proc.setProcessEnvironment(clean_environment())
        self.proc.setProcessChannelMode(QtCore.QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(
            lambda: self.log.emit(bytes(self.proc.readAllStandardOutput()).decode(errors="replace")))
        self.proc.finished.connect(self._step_done)
        self.steps = []

    def start(self):
        base, ver = find_base_python()
        if base is None:
            lo, hi = (".".join(map(str, v)) for v in SUPPORTED_PY)
            self.finished.emit(False, f"No Python {lo}–{hi} found on PATH. Install one "
                                      "(python.org or your package manager) and try again.")
            return
        venv = Prefs.engine_venv_dir()
        py = Prefs.venv_python(venv)
        self.log.emit(f"Using {' '.join(base)} (Python {ver[0]}.{ver[1]}) -> {venv}\n")
        self.steps = [
            (base[0], base[1:] + ["-m", "venv", "--clear", venv]),
            (py, ["-m", "pip", "install", "--upgrade", "pip"]),
            (py, ["-m", "pip", "install", OCP_REQUIREMENT, "numpy"]),
            (py, ["-E", "-s", Prefs.ENGINE_SCRIPT, "check"]),
        ]
        self._next()

    def cancel(self):
        self.steps = []
        if self.proc.state() != QtCore.QProcess.NotRunning:
            self.proc.kill()

    def _next(self):
        prog, args = self.steps.pop(0)
        self.log.emit(f"\n$ {prog} {' '.join(args)}\n")
        self.proc.start(prog, args)

    def _step_done(self, code, status):
        if code != 0 or status == QtCore.QProcess.CrashExit:
            self.finished.emit(False, f"step failed (exit {code}); see the log above")
        elif self.steps:
            self._next()
        else:
            Prefs.set("EnginePython", "")          # auto-detect picks the new venv
            self.finished.emit(True, f"Engine installed: {Prefs.venv_python(Prefs.engine_venv_dir())}")

"""BigStep GUI: commands, import dialog, settings and report."""
import json
import os
import time

import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtCore, QtGui, QtWidgets

import BigStepLoader as Loader
import BigStepPreview
import BigStepPrefs as Prefs
from BigStepRunner import EngineInstaller, EngineProcess, check_engine

ICON = os.path.join(Prefs.ADDON_DIR, "Resources", "icons", "BigStep.svg")
PROBLEM_STATES = ("crashed", "timeout", "failed", "invalid", "healed")
_dialogs = []          # keep non-modal dialogs alive


def mw():
    return Gui.getMainWindow()


def fmt_s(s):
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def load_into_freecad(manifest, status_cb=None):
    changed = Prefs.tune_viewer()
    if changed:
        App.Console.PrintMessage(
            f"BigStep: set {', '.join(changed)} for large models "
            "(Preferences > Display > 3D View; turn off in BigStep > Settings)\n")
    QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
    try:
        last = [0.0]

        def progress(i, n, what):
            # Each event pump also flushes pending tree-view updates, which is
            # expensive; refresh the UI at most every 2 seconds.
            if time.time() - last[0] < 2.0:
                return
            last[0] = time.time()
            if status_cb:
                status_cb(i, n, what)
            QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ExcludeUserInputEvents)
        doc, rep = Loader.load_manifest(manifest, progress=progress,
                                        preview=Prefs.get("FastPreview"))
        Gui.ActiveDocument = Gui.getDocument(doc.Name)
        view = Gui.ActiveDocument.ActiveView
        if hasattr(view, "viewIsometric"):
            view.viewIsometric()
            view.fitAll()
        App.Console.PrintMessage(f"BigStep: loaded {manifest}: {rep}\n")
        return doc, rep
    finally:
        QtWidgets.QApplication.restoreOverrideCursor()


# --- import dialog -----------------------------------------------------------

class ImportDialog(QtWidgets.QDialog):
    def __init__(self, step_path=""):
        super().__init__(mw())
        self.setWindowTitle("BigStep – Import large STEP (OCCT 8 engine)")
        self.setWindowIcon(QtGui.QIcon(ICON))
        self.resize(820, 620)
        self.engine, self.names, self.counts = None, {}, {}
        self.t0, self.manifest = 0.0, None

        self.step = QtWidgets.QLineEdit(step_path)
        self.out = QtWidgets.QLineEdit(Prefs.output_dir_for(step_path) if step_path else "")
        browse = QtWidgets.QPushButton("…")
        browse.clicked.connect(self.pick_step)
        self.step.editingFinished.connect(self.step_changed)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.step)
        row.addWidget(browse)

        self.jobs = QtWidgets.QSpinBox()
        self.jobs.setRange(1, max(1, os.cpu_count() or 1))
        self.jobs.setValue(Prefs.get("Jobs"))
        self.defl = QtWidgets.QDoubleSpinBox()
        self.defl.setDecimals(4)
        self.defl.setRange(0.0001, 0.1)
        self.defl.setSingleStep(0.001)
        self.defl.setValue(Prefs.get("Deflection"))
        self.timeout = QtWidgets.QSpinBox()
        self.timeout.setRange(5, 86400)
        self.timeout.setSuffix(" s")
        self.timeout.setValue(int(Prefs.get("PartTimeout")))
        self.autoload = QtWidgets.QCheckBox("Load into FreeCAD when finished")
        self.autoload.setChecked(Prefs.get("AutoLoad"))
        self.fast = QtWidgets.QCheckBox("Fast transfer: skip OCCT's single-threaded healing "
                                        "(workers heal in parallel)")
        self.fast.setChecked(Prefs.get("FastTransfer"))

        form = QtWidgets.QFormLayout()
        form.addRow("STEP file", row)
        form.addRow("Output folder", self.out)
        form.addRow("Worker processes", self.jobs)
        form.addRow("Mesh deflection (× part size)", self.defl)
        form.addRow("Per-part timeout", self.timeout)
        form.addRow("", self.fast)
        form.addRow("", self.autoload)

        self.stage = QtWidgets.QLabel("Ready.")
        self.stage.setWordWrap(True)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.problems = QtWidgets.QTreeWidget()
        self.problems.setHeaderLabels(["Part", "Name", "Status", "Message"])
        self.problems.setRootIsDecorated(False)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self.problems, "Problem parts")
        tabs.addTab(self.log, "Engine log")

        self.btn_start = QtWidgets.QPushButton("Start")
        self.btn_cancel = QtWidgets.QPushButton("Cancel")
        self.btn_load = QtWidgets.QPushButton("Load into FreeCAD")
        self.btn_close = QtWidgets.QPushButton("Close")
        self.btn_start.clicked.connect(self.start)
        self.btn_cancel.clicked.connect(self.cancel)
        self.btn_load.clicked.connect(self.load)
        self.btn_close.clicked.connect(self.close)
        self.btn_cancel.setEnabled(False)
        self.btn_load.setEnabled(False)
        btns = QtWidgets.QHBoxLayout()
        btns.addStretch(1)
        for b in (self.btn_start, self.btn_cancel, self.btn_load, self.btn_close):
            btns.addWidget(b)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.stage)
        lay.addWidget(self.bar)
        lay.addWidget(tabs, 1)
        lay.addLayout(btns)

        self.clock = QtCore.QTimer(self)
        self.clock.timeout.connect(self.tick)
        self.extra = ""

    # -- inputs
    def pick_step(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open STEP", os.path.dirname(self.step.text()) or os.path.expanduser("~"),
            "STEP (*.step *.stp *.STEP *.STP);;All files (*)")
        if path:
            self.step.setText(path)
            self.step_changed()

    def step_changed(self):
        if self.step.text():
            self.out.setText(Prefs.output_dir_for(self.step.text()))

    # -- run
    def start(self):
        step, out = self.step.text().strip(), self.out.text().strip()
        if not os.path.isfile(step):
            QtWidgets.QMessageBox.warning(self, "BigStep", f"File not found:\n{step}")
            return
        if os.path.exists(os.path.join(out, "manifest.json")):
            r = QtWidgets.QMessageBox.question(
                self, "BigStep", "This file was already processed.\n\n"
                "Yes: load the existing result\nNo: process it again",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No | QtWidgets.QMessageBox.Cancel)
            if r == QtWidgets.QMessageBox.Yes:
                self.manifest = os.path.join(out, "manifest.json")
                self.load()
                return
            if r == QtWidgets.QMessageBox.Cancel:
                return
        Prefs.set("Jobs", self.jobs.value())
        Prefs.set("Deflection", self.defl.value())
        Prefs.set("PartTimeout", float(self.timeout.value()))
        Prefs.set("AutoLoad", self.autoload.isChecked())
        Prefs.set("FastTransfer", self.fast.isChecked())

        self.problems.clear()
        self.log.clear()
        self.counts, self.names, self.manifest = {}, {}, None
        self.bar.setRange(0, 0)                    # busy until the part count is known
        self.engine = EngineProcess(self)
        self.engine.event.connect(self.on_event)
        self.engine.stderr.connect(lambda s: self.log.appendPlainText(s.rstrip()))
        self.engine.finished.connect(self.on_finished)
        self.t0 = time.time()
        if self.engine.run_step(step, out):
            self.set_running(True)
            self.clock.start(1000)

    def set_running(self, on):
        self.btn_start.setEnabled(not on)
        self.btn_cancel.setEnabled(on)
        self.btn_load.setEnabled(not on and bool(self.manifest))
        for w in (self.step, self.out, self.jobs, self.defl, self.timeout, self.fast):
            w.setEnabled(not on)

    def cancel(self):
        if self.engine and self.engine.running():
            self.engine.cancel()

    def tick(self):
        self.stage.setText(f"{self.stage_msg}  ·  {fmt_s(time.time() - self.t0)}{self.extra}")

    stage_msg = ""

    def on_event(self, ev):
        kind = ev.get("ev")
        if kind == "begin":
            self.stage_msg = f"Engine started (OCCT {ev.get('occt')}, {ev.get('jobs')} workers)"
        elif kind == "stage":
            self.stage_msg = ev.get("msg", ev.get("stage", ""))
            self.log.appendPlainText(f"[stage] {self.stage_msg}  {self._extras(ev)}")
            if ev.get("stage") == "parts":
                self.bar.setRange(0, max(1, ev["total"]))
                self.bar.setValue(0)
        elif kind == "heartbeat":
            self.extra = f"  ·  reader {ev.get('rss_mb', 0):,} MB"
        elif kind == "read_done":
            self.log.appendPlainText(f"[read] {self._extras(ev)}")
            self.load_names()
        elif kind == "progress":
            self.bar.setValue(ev["done"])
            summary = ", ".join(f"{k} {v}" for k, v in sorted(self.counts.items()))
            self.extra = (f"  ·  {ev['done']:,}/{ev['total']:,} parts  ·  workers "
                          f"{ev.get('rss_mb', 0):,} MB  ·  {summary}")
        elif kind == "part":
            st = ev.get("status", "?")
            self.counts[st] = self.counts.get(st, 0) + 1
            if st in PROBLEM_STATES:
                QtWidgets.QTreeWidgetItem(self.problems, [
                    ev["id"], self.names.get(ev["id"], ""), st, ev.get("msg", "")])
        elif kind == "preview_progress":
            self.extra = f"  ·  {ev['done']:,}/{ev['total']:,} part meshes"
        elif kind in ("preview_done", "warning"):
            self.log.appendPlainText(f"[{kind}] {ev.get('msg', '')} {self._extras(ev)}")
        elif kind == "retry":
            self.log.appendPlainText(f"[retry] {ev['id']} {self.names.get(ev['id'], '')}: {ev.get('msg')}")
        elif kind == "done":
            self.manifest = ev["manifest"]
            self.log.appendPlainText(f"[done] {json.dumps(ev.get('stats'))}")
        elif kind in ("fatal", "cancelled"):
            self.stage_msg = ev.get("msg", kind)
            self.log.appendPlainText(f"[{kind}] {ev.get('msg')}")

    @staticmethod
    def _extras(ev):
        return " ".join(f"{k}={v}" for k, v in ev.items() if k not in ("ev", "t", "msg", "stage"))

    def load_names(self):
        try:
            with open(os.path.join(self.out.text(), "structure.json")) as f:
                self.names = {k: v.get("name", "") for k, v in json.load(f)["parts"].items()}
        except (OSError, ValueError, KeyError):
            self.names = {}

    def on_finished(self, code, crashed):
        self.clock.stop()
        self.bar.setRange(0, 1)
        self.bar.setValue(1 if self.manifest else 0)
        took = fmt_s(time.time() - self.t0)
        if self.manifest:
            bad = sum(v for k, v in self.counts.items() if k in ("crashed", "timeout", "failed"))
            self.stage_msg = f"Finished in {took}. {bad} part(s) could not be processed."
        elif crashed:
            self.stage_msg = f"Engine process crashed after {took}. FreeCAD is unaffected; see log."
        elif not self.stage_msg.startswith(("STEP", "stopped", "worker", "OCCT", "Cannot")):
            self.stage_msg = f"Engine stopped (exit code {code}) after {took}."
        self.extra = ""
        self.tick()
        self.set_running(False)
        if self.manifest and self.autoload.isChecked():
            self.load()

    def load(self):
        if not self.manifest:
            return
        self.stage_msg = "Building FreeCAD document…"
        self.extra = ""

        def cb(i, n, what):
            self.bar.setRange(0, max(1, n))
            self.bar.setValue(i)
            self.stage.setText(f"Building FreeCAD document… {what} {i:,}/{n:,}")
        doc, rep = load_into_freecad(self.manifest, cb)
        self.bar.setRange(0, 1)
        self.bar.setValue(1)
        self.stage.setText(f"Loaded into '{doc.Label}': {rep}. "
                           "Select parts and use 'Load B-Rep' for real geometry.")

    def closeEvent(self, e):
        if self.engine and self.engine.running():
            r = QtWidgets.QMessageBox.question(self, "BigStep", "Stop the running engine?")
            if r != QtWidgets.QMessageBox.Yes:
                e.ignore()
                return
            self.engine.cancel()
        if self in _dialogs:
            _dialogs.remove(self)
        super().closeEvent(e)


# --- settings ----------------------------------------------------------------

class InstallDialog(QtWidgets.QDialog):
    """Runs EngineInstaller and shows its output."""

    def __init__(self, parent=None):
        super().__init__(parent or mw())
        self.setWindowTitle("Install BigStep engine (OCCT 8)")
        self.resize(760, 420)
        self.text = QtWidgets.QPlainTextEdit(readOnly=True)
        self.status = QtWidgets.QLabel("Creates a Python environment with OCCT 8 bindings "
                                       "(cadquery-ocp, about 300 MB download).")
        self.status.setWordWrap(True)
        self.button = QtWidgets.QPushButton("Cancel")
        self.button.clicked.connect(self.cancel_or_close)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(self.status)
        lay.addWidget(self.text)
        lay.addWidget(self.button, 0, QtCore.Qt.AlignRight)
        self.inst = EngineInstaller(self)
        self.inst.log.connect(self.append)
        self.inst.finished.connect(self.done_)
        self.running = True
        QtCore.QTimer.singleShot(0, self.inst.start)

    def append(self, s):
        self.text.moveCursor(QtGui.QTextCursor.End)
        self.text.insertPlainText(s)
        self.text.moveCursor(QtGui.QTextCursor.End)

    def done_(self, ok, msg):
        self.running = False
        self.status.setText(("✔ " if ok else "✘ ") + msg)
        self.button.setText("Close")

    def cancel_or_close(self):
        if self.running:
            self.inst.cancel()
        self.accept()


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self):
        super().__init__(mw())
        self.setWindowTitle("BigStep settings")
        self.py = QtWidgets.QLineEdit(Prefs.get("EnginePython"))
        self.py.setPlaceholderText(Prefs.engine_python() or "path to python with OCP (OCCT 8)")
        self.root = QtWidgets.QLineEdit(Prefs.get("OutputRoot"))
        self.root.setPlaceholderText("next to the STEP file")
        self.angular = QtWidgets.QDoubleSpinBox()
        self.angular.setRange(1, 90)
        self.angular.setSuffix("°")
        self.angular.setValue(Prefs.get("Angular"))
        self.tune = QtWidgets.QCheckBox("Use OpenGL VBO (needed for smooth large models)")
        self.tune.setChecked(Prefs.get("TuneViewer"))
        self.fastpv = QtWidgets.QCheckBox("Show large models through the fast merged preview")
        self.fastpv.setChecked(Prefs.get("FastPreview"))
        self.info = QtWidgets.QLabel()
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        test = QtWidgets.QPushButton("Check engine")
        test.clicked.connect(self.check)
        install = QtWidgets.QPushButton("Install engine…")
        install.setToolTip("Create the OCCT 8 Python environment the engine runs in")
        install.clicked.connect(self.install)
        form = QtWidgets.QFormLayout(self)
        form.addRow("Engine Python", self.py)
        form.addRow("Output root", self.root)
        form.addRow("Angular deflection", self.angular)
        form.addRow("3D view", self.tune)
        form.addRow("", self.fastpv)
        form.addRow(test, self.info)
        form.addRow(install)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok
                                        | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def install(self):
        InstallDialog(self).exec()
        self.py.setText(Prefs.get("EnginePython"))
        self.py.setPlaceholderText(Prefs.engine_python() or "path to python with OCP (OCCT 8)")
        self.check()

    def check(self):
        Prefs.set("EnginePython", self.py.text().strip())
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            info, err = check_engine()
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        import Part
        if info:
            self.info.setText(f"Engine OK: Python {info['python']}, OCCT {info['occt']}\n"
                              f"{info['executable']}\nFreeCAD's own OCCT: {Part.OCC_VERSION}")
        else:
            self.info.setText(f"Engine not usable: {err}")

    def accept(self):
        Prefs.set("EnginePython", self.py.text().strip())
        Prefs.set("OutputRoot", self.root.text().strip())
        Prefs.set("Angular", self.angular.value())
        Prefs.set("TuneViewer", self.tune.isChecked())
        Prefs.set("FastPreview", self.fastpv.isChecked())
        Prefs.tune_viewer()
        super().accept()


# --- selection helpers ---------------------------------------------------------

def selected_definitions():
    defs = {}
    for sel in Gui.Selection.getSelectionEx("", 0):
        for sub in sel.SubElementNames or [""]:
            leaf = sel.Object.getSubObject(sub, retType=1) if sub else sel.Object
            Loader.definitions_under(leaf, defs) if leaf is not None else None
    return defs


def selected_with_paths():
    """[(object, [objects on its tree path above it])] for the current selection."""
    out = []
    for sel in Gui.Selection.getSelectionEx("", 0):
        for sub in sel.SubElementNames or [""]:
            objs = sel.Object.getSubObjectList(sub) if sub else [sel.Object]
            if objs:
                out.append((objs[-1], list(objs[:-1])))
    return out


def show_parts_for_selection(doc):
    keys = BigStepPreview.areas_for(doc, selected_with_paths())
    if keys:
        BigStepPreview.set_detail(doc, keys, on=True)
        App.Console.PrintMessage(f"BigStep: {len(keys)} area(s) shown as selectable parts\n")
    return keys


def bigstep_library(doc):
    if doc is None:
        return None
    for o in doc.Objects:
        if "BigStepManifest" in o.PropertiesList:
            return o
    return None


# --- commands ------------------------------------------------------------------

class CmdImport:
    def GetResources(self):
        return {"Pixmap": ICON, "MenuText": "Import large STEP…",
                "ToolTip": "Process a STEP file in an external OCCT 8 engine "
                           "(crash-isolated, parallel) and load a lightweight model"}

    def Activated(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            mw(), "Open large STEP", os.path.expanduser("~"),
            "STEP (*.step *.stp *.STEP *.STP);;All files (*)")
        if path:
            d = ImportDialog(path)
            _dialogs.append(d)
            d.show()

    def IsActive(self):
        return True


class CmdOpenResult:
    def GetResources(self):
        return {"Pixmap": "document-open", "MenuText": "Open processed result…",
                "ToolTip": "Load a manifest.json produced earlier by the engine"}

    def Activated(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            mw(), "Open BigStep result", os.path.expanduser("~"), "BigStep manifest (manifest.json)")
        if path:
            load_into_freecad(path)

    def IsActive(self):
        return True


class CmdLoadBrep:
    def GetResources(self):
        return {"Pixmap": "Part_Box", "MenuText": "Load B-Rep for selection",
                "ToolTip": "Replace the mesh preview of the selected parts/assemblies "
                           "with the real (healed) B-Rep geometry"}

    def Activated(self):
        defs = [d for d in selected_definitions().values() if d.TypeId != "Part::Feature"]
        if not defs:
            App.Console.PrintWarning("BigStep: select mesh-preview parts or assemblies first\n")
            return
        if len(defs) > 500:
            r = QtWidgets.QMessageBox.question(
                mw(), "BigStep", f"Load real geometry for {len(defs):,} part definitions?\n"
                "This can take a long time and a lot of memory.")
            if r != QtWidgets.QMessageBox.Yes:
                return
        doc = defs[0].Document
        doc.openTransaction("BigStep load B-Rep")
        prog = QtWidgets.QProgressDialog("Loading B-Rep…", "Stop", 0, len(defs), mw())
        prog.setWindowModality(QtCore.Qt.WindowModal)
        try:
            for i, d in enumerate(defs):
                if prog.wasCanceled():
                    break
                prog.setValue(i)
                Loader.use_brep(d)
            show_parts_for_selection(doc)       # B-Rep is only drawn in the per-part view
            doc.recompute()
        finally:
            prog.close()
            doc.commitTransaction()

    def IsActive(self):
        return bool(Gui.Selection.getSelection())


class CmdShowParts:
    def GetResources(self):
        return {"Pixmap": ICON, "MenuText": "Show selectable parts here",
                "ToolTip": "Switch the areas containing the selection from the fast preview "
                           "to the per-part view (click, select, edit, load B-Rep)"}

    def Activated(self):
        doc = App.ActiveDocument
        if not show_parts_for_selection(doc):
            App.Console.PrintWarning("BigStep: select an assembly or part in the tree first\n")

    def IsActive(self):
        doc = App.ActiveDocument
        return bool(doc and BigStepPreview.preview_object(doc) and Gui.Selection.getSelection())


class CmdShowPreview:
    def GetResources(self):
        return {"Pixmap": ICON, "MenuText": "Back to fast preview",
                "ToolTip": "Show the selected areas (or everything, with nothing selected) "
                           "through the fast merged preview again"}

    def Activated(self):
        doc = App.ActiveDocument
        pv = BigStepPreview.preview_object(doc)
        keys = (BigStepPreview.areas_for(doc, selected_with_paths())
                if Gui.Selection.getSelection() else list(pv.DetailAreas))
        BigStepPreview.set_detail(doc, keys, on=False)

    def IsActive(self):
        doc = App.ActiveDocument
        pv = BigStepPreview.preview_object(doc) if doc else None
        return bool(pv and pv.DetailAreas)


class CmdUseMesh:
    def GetResources(self):
        return {"Pixmap": "Mesh_Tree", "MenuText": "Back to mesh preview",
                "ToolTip": "Switch the selected parts back to the lightweight mesh"}

    def Activated(self):
        defs = [d for d in selected_definitions().values() if d.TypeId == "Part::Feature"]
        if defs:
            doc = defs[0].Document
            doc.openTransaction("BigStep use mesh")
            for d in defs:
                Loader.use_mesh(d)
            doc.recompute()
            doc.commitTransaction()

    def IsActive(self):
        return bool(Gui.Selection.getSelection())


class CmdReport:
    def GetResources(self):
        return {"Pixmap": "document-properties", "MenuText": "Processing report",
                "ToolTip": "Show engine statistics and parts that failed, crashed or were healed"}

    def Activated(self):
        lib = bigstep_library(App.ActiveDocument)
        with open(lib.BigStepManifest) as f:
            man = json.load(f)
        dlg = QtWidgets.QDialog(mw())
        dlg.setWindowTitle(f"BigStep report – {os.path.basename(man.get('source', ''))}")
        dlg.resize(760, 520)
        st = man.get("stats", {})
        head = QtWidgets.QLabel(
            f"Engine OCCT {man.get('engine_occt')}  ·  parse {st.get('parse_s')} s  ·  "
            f"transfer {st.get('transfer_s')} s  ·  total {st.get('total_s')} s  ·  "
            f"reader peak {st.get('peak_rss_mb'):,} MB\n"
            f"{st.get('parts'):,} parts, {st.get('assemblies'):,} assemblies, "
            f"{st.get('instances'):,} instances  ·  status: {st.get('status')}")
        head.setWordWrap(True)
        tree = QtWidgets.QTreeWidget()
        tree.setHeaderLabels(["Part", "Name", "Status", "Message"])
        tree.setRootIsDecorated(False)
        for pid, p in man["parts"].items():
            if p.get("status") in PROBLEM_STATES:
                QtWidgets.QTreeWidgetItem(tree, [pid, p.get("name", ""), p["status"],
                                                 p.get("msg", "")])
        lay = QtWidgets.QVBoxLayout(dlg)
        lay.addWidget(head)
        lay.addWidget(tree)
        dlg.show()
        _dialogs.append(dlg)

    def IsActive(self):
        return bigstep_library(App.ActiveDocument) is not None


class CmdSettings:
    def GetResources(self):
        return {"Pixmap": "preferences-system", "MenuText": "Settings…",
                "ToolTip": "Engine Python (OCCT 8) and processing options"}

    def Activated(self):
        SettingsDialog().exec_()

    def IsActive(self):
        return True


COMMANDS = {
    "BigStep_Import": CmdImport(),
    "BigStep_OpenResult": CmdOpenResult(),
    "BigStep_LoadBrep": CmdLoadBrep(),
    "BigStep_UseMesh": CmdUseMesh(),
    "BigStep_ShowParts": CmdShowParts(),
    "BigStep_ShowPreview": CmdShowPreview(),
    "BigStep_Report": CmdReport(),
    "BigStep_Settings": CmdSettings(),
}


def register():
    for name, cmd in COMMANDS.items():
        Gui.addCommand(name, cmd)

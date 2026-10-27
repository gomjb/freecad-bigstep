"""FreeCAD-only 3D view benchmark: many App::Link instances of tessellation-heavy parts.

Run as a macro in the FreeCAD GUI (no BigStep needed), once per mode:

    MODE = "defaults"   UseVBO off, RenderCache 0 (Auto)  - FreeCAD's defaults
    MODE = "vbo"        UseVBO on,  RenderCache 0
    MODE = "vbo_cache"  UseVBO on,  RenderCache 1 (Distributed)

or from a shell:  BENCH_MODE=vbo freecad tests/freecad_view_benchmark.py

It builds PARTS distinct parts (a plate plus a grid of pins, as one compound),
places LINKS links to them in AREAS App::Part groups, then repaints the view
FRAMES times while rotating and prints the mean time per frame. The view
preferences are changed for this session only and restored at the end.
"""
import math
import os
import time

import FreeCAD as App
import FreeCADGui as Gui
import Part

MODE = os.environ.get("BENCH_MODE", "defaults")
PARTS, PINS, LINKS, AREAS, FRAMES = 40, 400, 3000, 24, 60

SETTINGS = {"defaults": (False, 0), "vbo": (True, 0), "vbo_cache": (True, 1)}


def build():
    doc = App.newDocument("ViewBenchmark")
    parts = []
    for i in range(PARTS):
        n = int(math.sqrt(PINS))
        shapes = [Part.makeBox(2000, 2000, 50)]
        for a in range(n):
            for b in range(n):
                shapes.append(Part.makeCylinder(15 + i % 5, 60, App.Vector(100 + a * 1800 / n,
                                                                          100 + b * 1800 / n, 50)))
        f = doc.addObject("Part::Feature", f"Jig{i:02d}")
        f.Shape = Part.makeCompound(shapes)
        f.Visibility = False
        parts.append(f)
    per_area = LINKS // AREAS
    for k in range(AREAS):
        area = doc.addObject("App::Part", f"Area{k:02d}")
        links = []
        for j in range(per_area):
            lk = doc.addObject("App::Link", f"L{k:02d}_{j:03d}")
            lk.LinkedObject = parts[(k * per_area + j) % PARTS]
            lk.Placement.Base = App.Vector((k % 6) * 30000 + (j % 12) * 2400,
                                           (k // 6) * 30000 + (j // 12) * 2400, 0)
            links.append(lk)
        area.addObjects(links)
    doc.recompute()
    return doc


def frames(view, n):
    from pivy import coin
    from PySide import QtWidgets
    gv = [w for w in Gui.getMainWindow().findChildren(QtWidgets.QGraphicsView) if w.isVisible()][0]
    cam = view.getCameraNode()
    t = time.time()
    for _ in range(n):
        cam.orientation.setValue(cam.orientation.getValue() * coin.SbRotation(coin.SbVec3f(0, 0, 1), 0.15))
        gv.viewport().repaint()
        QtWidgets.QApplication.processEvents()
    return (time.time() - t) / n * 1000


def main():
    p = App.ParamGet("User parameter:BaseApp/Preferences/View")
    saved = (p.GetBool("UseVBO", False), p.GetInt("RenderCache", 0))
    vbo, cache = SETTINGS[MODE]
    p.SetBool("UseVBO", vbo)
    p.SetInt("RenderCache", cache)
    try:
        t = time.time()
        doc = build()
        view = Gui.ActiveDocument.ActiveView
        view.viewIsometric()
        view.fitAll()
        frames(view, 2)                         # first frames build caches / VBOs
        build_s = time.time() - t
        ms = frames(view, FRAMES)
        ms2 = frames(view, FRAMES)              # steady state: caches must be warm by now
        msg = (f"BENCH mode={MODE} parts={PARTS} links={LINKS} build+first={build_s:.1f}s "
               f"mean_frame={ms:.1f}ms then {ms2:.1f}ms (FreeCAD {'.'.join(App.Version()[:3])})\n")
        App.Console.PrintMessage(msg)
        out = os.environ.get("BENCH_OUT")
        if out:
            with open(out, "a") as f:
                f.write(msg)
    finally:
        p.SetBool("UseVBO", saved[0])
        p.SetInt("RenderCache", saved[1])


if os.environ.get("BENCH_EXIT"):
    from PySide import QtCore

    def _run_and_exit():
        main()
        os._exit(0)

    QtCore.QTimer.singleShot(1000, _run_and_exit)
else:
    main()

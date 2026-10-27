"""Stage 2 worker: check, heal and tessellate parts with OCCT 8.

    python occt_worker.py <out_dir> <deflection> <angular_deg>   (part ids on stdin)

stdin lines are `<pid>` or `<pid> noheal` (mesh as is; used after a healing timeout).
For every pid it emits `start` before touching the shape and `part` when done.
If the process dies (segfault, abort, OOM kill) the orchestrator knows exactly
which part was in progress from the last unmatched `start`.

Outputs per part (the input parts/<pid>.bin is removed once it succeeds):
    parts/<pid>.brep   ASCII B-Rep (healed if needed), TopTools format v3 so
                       FreeCAD's bundled OCCT 7.8 can read what OCCT 8 wrote
    meshes/<pid>.stl   binary STL for the lightweight preview
"""
import json
import math
import os
import sys
import time

from common import emit, open_event_channel

from OCP.BinTools import BinTools
from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools
from OCP.ShapeAnalysis import ShapeAnalysis_ShapeTolerance
from OCP.ShapeFix import ShapeFix_Shape
from OCP.StlAPI import StlAPI_Writer
from OCP.TopAbs import TopAbs_FACE, TopAbs_VERTEX
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.TopTools import TopTools_FormatVersion

BREP_VERSION = TopTools_FormatVersion.TopTools_FormatVersion_VERSION_3
MAX_TOLERANCE = 1.0          # mm, OCCT's read.maxprecision.val default


def _test_hooks(pid, noheal):
    """Fault injection for tests, each = pid[,pid]: BIGSTEP_TEST_CRASH,
    BIGSTEP_TEST_HANG (only while healing is on), BIGSTEP_TEST_HANG_ALWAYS."""
    def listed(var):
        return pid in os.environ.get(var, "").split(",")
    if listed("BIGSTEP_TEST_CRASH"):
        os.abort()
    if listed("BIGSTEP_TEST_HANG_ALWAYS") or (listed("BIGSTEP_TEST_HANG") and not noheal):
        time.sleep(1e9)


def count_triangles(shape):
    n = 0
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    loc = TopLoc_Location()
    while exp.More():
        tri = BRep_Tool.Triangulation_s(TopoDS.Face(exp.Current()), loc)
        if tri is not None:
            n += tri.NbTriangles()
        exp.Next()
    return n


def load_tolerances(out_dir):
    try:
        with open(os.path.join(out_dir, "tolerances.json")) as f:
            return [float(t) for t in json.load(f)]
    except (OSError, ValueError):
        return []


def fix(shape, precision):
    sf = ShapeFix_Shape(shape)
    sf.SetPrecision(precision)
    sf.SetMinTolerance(1e-7)
    sf.SetMaxTolerance(max(MAX_TOLERANCE, precision))
    sf.Perform()
    return sf.Shape()


def heal(shape, tolerances):
    """Heal like OCCT's STEP transfer does: ShapeFix with the context uncertainty.

    Starts from the shape's own average vertex tolerance, then tries the file's
    declared uncertainties above it (the part's real context is one of them).
    Returns (shape, valid, precision used).
    """
    base = max(ShapeAnalysis_ShapeTolerance().Tolerance(shape, 0, TopAbs_VERTEX), 1e-7)
    fixed = shape
    for prec in [base] + [t for t in tolerances if t > base * 1.01]:
        fixed = fix(shape, prec)
        if BRepCheck_Analyzer(fixed).IsValid():
            return fixed, True, prec
    return fixed, False, prec


def process(out_dir, pid, deflection, angular, tolerances, noheal=False):
    t0 = time.time()
    src = os.path.join(out_dir, "parts", pid + ".bin")
    shape = TopoDS_Shape()
    if not BinTools.Read_s(shape, src) or shape.IsNull():
        return {"status": "failed", "msg": "empty or unreadable B-Rep"}

    res = {"status": "ok"}
    if noheal:
        if not BRepCheck_Analyzer(shape).IsValid():
            res.update(status="invalid", msg="healing timed out; geometry is unhealed")
    elif not BRepCheck_Analyzer(shape).IsValid():
        shape, valid, prec = heal(shape, tolerances)
        res["heal_tol"] = prec
        if valid:
            res["status"] = "healed"
        else:
            res["status"] = "invalid"      # still usable, but flagged for the user
            res["msg"] = "BRepCheck fails after ShapeFix"

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    if box.IsVoid():
        res.update(status="failed", msg="no geometry (void bounding box)")
        return res
    lo, hi = box.CornerMin(), box.CornerMax()   # Bnd_Box::Get() returns a struct in OCCT 8
    xmin, ymin, zmin, xmax, ymax, zmax = lo.X(), lo.Y(), lo.Z(), hi.X(), hi.Y(), hi.Z()
    diag = math.dist((xmin, ymin, zmin), (xmax, ymax, zmax))
    res["bbox"] = [round(v, 6) for v in (xmin, ymin, zmin, xmax, ymax, zmax)]

    # Linear deflection relative to the part size, clamped to something sane.
    lin = max(diag * deflection, 1e-4)
    BRepMesh_IncrementalMesh(shape, lin, False, math.radians(angular), False)
    res["triangles"] = count_triangles(shape)
    if res["triangles"]:
        w = StlAPI_Writer()
        w.ASCIIMode = False
        mesh = os.path.join(out_dir, "meshes", pid + ".stl")
        if w.Write(shape, mesh):
            res["mesh"] = f"meshes/{pid}.stl"
    if "mesh" not in res and res["status"] != "invalid":
        res.update(status="failed", msg="tessellation produced no triangles")
    if BRepTools.Write_s(shape, os.path.join(out_dir, "parts", pid + ".brep"),
                         False, False, BREP_VERSION):
        res["brep"] = f"parts/{pid}.brep"
        os.remove(src)
    res["secs"] = round(time.time() - t0, 3)
    return res


def main():
    out_dir, deflection, angular = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
    open_event_channel()
    os.makedirs(os.path.join(out_dir, "meshes"), exist_ok=True)
    tolerances = load_tolerances(out_dir)
    for line in sys.stdin:
        if not line.split():
            continue
        pid, *flags = line.split()
        emit("start", id=pid)
        _test_hooks(pid, "noheal" in flags)
        try:
            res = process(out_dir, pid, deflection, angular, tolerances, "noheal" in flags)
        except Exception as e:  # Python-level OCCT exceptions (Standard_Failure etc.)
            res = {"status": "failed", "msg": f"{type(e).__name__}: {e}"}
        emit("part", id=pid, **res)


if __name__ == "__main__":
    main()

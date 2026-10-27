"""Stage 1: read a STEP file with OCCT 8 (XCAF) and split it into prototypes.

    python occt_read.py <file.step> <out_dir> [fast|full]

`fast` (default) turns off OCCT's single-threaded healing during transfer
(see occt_flags.py); the parallel workers heal every part instead.

Writes
    out_dir/parts/<pid>.bin    one binary B-Rep per distinct part definition
    out_dir/structure.json     assembly definitions, roots, names, colors
    out_dir/tolerances.json    length uncertainties declared in the file (mm),
                               used by the workers to heal like OCCT's transfer

Repeated parts are written once; every placement refers to the same pid.
This is the only stage that has to hold the whole model in memory.
"""
import json
import mmap
import os
import re
import sys
import time

from common import emit, open_event_channel, peak_rss_mb
from occt_flags import disable_transfer_healing

from OCP.BinTools import BinTools, BinTools_FormatVersion
from OCP.IFSelect import IFSelect_RetDone
from OCP.Quantity import Quantity_Color
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_AsciiString, TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label, TDF_Tool
from OCP.collections import Sequence_TDF_Label as TDF_LabelSequence
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.UnitsMethods import UnitsMethods_LengthUnit_Millimeter
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import (XCAFDoc_ColorCurv, XCAFDoc_ColorGen, XCAFDoc_ColorSurf,
                         XCAFDoc_ColorTool as CT, XCAFDoc_DocumentTool, XCAFDoc_ShapeTool as ST)

# Engine-internal format: fast binary, no triangulation.  The worker converts
# each part to an ASCII .brep that FreeCAD's OCCT 7.8 reads.
BREP_VERSION = BinTools_FormatVersion.BinTools_FormatVersion_VERSION_3


_UNCERTAINTY = re.compile(rb"UNCERTAINTY_MEASURE_WITH_UNIT\s*\(\s*LENGTH_MEASURE\s*\(\s*([-+0-9.EeDd]+)")
_LENGTH_UNIT = re.compile(rb"SI_UNIT\s*\(\s*(\.\w+\.|\$)\s*,\s*\.METRE\.\s*\)|CONVERSION_BASED_UNIT\s*\(\s*'(INCH|FOOT)'")
_TO_MM = {b".MILLI.": 1.0, b".CENTI.": 10.0, b".DECI.": 100.0, b"$": 1000.0, b".MICRO.": 1e-3,
          b"INCH": 25.4, b"FOOT": 304.8}


def file_tolerances(step_path):
    """Distinct length uncertainties of the file's representation contexts, in mm.

    OCCT's transfer heals each shape with the uncertainty of its own context;
    the vertex tolerances of an unhealed shape do not carry it.  Which context
    a part belongs to is not cheap to recover, so the workers try these values
    in ascending order on parts that are still invalid.  Units are matched by
    scale only: with mixed units every combination is listed.
    """
    try:
        with open(step_path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            values = {float(v.replace(b"D", b"E").replace(b"d", b"e"))
                      for v in set(_UNCERTAINTY.findall(m))}
            units = {a or b for a, b in set(_LENGTH_UNIT.findall(m))}
    except (OSError, ValueError):
        return []
    scales = {_TO_MM[u] for u in units if u in _TO_MM} or {1.0}
    return sorted({round(v * k, 12) for v in values for k in scales if 0 < v * k <= 1.0})


def label_entry(label):
    s = TCollection_AsciiString()
    TDF_Tool.Entry_s(label, s)
    return s.ToCString()


def label_name(label):
    attr = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
        name = attr.Get().ToExtString()
        if name and not name.startswith("=>"):   # "=>[0:1:1:2]" is an OCCT placeholder
            return name
    return None


def trsf_to_list(loc):
    """TopLoc_Location -> row-major 3x4 matrix (rotation | translation)."""
    t = loc.Transformation()
    return [round(t.Value(r, c), 12) for r in (1, 2, 3) for c in (1, 2, 3, 4)]


class Splitter:
    def __init__(self, doc, out_dir):
        self.st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
        self.ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
        self.parts_dir = os.path.join(out_dir, "parts")
        os.makedirs(self.parts_dir, exist_ok=True)
        self.ids = {}          # label entry -> pid / aid
        self.parts = {}        # pid -> info
        self.assemblies = {}   # aid -> info
        self.n_instances = 0

    def color_of_label(self, label):
        c = Quantity_Color()
        for kind in (XCAFDoc_ColorSurf, XCAFDoc_ColorGen, XCAFDoc_ColorCurv):
            if CT.GetColor_s(label, kind, c):
                return [round(c.Red(), 4), round(c.Green(), 4), round(c.Blue(), 4)]
        return None

    def color_of_shape(self, shape):
        """Fallback: many exporters only color solids or faces."""
        c = Quantity_Color()
        for typ in (TopAbs_SOLID, TopAbs_FACE):
            exp = TopExp_Explorer(shape, typ)
            if exp.More():
                for kind in (XCAFDoc_ColorSurf, XCAFDoc_ColorGen):
                    if self.ct.GetColor(exp.Current(), kind, c):
                        return [round(c.Red(), 4), round(c.Green(), 4), round(c.Blue(), 4)]
        return None

    def visit(self, label):
        """Return the id of the definition behind `label`, creating it once."""
        if ST.IsReference_s(label):
            ref = TDF_Label()
            ST.GetReferredShape_s(label, ref)
            label = ref
        entry = label_entry(label)
        if entry in self.ids:
            return self.ids[entry]

        name = label_name(label) or entry
        if ST.IsAssembly_s(label):
            aid = f"a{len(self.assemblies):06d}"
            self.ids[entry] = aid
            info = {"name": name, "color": self.color_of_label(label), "children": []}
            self.assemblies[aid] = info
            comps = TDF_LabelSequence()
            ST.GetComponents_s(label, comps, False)
            for i in range(1, comps.Length() + 1):
                comp = comps.Value(i)
                child = self.visit(comp)
                self.n_instances += 1
                info["children"].append({
                    "ref": child,
                    "name": label_name(comp),
                    "matrix": trsf_to_list(ST.GetLocation_s(comp)),
                    "color": self.color_of_label(comp),
                })
            return aid

        pid = f"p{len(self.parts):06d}"
        self.ids[entry] = pid
        shape = ST.GetShape_s(label)
        path = os.path.join(self.parts_dir, pid + ".bin")
        ok = BinTools.Write_s(shape, path, False, False, BREP_VERSION)
        color = self.color_of_label(label) or self.color_of_shape(shape)
        self.parts[pid] = {"name": name, "color": color,
                           "empty": shape.IsNull(), "written": bool(ok)}
        if len(self.parts) % 200 == 0:
            emit("split", parts=len(self.parts), assemblies=len(self.assemblies))
        return pid


def main():
    step_path, out_dir = sys.argv[1], sys.argv[2]
    transfer = sys.argv[3] if len(sys.argv) > 3 else "fast"
    open_event_channel()
    os.makedirs(out_dir, exist_ok=True)
    t0 = time.time()

    doc = TDocStd_Document(TCollection_ExtendedString("XmlXCAF"))
    XCAFApp_Application.GetApplication_s().InitDocument(doc)
    # Work in millimetres, like FreeCAD.
    XCAFDoc_DocumentTool.SetLengthUnit_s(doc, 1.0, UnitsMethods_LengthUnit_Millimeter)

    reader = STEPCAFControl_Reader()
    reader.SetColorMode(True)
    reader.SetNameMode(True)
    reader.SetLayerMode(False)
    reader.SetGDTMode(False)
    reader.SetMatMode(False)
    reader.SetViewMode(False)

    tolerances = file_tolerances(step_path)
    with open(os.path.join(out_dir, "tolerances.json"), "w") as f:
        json.dump(tolerances, f)

    emit("stage", stage="parse", msg="Parsing STEP file")
    if reader.ReadFile(step_path) != IFSelect_RetDone:
        emit("fatal", msg=f"STEP parse failed: {step_path}")
        sys.exit(2)
    t_parse = time.time() - t0
    healing = "on"
    if transfer == "fast":
        healing = "off" if disable_transfer_healing(reader) else "on (could not disable)"
    emit("stage", stage="transfer", msg=f"Converting to B-Rep (transfer healing {healing})",
         parse_s=round(t_parse, 2), roots=reader.NbRootsForTransfer(),
         peak_rss_mb=round(peak_rss_mb()))

    if not reader.Transfer(doc):
        emit("fatal", msg="STEP transfer to XCAF failed")
        sys.exit(3)
    t_xfer = time.time() - t0 - t_parse
    emit("stage", stage="split", msg="Writing parts", transfer_s=round(t_xfer, 2),
         peak_rss_mb=round(peak_rss_mb()))

    sp = Splitter(doc, out_dir)
    free = TDF_LabelSequence()
    sp.st.GetFreeShapes(free)
    roots = []
    for i in range(1, free.Length() + 1):
        lab = free.Value(i)
        roots.append({"ref": sp.visit(lab), "name": label_name(lab),
                      "matrix": trsf_to_list(ST.GetLocation_s(lab)), "color": None})

    structure = {
        "source": os.path.abspath(step_path),
        "units": "mm",
        "roots": roots,
        "assemblies": sp.assemblies,
        "parts": sp.parts,
        "read_stats": {
            "transfer_healing": healing,
            "file_tolerances_mm": tolerances,
            "parse_s": round(t_parse, 2),
            "transfer_s": round(t_xfer, 2),
            "split_s": round(time.time() - t0 - t_parse - t_xfer, 2),
            "peak_rss_mb": round(peak_rss_mb()),
            "parts": len(sp.parts),
            "assemblies": len(sp.assemblies),
            "instances": sp.n_instances + len(roots),
        },
    }
    tmp = os.path.join(out_dir, "structure.json.tmp")
    with open(tmp, "w") as f:
        json.dump(structure, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(out_dir, "structure.json"))
    emit("read_done", **structure["read_stats"])


if __name__ == "__main__":
    main()

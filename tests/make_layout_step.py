"""Generate a large synthetic factory-layout STEP file (open benchmark model).

Run with the engine venv (OCCT 8):
    .venv-occt8/bin/python tests/make_layout_step.py layout.step [--areas 24] [--pins 3000]

The output is deterministic: the same arguments give the same model. Everything
is in millimetres.

    Layout
     └─ Area_<k>  x areas, cycling through 4 area types (same type = same definition)
          ├─ Slab, Building (walls + roof), Columns x 40
          ├─ JigTable_<v> x 6     plate + a grid of pins: heavy in triangles
          ├─ Crane x 2            girders, trolley, hoist drum, hook (sub-assembly)
          ├─ Cabinet_<t>_<i> x 60 a different variant per area type
          └─ Stair x 4            many steps in one part

With the defaults, the model has about 24 areas, ~400 distinct parts,
~4,900 instances and ~30 M rendered triangles at BigStep's default mesh settings.
"""
import argparse
import math

from OCP.BRep import BRep_Builder
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepPrimAPI import (BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder,
                             BRepPrimAPI_MakeTorus)
from OCP.IFSelect import IFSelect_RetDone
from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_AsIs
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDocStd import TDocStd_Document
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS_Compound
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_ColorSurf, XCAFDoc_DocumentTool
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec

AREA_W, AREA_D = 200_000.0, 80_000.0          # one area: 200 m x 80 m
AREA_TYPES = 4


def loc(x, y, z, rot_deg=0.0):
    r = gp_Trsf()
    r.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), math.radians(rot_deg))
    t = gp_Trsf()
    t.SetTranslation(gp_Vec(x, y, z))
    return TopLoc_Location(t.Multiplied(r))


def box(dx, dy, dz, x=0.0, y=0.0, z=0.0):
    return BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), dx, dy, dz).Shape()


def cyl(r, h, x=0.0, y=0.0, z=0.0, axis=(0, 0, 1)):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, z), gp_Dir(*axis)), r, h).Shape()


def compound(shapes):
    c = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(c)
    for s in shapes:
        b.Add(c, s)
    return c


class Model:
    def __init__(self):
        self.doc = TDocStd_Document(TCollection_ExtendedString("XmlXCAF"))
        XCAFApp_Application.GetApplication_s().InitDocument(self.doc)
        self.st = XCAFDoc_DocumentTool.ShapeTool_s(self.doc.Main())
        self.ct = XCAFDoc_DocumentTool.ColorTool_s(self.doc.Main())
        self.parts = 0

    def name(self, label, text):
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(text))
        return label

    def part(self, shape, text, rgb):
        lab = self.name(self.st.AddShape(shape, False), text)
        self.ct.SetColor(lab, Quantity_Color(*rgb, Quantity_TOC_RGB), XCAFDoc_ColorSurf)
        self.parts += 1
        return lab

    def assembly(self, text):
        return self.name(self.st.NewShape(), text)

    def add(self, parent, child, *where):
        return self.st.AddComponent(parent, child, loc(*where))


def jig_table(m, variant, pins):
    """A 20 m x 6 m plate with a grid of pins (many small cylinders, one part)."""
    nx = max(1, int(math.sqrt(pins * 20 / 6)))
    ny = max(1, pins // nx)
    shapes = [box(20_000, 6_000, 400)]
    r = 30 + 5 * (variant % 4)
    for i in range(nx):
        for j in range(ny):
            shapes.append(cyl(r, 250 + 25 * (variant % 3),
                              (i + 0.5) * 20_000 / nx, (j + 0.5) * 6_000 / ny, 400))
    return m.part(compound(shapes), f"JigTable_{variant}", (0.35, 0.42, 0.45))


def building(m, t):
    w, d, h = AREA_W * 0.55, AREA_D * 0.6, 18_000 + 2_000 * t
    shell = BRepAlgoAPI_Cut(box(w, d, h), box(w - 600, d - 600, h, 300, 300, 0)).Shape()
    roof = box(w + 2_000, d + 2_000, 500, -1_000, -1_000, h)
    return m.part(compound([shell, roof]), f"Building_{t}", (0.16, 0.16, 0.17))


def crane(m, t):
    asm = m.assembly(f"Crane_{t}")
    span = AREA_D * 0.55
    girder = m.part(box(1_500, span, 2_500), f"Crane_{t}_Girder", (0.85, 0.65, 0.1))
    trolley = m.part(box(4_000, 3_000, 1_500), f"Crane_{t}_Trolley", (0.8, 0.2, 0.15))
    drum = m.part(cyl(600, 2_500, 0, 0, 0, (1, 0, 0)), f"Crane_{t}_Drum", (0.3, 0.3, 0.32))
    hook = m.part(BRepPrimAPI_MakeTorus(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)), 500, 120).Shape(),
                  f"Crane_{t}_Hook", (0.9, 0.85, 0.1))
    m.add(asm, girder, 0, 0, 0)
    m.add(asm, girder, 4_500, 0, 0)
    m.add(asm, trolley, 1_000, span * 0.4, 2_500)
    m.add(asm, drum, 1_750, span * 0.4 + 1_500, 4_500)
    m.add(asm, hook, 3_000, span * 0.4 + 1_500, -6_000)
    return asm


def stair(m, t):
    steps = [box(1_200, 300, 200, 0, 280 * k, 180 * k) for k in range(60)]
    return m.part(compound(steps), f"Stair_{t}", (0.55, 0.55, 0.6))


def area_type(m, t, pins):
    asm = m.assembly(f"AreaType_{t}")
    slab = m.part(box(AREA_W, AREA_D, 300, 0, 0, -300), f"Slab_{t}", (0.75, 0.75, 0.75))
    m.add(asm, slab, 0, 0, 0)
    m.add(asm, building(m, t), AREA_W * 0.05, AREA_D * 0.2, 0)
    column = m.part(cyl(300, 12_000), f"Column_{t}", (0.6, 0.62, 0.65))
    for k in range(40):
        m.add(asm, column, AREA_W * 0.65 + (k % 10) * 6_000, AREA_D * 0.15 + (k // 10) * 15_000, 0)
    for k in range(6):
        jig = jig_table(m, t * 2 + k % 2, pins)
        m.add(asm, jig, AREA_W * 0.66 + (k % 2) * 30_000, AREA_D * 0.05 + (k // 2) * 25_000, 0)
    cr = crane(m, t)
    m.add(asm, cr, AREA_W * 0.7, AREA_D * 0.2, 14_000)
    m.add(asm, cr, AREA_W * 0.85, AREA_D * 0.2, 14_000)
    for i in range(60):
        cab = m.part(box(600 + 37 * (i % 11), 400 + 23 * (i % 7), 1_800 + 50 * (i % 5)),
                     f"Cabinet_{t}_{i}", (0.2 + 0.01 * i % 0.6, 0.4, 0.6 - 0.005 * i))
        m.add(asm, cab, AREA_W * 0.08 + (i % 20) * 3_000, AREA_D * 0.85 + (i // 20) * 2_500, 0)
    st = stair(m, t)
    for k in range(4):
        m.add(asm, st, AREA_W * 0.06 + k * 25_000, AREA_D * 0.12, 0, 90 * (k % 2))
    return asm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--areas", type=int, default=24)
    ap.add_argument("--pins", type=int, default=3000, help="pins per jig table")
    a = ap.parse_args()

    m = Model()
    types = [area_type(m, t, a.pins) for t in range(AREA_TYPES)]
    top = m.assembly("Layout")
    cols = 4
    for k in range(a.areas):
        x, y = (k % cols) * (AREA_W + 20_000), (k // cols) * (AREA_D + 20_000)
        m.name(m.add(top, types[k % AREA_TYPES], x, y, 0), f"Area_{k:02d}")
    m.st.UpdateAssemblies()

    w = STEPCAFControl_Writer()
    w.SetColorMode(True)
    w.SetNameMode(True)
    w.Transfer(m.doc, STEPControl_AsIs)
    if w.Write(a.out) != IFSelect_RetDone:
        raise SystemExit("STEP write failed")
    print(f"wrote {a.out}: {a.areas} areas of {AREA_TYPES} types, {m.parts} distinct parts")


if __name__ == "__main__":
    main()

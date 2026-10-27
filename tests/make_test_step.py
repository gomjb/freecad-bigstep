"""Generate a synthetic STEP assembly for testing the BigStep engine.

Run with the engine venv (OCCT 8):
    .venv-occt8/bin/python tests/make_test_step.py out.step --parts 40 --instances 5

Structure:
    Top
     ├─ SubAsm_A  (x2 instances)  ─ boxes / cylinders, colored
     ├─ SubAsm_B                    ─ spheres, repeated instances
     └─ Broken_Part                 ─ solid with an open shell (BRepCheck fails)
"""
import argparse
import math

from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakeSphere
from OCP.IFSelect import IFSelect_RetDone
from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_AsIs
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_FACE
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_ColorSurf, XCAFDoc_DocumentTool
from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec


def loc(x, y, z, rot_deg=0.0):
    t = gp_Trsf()
    t.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), math.radians(rot_deg))
    t2 = gp_Trsf()
    t2.SetTranslation(gp_Vec(x, y, z))
    return TopLoc_Location(t2.Multiplied(t))


def open_shell_solid():
    """A box missing one face, wrapped as a solid -> invalid B-Rep."""
    box = BRepPrimAPI_MakeBox(10, 10, 10).Shape()
    sew = BRepBuilderAPI_Sewing()
    exp = TopExp_Explorer(box, TopAbs_FACE)
    i = 0
    while exp.More():
        if i != 0:
            sew.Add(exp.Current())
        i += 1
        exp.Next()
    sew.Perform()
    shell = TopoDS.Shell(sew.SewedShape())
    return BRepBuilderAPI_MakeSolid(shell).Solid()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--parts", type=int, default=40, help="distinct parts in SubAsm_A")
    ap.add_argument("--instances", type=int, default=5, help="instances of each sphere in SubAsm_B")
    a = ap.parse_args()

    doc = TDocStd_Document(TCollection_ExtendedString("XmlXCAF"))
    XCAFApp_Application.GetApplication_s().InitDocument(doc)
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())

    def named(label, name):
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))
        return label

    top = named(st.NewShape(), "Top")
    asm_a = named(st.NewShape(), "SubAsm_A")
    asm_b = named(st.NewShape(), "SubAsm_B")

    for i in range(a.parts):
        if i % 2:
            shp = BRepPrimAPI_MakeBox(8 + i % 5, 6, 4 + i % 3).Shape()
        else:
            shp = BRepPrimAPI_MakeCylinder(2 + i % 4, 10 + i % 7).Shape()
        lab = named(st.AddShape(shp, False), f"Part_{i:04d}")
        hue = i / max(1, a.parts)
        ct.SetColor(lab, Quantity_Color(hue, 1 - hue, 0.4, Quantity_TOC_RGB), XCAFDoc_ColorSurf)
        st.AddComponent(asm_a, lab, loc((i % 10) * 20, (i // 10) * 20, 0))

    sphere = named(st.AddShape(BRepPrimAPI_MakeSphere(5).Shape(), False), "Ball")
    ct.SetColor(sphere, Quantity_Color(0.9, 0.8, 0.1, Quantity_TOC_RGB), XCAFDoc_ColorSurf)
    for j in range(a.instances):
        st.AddComponent(asm_b, sphere, loc(j * 15, 0, 0))

    broken = named(st.AddShape(open_shell_solid(), False), "Broken_Part")

    st.AddComponent(top, asm_a, loc(0, 0, 0))
    st.AddComponent(top, asm_a, loc(0, 0, 60, 90))
    st.AddComponent(top, asm_b, loc(0, -40, 0))
    st.AddComponent(top, broken, loc(-30, -30, 0))
    st.UpdateAssemblies()

    w = STEPCAFControl_Writer()
    w.SetColorMode(True)
    w.SetNameMode(True)
    w.Transfer(doc, STEPControl_AsIs)
    if w.Write(a.out) != IFSelect_RetDone:
        raise SystemExit("STEP write failed")
    print(f"wrote {a.out}: {a.parts} parts x2, 1 ball x{a.instances}, 1 broken part")


if __name__ == "__main__":
    main()

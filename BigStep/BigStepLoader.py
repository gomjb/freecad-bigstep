"""Build a FreeCAD document from a BigStep manifest.

Document layout (object count ~ distinct parts + assembly components, not
total instances, because repeated parts and sub-assemblies are App::Links):

    <StepName>            App::Part, visible: one link per component of each
                          root assembly ("area"), or to the root itself
    BigStep_Preview       merged meshes of the whole model for fast viewing
                          (BigStepPreview.py); areas are shown either there
                          or as their per-part link
    BigStep_Library       group, hidden
      Parts               Mesh::Feature per part (preview); Part::Feature once
                          the user loads the real B-Rep
      Assemblies          App::Part per assembly definition, holding Links

Only App/Mesh/Part are used here, so this also works in FreeCADCmd.
"""
import json
import os
import re

import FreeCAD as App
import Mesh
import Part

GROUP = "BigStep"


class Report:
    def __init__(self):
        self.loaded, self.skipped, self.links, self.preview = 0, [], 0, False

    def __str__(self):
        return (f"{self.loaded} parts, {self.links} links, "
                f"{len(self.skipped)} parts skipped (failed in engine)"
                + (", fast preview" if self.preview else ""))


def _matrix(m):
    return App.Placement(App.Matrix(m[0], m[1], m[2], m[3],
                                    m[4], m[5], m[6], m[7],
                                    m[8], m[9], m[10], m[11],
                                    0, 0, 0, 1))


def _tag(obj, **props):
    for key, val in props.items():
        name = "BigStep" + key
        if name not in obj.PropertiesList:
            obj.addProperty("App::PropertyString", name, GROUP, "", 1)  # read-only
        setattr(obj, name, str(val))


def set_color(obj, rgb):
    vo = getattr(obj, "ViewObject", None)
    if vo is None or not rgb:
        return
    color = tuple(float(c) for c in rgb)
    if hasattr(vo, "ShapeAppearance"):           # FreeCAD >= 1.0
        mat = App.Material()
        mat.DiffuseColor = color
        vo.ShapeAppearance = [mat]
    elif hasattr(vo, "ShapeColor"):
        vo.ShapeColor = color


def _doc_name(path):
    return re.sub(r"\W", "_", os.path.splitext(os.path.basename(path))[0]) or "BigStep"


def load_manifest(path, doc=None, progress=None, preview=True):
    """Create the lightweight (mesh) document. Returns (doc, Report).
    preview: show the model through its merged preview, if the result has one."""
    with open(path) as f:
        man = json.load(f)
    base = os.path.dirname(os.path.abspath(path))
    src = man.get("source", path)
    doc = doc or App.newDocument(_doc_name(src))
    undo, doc.UndoMode = doc.UndoMode, 0
    rep = Report()
    try:
        lib = doc.addObject("App::DocumentObjectGroup", "BigStep_Library")
        parts_grp = doc.addObject("App::DocumentObjectGroup", "Parts")
        asm_grp = doc.addObject("App::DocumentObjectGroup", "Assemblies")
        lib.addObjects([parts_grp, asm_grp])
        _tag(lib, Manifest=os.path.abspath(path), Source=src,
             EngineOCCT=man.get("engine_occt"))

        objs, parts = {}, man["parts"]
        total = len(parts) + len(man["assemblies"])
        new_parts = []
        for i, (pid, info) in enumerate(parts.items()):
            if progress and i % 100 == 0:
                progress(i, total, "parts")
            mesh = info.get("mesh")
            if not mesh or info.get("status") not in ("ok", "healed", "invalid"):
                rep.skipped.append((pid, info.get("name"), info.get("status"), info.get("msg")))
                continue
            o = doc.addObject("Mesh::Feature", "M_" + pid)
            o.Label = info.get("name") or pid
            o.Mesh = Mesh.Mesh(os.path.join(base, mesh))
            _tag(o, Id=pid, Brep=os.path.join(base, info.get("brep", "")),
                 Status=info.get("status"), Color=json.dumps(info.get("color")))
            set_color(o, info.get("color"))
            new_parts.append(o)
            objs[pid] = o
            rep.loaded += 1
        parts_grp.addObjects(new_parts)

        asms = man["assemblies"]

        def build(aid):                       # children first, each definition once
            if aid in objs:
                return objs[aid]
            info = asms[aid]
            p = doc.addObject("App::Part", "A_" + aid)
            p.Label = info.get("name") or aid
            objs[aid] = p
            links = []
            for child in info["children"]:
                tgt = objs.get(child["ref"]) or (build(child["ref"]) if child["ref"] in asms
                                                 else None)
                if tgt is None:
                    continue
                links.append(_link(doc, tgt, child))
            p.addObjects(links)
            rep.links += len(links)
            asm_grp.addObject(p)
            if progress:
                progress(len(parts) + len(objs), total, "assemblies")
            return p

        top = doc.addObject("App::Part", _doc_name(src))
        top.Label = os.path.basename(src)
        # The components of each root assembly ("areas", e.g. buildings) are linked
        # directly under the top object, so each can be shown or hidden on its own
        # (the fast preview swaps areas in and out).
        areas = []
        for ri, r in enumerate(man["roots"]):
            if r["ref"] in asms:
                for ci, ch in enumerate(asms[r["ref"]]["children"]):
                    tgt = objs.get(ch["ref"]) or (build(ch["ref"]) if ch["ref"] in asms else None)
                    if tgt is None:
                        continue
                    lk = _link(doc, tgt, ch)
                    lk.Placement = _matrix(r["matrix"]).multiply(_matrix(ch["matrix"]))
                    _tag(lk, Area=f"{ri}/{ci}")
                    areas.append(lk)
            else:
                tgt = objs.get(r["ref"])
                if tgt is not None:
                    lk = _link(doc, tgt, r)
                    _tag(lk, Area=f"{ri}")
                    areas.append(lk)
        top.addObjects(areas)
        rep.links += len(areas)

        pv = man.get("preview")
        index = os.path.join(base, pv["index"]) if pv else ""
        if preview and index and os.path.exists(index):
            import BigStepPreview
            BigStepPreview.add_preview(doc, index)
            rep.preview = True

        for o in list(objs.values()) + [lib]:   # links still render hidden targets
            o.Visibility = False
        # Nothing here needs computing (meshes are data, links only reference), and a
        # full recompute of tens of thousands of objects costs minutes in the GUI.
        for o in doc.Objects:
            o.purgeTouched()
    finally:
        doc.UndoMode = undo
    return doc, rep


def _link(doc, target, entry):
    lk = doc.addObject("App::Link", "L")
    lk.LinkedObject = target
    lk.Label = entry.get("name") or target.Label
    lk.Placement = _matrix(entry["matrix"])
    return lk


# --- switching a part definition between mesh preview and real B-Rep --------

def part_definition(obj):
    """Map any selected object (link, nested link, part) to its BigStep definition."""
    if obj is None:
        return None
    leaf = obj.getLinkedObject(True)
    return leaf if "BigStepId" in leaf.PropertiesList else None


def definitions_under(obj, acc=None):
    """All part definitions reachable from an assembly / link."""
    acc = {} if acc is None else acc
    leaf = obj.getLinkedObject(True)
    if "BigStepId" in leaf.PropertiesList:
        acc[leaf.BigStepId] = leaf
    for child in getattr(leaf, "Group", []) or []:
        definitions_under(child, acc)
    return acc


def _retarget(old, new):
    n = 0
    for user in old.InList:
        if user.TypeId == "App::Link" and user.LinkedObject == old:
            user.LinkedObject = new
            n += 1
    return n


def _twin(obj, kind):
    """The mesh<->brep counterpart of a definition, if it was already created."""
    name = ("B_" if kind == "brep" else "M_") + obj.BigStepId
    return obj.Document.getObject(name)


def use_brep(obj):
    """Point every link of this part at a Part::Feature with the real B-Rep."""
    if obj.TypeId == "Part::Feature":
        return obj
    brep = _twin(obj, "brep")
    if brep is None:
        shape = Part.Shape()
        shape.read(obj.BigStepBrep)
        brep = obj.Document.addObject("Part::Feature", "B_" + obj.BigStepId)
        brep.Shape = shape
        brep.Label = obj.Label + " (B-Rep)"
        _tag(brep, Id=obj.BigStepId, Brep=obj.BigStepBrep, Status=obj.BigStepStatus,
             Color=obj.BigStepColor)
        for grp in obj.InList:
            if grp.TypeId == "App::DocumentObjectGroup":
                grp.addObject(brep)
        set_color(brep, json.loads(obj.BigStepColor))
        brep.Visibility = False
    _retarget(obj, brep)
    return brep


def use_mesh(obj):
    if obj.TypeId != "Part::Feature":
        return obj
    mesh = _twin(obj, "mesh")
    if mesh is not None:
        _retarget(obj, mesh)
    return mesh

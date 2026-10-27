"""Fast display of a whole BigStep model: merged meshes drawn as a few big VBOs.

The per-part document (links to Mesh::Features) stays as is and is what you
select and edit.  For speed, each *area* (a component of a root assembly, see
engine/preview.py) is shown either

    preview   its merged chunks, drawn here (not pickable), the area's link hidden
    parts     its link, i.e. the normal selectable per-part view

`BigStep_Preview.DetailAreas` lists the areas currently shown as parts.
"""
import json
import os

import FreeCAD as App

PREVIEW_NAME = "BigStep_Preview"


class PreviewFeature:
    def __init__(self, obj, index_path):
        obj.Proxy = self
        obj.addProperty("App::PropertyString", "BigStepPreviewIndex", "BigStep",
                        "preview/index.json of the result", 1)
        obj.addProperty("App::PropertyStringList", "DetailAreas", "BigStep",
                        "areas shown as selectable parts instead of the fast preview")
        obj.BigStepPreviewIndex = index_path

    def execute(self, obj):
        pass

    def dumps(self):
        return None

    def loads(self, state):
        return None


class ViewProviderPreview:
    def __init__(self, vobj):
        vobj.Proxy = self

    def attach(self, vobj):
        from pivy import coin
        self.obj = vobj.Object
        self.root = coin.SoSeparator()
        self.switches = {}
        try:
            self._build(coin)
        except Exception as e:          # missing/corrupt preview: keep the document usable
            App.Console.PrintWarning(f"BigStep: preview not shown: {type(e).__name__}: {e}\n")
        vobj.addDisplayMode(self.root, "Preview")
        self._apply_detail()

    def _build(self, coin):
        import numpy as np
        import PartGui  # noqa: F401  registers SoBrepFaceSet (VBO-capable face set)

        path = self.obj.BigStepPreviewIndex
        with open(path) as f:
            index = json.load(f)
        base = os.path.dirname(path)
        pick = coin.SoPickStyle()
        pick.style = coin.SoPickStyle.UNPICKABLE       # picking millions of triangles stalls the UI
        self.root.addChild(pick)
        for area in index["areas"]:
            sw = coin.SoSwitch()
            sw.whichChild = coin.SO_SWITCH_ALL
            for ci in area["chunks"]:
                sw.addChild(self._chunk(coin, np, base, index["chunks"][ci]))
            self.switches[area["key"]] = sw
            self.root.addChild(sw)

    @staticmethod
    def _chunk(coin, np, base, info):
        z = np.load(os.path.join(base, info["file"]))
        v = z["coords"][z["tris"]].reshape(-1, 3)          # flat shading: 3 vertices per triangle
        fn = np.cross(v[1::3] - v[0::3], v[2::3] - v[0::3])
        fn /= np.maximum(np.linalg.norm(fn, axis=1, keepdims=True), 1e-20)
        normals = np.repeat(fn.astype(np.float32), 3, axis=0)
        sep = coin.SoSeparator()
        tr = coin.SoTranslation()
        tr.translation.setValue(*info["origin"])
        mat = coin.SoMaterial()
        colors = z["colors"] if "colors" in z else np.array([info["color"]], np.float32)
        mat.diffuseColor.setValues(0, len(colors), colors)
        mb = coin.SoMaterialBinding()
        mb.value = coin.SoMaterialBinding.PER_PART   # one color per instance (partIndex entry)
        coords = coin.SoCoordinate3()
        coords.point.setValues(0, len(v), v)
        nrm = coin.SoNormal()
        nrm.vector.setValues(0, len(normals), normals)
        nb = coin.SoNormalBinding()
        nb.value = coin.SoNormalBinding.PER_VERTEX_INDEXED
        idx = np.hstack([np.arange(len(v), dtype=np.int32).reshape(-1, 3),
                         np.full((len(v) // 3, 1), -1, np.int32)]).ravel()
        parts = z["part_tris"] if "part_tris" in z else np.array([len(v) // 3], np.int32)
        node = coin.SoType.fromName("SoBrepFaceSet").createInstance()
        coin.cast(node, "SoIndexedFaceSet").coordIndex.setValues(0, len(idx), idx)
        coin.cast(node.getField("partIndex"), "SoMFInt32").setValues(0, len(parts), parts)
        for n in (tr, mat, mb, coords, nrm, nb, node):
            sep.addChild(n)
        return sep

    def _apply_detail(self):
        from pivy import coin
        detail = set(self.obj.DetailAreas)
        for key, sw in self.switches.items():
            sw.whichChild = coin.SO_SWITCH_NONE if key in detail else coin.SO_SWITCH_ALL

    def updateData(self, obj, prop):
        if prop == "DetailAreas" and hasattr(self, "switches"):
            self._apply_detail()

    def getDisplayModes(self, vobj):
        return ["Preview"]

    def getDefaultDisplayMode(self):
        return "Preview"

    def setDisplayMode(self, mode):
        return mode

    def onChanged(self, vobj, prop):
        pass

    def getIcon(self):
        from BigStepPrefs import ADDON_DIR
        return os.path.join(ADDON_DIR, "Resources", "icons", "BigStep.svg")

    def dumps(self):
        return None

    def loads(self, state):
        return None


# --- document side -----------------------------------------------------------

def area_links(doc):
    """area key -> the link object that shows that area as parts."""
    return {o.BigStepArea: o for o in doc.Objects
            if o.TypeId == "App::Link" and "BigStepArea" in o.PropertiesList}


def preview_object(doc):
    return doc.getObject(PREVIEW_NAME)


def add_preview(doc, index_path, parent=None):
    """Create the preview object and hide every area's per-part link."""
    obj = doc.addObject("App::FeaturePython", PREVIEW_NAME)
    PreviewFeature(obj, index_path)
    obj.Label = "Fast preview"
    if App.GuiUp:
        ViewProviderPreview(obj.ViewObject)
    if parent is not None:
        parent.addObject(obj)
    set_detail(doc, [], replace=True)
    obj.purgeTouched()
    return obj


def set_detail(doc, keys, on=True, replace=False):
    """Show the given areas as selectable parts (on) or as fast preview (off)."""
    pv = preview_object(doc)
    if pv is None:
        return []
    cur = set() if replace else set(pv.DetailAreas)
    cur = cur | set(keys) if on else cur - set(keys)
    links = area_links(doc)
    for key, lk in links.items():
        vis = key in cur
        if lk.Visibility != vis:
            lk.Visibility = vis
            lk.purgeTouched()
    pv.DetailAreas = sorted(cur)
    pv.purgeTouched()
    return sorted(cur)


def _index(pv):
    with open(pv.BigStepPreviewIndex) as f:
        return json.load(f)


def areas_for(doc, objs_with_paths):
    """Areas containing the selection. objs_with_paths: [(obj, [objects on its tree path])]."""
    pv = preview_object(doc)
    if pv is None:
        return []
    keys = set()
    defs = set()
    for obj, path in objs_with_paths:
        hit = [o.BigStepArea for o in path + [obj] if "BigStepArea" in o.PropertiesList]
        if hit:
            keys.add(hit[0])
            continue
        if obj.Name == PREVIEW_NAME:
            continue
        under = [o.BigStepArea for o in obj.OutListRecursive if "BigStepArea" in o.PropertiesList]
        if under and not obj.Name.startswith("A_"):    # the model's top object or a root link
            keys.update(under)
            continue
        leaf = obj.getLinkedObject(True)
        if "BigStepId" in leaf.PropertiesList:
            defs.add(leaf.BigStepId)
        elif leaf.Name.startswith("A_"):
            defs.add(leaf.Name[2:])
    if defs:
        for area in _index(pv)["areas"]:
            if defs & set(area["defs"]):
                keys.add(area["key"])
    return sorted(keys)

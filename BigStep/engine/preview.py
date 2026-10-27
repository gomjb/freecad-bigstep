"""Stage 3: merged display meshes for fast viewing (numpy only, no OCCT).

FreeCAD's 3D view (Coin3D) walks every object on one CPU core each frame; at
thousands of instances that, not the triangle count, limits the frame rate.
This stage bakes all instances of an area into a few large meshes (split at
CHUNK_TRIS), so the viewer issues tens of draw calls instead of thousands.
Colors stay per part instance (drawn with per-part material binding), so many
colors do not multiply the number of chunks.

    out/preview/index.json        areas, chunks, and which definitions each area uses
    out/preview/chunk_NNNN.npz    coords (n,3) f4 relative to `origin`, tris (m,3) i4,
                                  part_tris (k,) i4 triangles per instance (in order),
                                  colors (k,3) f4 one color per instance

An *area* is one component of a root assembly (e.g. one building), the unit
the addon switches between this preview and the selectable per-part view.

Parts with many triangles are simplified by vertex clustering (cell = `lod`
x part diagonal, never more than half the part's thinnest extent, so slabs and
plates keep their faces).  Simple parts are copied as is.
"""
import json
import os
from concurrent.futures import ProcessPoolExecutor

import numpy as np

PREVIEW_VERSION = 2
CHUNK_TRIS = 4_000_000
LOD_MIN_TRIS = 20_000          # below this a part is kept as tessellated
_STL = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])


def read_stl(path):
    with open(path, "rb") as f:
        f.seek(80)
        n = int(np.frombuffer(f.read(4), "<u4")[0])
        return np.frombuffer(f.read(n * 50), _STL)["v"].reshape(-1, 3).astype(np.float64)


def simplify(v, lod):
    """Triangle soup (3n,3) -> (points, tris), clustered when the part is heavy."""
    n = len(v) // 3
    if lod > 0 and n >= LOD_MIN_TRIS:
        lo, hi = v.min(0), v.max(0)
        cell = min(np.linalg.norm(hi - lo) * lod, (hi - lo).min() * 0.5)
        if cell > 0:
            key = np.floor((v - lo) / cell).astype(np.int64)
            _, inv = np.unique(key, axis=0, return_inverse=True)
            inv = inv.reshape(-1, 3)
            cnt = np.bincount(inv.ravel())
            pts = np.zeros((len(cnt), 3))
            np.add.at(pts, inv.ravel(), v)
            pts /= cnt[:, None]
            keep = (inv[:, 0] != inv[:, 1]) & (inv[:, 1] != inv[:, 2]) & (inv[:, 0] != inv[:, 2])
            return pts, inv[keep].astype(np.int32)
    pts, inv = np.unique(v, axis=0, return_inverse=True)
    return pts, inv.reshape(-1, 3).astype(np.int32)


def _load_part(args):
    path, lod = args
    pts, tris = simplify(read_stl(path), lod)
    return pts.astype(np.float32), tris


def _matrix(m):
    M = np.eye(4)
    M[:3, :] = np.asarray(m, dtype=np.float64).reshape(3, 4)
    return M


def areas_of(manifest):
    """[(key, name, ref, matrix 4x4)]: components of each root assembly, or the root."""
    A = manifest["assemblies"]
    out = []
    for ri, root in enumerate(manifest["roots"]):
        R = _matrix(root["matrix"])
        if root["ref"] in A:
            for ci, ch in enumerate(A[root["ref"]]["children"]):
                out.append((f"{ri}/{ci}", ch.get("name") or ch["ref"], ch["ref"],
                            R @ _matrix(ch["matrix"])))
        else:
            out.append((f"{ri}", root.get("name") or root["ref"], root["ref"], R))
    return out


def build(out_dir, manifest, jobs=4, lod=0.005, progress=None):
    base = out_dir
    P, A = manifest["parts"], manifest["assemblies"]
    pdir = os.path.join(out_dir, "preview")
    os.makedirs(pdir, exist_ok=True)
    for f in os.listdir(pdir):
        if f.startswith("chunk_"):
            os.remove(os.path.join(pdir, f))

    pids = [p for p, info in P.items() if info.get("mesh")]
    meshes = {}
    with ProcessPoolExecutor(max_workers=max(1, jobs)) as ex:
        args = [(os.path.join(base, P[p]["mesh"]), lod) for p in pids]
        for i, (pid, res) in enumerate(zip(pids, ex.map(_load_part, args, chunksize=4))):
            meshes[pid] = res
            if progress and i % 50 == 0:
                progress(i, len(pids))

    areas, chunks = [], []
    for key, name, ref, M0 in areas_of(manifest):
        items, defs = [], set()

        def walk(r, M, color):
            defs.add(r)
            if r in A:
                for ch in A[r]["children"]:
                    walk(ch["ref"], M @ _matrix(ch["matrix"]), ch.get("color") or color)
                return
            if r not in meshes or not len(meshes[r][1]):
                return
            pts, tris = meshes[r]
            c = P[r].get("color") or color or (0.8, 0.8, 0.8)
            items.append((pts @ M[:3, :3].T + M[:3, 3], tris, c))

        walk(ref, M0, None)
        area = {"key": key, "name": name, "ref": ref, "defs": sorted(defs), "chunks": []}
        start = 0
        while start < len(items):
            cnt, end = 0, start
            while end < len(items) and (cnt == 0 or cnt + len(items[end][1]) <= CHUNK_TRIS):
                cnt += len(items[end][1])
                end += 1
            group = items[start:end]
            origin = np.min([p.min(0) for p, _, _ in group], axis=0)
            coords, tris, off = [], [], 0
            for p, t, _ in group:
                coords.append((p - origin).astype(np.float32))
                tris.append(t + off)
                off += len(p)
            coords = np.concatenate(coords)
            fname = f"chunk_{len(chunks):04d}.npz"
            np.savez(os.path.join(pdir, fname), coords=coords, tris=np.concatenate(tris),
                     part_tris=np.array([len(t) for _, t, _ in group], np.int32),
                     colors=np.array([c for _, _, c in group], np.float32))
            hi = origin + coords.max(0)
            chunks.append({"file": fname, "area": key, "tris": cnt, "instances": len(group),
                           "origin": origin.tolist(), "bbox": origin.tolist() + hi.tolist()})
            area["chunks"].append(len(chunks) - 1)
            start = end
        areas.append(area)

    index = {"bigstep_preview": PREVIEW_VERSION, "lod": lod, "areas": areas, "chunks": chunks,
             "tris": sum(c["tris"] for c in chunks),
             "source_tris": sum(P[p].get("triangles", 0) for p in pids)}
    tmp = os.path.join(pdir, "index.json.tmp")
    with open(tmp, "w") as f:
        json.dump(index, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(pdir, "index.json"))
    return index

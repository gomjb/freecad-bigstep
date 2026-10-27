"""BigStep engine orchestrator (runs outside FreeCAD, with the OCCT 8 Python env).

    python bigstep_engine.py check
    python bigstep_engine.py run <file.step> <out_dir> [--jobs N] [--deflection F]
                                 [--angular DEG] [--part-timeout S] [--chunk N]
                                 [--lod F] [--no-preview]
    python bigstep_engine.py preview <out_dir> [--jobs N] [--lod F]

Pipeline
    1. occt_read.py   one process: parse STEP, split into parts/<pid>.brep
    2. occt_worker.py N processes: BRepCheck -> ShapeFix -> mesh, per part
    3. preview.py     merged display meshes per area and color (fast 3D view)
    4. manifest.json  everything the FreeCAD addon needs to build the document

Crash isolation: a worker that segfaults or hangs takes down only itself.  The
part it was working on is marked `crashed`/`timeout`, the rest of its batch is
re-queued on a fresh worker.  A part that times out is retried once without
healing, so it still shows up (flagged invalid).  Parts are queued largest
first, so the slow ones overlap with the rest.  This module never imports OCCT.
"""
import argparse
import collections
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import emit, occt_version, open_event_channel, rss_mb  # noqa: E402

PY = [sys.executable, "-E", "-s"]
MANIFEST_VERSION = 1
_children = set()


def _pdeathsig():
    """Linux: kill the child if this orchestrator dies (e.g. FreeCAD was killed)."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except Exception:
        pass


def spawn(args, log_path):
    log = open(log_path, "ab")
    p = subprocess.Popen(PY + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=log, preexec_fn=_pdeathsig, cwd=HERE)
    log.close()
    _children.add(p)
    return p


def pump(proc, tag, q):
    """Forward a child's JSON lines into the event queue; None marks EOF."""
    for raw in proc.stdout:
        try:
            q.put((tag, json.loads(raw)))
        except ValueError:
            pass
    q.put((tag, None))


def on_signal(signum, _frame):
    for p in list(_children):
        try:
            p.kill()
        except OSError:
            pass
    emit("cancelled", msg=f"stopped by signal {signum}")
    sys.exit(130)


def describe_exit(rc):
    if rc is None:
        return "still running"
    if rc < 0:
        try:
            return f"killed by {signal.Signals(-rc).name}"
        except ValueError:
            return f"killed by signal {-rc}"
    return f"exit code {rc}"


def run_read(step, out, logs, transfer):
    q = queue.Queue()
    p = spawn([os.path.join(HERE, "occt_read.py"), step, out, transfer],
              os.path.join(logs, "read.log"))
    p.stdin.close()
    threading.Thread(target=pump, args=(p, "read", q), daemon=True).start()
    t0, peak = time.time(), 0.0
    while True:
        try:
            _, ev = q.get(timeout=2.0)
        except queue.Empty:
            m = rss_mb(p.pid) or 0.0
            peak = max(peak, m)
            emit("heartbeat", stage="read", elapsed_s=round(time.time() - t0, 1), rss_mb=round(m))
            continue
        if ev is None:
            break
        emit(ev.pop("ev"), **ev)
    rc = p.wait()
    _children.discard(p)
    if rc != 0:
        emit("fatal", msg=f"STEP read process failed ({describe_exit(rc)}); see logs/read.log")
        sys.exit(2)
    with open(os.path.join(out, "structure.json")) as f:
        return json.load(f)


class Worker:
    def __init__(self, wid, chunk, out, logs, args, q):
        self.wid, self.chunk = wid, chunk
        self.done, self.current, self.started_at, self.verdict = set(), None, 0.0, None
        self.proc = spawn([os.path.join(HERE, "occt_worker.py"), out,
                           str(args.deflection), str(args.angular)],
                          os.path.join(logs, f"worker-{wid % args.jobs}.log"))
        self.proc.stdin.write(("\n".join(chunk) + "\n").encode())
        self.proc.stdin.close()
        threading.Thread(target=pump, args=(self.proc, wid, q), daemon=True).start()


def by_size(pids, out):
    def size(pid):
        try:
            return os.path.getsize(os.path.join(out, "parts", pid + ".bin"))
        except OSError:
            return 0
    return sorted(pids, key=size, reverse=True)


def run_parts(pids, out, logs, args):
    """Chunks hold stdin lines for occt_worker: `<pid>` or `<pid> noheal`."""
    results, retried = {}, set()
    total = len(pids)
    pids = by_size(pids, out)
    chunk = args.chunk or max(1, min(100, total // (args.jobs * 8) or 1))
    # The largest parts go one per batch, so no worker gets several slow ones in a row.
    solo = min(total, args.jobs * 4)
    pending = collections.deque([p] for p in pids[:solo])
    pending.extend(pids[i:i + chunk] for i in range(solo, total, chunk))
    workers, q, next_id, dead_starts = {}, queue.Queue(), 0, 0
    last_beat = 0.0

    while pending or workers:
        while pending and len(workers) < args.jobs:
            w = Worker(next_id, pending.popleft(), out, logs, args, q)
            workers[next_id] = w
            next_id += 1

        try:
            wid, ev = q.get(timeout=1.0)
        except queue.Empty:
            wid, ev = None, None

        now = time.time()
        for w in workers.values():
            if w.current and w.verdict is None and now - w.started_at > args.part_timeout:
                w.verdict = "timeout"
                w.proc.kill()

        if wid is not None:
            w = workers[wid]
            if ev is None:                               # worker finished or died
                rc = w.proc.wait()
                _children.discard(w.proc)
                del workers[wid]
                if w.current:
                    status = w.verdict or "crashed"
                    msg = (f"exceeded {args.part_timeout:g}s" if status == "timeout"
                           else f"worker {describe_exit(rc)}")
                    if status == "timeout" and w.current not in retried:
                        retried.add(w.current)
                        pending.append([w.current + " noheal"])
                        emit("retry", id=w.current, msg=f"{msg}; retrying without healing")
                    else:
                        results[w.current] = {"status": status, "msg": msg}
                        emit("part", id=w.current, status=status, msg=msg)
                    dead_starts = 0
                elif rc != 0:
                    dead_starts += 1                     # died before touching any part
                    if dead_starts >= 3:
                        emit("fatal", msg=f"worker cannot start ({describe_exit(rc)}); "
                                          "see logs/worker-*.log")
                        sys.exit(3)
                rest = [e for e in w.chunk
                        if e.split()[0] not in w.done | results.keys() | {w.current}]
                if rest:
                    pending.appendleft(rest)
            elif ev["ev"] == "start":
                w.current, w.started_at = ev["id"], time.time()
            elif ev["ev"] == "part":
                pid = ev.pop("id")
                ev.pop("ev"), ev.pop("t", None)
                results[pid] = ev
                w.done.add(pid)
                w.current = None
                emit("part", id=pid, **ev)

        if now - last_beat > 1.0 or not (pending or workers):
            last_beat = now
            mem = sum(rss_mb(w.proc.pid) or 0.0 for w in workers.values())
            emit("progress", done=len(results), total=total, workers=len(workers),
                 rss_mb=round(mem))
    return results


def cmd_run(args):
    out = os.path.abspath(args.out)
    logs = os.path.join(out, "logs")
    os.makedirs(logs, exist_ok=True)
    t0 = time.time()
    emit("begin", step=os.path.abspath(args.step), out=out, occt=occt_version(),
         jobs=args.jobs)

    st = run_read(os.path.abspath(args.step), out, logs, args.transfer)
    parts = st["parts"]
    pids = [p for p, info in parts.items() if info.get("written") and not info.get("empty")]
    for p in parts.keys() - set(pids):
        parts[p].update(status="failed", msg="empty shape in STEP")

    emit("stage", stage="parts", msg=f"Checking, healing, meshing {len(pids)} parts",
         total=len(pids))
    results = run_parts(pids, out, logs, args)
    for pid, res in results.items():
        parts[pid].update(res)

    counts = collections.Counter(p.get("status", "failed") for p in parts.values())
    preview = None
    if not args.no_preview:
        preview = run_preview(out, {"parts": parts, **{k: st[k] for k in ("assemblies", "roots")}},
                              args.jobs, args.lod)
    manifest = {
        "bigstep_manifest": MANIFEST_VERSION,
        "engine_occt": occt_version(),
        "settings": {"deflection": args.deflection, "angular": args.angular,
                     "jobs": args.jobs, "transfer": args.transfer},
        **st,
        "stats": {**st["read_stats"], "status": dict(counts),
                  "total_s": round(time.time() - t0, 2)},
    }
    if preview:
        manifest["preview"] = preview
    path = os.path.join(out, "manifest.json")
    with open(path + ".tmp", "w") as f:
        json.dump(manifest, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)
    emit("done", manifest=path, stats=manifest["stats"])


def run_preview(out, manifest, jobs, lod):
    """Build preview/ (see preview.py); returns the manifest entry, or None on failure."""
    t = time.time()
    emit("stage", stage="preview", msg="Building fast display meshes")
    try:
        import preview
        idx = preview.build(out, manifest, jobs=jobs, lod=lod,
                            progress=lambda i, n: emit("preview_progress", done=i, total=n))
    except Exception as e:                       # the preview is optional
        emit("warning", msg=f"preview not built: {type(e).__name__}: {e}")
        return None
    entry = {"index": "preview/index.json", "areas": len(idx["areas"]),
             "chunks": len(idx["chunks"]), "tris": idx["tris"],
             "source_tris": idx["source_tris"], "lod": lod, "secs": round(time.time() - t, 1)}
    emit("preview_done", **entry)
    return entry


def cmd_preview(args):
    out = os.path.abspath(args.out)
    path = os.path.join(out, "manifest.json")
    with open(path) as f:
        manifest = json.load(f)
    entry = run_preview(out, manifest, args.jobs, args.lod)
    if entry is None:
        sys.exit(4)
    manifest["preview"] = entry
    with open(path + ".tmp", "w") as f:
        json.dump(manifest, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)
    emit("done", manifest=path, stats=manifest.get("stats"))


def cmd_check(_args):
    emit("check", python=sys.version.split()[0], executable=sys.executable,
         occt=occt_version())


def main():
    open_event_channel()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    ap = argparse.ArgumentParser(prog="bigstep_engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    r = sub.add_parser("run")
    r.add_argument("step")
    r.add_argument("out")
    r.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    r.add_argument("--deflection", type=float, default=0.002,
                   help="linear mesh deflection as a fraction of each part's bbox diagonal")
    r.add_argument("--angular", type=float, default=20.0, help="angular deflection, degrees")
    r.add_argument("--part-timeout", type=float, default=1200.0,
                   help="seconds before a single part is declared hung")
    r.add_argument("--transfer", choices=("fast", "full"), default="fast",
                   help="fast: skip OCCT's single-threaded healing during STEP transfer "
                        "(workers heal in parallel); full: OCCT default")
    r.add_argument("--chunk", type=int, default=0, help="parts per worker batch (0 = auto)")
    r.add_argument("--no-preview", action="store_true", help="skip the fast display meshes")
    pv = sub.add_parser("preview", help="(re)build the fast display meshes of a result")
    pv.add_argument("out")
    for sp in (r, pv):
        sp.add_argument("--lod", type=float, default=0.005,
                        help="simplification of heavy parts in the fast view, as a fraction "
                             "of each part's diagonal (0 = none)")
    pv.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    args = ap.parse_args()
    {"run": cmd_run, "check": cmd_check, "preview": cmd_preview}[args.cmd](args)


if __name__ == "__main__":
    main()

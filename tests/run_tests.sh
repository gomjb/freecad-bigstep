#!/usr/bin/env bash
# End-to-end checks: engine (with injected crash + hang) and headless FreeCAD load.
#   tests/run_tests.sh [/path/to/FreeCAD/AppRun]
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY="$HERE/.venv-occt8/bin/python"
FC="${1:-$HOME/Applications/FreeCAD-1.1.4/AppRun}"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

"$PY" "$HERE/tests/make_test_step.py" "$T/t.step" --parts 40 --instances 5 >/dev/null 2>&1
BIGSTEP_TEST_CRASH=p000007 BIGSTEP_TEST_HANG=p000013 BIGSTEP_TEST_HANG_ALWAYS=p000020 \
  "$PY" -E -s "$HERE/BigStep/engine/bigstep_engine.py" run "$T/t.step" "$T/out" \
  --jobs 4 --chunk 6 --part-timeout 5 > "$T/events.jsonl"

python3 - "$T" <<'EOF'
import json, sys
t = sys.argv[1]
m = json.load(open(f"{t}/out/manifest.json"))
st = {k: v.get("status") for k, v in m["parts"].items()}
assert st["p000007"] == "crashed", st["p000007"]
assert st["p000013"] == "ok", st["p000013"]          # hung while healing, retried unhealed
assert st["p000020"] == "timeout", st["p000020"]     # hangs on the retry too
broken = next(k for k, v in m["parts"].items() if v["name"] == "Broken_Part")
# fast transfer: OCCT no longer heals during transfer, so the worker must
assert m["stats"]["transfer_healing"] == "off", m["stats"]
assert st[broken] == "healed", st[broken]
assert sum(s == "ok" for s in st.values()) == 39, m["stats"]
assert m["engine_occt"].startswith("8."), m["engine_occt"]
pv = json.load(open(f"{t}/out/preview/index.json"))
assert len(pv["areas"]) == 4 and pv["tris"] > 0 and m["preview"]["chunks"] == len(pv["chunks"]), m["preview"]
print("engine OK:", m["stats"]["status"], "OCCT", m["engine_occt"])
EOF

cat > "$T/load.py" <<EOF
import sys; sys.path.insert(0, "$HERE/BigStep")
import BigStepLoader as L
doc, rep = L.load_manifest("$T/out/manifest.json")
assert rep.loaded == 40 and len(rep.skipped) == 2, rep
import BigStepPreview as PV
assert rep.preview and PV.preview_object(doc) is not None, rep
areas = PV.area_links(doc)
assert sorted(areas) == ["0/0", "0/1", "0/2", "0/3"], sorted(areas)
assert not any(l.Visibility for l in areas.values())         # all areas start in the fast preview
assert PV.set_detail(doc, ["0/1"]) == ["0/1"] and areas["0/1"].Visibility
PV.set_detail(doc, ["0/1"], on=False)
assert not areas["0/1"].Visibility
ball = [o for o in doc.Objects if o.Label == "Ball"][0]
b = L.use_brep(ball)
assert b.Shape.isValid() and len([u for u in b.InList if u.TypeId == "App::Link"]) == 5
print("FREECAD_OK", rep)
EOF
"$FC" freecadcmd "$T/load.py" 2>&1 | grep FREECAD_OK

import json
from pathlib import Path

WANT = [
    "MOM_V1_baseline",
    "MOM_LONG_only",
    "MOM_LONG_lowpb<=0.3",
    "MOM_LONG_trend>=10",
    "MOM_left_c5s_LONG_entry+5s",
    "MOM_left_c5s_late_entry+5s",
    "PC_V1_baseline",
    "PC_loose_after_LONGslice",
]

for split in ("train", "holdout"):
    p = Path(f"results/MOMENTUM_SLICE/entry_sweep/validate_{split}.json")
    d = json.loads(p.read_text(encoding="utf-8"))
    print(f"=== {split} ===")
    for name in WANT:
        res = d["cases"].get(name)
        if not res:
            # may be in prior overwrite - skip
            print(f"MISSING {name}")
            continue
        a = res.get("all") or {}
        eq = res.get("equity") or []
        print(f"{name}|n={a.get('n')}|net={a.get('net')}|pf={a.get('pf')}|dd={a.get('max_dd')}")
        print("EQ|" + ",".join(str(x) for x in eq))

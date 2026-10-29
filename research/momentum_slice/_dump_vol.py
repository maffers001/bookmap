import csv, statistics as st
from research.momentum_slice.rules import enrich, f
for label, path in (("train","results/MOMENTUM_SLICE/features/features.csv"),("holdout","results/MOMENTUM_SLICE/holdout/features.csv")):
    rows=[enrich(r) for r in csv.DictReader(open(path,encoding="utf-8"))]
    print("==",label,len(rows))
    for k in ("range_5m_pts","range_1m_pts","med_min_range_pts","compression_1m_5m","prior_run_5m_pts"):
        xs=[f(r,k) for r in rows]; xs=[x for x in xs if x is not None]
        xs.sort()
        if xs:
            q=lambda p: xs[min(len(xs)-1,int(p*len(xs)))]
            print(k, f"min{xs[0]:.1f} p25{q(.25):.1f} med{st.median(xs):.1f} p75{q(.75):.1f} max{xs[-1]:.1f}")

"""Re-verify RULE_SPEC_V1 once on train tags; write features/freeze_verify.md."""
from __future__ import annotations

import statistics as st
from collections import Counter
from pathlib import Path

from research.momentum_slice.rules import enrich, f, is_momentum_v1, is_pc_v1
from research.momentum_slice.taglib import BASE, load_tagged_features


def stats(rows, key="ret10s"):
    xs = [f(r, key) for r in rows if f(r, key) is not None]
    if not xs:
        return "n=0"
    return (f"n={len(xs)} {key} {st.mean(xs):+.1f}/{st.median(xs):+.1f} "
            f"P>5 {sum(x > 5 for x in xs)/len(xs):.2f} P<-5 {sum(x < -5 for x in xs)/len(xs):.2f}")


def main():
    rows = [enrich(r) for r in load_tagged_features()]
    tagged = [r for r in rows if r.get("lst_tag") in ("TRUE", "FALSE")]
    mom = [r for r in rows if is_momentum_v1(r)]
    pc = [r for r in rows if is_pc_v1(r)]
    lines = [
        "# RULE_SPEC_V1 train re-verify", "",
        f"candidates={len(rows)} tagged={dict(Counter(r['lst_tag'] for r in tagged))}",
        f"MOMENTUM_V1 fires {len(mom)} "
        f"(tagged {dict(Counter(r.get('lst_tag') for r in mom if r.get('lst_tag')))})",
        f"PC_V1 fires {len(pc)} "
        f"(tagged {dict(Counter(r.get('lst_tag') for r in pc if r.get('lst_tag')))})",
        "",
        "## Momentum",
        stats(mom),
        f"  late: {stats([r for r in mom if r.get('tod')=='late'])}",
        f"  tagged TRUE rate: "
        f"{sum(1 for r in mom if r.get('lst_tag')=='TRUE')}/"
        f"{sum(1 for r in mom if r.get('lst_tag') in ('TRUE','FALSE'))}",
        "",
        "## PC (continuation label — expect negative; fade is opposite)",
        stats(pc),
        f"  late: {stats([r for r in pc if r.get('tod')=='late'])}",
        "",
        "Thresholds locked in RULE_SPEC_V1.md / rules.py. Holdout next.",
    ]
    text = "\n".join(lines) + "\n"
    (BASE / "features/freeze_verify.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()

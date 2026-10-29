"""Rank MOMENTUM_SLICE candidates by ex-ante score; tier stats (no outcome in score).

  .venv\\Scripts\\python.exe -m research.momentum_slice.analyse results/MOMENTUM_SLICE/scan_sep_oct
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path


def stats(rows: list[dict]) -> list[str]:
    if not rows:
        return ["n=0"]
    n = len(rows)
    r10 = [r["ret10s"] for r in rows]
    r60 = [r["ret60s"] for r in rows]
    mfe = [r["mfe"] for r in rows]
    mae = [r["mae"] for r in rows]
    mean = lambda xs: sum(xs) / len(xs)
    med = lambda xs: sorted(xs)[len(xs) // 2]
    return [
        f"n={n}  L={sum(r['side'] == 'LONG' for r in rows)} S={sum(r['side'] == 'SHORT' for r in rows)}",
        (f"ret10s mean {mean(r10):+.1f}  med {med(r10):+.1f}  "
         f"P(>5) {sum(x > 5 for x in r10) / n:.2f}  P(<-5) {sum(x < -5 for x in r10) / n:.2f}"),
        f"ret60s mean {mean(r60):+.1f}  MFE {mean(mfe):.1f}  MAE {mean(mae):.1f}",
    ]


def main():
    d = Path(sys.argv[1] if len(sys.argv) > 1 else "results/MOMENTUM_SLICE/scan_sep_oct")
    with open(d / "candidates.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in list(r):
            if k in ("session", "side", "ignition_bst", "confirm_bst"):
                continue
            try:
                r[k] = float(r[k])
            except (ValueError, TypeError):
                pass
        r["min_consume"] = min(r["nq_consume"], r["es_consume"])
        r["min_density"] = min(r["nq_density"], r["es_density"])
        r["score"] = min(r["min_consume"], 1.2) + 0.5 * r["min_density"]
        r["tierA"] = (
            r["nq_consume"] >= 0.5 and r["es_consume"] >= 0.5
            and r["nq_density"] >= 1.2 and r["es_density"] >= 1.0
            and r["nq_persist"] >= 0.7 and r["es_persist"] >= 0.7
        )
    rows.sort(key=lambda r: -r["score"])
    with open(d / "candidates_ranked.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    sessions = len({r["session"] for r in rows})
    lines: list[str] = [
        "# Liquidity Slice-Through — scan summary",
        "",
        f"sessions scanned: {sessions}",
        f"cross-confirmed candidates: {len(rows)}",
        "",
        "## ALL",
        *stats(rows),
        "",
        "## TierA (consume>=0.5 both, NQ dens>=1.2, ES dens>=1.0, persist>=0.7 both)",
        *stats([r for r in rows if r["tierA"]]),
        "",
        "## Rest",
        *stats([r for r in rows if not r["tierA"]]),
        "",
        "## Top 20 by ex-ante score",
        "",
        "Bookmap replay lookup = ignition_bst (BST).",
        "",
        # HTML table: Cursor's GFM preview often clips wide pipe-tables out of view.
        "<table>",
        "<thead>",
        "<tr>",
        "<th>#</th><th>session</th><th>side</th><th>ignition BST</th>"
        "<th>NQ px</th><th>disp NQ/ES</th><th>stack NQ/ES</th>"
        "<th>consume NQ/ES</th><th>dens NQ/ES</th>"
        "<th>ret10s</th><th>ret60s</th><th>MFE/MAE</th>",
        "</tr>",
        "</thead>",
        "<tbody>",
    ]
    for i, r in enumerate(rows[:20], 1):
        cells = [
            str(i),
            str(r["session"]),
            str(r["side"]),
            str(r["ignition_bst"]),
            f"{r['nq_px_ignition']:.2f}",
            f"{r['nq_disp_pt']:+.1f}/{r['es_disp_pt']:+.1f}",
            f"{int(r['nq_stack'])}/{int(r['es_stack'])}",
            f"{r['nq_consume']:.2f}/{r['es_consume']:.2f}",
            f"{r['nq_density']:.2f}/{r['es_density']:.2f}",
            f"{r['ret10s']:+.1f}",
            f"{r['ret60s']:+.1f}",
            f"{r['mfe']:.0f}/{r['mae']:.0f}",
        ]
        lines.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    lines.extend(["</tbody>", "</table>", ""])

    text = "\n".join(lines)
    (d / "summary.md").write_text(text, encoding="utf-8", newline="\n")
    print(text)


if __name__ == "__main__":
    main()

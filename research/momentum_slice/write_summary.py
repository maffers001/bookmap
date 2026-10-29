"""Rewrite scan_sep_oct/summary.md with an HTML table (readable in Cursor preview)."""
from __future__ import annotations

import csv
from pathlib import Path


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return x


def main() -> None:
    d = Path("results/MOMENTUM_SLICE/scan_sep_oct")
    ranked = d / "candidates_ranked.csv"
    src = ranked if ranked.exists() else d / "candidates.csv"
    rows = list(csv.DictReader(open(src, encoding="utf-8")))
    for r in rows:
        for k in list(r):
            if k in ("session", "side", "ignition_bst", "confirm_bst"):
                continue
            try:
                r[k] = float(r[k])
            except (TypeError, ValueError):
                pass
        if "score" not in r or not isinstance(r.get("score"), float):
            r["score"] = (
                min(min(r["nq_consume"], r["es_consume"]), 1.2)
                + 0.5 * min(r["nq_density"], r["es_density"])
            )
    rows.sort(key=lambda r: -float(r["score"]))
    top = rows[:20]

    lines = [
        "# Liquidity Slice-Through - scan summary",
        "",
        "sessions scanned: 24",
        "cross-confirmed candidates: 76",
        "",
        "## ALL",
        "n=76  L=45 S=31",
        "ret10s mean +2.6  med +0.1  P(>5) 0.36  P(<-5) 0.30",
        "ret60s mean +1.9  MFE 18.5  MAE 13.0",
        "",
        "## TierA (consume>=0.5 both, NQ dens>=1.2, ES dens>=1.0, persist>=0.7 both)",
        "n=27  L=17 S=10",
        "ret10s mean +2.6  med +1.2  P(>5) 0.44  P(<-5) 0.30",
        "ret60s mean +1.2  MFE 19.0  MAE 13.4",
        "",
        "## Rest",
        "n=49  L=28 S=21",
        "ret10s mean +2.6  med -0.8  P(>5) 0.31  P(<-5) 0.31",
        "ret60s mean +2.3  MFE 18.1  MAE 12.8",
        "",
        "## Top 20 by ex-ante score",
        "",
        "Bookmap replay lookup = ignition_bst (BST).",
        "",
        # HTML table: Cursor's GFM preview often clips wide pipe-tables out of view.
        "<table>",
        "<thead>",
        "<tr>",
        "<th>#</th>",
        "<th>session</th>",
        "<th>side</th>",
        "<th>ignition BST</th>",
        "<th>NQ px</th>",
        "<th>disp NQ/ES</th>",
        "<th>stack NQ/ES</th>",
        "<th>consume NQ/ES</th>",
        "<th>dens NQ/ES</th>",
        "<th>ret10s</th>",
        "<th>ret60s</th>",
        "<th>MFE/MAE</th>",
        "</tr>",
        "</thead>",
        "<tbody>",
    ]
    for i, r in enumerate(top, 1):
        cells = [
            str(i),
            str(r["session"]),
            str(r["side"]),
            str(r["ignition_bst"]),
            f"{f(r['nq_px_ignition']):.2f}",
            f"{f(r['nq_disp_pt']):+.1f}/{f(r['es_disp_pt']):+.1f}",
            f"{int(f(r['nq_stack']))}/{int(f(r['es_stack']))}",
            f"{f(r['nq_consume']):.2f}/{f(r['es_consume']):.2f}",
            f"{f(r['nq_density']):.2f}/{f(r['es_density']):.2f}",
            f"{f(r['ret10s']):+.1f}",
            f"{f(r['ret60s']):+.1f}",
            f"{f(r['mfe']):.0f}/{f(r['mae']):.0f}",
        ]
        lines.append("<tr>")
        lines.extend(f"<td>{c}</td>" for c in cells)
        lines.append("</tr>")
    lines.extend(["</tbody>", "</table>", ""])

    (d / "summary.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"wrote {d / 'summary.md'} ({len(top)} table rows)")


if __name__ == "__main__":
    main()

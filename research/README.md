# Orderflow Research

Offline event studies on Bookmap extracts (Parquet + DuckDB/Pandas).

## Setup

```powershell
cd c:\Work\Mat\bookmap
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r research\requirements.txt
```

## After you extract sessions in Bookmap

```powershell
python -m research.extract.to_parquet
python -m research.run_experiment EXP-0002 --sessions all
```

Reports land in `results/EXP-0002/`.

MAT gate / bracket backtest (bar-end corrected):

```powershell
python -m research.run_experiment EXP-0034 --sessions all
```

See [docs/RESEARCH_RULES.md](../docs/RESEARCH_RULES.md) (momentum bar-end note), [docs/HYPOTHESES.md](../docs/HYPOTHESES.md), [docs/FEEDS_INVENTORY.md](../docs/FEEDS_INVENTORY.md), [results/README.md](../results/README.md).

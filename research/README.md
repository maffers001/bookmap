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

See [docs/RESEARCH_RULES.md](../docs/RESEARCH_RULES.md), [docs/HYPOTHESES.md](../docs/HYPOTHESES.md), [docs/FEEDS_INVENTORY.md](../docs/FEEDS_INVENTORY.md).

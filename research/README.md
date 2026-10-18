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

Convert one new session (full trades+depth+mbo):

```powershell
python -m research.extract.to_parquet --session <sid>
```

Or convert everything pending:

```powershell
python -m research.extract.to_parquet
```

MAT frozen base (vol k=1, UK 14:30–17:00, trend+VWAP, 1-tick slip, 0.25 pt/RT commission):

```powershell
python -m research.run_all_sessions
python -m research.run_mat_overlays
python -m research.run_mat_exits
```

Replay reference (confirm + vol k=1):

```powershell
python -m research.export_mat2_ref --session 021026 --stamp confirm --bracket vol_k1
python -m research.compare_replay_ref --ref results/MAT2_REF_021026_confirm_vol_k1.csv --log <bookmap.log>
```

New sessions stay **OOS** until a batch of 5+ exists. See [docs/RESEARCH_RULES.md](../docs/RESEARCH_RULES.md), [docs/EXPERIMENT_LOG.md](../docs/EXPERIMENT_LOG.md), [docs/HYPOTHESES.md](../docs/HYPOTHESES.md).

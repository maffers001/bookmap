# Bookmap Orderflow Research

NQ/ES orderflow indicators (Bookmap add-on) + offline event research on extracted `.bmf` sessions.

## Quick start

### 1. Build / install Bookmap add-on

See [bookmap-addon/README.md](bookmap-addon/README.md) — install `bookmap-orderflow.jar`, enable **Session Extractor**, replay 5–10 RTH `.bmf` files from `C:\Bookmap\Feeds` into `data/raw/`.

### 2. Research environment

```powershell
cd c:\Work\Mat\bookmap
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r research\requirements.txt
```

### 3. Convert + run experiments

```powershell
python -m research.extract.to_parquet
python -m research.run_experiment EXP-0002 --sessions all
```

Docs: [RESEARCH_RULES](docs/RESEARCH_RULES.md) · [HYPOTHESES](docs/HYPOTHESES.md) · [FEEDS_INVENTORY](docs/FEEDS_INVENTORY.md) · [EXPERIMENT_LOG](docs/EXPERIMENT_LOG.md)

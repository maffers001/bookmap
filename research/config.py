from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_config(path: Path | None = None) -> dict:
    cfg_path = path or (Path(__file__).parent / "config.yaml")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # Resolve relative paths against repo root
    for key in ("data_dir", "raw_dir", "parquet_dir", "results_dir"):
        if key in cfg:
            p = Path(cfg[key])
            if not p.is_absolute():
                cfg[key] = str((ROOT / p).resolve())
    return cfg

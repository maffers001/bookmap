"""Thin wrapper: run stage 3 (validate/freeze) then stage 4 (final test)."""
from __future__ import annotations

import sys

from research.hvext.run_hvext_sweep import main

if __name__ == "__main__":
    # Default: validate then final test (expects stage1/1b/2 outputs).
    if len(sys.argv) == 1:
        rc = main(["--stage", "3"])
        if rc != 0:
            raise SystemExit(rc)
        raise SystemExit(main(["--stage", "4"]))
    raise SystemExit(main(sys.argv[1:]))

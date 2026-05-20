#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict

from mysql_storage import load_drafts, load_history, load_model_catalog, load_registry


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = ROOT / "runtime" / "mysql-json-export"


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export MySQL storage data back to JSON backup files.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for exported JSON files")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    files = {
        "site-price-registry.json": load_registry(),
        "site-price-history.json": load_history(),
        "site-price-drafts.json": load_drafts(),
        "model-catalog.json": load_model_catalog(),
    }
    for filename, data in files.items():
        write_json(output_dir / filename, data)

    print(
        json.dumps(
            {
                "ok": True,
                "output_dir": str(output_dir),
                "files": [str(output_dir / filename) for filename in files.keys()],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

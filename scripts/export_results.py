from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def flatten(prefix: str, value: Any, output: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            flatten(f"{prefix}.{key}" if prefix else key, item, output)
    elif isinstance(value, list):
        output[prefix] = json.dumps(value, ensure_ascii=False)
    else:
        output[prefix] = value


def main() -> None:
    results_dir = Path("results")
    rows = []
    for path in sorted(results_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        row: dict[str, Any] = {"file": path.name, "scenario": data.get("scenario")}
        flatten("metrics", data.get("metrics", {}), row)
        rows.append(row)

    if not rows:
        raise SystemExit("no result JSON files found in results/")

    fieldnames = sorted({key for row in rows for key in row})
    out_path = results_dir / "summary.csv"
    with out_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()

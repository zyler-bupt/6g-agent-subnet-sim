from __future__ import annotations

import argparse
import csv
from pathlib import Path


def normalize(source: Path, target: Path) -> list[dict[str, str]]:
    with source.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"proposed", "cspf", "global_sfc_embedding"}
    grouped: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        if row.get("result_mode") != "real_linux_netns_veth_tc_data_plane":
            raise ValueError("Exp1 canonical input must be measured Linux netns data")
        row["protocol_id"] = "wcnc_final_v3"
        row["execution_mode_detail"] = row["result_mode"]
        row["result_mode"] = "measured_netns"
        row["failure_reason"] = row.get("failure_reason", "")
        row["timeout"] = str("timeout" in row["failure_reason"].lower()).lower()
        grouped.setdefault((row["scenario_fingerprint"], row["seed"]), set()).add(row["method_id"])
    if any(methods != required for methods in grouped.values()):
        raise ValueError("Exp1 paired scenario is missing a canonical method")
    target.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0])
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--source", required=True); parser.add_argument("--target", default="results/paper/wcnc_final_v3/raw/exp1/trials.csv")
    args = parser.parse_args(); normalize(Path(args.source), Path(args.target))


if __name__ == "__main__": main()

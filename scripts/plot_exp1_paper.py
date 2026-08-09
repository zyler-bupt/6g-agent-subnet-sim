from __future__ import annotations

import argparse
from pathlib import Path

try:
    from scripts.plot_paper_figures import build_fig1_data, generate_fig1, _write_csv
except ModuleNotFoundError:
    from plot_paper_figures import build_fig1_data, generate_fig1, _write_csv  # type: ignore[no-redef]


def main() -> None:
    parser = argparse.ArgumentParser(description="Plot the WCNC initial-formation figure")
    parser.add_argument("--input", default="results/exp1/raw/runs.csv")
    parser.add_argument("--output-dir", default="results/exp1/figures")
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    data_dir = output_dir / "data"
    panel_a, panel_b = build_fig1_data(Path(args.input))
    _write_csv(data_dir / "Fig1_a_formation_latency.csv", panel_a)
    _write_csv(data_dir / "Fig1_b_formation_breakdown.csv", panel_b)
    outputs = generate_fig1(panel_a, panel_b, output_dir)
    print(f"Wrote {len(outputs)} Exp1 paper figure files to {output_dir.resolve()}")


if __name__ == "__main__":
    main()

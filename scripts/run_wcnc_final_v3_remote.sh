#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
protocol_root="${WCNC_V3_ROOT:-results/paper/wcnc_final_v3}"
mkdir -p "$protocol_root/environment" "$protocol_root/logs"

for command in git ip tc unshare nsenter ping iperf3; do
  command -v "$command" >/dev/null || { echo "missing dependency: $command" >&2; exit 2; }
done
test -x .venv/bin/python || { echo "missing .venv/bin/python" >&2; exit 2; }

git rev-parse HEAD > "$protocol_root/environment/git_commit.txt"
uname -a > "$protocol_root/environment/uname.txt"
.venv/bin/python -m pip freeze > "$protocol_root/environment/pip_freeze.txt"

run_step() {
  local marker="$protocol_root/logs/$1.done"
  shift
  if [[ -f "$marker" ]]; then echo "resume: $marker"; return; fi
  "$@"
  date -u +%FT%TZ > "$marker"
}

run_step exp1 .venv/bin/python -m experiments.exp1_netns_verified_formation \
  --config configs/exp1_netns_verified_formation_v2.yaml \
  --output-dir "$protocol_root/exp1_netns_staging" --seeds 0:49 --task-sizes 4,8,12,16,20 \
  --methods proposed,cspf,global_sfc_embedding
run_step exp1_normalize .venv/bin/python scripts/normalize_wcnc_final_v3_exp1.py \
  --source "$protocol_root/exp1_netns_staging/raw/runs.csv" --target "$protocol_root/raw/exp1/trials.csv"
run_step exp2 .venv/bin/python -m experiments.run_wcnc_final_v3 --experiment exp2 --seeds 0:99 --output-root "$protocol_root"
run_step exp3 .venv/bin/python -m experiments.run_wcnc_final_v3 --experiment exp3 --seeds 0:99 --output-root "$protocol_root"
run_step exp4 .venv/bin/python -m experiments.run_wcnc_final_v3 --experiment exp4 --seeds 0:99 --output-root "$protocol_root"
run_step aggregate .venv/bin/python scripts/aggregate_wcnc_final_v3.py --root "$protocol_root"
run_step manifest .venv/bin/python scripts/audit_wcnc_final_v3.py --root "$protocol_root" --write-manifest
run_step figures .venv/bin/python scripts/plot_wcnc_final_v3.py --root "$protocol_root"

echo "wcnc_final_v3 complete: $protocol_root"

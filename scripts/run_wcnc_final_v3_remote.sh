#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
protocol_root="${WCNC_V3_ROOT:-results/paper/wcnc_final_v3}"
mkdir -p "$protocol_root/environment" "$protocol_root/logs"
current_commit="$(git rev-parse HEAD)"
protocol_files=(
  configs/wcnc_final_v3.yaml
  configs/wcnc_final_v3_raw_schema.json
  configs/exp1_netns_verified_formation_v3.yaml
  configs/exp1_netns_verified_formation_pilot_v3.yaml
  experiments/exp1_netns_verified_formation.py
  experiments/exp2_cross_layer_robustness.py
  experiments/exp3_business_elasticity.py
  experiments/exp4_failure.py
  experiments/paper_protocol.py
  experiments/run_wcnc_final_v3.py
  src/controller/business_reconfiguration.py
  src/controller/cross_layer_coordinator.py
  src/controller/paper_failure_recovery.py
  src/simulation/demand_capacity_ratio.py
  src/simulation/demand_capacity_ratio_v3.py
  src/simulation/paper_failure_scenarios.py
  scripts/aggregate_wcnc_final_v3.py
  scripts/audit_wcnc_final_v3.py
  scripts/normalize_wcnc_final_v3_exp1.py
  scripts/plot_wcnc_final_v3.py
  scripts/run_wcnc_final_v3_remote.sh
)
protocol_signature="$(sha256sum "${protocol_files[@]}" | sha256sum | awk '{print $1}')"

for command in git ip tc unshare nsenter ping iperf3; do
  command -v "$command" >/dev/null || { echo "missing dependency: $command" >&2; exit 2; }
done
test -x .venv/bin/python || { echo "missing .venv/bin/python" >&2; exit 2; }

git rev-parse HEAD > "$protocol_root/environment/git_commit.txt"
uname -a > "$protocol_root/environment/uname.txt"
.venv/bin/python -m pip freeze > "$protocol_root/environment/pip_freeze.txt"

run_step() {
  local step="$1"
  local marker="$protocol_root/logs/$step.done"
  local artifact="$2"
  shift 2
  local step_signature
  local current_artifact_hash
  step_signature="$({ printf '%s\0' "$protocol_signature"; printf '%s\0' "$@"; } | sha256sum | awk '{print $1}')"
  current_artifact_hash="$(_artifact_hash "$artifact" 2>/dev/null || true)"
  if [[ -n "$current_artifact_hash" && -f "$marker" ]] \
      && grep -qx "git_commit=$current_commit" "$marker" \
      && grep -qx "step_signature=$step_signature" "$marker" \
      && grep -qx "artifact_hash=$current_artifact_hash" "$marker"; then
    echo "resume: $marker"
    return
  fi
  "$@"
  current_artifact_hash="$(_artifact_hash "$artifact" 2>/dev/null || true)"
  if [[ -z "$current_artifact_hash" ]]; then
    echo "step $step did not produce required artifact: $artifact" >&2
    return 3
  fi
  {
    echo "git_commit=$current_commit"
    echo "step_signature=$step_signature"
    echo "artifact_hash=$current_artifact_hash"
    echo "completed_at=$(date -u +%FT%TZ)"
  } > "$marker"
}

run_always() {
  local step="$1"
  local artifact="$2"
  shift 2
  "$@"
  local artifact_hash
  artifact_hash="$(_artifact_hash "$artifact" 2>/dev/null || true)"
  if [[ -z "$artifact_hash" ]]; then
    echo "step $step did not produce required artifact: $artifact" >&2
    return 3
  fi
  {
    echo "git_commit=$current_commit"
    echo "artifact_hash=$artifact_hash"
    echo "completed_at=$(date -u +%FT%TZ)"
  } > "$protocol_root/logs/$step.done"
}

_artifact_hash() {
  local artifact="$1"
  if [[ -f "$artifact" && -s "$artifact" ]]; then
    sha256sum "$artifact" | awk '{print $1}'
    return
  fi
  if [[ -d "$artifact" ]]; then
    local count
    count="$(find "$artifact" -type f -size +0c | wc -l)"
    if [[ "$count" -gt 0 ]]; then
      find "$artifact" -type f -size +0c -print0 \
        | sort -z \
        | xargs -0 sha256sum \
        | sha256sum \
        | awk '{print $1}'
      return
    fi
  fi
  return 1
}

run_exp1() {
  local output_dir="$1"
  local config="$2"
  local seeds="$3"
  local sizes="$4"
  shift 4
  local command=(
    env -u WCNC_EXP1_INSIDE_USERNS -u WCNC_EXP1_PARENT_NETNS_INODE
    .venv/bin/python -m experiments.exp1_netns_verified_formation
    --config "$config" --output-dir "$output_dir" --seeds "$seeds"
    --task-sizes "$sizes" --methods proposed,cspf,global_sfc_embedding "$@"
  )
  if [[ "$EUID" -eq 0 ]] || unshare --user --map-root-user --net --fork \
      sh -c 'ip link set lo up' >/dev/null 2>&1; then
    "${command[@]}"
    return
  fi
  echo "Exp1 requires an isolated network namespace; requesting sudo." >&2
  sudo -v
  local status=0
  sudo -E "${command[@]}" || status=$?
  if [[ -e "$output_dir" ]]; then
    sudo chown -R "$(id -u):$(id -g)" "$output_dir"
  fi
  return "$status"
}

run_step exp1_smoke "$protocol_root/pilot/exp1_netns_smoke_v3/raw" run_exp1 \
  "$protocol_root/pilot/exp1_netns_smoke_v3" \
  configs/exp1_netns_verified_formation_pilot_v3.yaml \
  9000:9001 4 --require-all-success
run_step exp1 "$protocol_root/exp1_netns_staging_v3/raw" run_exp1 \
  "$protocol_root/exp1_netns_staging_v3" \
  configs/exp1_netns_verified_formation_v3.yaml \
  0:49 4,8,12,16,20
run_step exp1_normalize "$protocol_root/raw/exp1/trials.csv" \
  .venv/bin/python scripts/normalize_wcnc_final_v3_exp1.py \
  --source "$protocol_root/exp1_netns_staging_v3/raw/runs.csv" --target "$protocol_root/raw/exp1/trials.csv"
run_step exp2 "$protocol_root/raw/exp2" \
  .venv/bin/python -m experiments.run_wcnc_final_v3 --experiment exp2 --seeds 0:99 --output-root "$protocol_root"
run_step exp3 "$protocol_root/raw/exp3" \
  .venv/bin/python -m experiments.run_wcnc_final_v3 --experiment exp3 --seeds 0:99 --output-root "$protocol_root"
run_step exp4 "$protocol_root/raw/exp4" \
  .venv/bin/python -m experiments.run_wcnc_final_v3 --experiment exp4 --seeds 0:99 --output-root "$protocol_root"
run_always aggregate "$protocol_root/aggregated" \
  .venv/bin/python scripts/aggregate_wcnc_final_v3.py --root "$protocol_root"
run_always manifest "$protocol_root/integrity_report.json" \
  .venv/bin/python scripts/audit_wcnc_final_v3.py --root "$protocol_root" --write-manifest
run_always figures "$protocol_root/figures" \
  .venv/bin/python scripts/plot_wcnc_final_v3.py --root "$protocol_root"

for exp in exp1 exp2 exp3 exp4; do
  test -s "$protocol_root/aggregated/$exp/metrics.csv" || {
    echo "missing aggregate: $exp/metrics.csv" >&2
    exit 3
  }
done
for suffix in pdf png svg; do
  count="$(find "$protocol_root/figures" -maxdepth 1 -type f -name "*.$suffix" | wc -l)"
  [[ "$count" -eq 16 ]] || {
    echo "expected 16 $suffix figures, found $count" >&2
    exit 3
  }
done

echo "wcnc_final_v3 complete: $protocol_root"

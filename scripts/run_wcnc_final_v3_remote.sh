#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
protocol_root="${WCNC_V3_ROOT:-results/paper/wcnc_final_v3}"
mkdir -p "$protocol_root/environment" "$protocol_root/logs"
current_commit="$(git rev-parse HEAD)"

# Every source that can change either Exp1 arm, canonicalization, statistics,
# figures, or integrity evidence participates in resumability signatures.
protocol_files=(
  configs/wcnc_final_v3.yaml
  configs/wcnc_final_v3_raw_schema.json
  configs/exp1_netns_verified_formation_v3.yaml
  configs/exp1_netns_verified_formation_pilot_v3.yaml
  configs/exp1_transactional_formation_v1.yaml
  configs/exp1_transactional_formation_pilot_v1.yaml
  experiments/exp1_netns_verified_formation.py
  experiments/exp1_transactional_formation.py
  experiments/exp2_cross_layer_robustness.py
  experiments/exp3_business_elasticity.py
  experiments/exp4_failure.py
  experiments/paper_protocol.py
  experiments/run_wcnc_final_v3.py
  src/controller/formation_transactions.py
  src/controller/business_reconfiguration.py
  src/controller/cross_layer_coordinator.py
  src/controller/paper_failure_recovery.py
  src/simulation/demand_capacity_ratio.py
  src/simulation/demand_capacity_ratio_v3.py
  src/simulation/paper_failure_scenarios.py
  scripts/aggregate_wcnc_final_v3.py
  scripts/audit_wcnc_final_v3.py
  scripts/normalize_wcnc_final_v3_exp1.py
  scripts/normalize_wcnc_final_v3_exp1_transactional.py
  scripts/publish_wcnc_final_v3_staging.py
  scripts/validate_wcnc_final_v3_exp1_nominal.py
  scripts/plot_wcnc_final_v3.py
  scripts/run_wcnc_final_v3_remote.sh
)
protocol_signature="$(sha256sum "${protocol_files[@]}" | sha256sum | awk '{print $1}')"

for command in git ip tc unshare nsenter ping iperf3 sha256sum; do
  command -v "$command" >/dev/null || { echo "missing dependency: $command" >&2; exit 2; }
done
test -x .venv/bin/python || { echo "missing .venv/bin/python" >&2; exit 2; }

git rev-parse HEAD > "$protocol_root/environment/git_commit.txt"
uname -a > "$protocol_root/environment/uname.txt"
.venv/bin/python -m pip freeze > "$protocol_root/environment/pip_freeze.txt"

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

run_step() {
  local step="$1"
  local artifact="$2"
  shift 2
  local marker="$protocol_root/logs/$step.done"
  local command_signature current_artifact_hash
  command_signature="$(printf '%s\0' "$@" | sha256sum | awk '{print $1}')"
  current_artifact_hash="$(_artifact_hash "$artifact" 2>/dev/null || true)"
  if [[ -n "$current_artifact_hash" && -f "$marker" ]] \
      && grep -qx "git_commit=$current_commit" "$marker" \
      && grep -qx "protocol_signature=$protocol_signature" "$marker" \
      && grep -qx "command_signature=$command_signature" "$marker" \
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
    echo "protocol_signature=$protocol_signature"
    echo "command_signature=$command_signature"
    echo "artifact_hash=$current_artifact_hash"
    echo "completed_at=$(date -u +%FT%TZ)"
  } > "$marker"
}

run_always() {
  local step="$1"
  local artifact="$2"
  shift 2
  local marker="$protocol_root/logs/$step.done"
  local command_signature current_artifact_hash
  command_signature="$(printf '%s\0' "$@" | sha256sum | awk '{print $1}')"
  "$@"
  current_artifact_hash="$(_artifact_hash "$artifact" 2>/dev/null || true)"
  if [[ -z "$current_artifact_hash" ]]; then
    echo "step $step did not produce required artifact: $artifact" >&2
    return 3
  fi
  {
    echo "git_commit=$current_commit"
    echo "protocol_signature=$protocol_signature"
    echo "command_signature=$command_signature"
    echo "artifact_hash=$current_artifact_hash"
    echo "completed_at=$(date -u +%FT%TZ)"
  } > "$marker"
}

run_netns_python() {
  local output_dir="$1"
  shift
  local command=(
    env -u WCNC_EXP1_INSIDE_USERNS -u WCNC_EXP1_PARENT_NETNS_INODE
    .venv/bin/python "$@"
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

run_nominal_smoke() {
  local output_dir="$protocol_root/pilot/exp1_netns_smoke_v3"
  run_netns_python "$output_dir" -m experiments.exp1_netns_verified_formation \
    --config configs/exp1_netns_verified_formation_pilot_v3.yaml \
    --output-dir "$output_dir" --seeds 9000:9001 --task-sizes 4 \
    --methods proposed,cspf,global_sfc_embedding --require-all-success
}

run_nominal_formal_with_readonly_audit() {
  local base_dir="$protocol_root/exp1_netns_staging_v3"
  local attempts_dir="$protocol_root/exp1_netns_staging_v3_attempts"
  local selection="$protocol_root/environment/exp1_nominal_selection.json"
  local output_dir=""
  local runner_status=0
  if [[ -s "$selection" ]]; then
    output_dir="$(.venv/bin/python scripts/validate_wcnc_final_v3_exp1_nominal.py \
      --protocol-root "$protocol_root" --receipt "$selection" --verify-selection \
      --config configs/exp1_netns_verified_formation_v3.yaml)"
  else
    local candidate candidates=()
    for candidate in "$base_dir" "$attempts_dir"/attempt-*; do
      [[ -d "$candidate/raw" ]] || continue
      candidates+=(--candidate "$candidate")
    done
    if [[ "${#candidates[@]}" -gt 0 ]]; then
      output_dir="$(.venv/bin/python scripts/validate_wcnc_final_v3_exp1_nominal.py \
        --protocol-root "$protocol_root" --receipt "$selection" \
        --config configs/exp1_netns_verified_formation_v3.yaml \
        "${candidates[@]}" 2>/dev/null || true)"
    fi
  fi
  if [[ -z "$output_dir" ]]; then
    if [[ ! -e "$base_dir" ]]; then
      output_dir="$base_dir"
    else
      mkdir -p "$attempts_dir"
      local attempt=1
      while [[ -e "$attempts_dir/attempt-$(printf '%03d' "$attempt")" ]]; do
        attempt=$((attempt + 1))
      done
      output_dir="$attempts_dir/attempt-$(printf '%03d' "$attempt")"
    fi
    mkdir -p "$output_dir"
    git rev-parse HEAD > "$output_dir/execution_commit.txt"
    run_netns_python "$output_dir" -m experiments.exp1_netns_verified_formation \
      --config configs/exp1_netns_verified_formation_v3.yaml \
      --output-dir "$output_dir" --seeds 0:49 --task-sizes 4,8,12,16,20 \
      --methods proposed,cspf,global_sfc_embedding || runner_status=$?
  fi
  test -s "$output_dir/execution_commit.txt" || {
    echo "nominal staging lacks its original execution commit provenance" >&2
    return 3
  }
  .venv/bin/python scripts/validate_wcnc_final_v3_exp1_nominal.py \
    --runs "$output_dir/raw/runs.csv" --events "$output_dir/raw/events.jsonl" \
    --scope "$output_dir/raw/measurement_scope.json" \
    --config configs/exp1_netns_verified_formation_v3.yaml
  .venv/bin/python scripts/validate_wcnc_final_v3_exp1_nominal.py \
    --protocol-root "$protocol_root" --receipt "$selection" \
    --candidate "$output_dir" --config configs/exp1_netns_verified_formation_v3.yaml \
    >/dev/null
  if [[ "$runner_status" -ne 0 ]]; then
    echo "nominal runner returned $runner_status; exact 750-row artifact/event audit passed" >&2
  fi
}

run_exp1_transactional() {
  local output_dir="$1"
  shift
  local resume_args=()
  mkdir -p "$output_dir"
  if [[ -d "$output_dir/raw" ]]; then
    test -s "$output_dir/execution_commit.txt" || {
      echo "transactional raw prefix lacks execution commit provenance" >&2
      return 3
    }
    [[ "$(<"$output_dir/execution_commit.txt")" == "$current_commit" ]] || {
      echo "transactional raw prefix execution commit differs from current HEAD" >&2
      return 3
    }
    resume_args=(--resume)
  else
    git rev-parse HEAD > "$output_dir/execution_commit.txt"
  fi
  run_netns_python "$output_dir" -m experiments.exp1_transactional_formation \
    --output-dir "$output_dir" "${resume_args[@]}" "$@"
}

publish_immutable_file() {
  local source="$1"
  local target="$2"
  test -s "$source" || { echo "missing publication source: $source" >&2; return 3; }
  mkdir -p "$(dirname "$target")"
  if [[ -e "$target" ]]; then
    cmp -s "$source" "$target" || {
      echo "refusing to overwrite immutable canonical artifact: $target" >&2
      return 3
    }
    return
  fi
  cp -- "$source" "$target"
}

require_immutable_compatible() {
  local source="$1"
  local target="$2"
  test -s "$source" || { echo "missing publication source: $source" >&2; return 3; }
  if [[ -e "$target" ]] && ! cmp -s "$source" "$target"; then
    echo "refusing to overwrite immutable canonical artifact: $target" >&2
    return 3
  fi
}

normalize_nominal_arm() {
  local selection="$protocol_root/environment/exp1_nominal_selection.json"
  test -s "$selection" || { echo "nominal staging selection is missing" >&2; return 3; }
  local selected_dir source_dir
  selected_dir="$(.venv/bin/python scripts/validate_wcnc_final_v3_exp1_nominal.py \
    --protocol-root "$protocol_root" --receipt "$selection" --verify-selection \
    --config configs/exp1_netns_verified_formation_v3.yaml)"
  source_dir="$selected_dir/raw"
  local staging="$protocol_root/normalization_staging/exp1"
  mkdir -p "$staging"
  .venv/bin/python scripts/validate_wcnc_final_v3_exp1_nominal.py \
    --runs "$source_dir/runs.csv" --events "$source_dir/events.jsonl" \
    --scope "$source_dir/measurement_scope.json" \
    --config configs/exp1_netns_verified_formation_v3.yaml
  .venv/bin/python scripts/normalize_wcnc_final_v3_exp1.py \
    --source "$source_dir/runs.csv" --target "$staging/trials.csv" \
    --config configs/exp1_netns_verified_formation_v3.yaml
  cp -- "$selected_dir/execution_commit.txt" \
    "$staging/execution_commit.txt"
  require_immutable_compatible "$staging/trials.csv" "$protocol_root/raw/exp1/trials.csv"
  require_immutable_compatible "$staging/execution_commit.txt" "$protocol_root/raw/exp1/execution_commit.txt"
  publish_immutable_file "$staging/trials.csv" "$protocol_root/raw/exp1/trials.csv"
  publish_immutable_file "$staging/execution_commit.txt" "$protocol_root/raw/exp1/execution_commit.txt"
}

normalize_transactional_arm() {
  local source_dir="$protocol_root/exp1_transactional_staging_v1/raw"
  local staging="$protocol_root/normalization_staging/exp1_transactional"
  mkdir -p "$staging"
  .venv/bin/python scripts/normalize_wcnc_final_v3_exp1_transactional.py \
    --source "$source_dir/runs.csv" --attempts "$source_dir/attempts.jsonl" \
    --scope "$source_dir/measurement_scope.json" --target "$staging/trials.csv" \
    --config configs/exp1_transactional_formation_v1.yaml
  cp -- "$protocol_root/exp1_transactional_staging_v1/execution_commit.txt" \
    "$staging/execution_commit.txt"
  require_immutable_compatible "$staging/trials.csv" "$protocol_root/raw/exp1_transactional/trials.csv"
  require_immutable_compatible "$staging/normalization_manifest.json" "$protocol_root/raw/exp1_transactional/normalization_manifest.json"
  require_immutable_compatible "$source_dir/attempts.jsonl" "$protocol_root/raw/exp1_transactional/attempts.jsonl"
  require_immutable_compatible "$source_dir/measurement_scope.json" "$protocol_root/raw/exp1_transactional/measurement_scope.json"
  require_immutable_compatible "$staging/execution_commit.txt" "$protocol_root/raw/exp1_transactional/execution_commit.txt"
  publish_immutable_file "$staging/trials.csv" "$protocol_root/raw/exp1_transactional/trials.csv"
  publish_immutable_file "$staging/normalization_manifest.json" "$protocol_root/raw/exp1_transactional/normalization_manifest.json"
  publish_immutable_file "$source_dir/attempts.jsonl" "$protocol_root/raw/exp1_transactional/attempts.jsonl"
  publish_immutable_file "$source_dir/measurement_scope.json" "$protocol_root/raw/exp1_transactional/measurement_scope.json"
  publish_immutable_file "$staging/execution_commit.txt" "$protocol_root/raw/exp1_transactional/execution_commit.txt"
}

run_and_publish_simulated_formal() {
  local experiment="$1"
  local staging_root="$protocol_root/simulation_staging/${experiment}-${current_commit}-${protocol_signature:0:12}"
  local staged_raw="$staging_root/raw/$experiment"
  local canonical_raw="$protocol_root/raw/$experiment"
  mkdir -p "$staged_raw"
  if [[ -e "$staged_raw/trials.csv" ]]; then
    test -s "$staged_raw/execution_commit.txt" || {
      echo "$experiment staging lacks execution commit provenance" >&2
      return 3
    }
    [[ "$(<"$staged_raw/execution_commit.txt")" == "$current_commit" ]] || {
      echo "$experiment staging execution commit differs from current HEAD" >&2
      return 3
    }
  else
    git rev-parse HEAD > "$staged_raw/execution_commit.txt"
    .venv/bin/python -m experiments.run_wcnc_final_v3 \
      --experiment "$experiment" --seeds 0:99 --output-root "$staging_root"
  fi
  .venv/bin/python scripts/publish_wcnc_final_v3_staging.py \
    --source "$staged_raw" --target "$canonical_raw" \
    --experiment "$experiment" --seeds 0:99 \
    --config configs/wcnc_final_v3.yaml --expected-commit "$current_commit"
}

verify_required_figures() {
  local stems=(
    exp1_conditional_verified_latency_ms
    exp1_route_install_latency_ms
    exp2_feasible_qos_satisfaction_rate
    exp2_pre_verification_correct_decision_rate
    exp3_success_rate
    exp3_conditional_verified_latency_ms
    exp3_modification_scope_ratio
    exp4_link_failure_success_rate
    exp4_link_failure_conditional_verified_latency_ms
    exp4_link_failure_modification_scope_ratio
    exp4_agent_failure_success_rate
    exp4_agent_failure_conditional_verified_latency_ms
    exp4_agent_failure_modification_scope_ratio
    exp4_capacity_degradation_success_rate
    exp4_capacity_degradation_conditional_verified_latency_ms
    exp4_capacity_degradation_modification_scope_ratio
    exp1_transactional_method_owned_formation_latency_ms
    exp1_transactional_time_to_correct_formation_ms
    exp1_transactional_rollback_scope_objects
    exp1_transactional_wasted_rule_commands
  )
  local stem suffix
  for stem in "${stems[@]}"; do
    for suffix in pdf png svg; do
      test -s "$protocol_root/figures/$stem.$suffix" || {
        echo "missing required figure: $stem.$suffix" >&2
        return 3
      }
    done
  done
  for suffix in pdf png svg; do
    if [[ -e "$protocol_root/figures/exp1_transactional_success_rate.$suffix" ]]; then
      echo "forbidden stale transactional success figure: exp1_transactional_success_rate.$suffix" >&2
      return 3
    fi
  done
}

run_step exp1_smoke "$protocol_root/pilot/exp1_netns_smoke_v3" run_nominal_smoke
run_step exp1_nominal_formal "$protocol_root/environment/exp1_nominal_selection.json" \
  run_nominal_formal_with_readonly_audit
run_step exp1_nominal_normalize "$protocol_root/raw/exp1" normalize_nominal_arm

run_step exp1_transactional_smoke "$protocol_root/pilot/exp1_transactional_smoke" \
  run_exp1_transactional "$protocol_root/pilot/exp1_transactional_smoke" \
  --config configs/exp1_transactional_formation_pilot_v1.yaml \
  --seeds 9000:9001 --task-sizes 4 \
  --scenario-classes command_rejection \
  --methods proposed,cspf,global_sfc_embedding --require-complete-grid
run_step exp1_transactional_pilot "$protocol_root/pilot/exp1_transactional" \
  run_exp1_transactional "$protocol_root/pilot/exp1_transactional" \
  --config configs/exp1_transactional_formation_pilot_v1.yaml \
  --require-complete-grid
run_step exp1_transactional_formal "$protocol_root/exp1_transactional_staging_v1" \
  run_exp1_transactional "$protocol_root/exp1_transactional_staging_v1" \
  --config configs/exp1_transactional_formation_v1.yaml \
  --require-complete-grid
run_step exp1_transactional_normalize "$protocol_root/raw/exp1_transactional" \
  normalize_transactional_arm

# Exp2--Exp4 begin only after both measured Exp1 arms are immutable and complete.
run_step exp2 "$protocol_root/raw/exp2" run_and_publish_simulated_formal exp2
run_step exp3 "$protocol_root/raw/exp3" run_and_publish_simulated_formal exp3
run_step exp4 "$protocol_root/raw/exp4" run_and_publish_simulated_formal exp4

run_always aggregate "$protocol_root/aggregated" \
  .venv/bin/python scripts/aggregate_wcnc_final_v3.py --root "$protocol_root"
run_always figures "$protocol_root/figures/figure_manifest.json" \
  .venv/bin/python scripts/plot_wcnc_final_v3.py --root "$protocol_root"
verify_required_figures
run_always final_audit "$protocol_root/integrity_report.json" \
  .venv/bin/python scripts/audit_wcnc_final_v3.py \
  --root "$protocol_root" --write-manifest

for exp in exp1 exp1_transactional exp2 exp3 exp4; do
  test -s "$protocol_root/aggregated/$exp/metrics.csv" || {
    echo "missing aggregate: $exp/metrics.csv" >&2
    exit 3
  }
done

echo "wcnc_final_v3 complete: $protocol_root"

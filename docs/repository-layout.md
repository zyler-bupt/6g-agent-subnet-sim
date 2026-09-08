# Repository layout and retention policy

The default branch is intentionally limited to runnable system code and the frozen `wcnc_final_v3` experiment pipeline.

## Kept in Git

- `src/`: core agents, controller, task-subnet, metrics, simulation, and end-to-end implementations.
- `semantic_controller/`, `services/`, `viz/`, `testbed/`: supported semantic API, UI, and Linux testbed surfaces.
- `experiments/`: system diagnostics plus the runners used by `wcnc_final_v3`.
- `configs/` and `scripts/`: only the frozen v3 protocol, its Exp1 nominal/transactional configurations, and the canonical validate/normalize/aggregate/plot/audit pipeline.
- `tests/`: tests for retained runtime and canonical behavior. Small reproducibility fixtures must live under `tests/fixtures/`.

## Local-only archive

The ignored `local/` directory preserves material that is useful for research but is not part of the supported repository surface:

- `local/legacy-experiments/`: stage-specific and pre-v3 experiments, configurations, scripts, tests, reports, and planning notes.
- `local/baselines/`: downloaded or locally adapted baseline repositories.
- `local/artifacts/`: raw/processed results, figures, presentations, documents, and packaging outputs.
- `local/notes/`: review notes and machine-local project memory.

The archive is not backed up by Git. Copy it separately if it must survive disk loss.

## Classification method

The cleanup used documented entrypoints, Python import reachability, shell-script calls, test imports, last-touch history, and generated-file signatures. Static reachability was a screening signal rather than the sole deletion rule: dynamically invoked canonical scripts and Linux testbed files remain documented and tested.

The historical `codex/semantic-controller` branch remains unchanged at cleanup time as a remote reference. Git history was not rewritten, so archived files remain recoverable from earlier commits.

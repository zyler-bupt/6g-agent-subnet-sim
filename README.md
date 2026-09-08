# 6G 跨层异构 Agent 任务通信子网

本仓库实现“业务协作关系 → 跨层任务子网 → 网络感知 → 预测/决策 → 运行期弹性调整”闭环。远端主线只保留可运行系统、Linux netns 测试床和冻结的 `wcnc_final_v3` 实验流水线；历史实验、baseline 和大型结果不进入 Git。

## 安装

需要 Python 3.10+。建议在独立虚拟环境中安装：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[all,dev]'
```

如果只使用核心组网和 CLI，可执行 `python -m pip install -e .`。

## 系统入口

```bash
# 离线任务子网和弹性调整演示
python -m src.sim.run_rescue_task
python -m experiments.run --scenario rescue

# 模拟端到端构建/恢复
python -m experiments.e2e_build --scenario rescue --install-mode simulated --verify-mode synthetic
python -m experiments.e2e_recovery --scenario rescue --install-mode simulated --verify-mode synthetic

# 语义控制 HTTP 服务（默认 127.0.0.1:8010）
python -m services.semantic_controller_service

# 组网过程可视化（默认 127.0.0.1:5000）
python -m viz.server
```

真实网络模式需要 Linux 与 root/CAP_NET_ADMIN：

```bash
sudo bash testbed/preflight.sh
sudo bash testbed/setup_topology.sh
python -m experiments.real_probe --sudo --ping-count 5 --iperf-seconds 1
python -m experiments.real_run --sudo --target-profile rescue --summary-only
```

`real_run` 默认不加载外部 trace。如需回放 SANet 数据，先按下文获取上游仓库，再显式传入 `--trace-path`。

## 正式实验

唯一 canonical 协议为 [`configs/wcnc_final_v3.yaml`](configs/wcnc_final_v3.yaml)，详细口径见 [`docs/wcnc_final_v3.md`](docs/wcnc_final_v3.md)。

```bash
# 非特权 pilot
python scripts/pilot_wcnc_final_v3.py
python -m experiments.run_wcnc_final_v3 --experiment exp2 --seeds 9000:9001

# Linux netns 完整流水线
scripts/run_wcnc_final_v3_remote.sh
```

所有输出写入 `results/`，该目录被 Git 忽略。仓库不保存正式 raw 数据、中间图表或打包结果。

## SANet baseline

SANet 上游源码不再 vendoring。需要时执行：

```bash
scripts/fetch_sanet.sh
```

脚本会检出 [WirelessAIatHUST/SANet](https://github.com/WirelessAIatHUST/SANet) 的固定提交 `60d9b3c1db02aa2018e67b0020a0feb57d9e3d73` 到被 Git 忽略的 `local/baselines/SANet-upstream/`。正式实验所需的 SANet-DW 适配逻辑已包含在本仓库中，不依赖上游训练代码。

## 测试与仓库结构

```bash
python -m unittest discover -v
```

普通 Linux CI 运行核心、语义、可视化和 canonical 非特权测试。需要 root/netns 的用例在专用 Linux 环境执行。仓库取舍和本地归档方式见 [`docs/repository-layout.md`](docs/repository-layout.md)。

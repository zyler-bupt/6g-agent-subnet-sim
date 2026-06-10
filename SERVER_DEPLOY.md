# 服务器部署说明

本项目第二阶段实验建议部署在 Linux 服务器上运行。服务器需要安装 Docker 和 Docker Compose。

## 1. 获取代码

```bash
git clone https://github.com/zyler-bupt/6g-agent-subnet-sim.git
cd 6g-agent-subnet-sim
```

如果仓库仍是 private，需要先在服务器上配置 GitHub 访问权限。

## 2. 启动多节点实验环境

```bash
docker compose up --build -d
```

启动后会包含以下服务：

- `controller`
- `gw-ue`
- `gw-mec`
- `gw-cloud`
- `agent-drone-capture`
- `agent-edge-recognition`
- `agent-cloud-planning`
- `agent-terminal-feedback`
- `pagent-ue-link`
- `nagent-mec-cloud-path`

宿主机端口：

- Controller: `18000`
- UE Gateway: `18001`
- MEC Gateway: `18002`
- Cloud Gateway: `18003`

## 3. 运行实验

```bash
python3 scripts/run_experiment.py --scenario all
```

单独运行某个场景：

```bash
python3 scripts/run_experiment.py --scenario normal-build
python3 scripts/run_experiment.py --scenario whitelist
python3 scripts/run_experiment.py --scenario network-congestion
python3 scripts/run_experiment.py --scenario physical-degradation
python3 scripts/run_experiment.py --scenario agent-failure
python3 scripts/run_experiment.py --scenario full-rebuild
```

## 4. 导出论文数据

```bash
python3 scripts/export_results.py
```

输出文件：

- `results/*.json`：每次实验的完整原始结果。
- `results/summary.csv`：压平后的指标表，便于画图。

## 5. 查看服务状态

```bash
docker compose ps
docker compose logs controller
docker compose logs gw-ue
```

## 6. 停止实验环境

```bash
docker compose down
```

## 实验性质说明

当前实验模式是 `docker-http-simulation`。它包含真实容器进程、HTTP 通信、序列化、端口、网关转发和应用层链路模型，但不是直接接入真实 5G/6G 硬件。链路质量、路径拥塞、带宽下降和丢包由网关服务按规则模拟。


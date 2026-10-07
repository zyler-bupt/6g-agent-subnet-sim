# TCANet 原型现场演示 — Ubuntu 彩排清单

## 0. 一次性准备（演示机）

1. 登录界面右下角齿轮选择 **Ubuntu on Xorg**（Wayland 下 `wmctrl` 无法摆放窗口）。
2. 系统包：

```bash
sudo apt install -y iproute2 tmux wmctrl imagemagick x11-utils ffmpeg tcpdump python3-venv
```

3. Python 环境（仓库根目录）：

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[demo,dev]'
```

4. 自检：

```bash
.venv/bin/python -m pytest -q tests/tcanet
sudo -E .venv/bin/python -m pytest -q tests/tcanet/prototype/test_netns_integration.py
```

两条都必须全绿；第二条验证内核策略路由真正转发任务流量。

## 1. 开场

```bash
source .venv/bin/activate
tcanet-demo up --auto-capture
```

输入 sudo 密码后屏幕自动排成 3×2：①控制器 ②流量曲线 ③PhyAgent/链路 ④任务 DAG ⑤各层 Agent 日志 ⑥操作终端。
可先 `tcanet-demo record start` 全程录屏备份。

## 2. 五幕（在 ⑥ 操作终端执行）

| 幕 | 命令 | 讲解要点 |
|---|---|---|
| 1 组网 | `tcanetctl submit` | ①：E^aff = 全部依赖 → FT 下发为 `ip rule` → Φ 绑定 → Assess 实测 → Commit v1；②三条流量出现 |
| 2 跨层冲突 | `tcanetctl demand e1 25` | AppAgent 提速（任务更新）；①：拒绝的组合含 `shared_resource:link:L1`（25×1.2=30>28），J* 后按 M 选择 |
| 3 网关故障 | `tcanetctl fail gateway G2` | ③ L1/L2 DOWN；①：NetAgent 上报 → `gateway failure: G2` → E^aff={e1,e2}，**e3 retained unchanged** → J 相同、M=4 胜 M=5 → G1→G3→G4；② e3 全程平稳 |
| 4 回滚 | `tcanetctl reset` → `tcanetctl submit` → `tcanetctl degrade L3 10` → `tcanetctl fail gateway G2` | 控制器不知道 L3 丢包；首选经 L3 的方案 Assess 实测 `loss_hard:e1` → Rollback → 改走 L6 |
| 5 支撑代理失效 | `tcanetctl kill physical-G4` | 心跳超时 → support failure → 只重绑 e3 的 PhyAgent（M=1，路径不变） |

## 3. 老师追问时的现场验证

```bash
ps -ef | grep prototype.agents | grep -v grep
sudo ip netns exec tc-g1 ip rule show
sudo ip netns exec tc-g1 ip route show table 100
sudo ip netns exec tc-g1 tc -s qdisc show dev l1
sudo ip netns exec tc-g3 tcpdump -ni l5 udp -c 5
tcanetctl status
```

## 4. 产出演示图

```bash
tcanet-demo capture recovered
python -m src.tcanet.prototype.figure --captures /tmp/tcanet-demo/captures/recovered --photo desk.jpg --anchors "0.15,0.45;0.38,0.45;0.62,0.45;0.85,0.45" --out tcanet_prototype.pdf
python -m src.tcanet.prototype.timeline --out tcanet_timeline.pdf
```

`--anchors` 是照片上四块屏幕区域的位置（照片宽高的比例，原点左上）。

## 5. 收场与回退

```bash
tcanet-demo record stop
tcanet-demo down
```

若现场网络命名空间异常：`tcanet-demo down` 后 `tcanet-demo up --sim`，窗口与剧本不变，所有窗口标注 SIMULATION。

## 6. 常见问题

- 窗口没自动排列：确认是 Xorg 会话（`echo $XDG_SESSION_TYPE` 应为 `x11`），或 `tcanet-demo up --screen 2560x1440`。
- `bus did not start`：看 `/tmp/tcanet-demo/logs/bus.log`；上次未清理时先 `tcanet-demo down`。
- 某个 Agent 死了：`tcanetctl revive <agent-id>`；日志在 `/tmp/tcanet-demo/logs/<agent-id>.log`。

# xgc2-b2-link 子产品：APT 与源码双路径

## 定位

| 项 | 内容 |
|----|------|
| 产品 ID | `xgc2-b2-link` |
| 形态 | **独立 XGC2 子产品**（与 `xgc2-ros-image-rtp-adapter` 同类） |
| 不是 | XGC2 Core/Agent 本体；不是现场 B2/Odin 驱动改写 |
| 包内容 | 共享契约 + 机载 forwarder + 地面 peer/sim + 可视化闭环脚本 |

**一体 Adapter（G4 接 Go Runtime）** 可后续同仓演进；当前 APT 先交付 **可装即可跑的链路与恢复**。

## 两头部署

```text
        Thor（机载）                    地面 Core 主机
   apt install …-b2-link              开发: 源码 PYTHONPATH
   b2_forwarder_d0                    或未来 noetic 包 / 同 deb 的 peer
         │  Zenoh/TCP                 b2_ground_peer --publish-ros
         └──────────── 同 key 契约 ────────────┘
```

| 端 | 推荐安装 | 启动 |
|----|----------|------|
| **Thor 机载** | `sudo apt install ros-jazzy-xgc2-b2-link`（noble, arm64/amd64） | `ros2 run xgc2_b2_link b2_forwarder_d0 -- --robot-id …` |
| **地面开发** | **源码**：`export PYTHONPATH=…/xgc2_b2_link` + Noetic rospy | `python3 -m xgc2_b2_link.ground_peer --publish-ros …` |
| **地面生产（规划）** | 可选第二包 `ros-noetic-xgc2-b2-link` 或纯 python deb | 与开发同一 CLI |

### 为何先只钉 Jazzy APT？

- 机载 Thor = **Jazzy 真源**，forwarder 依赖 ROS2 消息序列化。  
- 地面 lab Core 常是 **Focal/Noetic**：peer 的 ROS1 恢复用系统 `rospy`，**不必**装 Jazzy；源码路径最快。  
- 若地面也装 Jazzy，同一 deb 的 `b2_ground_peer` 同样可用（ROS1 恢复仍可选 rospy）。

## 开发阶段（源码直启）

```bash
cd xgc2-devops/products/ros2/driver/xgc2_b2_link
export PYTHONPATH=$PWD
# 契约单测 + TCP 环回
python3 -m pytest test/ -q
# 可视化闭环（Noetic + foxglove + Lichtblick）
bash scripts/viz_closed_loop.sh
```

改 key/话题：**只改** `contract/zenoh_v1.yaml`，G3/G4 同源。

## APT 发布后（Thor）

```bash
sudo apt update
sudo apt install ros-jazzy-xgc2-b2-link
source /opt/ros/jazzy/setup.bash
ros2 pkg prefix xgc2_b2_link
ros2 run xgc2_b2_link b2_forwarder_d0 -- \
  --robot-id b2-01 --transport auto
```

Zenoh 生产依赖：

```bash
# 可选；无则 TCP/配置的 fallback
pip3 install eclipse-zenoh
# 或发行版提供的 python3-zenoh（若仓库有）
```

## 与「连接器 / Adapter」的关系

| 层次 | 是否进本 APT |
|------|----------------|
| Zenoh/TCP 契约 + forwarder + ground peer | **是（本产品）** |
| 仿真 sim_publisher + viz 脚本 | **是（开发/联调）** |
| XGC2 Adapter Runtime → Go → 仪表 SSE | **后续同产品扩展** 或 Core 侧接线，不阻塞先发 deb |
| 现场 b2_ros2_driver / R5 / Odin | **否**（已有现场包） |

## 合规

```bash
.xgc2/scripts/check_package_compliance.sh
```

## 发版列车（对齐 image-rtp-adapter）

1. monorepo 本目录稳定  
2. 公开子仓 `lxk36/xgc2-b2-link`（`product.yml` release.repository）  
3. CI matrix：noble × amd64/arm64  
4. APT 发布 → Thor `apt install`  
5. 地面继续源码或补 noetic 包  

## 产品边界铁律（继承面板）

- 机载 foxglove **不**作跨机主路径  
- 高层次命令白名单；默认无 `cmd_vel`  
- 地面 ROS 恢复属 **本产品 ground peer**，不是第二个无契约微服务  

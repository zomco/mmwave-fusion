# MMWave Fusion

[English](./README.md)

把多台毫米波雷达融合成一张统一户型图的 Home Assistant 集成：接入每台雷达的目标，变换到
共享的户型坐标系，跨雷达关联并追踪，对生成的轨迹评分，并持久化事件和录像片段。

> **实验性功能。** 单雷达用不上它。

---

## 你需要它吗

大概率不需要 —— 除非你在同一个空间里装了不止一台雷达。

| 你的情况 | 你需要 |
| --- | --- |
| 一个房间一台雷达 | 只要[卡片](https://github.com/zomco/mmwave-card)就够了，看到这里可以停了。 |
| 多台雷达，想要一张合并的图 | 卡片自己就能做 —— 但融合发生在**浏览器里**，不保存任何数据。 |
| 多台雷达，还要轨迹持久化、区域事件、摄像头录像 | 本集成。 |

卡片是唯一的用户界面；本集成不自带任何前端。它完全从卡片里配置，所以它的 config flow
什么都不问。

融合要求雷达能报**二维位置**。只测距的型号只有距离没有方向，没有可融合的信息，卡片的
编辑器也不会列出它们。

### 三个组成部分

| 仓库 | 是什么 | 要装吗 |
| --- | --- | --- |
| [mmwave-component](https://github.com/zomco/mmwave-component) | ESPHome 固件 | 要 —— 设备端。 |
| [mmwave-card](https://github.com/zomco/mmwave-card) | Lovelace 卡片（HACS 类别：**plugin**） | 要 —— 唯一的界面。 |
| **mmwave-fusion**（本仓库） | HA 集成（HACS 类别：**integration**） | 只有多雷达才需要。 |

卡片和本集成独立发版，因此集成会在每次推送里带上 `api_version`（当前为 **1**），卡片
发现后端版本低于所需时会直接拒绝运行并在界面提示，而不是半残地跑。

---

## 安装

### 通过 HACS

把本仓库添加为自定义仓库，类别选 **Integration**，安装后重启 Home Assistant。

### 然后添加集成

本集成带 config flow，所以文件就位到 `config/custom_components/mmwave_fusion/` 之后，
从 **设置 → 设备与服务 → 添加集成 → MMWave Fusion** 添加即可。

没有任何需要填写的内容 —— 雷达、区域、摄像头全部从卡片里配置。

已有的 YAML 配置仍然可用。残留的

```yaml
mmwave_fusion:
```

配置块会触发一次性导入并创建 config entry，之后这个块就可以删掉了。

### 最后从卡片里配置

打开卡片的融合面板，添加雷达，执行联合标定。这里没有任何东西需要写 YAML。

---

## 它暴露什么实体

每个融合系统对应一个设备，下挂：

| 实体 | 类型 | 说明 |
| --- | --- | --- |
| `sensor.mmwave_fusion_<id>_target_count` | sensor | 融合后的轨迹数。属性：`online_radars`、`radar_count`、`multi_source_targets`、`calibration_warnings` |
| `binary_sensor.mmwave_fusion_<id>_occupied` | binary_sensor | `occupancy` 设备类别 |

在第一帧到达之前两者都报 `unavailable`，而不是替一个还没产出任何数据的系统断言"房间没人"。

`multi_source_targets` 统计被两台及以上雷达同时看到的轨迹 —— 也就是融合真正起了作用的
那些，区别于单台雷达自己的检测结果。

只有这两个汇总实体会进 HA Recorder。轨迹数据写入独立数据库（见下文），避免把 recorder
冲垮。

---

## 工作原理

1. 后端订阅每台雷达的原子目标帧；没有原子帧的旧设备仍可通过拆分的 X/Y 实体接入。
2. 每台雷达的坐标按其安装位置和 yaw/pitch/roll 变换到统一的户型坐标系。
3. 不同雷达的近邻观测先聚类，再由全局最小成本分配加 alpha-beta tracker 维持 `track_id`。
4. `track_ttl_s` 允许短时丢帧后继续同一条轨迹；`confirm_hits` 拦截一次性误报。
5. 轨迹、事件和雷达校准健康信息通过 `mmwave_fusion/subscribe` 推送给卡片。

### 坐标约定

`yaw = 0` 朝户型 **+Y**，正角度转向 **+X**。平面图 FOV、区域编辑器、3D 安装视图和本后端
使用同一套约定。

> **这套约定在三个仓库里各实现了一遍** —— 本仓库的 `fusion.py::transform_point`、
> ESPHome 组件、以及卡片的 `src/utils/transform.ts`。只改其中一处会静默地把所有人的坐标
> 镜像，而本仓库自己的测试全是绿的。详见 [AGENTS.md](./AGENTS.md)。

### 校准诊断

后端统计每台雷达变换后落入户型矩形的观测比例。累计至少 100 个观测、且户型内比例低于 20%
时，卡片会显示安装校准警告。这个诊断能发现 yaw 符号搞反、安装点填错或者量纲配置错误。

---

## 轨迹质量与录像准入

区域仍然产生 `enter`、`exit` 和 `dwell` 原始事件。轨迹结束时，质量引擎还会额外产生：

- **`traverse`** —— 满足硬性条件且总分达到阈值的关键轨迹，可以触发录像。
- **`trajectory`** —— 未达准入条件的轨迹，只保存诊断结果，不触发录像。

评分满分 100，综合：是否形成完整的进入/离开拓扑、有效观测占比与最大观测间隔、位移与
路径效率、最大位置跳变、位于户型范围内的观测占比、参与观测的雷达数量、以及轨迹持续时间。

短促误报、位移不足、断续观测、大幅跳变、主要位于户型外的轨迹会被拒绝。拒绝原因、分项
得分、指标和每台摄像头的录像决策都写入事件 metadata 并显示在卡片中。

### 录像

只支持 `recording_source: ha_live`，不读取摄像头 SD 卡或 NVR 的历史录像。

1. 融合系统启动时为每个 camera 实体预热一路 HA HLS 流。
2. HA 复用该摄像头的单个解码 worker，并在内存中保留约 30 秒分片。
3. 只有被摄像头 `event_types` 允许的关键事件才调用 `camera.record`。
4. 动态 lookback 尽量覆盖完整轨迹，但不超过 `buffer_seconds`（最大 30 秒）。
5. 录像调用使用阻塞完成语义，随后检查文件存在且大小大于零，状态才变为 `ready`。

这样就不会向摄像头发起 ISAPI 查询、历史 RTSP 回放或反复索引请求。稳态负载是每台摄像头
一路实时流，HA 仅在出现关键轨迹时向 `/media/mmwave_fusion/<fusion_id>/...` 写入片段。

`cooldown_s` 限制同一摄像头、区域和事件类型的最小录像间隔。事件查询会明确返回
`waiting`、`extracting`、`ready` 或 `failed`，失败信息会显示在卡片中。

---

## 历史数据与保留策略

融合轨迹、轨迹点、区域事件和录像片段写入
`config/.storage/mmwave_fusion.sqlite`。

`track_points` 按融合频率写入，是数据库的主要体积来源。在开发实例上，5.4 天内达到 166 万行
276 MB，约每天 51 MB。因此每 6 小时执行一次保留清理：

| 数据 | 保留时长 |
| --- | --- |
| `track_points` | 7 天 |
| `tracks`、`events` | 90 天 |
| `clips` 及其所属事件 | 永不清理 |

录像片段豁免的原因是：那条记录是磁盘上录像文件的唯一指针，删掉它只会让文件变成孤儿，
并不能回收空间。

写入频率由 `quality.persist_interval_s` 限制，把默认 10 Hz 的融合频率降到落盘最多 2 Hz，
并且只保存有实际雷达观测支撑的轨迹点。

SQLite 会复用释放的页，但不会缩小文件，所以清理只能止住增长，不能回收已分配的空间。
要回收就停一次 Home Assistant，对数据库执行 `VACUUM`。

---

## WebSocket API 与权限

卡片通过以下命令与本集成通信：

| 命令 | 需要管理员 |
| --- | --- |
| `mmwave_fusion/configure` | 是 |
| `mmwave_fusion/get_config` | 是 |
| `mmwave_fusion/remove_config` | 是 |
| `mmwave_fusion/list_calibration_profiles` | 是 |
| `mmwave_fusion/upsert_calibration_profile` | 是 |
| `mmwave_fusion/remove_calibration_profile` | 是 |
| `mmwave_fusion/subscribe` | **否** |
| `mmwave_fusion/query_events` | **否** |
| `mmwave_fusion/query_track` | **否** |

所有会修改配置的命令都限管理员。三个只读命令刻意不限，这样非管理员家庭成员也能打开
面板看自己家。

**但要清楚这意味着什么。** 任何 Home Assistant 用户 —— 包括受限账号或访客账号 —— 都能
订阅居住者的实时位置，并查询任意融合系统的完整历史轨迹。如果这对你家不合适，在
`websocket_api.py` 里给这三个命令加上 `@websocket_api.require_admin`；代价是非管理员将
失去实时视图和事件列表。

这是一个有意为之的默认值，不是疏漏。之所以写在这里而不是悄悄改掉，是因为收紧它恰恰会
降低那些最需要它的人的使用体验。

---

## 蓝图

仓库在 [`blueprints/automation/mmwave_fusion/`](blueprints/automation/mmwave_fusion)
提供两个自动化蓝图。在**设置 → 自动化与场景 → 蓝图 → 导入蓝图**中按 URL 导入。

### 灯随区域有人而亮

[`zone_presence_light.yaml`](blueprints/automation/mmwave_fusion/zone_presence_light.yaml)

区域有人时开灯，区域空了之后关灯。

这不是把官方的移动感应蓝图换个传感器。PIR 只能报告**移动**，所以每个移动灯光
自动化都需要一个超时，而每个超时都是妥协：太短则有人看书时灯灭，太长则人走后
还亮十分钟。融合区域报告的是**存在**，因此没有需要猜的东西。宽限期的作用是扛过
偶尔丢的一帧，而不是估计一个人能坐多久不动——需要超过一分钟，说明是在绕开一个
标定问题而不是解决它。

### 有效穿越时通知

[`traverse_notification.yaml`](blueprints/automation/mmwave_fusion/traverse_notification.yaml)

在 `traverse` 事件上触发：一条完整且观测充分的穿越路径，由上文的质量引擎判定。
未通过检查的轨迹会以 `trajectory` 事件发出并附带原因，**不会**匹配本蓝图——这
正是关键，因为一个被窗帘反射就响的通知，最终会被人关掉。最低分数是在此之上的
第二层过滤，适用于那些"即使是干净的穿越也未必值得手机响一下"的房间。

---

## 开发

| 模块 | 职责 |
| --- | --- |
| `fusion.py` | 坐标变换、聚类、关联、追踪 |
| `frames.py` | 原子目标帧解码 |
| `coordinator.py` | 每个系统的生命周期与推送循环 |
| `quality.py` | 轨迹评分与录像准入 |
| `events.py` | 区域事件、摄像头录像编排 |
| `storage.py` | SQLite 表结构、写入、保留清理 |
| `profiles.py` | 按 HA `device_id` 标识的共享校准档案 |
| `websocket_api.py` | 上表中的命令 |
| `config_flow.py` | 创建 config entry（什么都不问） |

单元测试位于开发工作区
[mmwave-workspace](https://github.com/zomco/mmwave-workspace)，那里把本仓库连同卡片和
ESPHome 组件一起作为子模块管理：

```bash
python -m unittest discover -s tests/unit
```

测试用桩替代 Home Assistant 而不是真的导入它，因此无需安装 Home Assistant 即可运行。

---

## 许可证

MIT

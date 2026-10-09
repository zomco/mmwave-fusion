# MMWave Fusion

[English](./README.md)

把一台或多台 **2D** 毫米波雷达收成一张房间图的 Home Assistant 集成：统一坐标、轨迹、区域事件，以及可选的直播抓拍和短片。[卡片](https://github.com/zomco/mmwave-card)是唯一的界面。本集成的配置流什么都不问。

实验性。只要一个 Occupied 位的单雷达用不上它。

## 快速使用

1. 用 [mmwave-component](https://github.com/zomco/mmwave-component) 烧录雷达，并安装 [mmwave-card](https://github.com/zomco/mmwave-card)。先在卡片里校准。
2. 在 HACS 把本仓库加为自定义仓库，类别选 **Integration**。安装后重启 Home Assistant。
3. **设置 → 设备与服务 → 添加集成 → MMWave Fusion**。表单留空。
4. 打开卡片的融合面板，加入 2D 雷达，做联合校准。不要在 YAML 里配。

编辑器不会列出 1D 雷达。它们只有距离没有方向，没有可融合的信息。

| 你的情况 | 你需要 |
| --- | --- |
| 一台雷达，只要 Occupied（卫生间、书桌、床） | 卡片。到此为止。 |
| 一台 2D 雷达，要穿越、回放或热力图 | 本集成，`radars[]` 里放一台。 |
| 多台 2D 雷达，要合并视图，不保存 | 只用卡片。融合留在浏览器里。 |
| 要保存轨迹、区域事件或摄像头短片 | 本集成。 |

这不是 NVR 检索。短片来自 Home Assistant 直播流。有人/无人不能变成轨迹。

## 文档地图

| 想了解 | 看 |
| --- | --- |
| 烧录和第一次校准 | [新手上手指南](https://github.com/zomco/mmwave-component/blob/main/GETTING-STARTED_CN.md) |
| 卡片配置和 YAML | [mmwave-card DIY](https://github.com/zomco/mmwave-card/blob/main/DIY_CN.md) |
| 追踪、区域、评分（不依赖 Home Assistant） | [mmwave-engine](https://github.com/zomco/mmwave-engine) |
| 灯光、占用、穿越、停留自动化 | [`blueprints/automation/mmwave_fusion/`](blueprints/automation/mmwave_fusion) |
| 坐标约定，以及不能单独改的地方 | [AGENTS.md](./AGENTS.md) |
| 测试 | [mmwave-workspace](https://github.com/zomco/mmwave-workspace) 的 `tests/unit` |

在 **设置 → 自动化与场景 → 蓝图** 里按 URL 导入。卫生间、书桌、床的室内存在检测用设备上的 Area Occupied，不用这些蓝图。

## 它暴露什么

每个融合系统一个设备：

| 实体 | 类型 | 说明 |
| --- | --- | --- |
| `sensor.mmwave_fusion_<id>_target_count` | sensor | 融合轨迹数 |
| `binary_sensor.mmwave_fusion_<id>_occupied` | binary_sensor | 占用 |

第一帧到来之前，两者都是 `unavailable`。只有这两个进 Recorder。轨迹写到 `config/.storage/mmwave_fusion.sqlite`。

| 数据 | 保留 |
| --- | --- |
| `track_points` | 7 天 |
| `tracks`、`events` | 90 天 |
| `/media/mmwave_fusion` 下的短片 | 30 天 |

SQLite 不会自行缩小文件。要收回空间，先停 Home Assistant，再对数据库执行 `VACUUM`。

改配置的 WebSocket 命令仅管理员可用。`subscribe`、`query_events`、`query_track` 不是，因此任何 Home Assistant 用户都能看到实时位置和已存轨迹。若要收紧，在 `websocket_api.py` 里给这三条加上 `@websocket_api.require_admin`。非管理员会失去实时视图。

## 雷达型号

**1D 雷达不能使用本集成。** **2D 雷达可以使用项目的全部功能**，包括本集成。R60ABD1 同时上报高度，列在 2D 里是因为它可以参与融合。

数字和进度与 [mmwave-component](https://github.com/zomco/mmwave-component/blob/main/README_CN.md) 一致。`—` 表示说明书没有给出。标了「精度」的不是距离门宽度。LD2410 在 0.20 m 分辨率下最远 1.6 m。LD2451 按整米上报。LD2450、LD2454 的距离分辨率在每帧目标里。LD2454 性能表写 8 m，安装章节写跟踪 6 m。

### 1D 雷达

| 型号 | 检测距离 | 距离分辨率 | 开发进度 |
| --- | --- | --- | --- |
| LD2410 | 6 m | 0.75 m / 0.20 m | 开发中 |
| LD2410B | 6 m | 0.75 m（协议亦可设 0.20 m） | 测试中 |
| LD2410C | 6 m | 0.75 m（协议亦可设 0.20 m） | 测试中 |
| LD2411 | 2 m（手势 1 m） | — | 开发中 |
| LD2411S | 6 m（微动 3.5 m） | ±5 cm / ±5%（精度） | 开发中 |
| LD2412 | 9 m | 0.75 m | 开发中 |
| LD2420 | 8 m（微动 6 m） | 0.70 m | 开发中 |
| LD2450A | 2 m | 0.2 m（感知精度） | 开发中 |
| LD6002 | 1.5 m（呼吸心率） | — | 开发中 |
| RD-03E | 6 m（微动 3.5 m） | ±5 cm / ±5%（精度） | 开发中 |

### 2D 雷达

| 型号 | 检测距离 | 角度范围 | 距离分辨率 | 开发进度 |
| --- | --- | --- | --- | --- |
| LD2450 | 6 m | 方位 ±60°，俯仰 ±35° | 随帧上报 | 开发中 |
| LD2451 | 100 m | 水平 ±15°，垂直 ±7° | 1 m | 测试中 |
| LD2452 | 6 m | 方位 ±60°，俯仰 ±30° | 0.72 m | 测试中 |
| LD2453 | 6 m | 方位 ±40°，俯仰 ±30° | 0.72 m | 开发中 |
| LD2454 | 8 m | 方位 ±60°，俯仰 ±30° | 随帧上报 | 测试中 |
| R60ABD1 | 2.5 m（呼吸心率 1.5 m） | ±20° | — | 已完成 |

## 许可证

MIT

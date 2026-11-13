# AGENTS.md — USB Meter Dashboard

> 给 AI 编码代理 / 贡献者的上下文说明。改动代码前请先读完这一页。
> 项目是中文界面，代码标识符用英文，注释用中文。

## 1. 项目是什么

一个本地运行的 USB 功率计仪表盘：通过 Windows HID 读取 FNIRSI FNB 系列
（FNB58 / FNB48S / FNB48 / C1）的电压、电流、功率、D+/D-、温度，
用浏览器展示实时曲线、统计、纹波和 CSV 记录。

- 后端：Python 3.14 标准库（`http.server` + 线程），唯一外部依赖是随包的 hidapi `.pyd`
- 前端：原生 HTML / CSS / JS + Canvas，**没有框架、没有构建步骤**
- 便携：`runtime/` 内置 python.org 嵌入式解释器，双击 `start.bat` 即可运行
- 系统：64 位 Windows 10 / 11

## 2. 硬性约束（不要破坏）

1. **零第三方 Python 依赖**：只用标准库。需要新能力时优先自己写（参考 `hidlink.py` 的 SetupAPI 调用）。
2. **无构建步骤**：前端不引入 npm / 打包器；改完刷新页面即可。
3. **设备识别表不能删**：`usbmeter/hidlink.py` 的 `KNOWN_DEVICES` 是打开设备的唯一依据。
4. **隐私**：
   - 禁止把真实序列号、VID/PID、Windows 用户名、本机路径写进文档、截图、提交信息或示例数据。
   - 诊断页默认显示真实值（方便排查），但「隐私模式」打开后必须全部打码：状态栏 SN、事件日志、
     设备列表的 SN/VID:PID/路径、`paths` 和 `executable`、其他 HID 设备列表、导出 JSON。
   - 新增任何输出通道（日志、API、导出、页面）时，都要考虑隐私开关。
5. **语言**：界面文案中文；变量/函数英文；注释中文；不要引入英文 UI 文案（除非是单位/型号）。
6. **不要改 `runtime/` 和 `vendor/`** 里的二进制。

## 3. 文件地图

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `app.py` | ~70 | 命令行参数、启动服务、`--probe` 枚举 |
| `usbmeter/protocol.py` | ~107 | 64 字节包解析、CRC8、命令帧 |
| `usbmeter/hidlink.py` | ~418 | SetupAPI 枚举 + hidapi 读写 + 设备识别 |
| `usbmeter/device.py` | ~341 | 连接/重连线程、状态、事件、隐私打码 |
| `usbmeter/session.py` | ~232 | 环形缓冲、统计、纹波、CSV、平滑 |
| `usbmeter/server.py` | ~586 | HTTP 路由、SSE 广播、静态文件、端口切换、单实例 |
| `web/index.html` | ~212 | 仪表盘结构 |
| `web/app.js` | ~863 | SSE、环形缓冲、Canvas 绘制、KPI、设置 |
| `web/style.css` | ~506 | 深色主题 |
| `web/debug.html` / `debug.js` | ~111 / ~223 | 诊断页 |
| `tools/live.py` | ~198 | 独立 CMD 实时读数窗口（轮询 `/api/state`） |
| `tools/dump.py` | ~80 | 抓原始包存 `logs/raw-*.bin` |
| `tests/*.py` | ~162 | 协议 / 纹波 / 统计的单元测试 |

## 4. 数据流

```
USB HID（100 sps，25 包/秒，每包 4 个样本）
   │
   ▼  MeterService 读取线程（device.py）
解析 4 个 Sample（protocol.py）
   │
   ▼  Session.add()（session.py）
环形缓冲 + 累计能量/容量 + 极值 + 最近 200 点纹波 + 平滑 EMA + 可选 CSV
   │
   ├── SSE /api/stream（25 条/秒，data 消息）
   │        └── 浏览器环形缓冲（30 分钟）→ Canvas 绘制
   │
   └── 每 0.4 秒广播 stats（平滑值、纹波、极值、记录状态）
            ├── 页面 KPI / 侧栏
            └── tools/live.py 轮询 /api/state
```

## 5. USB 协议要点（`protocol.py`）

- 报告 64 字节：`AA 04` + 4 组 × 15 字节 + 1 字节 + CRC8
- CRC8：`poly=0x39, init=0x42`，计算 `[1..62]`
- 每组 15 字节：V `u32/1e5`、I `u32/1e5`、D+ `u16/1e3`、D- `u16/1e3`、标志位、温度 `u16/10`
- 命令帧：`AA 81 … 8E` 初始化、`AA 82 … 96` 开始、`AA 83 … 9E` 每秒请求
- 设备报告长度是 **65/65**（带 report id）：写命令前补 `0x00`，读时自动去掉
- 100 sps = 25 包/秒 × 4 样本；**不要**在这条链路上做高频纹波分析（奈奎斯特 50 Hz）

## 6. 后端要点

### `MeterService`（device.py）
- 单线程循环：`list_meters()` → 选一台 → `_run_device()` → 失败退避重连
- **关闭后必须等 2.5 秒再重开**：设备会重新枚举，立刻打开会拿到半初始化实例，写入返回 -1
- 收尾由读取线程自己做（先退出循环，再发 STOP/关句柄），避免和 `read()` 并发
- 默认不发 STOP（`stop_cmd` 可开）；软件重置（PnP 重启）实测无效，**不要加回来**
- 同一时间只连一台设备；多台时按 `_PREFERENCE` 选
- `set_privacy()` 控制状态栏/事件里的 SN 是否打码

### `Session`（session.py）
- `_wave`：最近 200 个 (v, i) 点，用于纹波
- 纹波：`dc = mean(v)`、`vpp = max-min`、`vrms = sqrt(mean((v-dc)²))`、`ipp = max(i)-min(i)`
- 累计：能量 Wh、容量 Ah（按采样间隔积分）
- 平滑：EMA，`set_smoothing(tau)`；只影响展示，CSV 用原始值
- CSV：默认不记录，`start_recording()` 才创建文件，固定写 `logs/`

### `App` / `StudioServer`（server.py）
- `ThreadingHTTPServer`，静态文件 + JSON API + SSE
- 路由表：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 设备状态 + 统计 + 配置 |
| GET | `/api/stream` | SSE（hello / data / stats） |
| GET | `/api/probe` | 枚举结果 + 最近原始包 |
| GET | `/api/debug` | 诊断快照（隐私模式时打码） |
| GET | `/api/debug/export` | 下载诊断 JSON |
| GET | `/api/export` | 下载当前 CSV |
| POST | `/api/session/new` | 新建记录文件 |
| POST | `/api/record` | 开始/停止记录 |
| POST | `/api/rescan` | 重新扫描设备 |
| POST | `/api/device/disconnect` | 断开并自动重连 |
| POST | `/api/port` | 切换端口（先监听新端口再关旧的） |
| POST | `/api/settings` | 保存报警、平滑、隐私开关 |

- 单实例：同端口再启动会检测 `/api/state` 并拒绝
- 端口从 `config.json` 读取，默认 8765

## 7. 前端要点（`web/app.js`）

- 全局 `state`：`series`、`windowSec`、`paused`、`chartPaused`、`frozenT`、`axis`、`hover`、`chartPts`
- 环形缓冲：`CAP = 30*60*100`，按时间查找用 `lowerBound`
- SSE：`data` 入缓冲；`stats` 更新 KPI；`hello` 初始化配置
- 绘制循环 `frame()`：
  - 数据变化时重绘（10m/30m 窗口节流到 5 fps）
  - `drawChart()` 内按像素分桶，**最多抽样 12000 个点**（`stride`）
  - 纵轴迟滞：越界才扩展，富余持续 3 秒才收缩；暂停/冻结时**完全锁定**
  - 冻结时纹波直流基线取冻结窗口末端，不取最新数据
  - 悬停圆点用**本帧实际绘制点**，不要用原始采样（否则会跳到尖峰上）
- `updateCrosshair()` 用 `state.chartPts` 找最近点
- 暂停（空格）和冻结（图表右上角）是两套状态，都要处理

## 8. 常见改动配方

### 新增一个 KPI 卡片
1. `index.html` 里复制一个 `.kpi` 卡片，给 `id`
2. `app.js` 的 `updateKPIs()` 里写值
3. `style.css` 加颜色变量（可选）

### 新增一条曲线/统计量
1. `session.py` 的 `snapshot()` 增加字段
2. `protocol.py` 若有新数据先解析
3. `app.js` 的 `SERIES` 加配置、`ring` 增加读取键、`drawChart` 里取值
4. `index.html` 的图表 tab 增加按钮

### 新增 API
1. `server.py` 的 `do_GET` / `do_POST` 加路由
2. `app.js` 里用 `api()` 调用
3. 注意隐私：返回设备信息时走 `_mask_serials` / `_mask_path`

### 新增设置项
1. `DEFAULT_CONFIG` 加默认值
2. `/api/settings` 处理字段
3. `index.html` 设置弹窗加控件
4. `app.js` 打开弹窗时回填、保存时提交

### 新增设备型号
1. `hidlink.py` 的 `KNOWN_DEVICES` 加 `(vid, pid): "型号"`
2. 确认启动序列（v2：INIT + START×2；v1：INIT + START + POLL）
3. 加到 `_PREFERENCE`

## 9. 测试与验证

```bat
runtime\python.exe -m unittest discover -s tests
node --check web/app.js
node --check web/debug.js
runtime\python.exe -m compileall -q usbmeter app.py tools tests
```

手动清单：
- 不接设备：页面显示空状态，不报错
- 接设备：1 秒内连上，曲线/数字更新，纹波有值
- 暂停 / 冻结：时间轴和曲线都不动
- 设置：改平滑、报警、隐私模式、端口
- `/debug`：设备列表、事件、原始包、复制/下载
- 断开设备：3 秒内自动重连（不要频繁点）

## 10. 隐私与发布检查

提交/打包前：

- [ ] `logs/` 为空（或已删除）
- [ ] `config.json` 是默认值（或删除，让程序重建）
- [ ] 截图里没有真实 SN / 本机路径
- [ ] 文档/示例里没有真实序列号、VID/PID、用户名
- [ ] 没有 `__pycache__`、`*.pyc`、临时脚本

建议 `.gitignore`：

```gitignore
__pycache__/
*.py[cod]
logs/
config.json
*.log
```

> 注意：程序运行后 `config.json` 会存用户的端口/报警设置，`logs/` 会有 CSV；
> 开源仓库里不要把这两样提交上去。

## 11. 已知坑

- 关闭连接后立刻重开会写失败 → 必须等 2.5 秒
- 设备"假死"（枚举在、写入 -1）→ 只能拔插；PnP 重启无效，已移除
- 65 字节报告 → 写命令补 `0x00`
- 100 sps → 纹波只能看 ≤50 Hz，高频纹波要用表的高速纹波模式
- 同一端口只允许一个实例
- 端口切换要先绑新端口再关旧端口，否则页面会失联
- 30 分钟窗口数据量大：绘制已限制抽样，不要取消 `stride`

## 12. 提交规范

- 一个提交只做一件事
- 提交信息用中文，说明"改了什么/为什么"
- 不要提交 `.pyc`、日志、真实设备截图
- 改到协议/设备行为时，同步更新本文件和 `README.md`

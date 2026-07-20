# QP Copilot（第一版）

内部工具：自动浏览 QP 平台某个 scene 的多帧数据，逐帧切帧、等待真正加载完成、截图保存、
记录网络请求、提取 BBox 几何数据，并跑一遍质检规则引擎，生成本地报告。

**不做的事**：不接 AI 视觉模型、**不自动点击"合格/驳回/提交"、不修改平台任何数据**。
本工具只做只读的事：**切帧、等待就绪、截图、记录网络请求、提取 BBox、跑质检规则、生成本地报告**。

---

## 1. 依赖与环境配置：Windows / Ubuntu（`cdp` 调试模式）

脚本连接你手动打开、已登录好的浏览器，**账号密码全程不经过脚本，也不会被保存**。
Edge 和 Chrome 都是 Chromium 内核，走一样的调试协议，两个都能用。按你实际使用的系统
选对应小节。

### 1.1 Windows 配置与运行

1. 安装依赖

   ```bash
   cd qp_copilot
   pip install -r requirements.txt
   playwright install chromium
   ```

   桌面 UI（`ui_app.py`）用的是 Python 自带的 `tkinter`，不需要额外安装。

2. **完全关闭**所有已打开的浏览器窗口（调试端口只能在启动时指定）。
3. 用调试端口重新启动浏览器：

   **Edge**（实际在用的）：
   ```powershell
   & "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222 --user-data-dir="C:\edge-qp-debug"
   ```

   **Chrome**（如果用 Chrome 就用这个）：
   ```powershell
   & "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\chrome-qp-debug"
   ```

   `--user-data-dir` 用一个独立目录，避免和日常浏览器资料冲突，首次要重新登录一次。

4. 在这个新打开的窗口里：
   1. 登录 QP 质检网站
   2. 打开要检查的 scene，进入点云查看页面
   3. **停在第 1 帧**
5. 确认 `config.yaml` 里 `browser.mode: "cdp"`、`browser.cdp_url` 跟实际端口一致
   （默认 `http://127.0.0.1:9222`，用上面的命令不用改）。

   如果 `cdp` 模式连不上（比如公司网络策略限制了调试端口），把 `browser.mode` 改成
   `"launch"`，脚本会自己开一个新浏览器窗口，你在里面手动登录、停在第 1 帧，回终端按 Enter 继续。

**运行**

**双击 `ui_app.pyw`** 直接打开界面。

（`ui_app.py` 和 `ui_app.pyw` 内容完全一样，区别只是双击 `.py` 的话 Windows 会额外弹一个
黑色命令行窗口跟在 UI 后面，关掉黑框 UI 也会跟着关掉；也可以在终端里运行
`python ui_app.py`，效果跟双击 `.pyw` 一样。）

前提：浏览器已经按上面步骤启动调试端口、登录、打开目标 scene、停在第 1 帧。

- 填 **Scene Number** / **Frame Count**（留空用 `config.yaml` 默认值），点 **Start Inspection**——
  实际是在后台起子进程跑 `main.py`，把 scene_id / 帧数 / 确认回车自动喂给它，界面上会显示
  实时进度条和状态
- 跑完之后 `bbox_data.csv` / `rule_report.csv` / `rule_summary.json` 三行会变成可点的
  **Open** 按钮，或者点 **Open Report Folder** 直接打开整个报告目录
- **Cleanup Scene** 区域：填 Scene Number，点 **Delete Scene Files**，弹窗要求输入 `DELETE`
  二次确认，才会真正调用 `cleanup_scene.py` 删除这个场景的本地文件

UI 本身不重新实现任何业务逻辑，只是把 `main.py` / `cleanup_scene.py` 当成两个可执行脚本调用
（子进程 + stdin），跟你在终端里手动运行完全一样。

### 1.2 Ubuntu 配置与启动



**第一次配置（仅需一次）**

1. 安装系统依赖

   ```bash
   sudo apt update
   sudo apt install -y python3-pip python3-venv python3-tk
   ```

2. 进入项目目录

   ```bash
   cd ~/Downloads/QP-copilot-main
   ```

3. 创建虚拟环境

   ```bash
   python3 -m venv .venv
   ```

4. 激活虚拟环境

   ```bash
   source .venv/bin/activate
   ```

5. 安装项目依赖

   ```bash
   pip install -r requirements.txt
   ```

   如果下载失败，可执行 `pip cache purge` 后重新安装：

   ```bash
   pip install --no-cache-dir -r requirements.txt
   ```

**每次使用**

1. 启动 Edge（调试模式）

   ```bash
   microsoft-edge-stable \
     --remote-debugging-port=9222 \
     --user-data-dir="$HOME/edge-qp-debug"
   ```

2. 登录 QP：浏览器打开后登录账号、打开需要质检的 Scene、保持浏览器不要关闭

3. 进入项目目录

   ```bash
   cd ~/Downloads/QP-copilot-main
   ```

4. 激活虚拟环境

   ```bash
   source .venv/bin/activate
   ```

5. 启动程序

   ```bash
   python ui_app.py
   ```

---

## 2. 输出文件一览

全部在 `outputs/reports/`（截图在 `outputs/screenshots/`），文件名固定，每次运行覆盖。

**最重要的两个、人工质检直接看这两个就够了**：

| 文件 | 内容 |
|---|---|
| `rule_report.csv` | **质检结论**：每一条规则命中记录（哪个目标、哪一帧、什么问题、`first_frame`/`last_frame`/`occurrence_count` 持续区间），含抓取失败的 `MissingBBox` 记录 |
| `rule_summary.json` | **质检结论汇总**：总帧数/总 BBox 数/总 warning 数、按 rule_id 分类的命中数、有问题的帧号和目标列表、`missing_bbox_frames`，跑完会直接打印在终端/UI 里，不用打开文件也能先看个大概 |

其余是采集过程的中间数据/诊断信息，一般不需要单独看，出问题时排查用：

| 文件 | 内容 |
|---|---|
| `bbox_data.csv` | 每帧每个框的完整原始字段（位置/朝向/尺寸/身份标识等，见第 4 节），是 `rule_report.csv` 的输入数据 |
| `capture_report.csv` | 每帧的切帧状态 / 就绪状态 / PCD-JPG 数量 / 截图路径 |
| `network_assets.csv` | 每帧实际捕获到的 PCD/JPG 请求 URL |
| `mapping_report.txt` | 这次实际拿到了哪些 BBox 字段、建议用哪个字段做跨帧标识 |
| `bbox_probe_report.json` | BBox 数据来源的广撒网探测报告（调试用，见第 4 节） |

---

## 3. 切帧：`visible_text_bottom` 模式

全局 `text=数字` 选择器会误匹配页面上其它"长得像帧号"的元素（比如标注对象的
`track-id`）。现在的做法是三层过滤：**精确文本匹配** → 只留**可见**元素 → 只留
落在**页面底部区域**（`bottom_region_ratio`，默认视口下 25%）的元素，多个候选取
最靠近底部的那个。一个候选都过滤不出来时（比如帧号栏做了虚拟滚动），自动 fallback
到坐标点击（`timeline_start_x`/`timeline_y`/`frame_step_px`），记一条 warning，不会中断。

```yaml
navigation:
  mode: "visible_text_bottom"
  bottom_region_ratio: 0.75   # 帧号栏靠页面上方的话调小，比如 0.5
  fallback_mode: "coordinates"
  timeline_start_x: 200        # fallback 用：第 1 帧的 x 坐标（F12 里悬停量出来）
  timeline_y: 780
  frame_step_px: 12            # 每往后一帧 x 增加多少像素
```

终端每次切帧会打印一行诊断日志（`文本候选`/`可见`/`底部候选`/`fallback`），
`底部候选=0` 多的话就是 `bottom_region_ratio` 需要调整。

---

## 4. BBox 数据与质检规则引擎

BBox 数据在 `window.viewer` 的 Three.js 场景图里，类型是 `BoxVolume` 的节点上
（`bbox_extractor.py` 递归查找 `constructor.name === 'BoxVolume'`，不认定固定属性名，
因为 `recycleVolumes`/`annotationGroup` 实测不总是指向当前渲染的框）。`position`/
`rotation`/`scale`/`trackId`/`className` 都确认能读到真实值，跟画面上的标注框对得上。

`bbox_data.csv` 主要字段：`scene_id, frame_index, bbox_index, track_id, target_id,
uuid, object_id, label, className, position_x/y/z, rotation_x/y/z, scale_x/y/z,
visible, resolved_target_field, resolved_target_key`。`target_id`/`object_id`/`label`
在当前平台上没有对应数据，列会保留、值留空。

**跨帧目标标识**：按 `track_id > target_id > uuid > object_id > bbox_index` 优先级，
`bbox_extractor.py` 已经算好写进 `resolved_target_field`/`resolved_target_key` 两列，
规则引擎和以后新增的规则都直接用这两列，不用重新判断优先级。

### 当前已实现的规则

全部在 `config.yaml -> rule_engine.enabled_rules` 里，跨帧比较用的就是上面的
`resolved_target_key`：

| rule_id | 说明 | 阈值参数 |
|---|---|---|
| `LabelConsistency` | 同一目标跨帧 className 不能变 | 无 |
| `PositionJump` | 同一目标相邻帧位置距离变化超过阈值 | `position_jump_threshold`（默认 25 米） |
| `RotationJump` | 同一目标相邻帧 `rotation_z` 变化超过阈值（已处理角度环绕） | `rotation_jump_threshold`（默认 1 弧度） |
| `SizeConsistency` | 同一目标相邻帧 `scale_x/y/z` 任一轴变化比例超过阈值 | `size_change_threshold`（默认 30%） |
| `BrokenTrack` | 同一目标连续消失 ≤ N 帧后又出现（消失区间跟"整帧 0 个框"重叠时不报，避免帧级问题被当成每个目标的追踪问题重复报） | `broken_track_gap`（默认 3 帧） |
| `EmptyFrame` | 某帧一个框都没有 | 无 |
| `SizeOutlier` | 框任一尺寸小于/大于合理范围 | `size_outlier_min`（0.1 米）/ `size_outlier_max`（30 米） |
| `StaticObjectPosition` | 静止目标（灯塔/岸桥/场桥/雪糕筒等，见 `static_object_classes`）相邻帧位置变化超过阈值 | `static_object_position_threshold`（默认 0.5 米） |

`PositionJump`/`RotationJump`/`SizeConsistency`/`StaticObjectPosition` 都要求
`frame_index` 正好相邻（差 1）才比较——跳过的帧不计入，避免几帧累积的正常位移被
误判成"一帧内的跳变"。同一目标同一种问题如果连续帧持续命中，只保留第一次发现的
记录，`rule_report.csv` 里的 `first_frame`/`last_frame`/`occurrence_count` 三列
记录持续到哪一帧、共命中几次。

### 新增一条规则

在 `rule_engine.py` 里写一个函数、注册到 `RULE_REGISTRY`，再把名字加进
`config.yaml -> rule_engine.enabled_rules`，不需要改这个文件之外的任何模块：

```python
def my_rule(ctx: RuleContext) -> list[RuleFinding]:
    findings = []
    for frame_index, boxes in ctx.frames.items():
        for b in boxes:
            if float(b["scale_x"] or 0) > 20:
                findings.append(RuleFinding(
                    scene_id=ctx.scene_id, frame_index=frame_index,
                    track_id=b["resolved_target_key"], bbox_index=b["bbox_index"],
                    label=b["className"], rule_id="box_too_long", severity="Warning",
                    message=f"scale_x={b['scale_x']} 超过阈值",
                ))
    return findings

RULE_REGISTRY["box_too_long"] = my_rule
```

`ctx.tracks[key]` 是已经按 `frame_index` 排好序的 `(frame_index, box_dict)` 列表，
跨帧类规则参考 `rule_position_jump` 的写法。

### 只重跑规则，不重新采集

`bbox_data.csv` 采好之后，调规则不需要重新打开浏览器：

```bash
python rule_engine.py                                # 默认读 outputs/reports/bbox_data.csv
python rule_engine.py outputs/reports/bbox_data.csv     # 也可以显式指定路径
```

### 单帧 BBox 读取失败不会中断

某一帧因为平台原因（页面未加载完成、JS 报错、网络延迟等）读取 BBox 失败，`main.py`
只记下这一帧继续处理下一帧，不重试不中断。`rule_report.csv` 会有一条
`rule_id=MissingBBox, severity=Warning` 的记录，`rule_summary.json` 的
`missing_bbox_frames` 会列出所有失败的帧号。`bbox_data.csv` 依然会生成（哪怕全部帧
都失败，也是一份只有表头的文件）。只有文件写入本身失败（磁盘满/没权限）这种程序级
异常才会让程序真正终止。

### `bbox_probe_report.json`：调试用的广撒网探测

`bbox_probe.py` 在确认 `bbox_extractor.py` 的精确提取位置之前，用来"探测数据大概在哪"——
按关键词扫描 `window` 全局变量和 Three.js 场景，全程只读。现在位置已经确认，这份报告
主要留作调试/以后平台改版时重新定位用，日常不需要看。

---

## 5. 清理某个 scene 的本地数据

```bash
python cleanup_scene.py              # 交互式：列出要删的文件，输入 DELETE 二次确认才真删
python cleanup_scene.py --dry-run     # 只看会删哪些文件，不会真的删
```

（UI 里 **Cleanup Scene** 区域是同一个脚本的图形化入口）

输入 scene_id 后，会在 `outputs/reports`、`outputs/screenshots`、`outputs/assets`
三个目录里找**文件名或内容包含这个 scene_id** 的文件（截图靠文件名，固定名字的报告文件
靠内容里的 `scene_id` 字段；`mapping_report.txt` 内容不带 scene_id，如果同目录下已经有
别的文件匹配上了，会一并纳入清理），列出清单，输入 `DELETE`（一字不差）才会真正删除。

安全边界：只在这三个目录里找，不会碰项目代码；只删文件，不删 `outputs/` 或子目录本身；
`.gitkeep` 不参与匹配；`scene_id` 留空直接退出，不做任何事。

---

## 6. 常见问题

- **截图空白 / 点云没渲染**：调大 `frame_ready.render_settle_ms`（网络加载完成不等于
  3D 渲染完成，实测过 BBox 渲染经常滞后网络确认 1~3 帧的时间，这是固定延迟，不保证
  100% 覆盖所有滞后情况）。
- **某几帧总是等到超时**：调大 `frame_ready.timeout_seconds` 或 `frame_ready.quiet_ms`。
- **`network_assets.csv` 里 pcd_count/jpg_count 都是 0**：默认 `network.grouping_mode:
  "timestamp"` 按 URL 里资源自带的时间戳分组，不依赖清空缓冲区的时机；如果平台 URL
  提取不出时间戳，改成 `"window"` 退回老式做法。
- **想在同一批截图上重新跑 AI/规则分析**（不用重新打开浏览器）：
  `python analyze.py`（读 `capture_report.csv`，走的是 `analyzers.py` 里的旧版可插拔
  框架，第一版为空；BBox 相关质检走的是 `rule_engine.py`，见第 4 节）。

---

## 7. 安全边界

**全程不会**：自动点击"合格/驳回/提交"等修改平台评审状态的按钮；修改、删除、覆盖 QP
平台上的任何数据；读取、保存、上传账号密码（登录全程手动完成）。

**只会**：切帧（模拟按键/点击帧号/点击坐标）；被动监听网络请求（不拦截不修改不重放）；
截图到本地 `outputs/`；在浏览器 Console context 里执行只读 JS；生成本地 CSV/JSON 报告。

---

## 8. 目录结构

```
qp_copilot/
├── ui_app.py / ui_app.pyw   # 桌面 UI（推荐入口），子进程调用 main.py / cleanup_scene.py
├── main.py                    # 抓取阶段入口：切帧 + 就绪等待 + 截图 + 网络记录 + BBox 提取
├── rule_engine.py                # 质检规则引擎：读 bbox_data.csv 跑规则
├── cleanup_scene.py                 # 按 scene_id 清理 outputs/ 下的本地文件
├── analyze.py                          # 旧版分析入口：读 capture_report.csv，见第 6 节
├── browser.py / navigator.py / frame_ready.py / capture.py / network_recorder.py
│                                          # 浏览器连接、切帧、就绪判断、截图、网络记录
├── bbox_probe.py / bbox_extractor.py         # BBox 广撒网探测 / 针对性提取
├── report.py / analyzers.py                     # capture_report.csv 读写 + 旧版分析框架
├── config.yaml                                     # 所有可调参数
├── requirements.txt / README.md
└── outputs/
    ├── screenshots/    # 每帧截图
    ├── assets/         # 保留目录（本版本不使用）
    └── reports/         # 见第 2 节「输出文件一览」
```

---

## 9. 后续规划（不在第一版范围内）

- 接入 AI 视觉模型：结合截图做画面级别的标注问题识别（漏标、错标、类别错误等
  BBox 几何数据本身看不出来的问题）。


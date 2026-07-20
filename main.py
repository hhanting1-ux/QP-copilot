"""
main.py
QP Copilot 主流程入口 —— 负责"抓取"阶段：切帧 + 就绪等待 + 截图 + 网络资源记录 +
BBox 只读探测/提取。判断分析（AI 视觉）不在这里做，见 analyze.py。

流程：
1. 用户手动在浏览器里登录 QP 平台、打开某个 scene，停在第 1 帧（脚本不碰账号密码）。
2. 运行本脚本，脚本连接到该浏览器（或按配置启动新浏览器）。
3. 用户输入本次场景的总帧数（直接回车用 config.yaml 里的默认值）。
4. 用户在终端按 Enter，确认已经登录、已经停在第 1 帧。
5. 脚本自动循环用户输入的帧数，每一帧：
   a. 第 1 帧不切帧，直接等待就绪；其余帧用可见文本+底部区域过滤找到帧号并点击
      （找不到候选时自动 fallback 到坐标点击，见 navigator.py）
   b. wait_for_frame_ready()：等抓到期望数量的 PCD/JPG + 网络安静一段时间 + 渲染稳定
   c. 整页截图
   d. 记录这一帧的切帧状态、就绪状态、PCD/JPG 数量、截图路径
   e. 提取这一帧的 BBox 几何数据（位置/朝向/尺寸，见 bbox_extractor.py）——如果这一帧
      读取失败（页面未加载完成/JS 异常/网络延迟等），只记下 frame_index + 原因，
      不会重试、不会中断，继续下一帧
   f. 任何一步失败都不会终止程序，只记录 warning，继续下一帧
6. 全部帧采集完成后（不是边采集边判断）：
   - 生成 outputs/reports/capture_report.csv     （每帧抓取结果 + 判断分析占位字段）
   - 生成 outputs/reports/network_assets.csv     （每帧实际捕获到的 PCD/JPG URL）
   - 生成 outputs/reports/bbox_probe_report.json （BBox 只读探测报告，探测本身不阻塞主流程）
   - 生成 outputs/reports/bbox_data.csv          （每帧的 BBox 几何数据，即使部分/全部帧
                                                    提取失败也会生成，只是对应帧没有行）
   - 生成 outputs/reports/mapping_report.txt     （字段填充情况 + 目标标识建议）
   - 读取 bbox_data.csv，运行质检规则引擎（第一版规则为空，只是框架，见 rule_engine.py），
     BBox 读取失败的帧会额外生成一条 rule_id=MissingBBox 的记录
   - 生成 outputs/reports/rule_report.csv        （规则命中结果，含 MissingBBox）
   - 生成 outputs/reports/rule_summary.json      （命中统计 + missing_bbox_frames 列表）

单帧级别的任何失败（切帧/就绪/截图/BBox 提取）都只记录、不中断；只有文件写入这种
程序级异常（比如 bbox_data.csv 写不出去）才会让程序真正终止。

本脚本只读页面、截图、生成本地文件，不会点击"提交/合格/驳回"等按钮，
也不会修改平台上的任何数据。
"""

from __future__ import annotations

import sys

import yaml

from analyzers import run_analyzers
from bbox_extractor import extract_bbox, save_mapping_report, write_bbox_csv
from bbox_probe import probe_frame, build_probe_report, save_probe_report
from browser import BrowserSession
from capture import take_frame_screenshot
from frame_ready import wait_for_frame_ready
from navigator import FrameNavigationError, FrameNavigator
from network_recorder import NetworkRecorder, write_asset_csv
from report import build_frame_record, write_report
from rule_engine import missing_bbox_finding, print_rule_summary, run_rules, write_rule_report, write_rule_summary


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def prompt_scene_id(config: dict) -> str:
    """场景 ID 用于截图/报告命名。config 里没填的话，运行时手动输入。"""
    scene_id = (config["scene"].get("scene_id") or "").strip()
    if scene_id:
        return scene_id

    scene_id = input("请输入 scene_id（用于截图和报告命名，可留空）: ").strip()
    return scene_id or "scene"


def prompt_frame_count(config: dict) -> int:
    """询问本次场景的总帧数。直接回车用 config.yaml 里 scene.frame_count 的默认值。

    切帧主要靠"可见文本+底部区域过滤"动态定位，不依赖固定坐标，帧数变化不影响这条路径；
    只有文本方式找不到候选、触发坐标 fallback 时才可能受帧栏布局变化影响（见 README）。
    """
    default = config["scene"].get("frame_count", 81)
    raw = input(f"请输入总帧数（默认{default}）：").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"[警告] 输入的不是数字，使用默认值 {default}")
        return default
    if value <= 0:
        print(f"[警告] 帧数必须大于 0，使用默认值 {default}")
        return default
    return value


def confirm_ready_to_start(nav_mode: str) -> None:
    print()
    print("请确认：")
    print("  1) 已经手动登录 QP 平台")
    print("  2) 已经打开目标 scene，并停在第 1 帧")
    print(f"  3) navigation.mode = {nav_mode}：从第 2 帧开始，脚本会按这个模式自动切帧")
    print("  4) 脚本运行期间不会点击『合格/驳回/提交』等按钮，只会截图、翻页和只读探测")
    input("准备好后按 Enter 开始自动浏览...")


def run(config_path: str = "config.yaml") -> None:
    config = load_config(config_path)

    scene_id = prompt_scene_id(config)
    frame_count = prompt_frame_count(config)
    start_index = config["scene"]["start_frame_index"]
    screenshot_cfg = config["screenshot"]
    report_cfg = config["report"]
    network_cfg = config["network"]
    frame_ready_cfg = config["frame_ready"]
    bbox_cfg = config["bbox_probe"]
    bbox_extract_cfg = config.get("bbox_extract", {})
    rule_engine_cfg = config.get("rule_engine", {})
    expected_pcd = network_cfg.get("expected_pcd_count", 1)
    expected_jpg = network_cfg.get("expected_jpg_count", 5)

    confirm_ready_to_start(config["navigation"].get("mode", "visible_text_bottom"))

    session = BrowserSession(config)
    try:
        page = session.start()
        print(f"[信息] 已连接页面: {page.url}")
    except Exception as exc:
        print(f"[错误] 浏览器连接失败: {exc}")
        sys.exit(1)

    navigator = FrameNavigator(page, config)
    recorder = NetworkRecorder(page, network_cfg) if network_cfg.get("enabled", True) else None

    records = []
    asset_records = []
    probe_results = []
    bbox_records = []
    missing_bbox_frames = []  # BBox 读取失败的帧号（不是"这一帧恰好没有框"，是真的读取失败）
    has_probed_once = False  # first_frame_only：探测"第一个成功抓取的帧"，不是"循环第 0 次"

    print(f"[信息] 开始自动浏览 {frame_count} 帧（scene_id={scene_id}）...")

    for i in range(frame_count):
        frame_index = start_index + i
        is_first = i == 0
        print(f"[进度] 第 {i + 1}/{frame_count} 帧 (frame_index={frame_index}) ...", end=" ")

        warnings = []
        navigation_status = "ok"

        if recorder:
            recorder.mark_frame_start()

        # ---- 1. 切帧（第 1 帧不切，直接等待就绪）----
        try:
            nav_result = navigator.goto_frame(frame_index, is_first=is_first)
            if nav_result.used_fallback:
                navigation_status = "warning"
                warnings.append(nav_result.warning)
        except FrameNavigationError as exc:
            navigation_status = "warning"
            warnings.append(f"切帧失败: {exc}")
            print(f"\n[警告] 第 {frame_index} 帧切帧失败: {exc}")

        # ---- 2. 等待就绪（数量达标 + 网络安静 + 渲染稳定）----
        if recorder:
            ready_result = wait_for_frame_ready(page, recorder, frame_ready_cfg)
        else:
            # 没启用网络记录就没法判断"资源是否加载完成"，退化成固定等待渲染稳定时间
            page.wait_for_timeout(frame_ready_cfg.get("render_settle_ms", 500))
            ready_result = None

        if ready_result and ready_result.ready_status != "ok":
            warnings.append(f"就绪等待超时: {ready_result.reason}")
            print(f"\n[警告] 第 {frame_index} 帧就绪等待超时: {ready_result.reason}")

        # ---- 3. 截图（不管前面是否有 warning，都尝试截图，方便人工核查当时画面）----
        screenshot_result = take_frame_screenshot(page, scene_id, frame_index, screenshot_cfg)
        if not screenshot_result.success:
            warnings.append(f"截图失败: {screenshot_result.error_message}")
            print(f"\n[警告] 第 {frame_index} 帧截图失败: {screenshot_result.error_message}")
        else:
            print(f"完成 -> {screenshot_result.screenshot_path}")

        # ---- 4. 记录这一帧实际捕获到的 PCD/JPG ----
        if recorder:
            asset_record = recorder.snapshot(scene_id, frame_index)
            asset_records.append(asset_record)
            if asset_record.status(expected_pcd, expected_jpg) == "warning":
                warning_text = asset_record.warning_text(expected_pcd, expected_jpg)
                warnings.append(warning_text)
                print(f"  [警告] 第 {frame_index} 帧资源数量异常：{warning_text}")
            pcd_count, jpg_count = asset_record.pcd_count, asset_record.jpg_count
        else:
            pcd_count = jpg_count = 0

        record = build_frame_record(
            scene_id=scene_id,
            frame_index=frame_index,
            screenshot_path=screenshot_result.screenshot_path if screenshot_result.success else "",
            navigation_status=navigation_status,
            ready_status=ready_result.ready_status if ready_result else "ok",
            pcd_count=pcd_count,
            jpg_count=jpg_count,
            warning="; ".join(warnings),
        )
        records.append(record)

        # ---- 5. BBox 只读探测（不阻塞主流程：探测本身出任何问题都只是跳过，不影响截图/记录）----
        probe_mode = bbox_cfg.get("mode", "disabled")
        should_probe = bbox_cfg.get("enabled", False) and screenshot_result.success and (
            probe_mode == "every_frame" or (probe_mode == "first_frame_only" and not has_probed_once)
        )
        if should_probe:
            try:
                print(f"  [信息] 正在对第 {frame_index} 帧做 BBox 只读探测...")
                probe_results.append(probe_frame(page, frame_index, bbox_cfg))
                has_probed_once = True
            except Exception as exc:
                print(f"  [警告] 第 {frame_index} 帧 BBox 探测出现异常（已跳过，不影响主流程）: {exc}")

        # ---- 6. BBox 几何数据提取（只是采集，不在这里做任何判断/规则检查）----
        # 不依赖截图是否成功——BBox 是独立读页面 JS 状态，跟截图是两回事。
        # 单帧提取失败（页面未加载完成/JS 异常/网络延迟等）只记录 frame_index，
        # 不会中断程序、不会影响后续帧的采集。
        if bbox_extract_cfg.get("enabled", True):
            bbox_result = extract_bbox(page, scene_id, frame_index)
            if bbox_result.success:
                bbox_records.extend(bbox_result.records)
            else:
                # extract_bbox() 内部已经打印过具体原因；这里只需要记住是哪一帧、什么原因，
                # 供采集全部结束后写进 rule_report.csv（MissingBBox）和 rule_summary.json。
                missing_bbox_frames.append((frame_index, bbox_result.error))

    print()
    ok_count = sum(1 for r in records if not r.warning)
    print(f"[信息] 自动浏览结束，共 {len(records)} 帧，其中 {ok_count} 帧完全正常，{len(records) - ok_count} 帧有 warning。")

    if recorder:
        failed = recorder.total_failed_requests()
        if failed:
            print(f"[信息] 整场运行中有 {len(failed)} 个 PCD/JPG 请求在网络层面直接失败（未产生 response）：")
            for f in failed[:20]:
                print(f"  - [{f['reason']}] {f['url']}")
            if len(failed) > 20:
                print(f"  ...（还有 {len(failed) - 20} 条，未全部列出）")

    session.close(close_browser=False)  # CDP 模式下不关闭用户的浏览器，抓取阶段到此结束

    # ---- 落盘：报告、网络资源 CSV、BBox 探测报告 ----
    if recorder and asset_records:
        asset_csv_path = write_asset_csv(asset_records, network_cfg, report_cfg)
        print(f"[信息] 网络资源记录已生成: {asset_csv_path}")

    if probe_results:
        try:
            probe_report = build_probe_report(probe_results, scene_id)
            probe_report_path = save_probe_report(probe_report, bbox_cfg, report_cfg)
            found = "找到疑似 BBox 数据" if probe_report["found_candidates"] else "未找到 BBox 数据"
            print(f"[信息] BBox 探测报告已生成: {probe_report_path}（{found}，详情见文件）")
        except Exception as exc:
            print(f"[警告] BBox 探测报告生成失败（不影响其它报告）: {exc}")

    # BBox 数据文件只要功能是开启的就会生成，即使某些/全部帧提取失败——
    # 这样 rule_engine 才总能拿到一份 bbox_data.csv（哪怕只有表头），完成"5. 必须跑完全部帧的
    # 采集和规则检查，不因个别帧失败而中断"这条要求。只有 write_bbox_csv 这种文件写入本身
    # 失败（磁盘满/权限问题）才会抛异常导致程序终止，这是有意为之的程序级异常。
    bbox_csv_path = None
    if bbox_extract_cfg.get("enabled", True):
        bbox_csv_path = write_bbox_csv(bbox_records, report_cfg, bbox_extract_cfg)
        print(f"[信息] BBox 几何数据已生成: {bbox_csv_path}（共 {len(bbox_records)} 条）")
        if missing_bbox_frames:
            missing_indexes = [frame_index for frame_index, _ in missing_bbox_frames]
            print(f"[信息] 其中 {len(missing_bbox_frames)} 帧 BBox 读取失败: {missing_indexes}")

        mapping_report_path = save_mapping_report(bbox_records, report_cfg)
        print(f"[信息] 字段映射报告已生成: {mapping_report_path}")

    records = [run_analyzers(r, config) for r in records]  # 第一版 enabled_analyzers 为空，原样返回

    report_path = write_report(records, report_cfg)
    print(f"[信息] 报告已生成: {report_path}")

    # ---- 采集阶段到此结束，全部帧都跑完、bbox_data.csv 已经落盘之后，才开始跑规则检查 ----
    # 不在逐帧循环里做判断：Rule Engine 只读 bbox_data.csv，不连浏览器、不影响采集过程。
    if bbox_csv_path:
        print()
        print("[信息] 采集阶段完成，开始运行质检规则引擎...")
        # 本次场景理论上应该采集的完整帧号列表，传给 EmptyFrame/BBoxCountChange 这类
        # 需要知道"整帧缺失"的规则——只看 bbox_data.csv 里实际出现过的帧号是不够的，
        # 因为 0 个框的帧在 bbox_data.csv 里本来就没有对应的行。
        expected_frame_indices = list(range(start_index, start_index + frame_count))

        findings = run_rules(bbox_csv_path, scene_id, config, expected_frame_indices)

        # 单帧 BBox 读取失败不算常规规则命中，但要跟规则命中一起进 rule_report.csv，
        # 方便人工在同一份表里看到"这一帧到底是规则不合格，还是压根没读到数据"。
        for frame_index, reason in missing_bbox_frames:
            findings.append(missing_bbox_finding(scene_id, frame_index, reason))

        rule_report_path = write_rule_report(findings, report_cfg, rule_engine_cfg)
        enabled_rules = rule_engine_cfg.get("enabled_rules", []) or []
        if enabled_rules:
            print(f"[信息] 规则引擎运行结束，共命中 {len(findings)} 条 finding: {rule_report_path}")
        else:
            print(f"[信息] rule_engine.enabled_rules 目前为空，"
                  f"规则报告只包含 MissingBBox 记录（如果有）: {rule_report_path}")

        summary_path, summary = write_rule_summary(
            bbox_csv_path,
            findings,
            [frame_index for frame_index, _ in missing_bbox_frames],
            scene_id,
            report_cfg,
            expected_frame_indices,
        )
        print(f"[信息] 规则汇总已生成: {summary_path}")
        print_rule_summary(summary)

    print("[信息] 完成。")


if __name__ == "__main__":
    run()

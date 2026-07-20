"""
navigator.py
QP 平台切帧的唯一支持方式：可见文本 + 底部区域过滤（navigation.mode = visible_text_bottom）。

背景：
  - 全局 text=数字 选择器会误匹配页面其它地方"长得像帧号"的文本，比如：
      <span class="track-id">5</span>        （标注对象 ID，不是帧号）
      <span class="mark-name">3D (46)</span>  （不是帧号，且用精确匹配已经排除）
    这些元素文本包含目标数字，但既不在底部帧号栏、通常也不在可视区域内。
  - 但 QP 平台底部帧号栏确实是用纯文本渲染帧号的（用 text= 偶尔能点成功，说明目标元素
    本身是有真实 DOM 文本节点的），所以不能完全放弃文本匹配，只需要再加两层过滤：
      1. 只保留"可见"的候选元素（排除 display:none / 不在渲染树里的隐藏元素）
      2. 只保留 bounding_box 落在页面底部区域（y > viewport_height * bottom_region_ratio）的候选
    多个候选时，选 y 坐标最大（最靠近页面底部）的那个，点击其中心点。

  如果一个候选都过滤不出来（比如帧号栏做了虚拟滚动，目标帧号暂时不在 DOM 里），
  不会报错终止，而是自动 fallback 到坐标点击（navigation.fallback_mode），并记录 warning。
"""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Page


class FrameNavigationError(Exception):
    """切帧彻底失败时抛出（可见文本匹配 + fallback 都失败）。
    main.py 会捕获并记录 warning、继续下一帧，不会中断整个流程。"""


@dataclass
class NavigationResult:
    """一次切帧操作的诊断信息，用于日志输出和写进 capture_report.csv 的 warning 列。"""
    frame_index: int
    text_candidates: int = 0
    visible_candidates: int = 0
    bottom_candidates: int = 0
    clicked_x: float | None = None
    clicked_y: float | None = None
    used_fallback: bool = False
    warning: str = ""


class FrameNavigator:
    """封装"可见文本 + 底部区域过滤"切帧逻辑，找不到候选时自动 fallback 到坐标点击。"""

    def __init__(self, page: Page, config: dict):
        self.page = page
        self.nav_cfg = config["navigation"]
        self.start_frame_index = config["scene"].get("start_frame_index", 1)

        mode = self.nav_cfg.get("mode", "visible_text_bottom")
        if mode != "visible_text_bottom":
            raise ValueError(f"未知的 navigation.mode: {mode}，当前版本只支持 'visible_text_bottom'")

    def goto_frame(self, frame_index: int, is_first: bool) -> NavigationResult:
        """切到指定帧，返回诊断信息（供日志输出 / 写进报告）。

        is_first=True（循环第一帧）：假设用户已经手动停在第 1 帧，不做任何操作。
        """
        if is_first:
            return NavigationResult(frame_index=frame_index)

        result = self._find_by_visible_text(frame_index)

        if result.clicked_x is None:
            result = self._fallback_to_coordinates(frame_index, result)

        print(
            f"[导航] frame_index={frame_index} "
            f"文本候选={result.text_candidates} 可见={result.visible_candidates} "
            f"底部候选={result.bottom_candidates} "
            f"点击坐标=({result.clicked_x}, {result.clicked_y}) "
            f"fallback={'是' if result.used_fallback else '否'}"
        )
        return result

    # ------------------------------------------------------------------
    def _get_viewport_height(self) -> float | None:
        """读取当前页面的实际视口高度。

        不用 page.viewport_size —— 那个属性只有 Playwright 自己启动/设置过 viewport 的页面
        才有值；用 connect_over_cdp 连接你手动打开的已有 Chrome 窗口时（本工具推荐、默认的
        browser.mode），它几乎总是 None。之前就是因为这里直接判了 None 就短路返回"0 个候选"，
        导致可见文本匹配从来没真正执行过一次，每一帧都误触发了坐标 fallback。
        改成读 window.innerHeight，CDP 和 launch 两种连接方式下都可靠。
        """
        try:
            height = self.page.evaluate("() => window.innerHeight")
            if height:
                return float(height)
        except Exception:
            pass
        return None

    def _find_by_visible_text(self, frame_index: int) -> NavigationResult:
        """获取页面中所有文本精确等于目标帧号的元素，依次过滤"可见"和"底部区域"。"""
        label_format = self.nav_cfg.get("frame_number_format", "{n}")
        label = label_format.format(n=frame_index)
        bottom_ratio = self.nav_cfg.get("bottom_region_ratio", 0.75)
        offset_x = self.nav_cfg.get("click_offset_x", 0)
        offset_y = self.nav_cfg.get("click_offset_y", 0)

        result = NavigationResult(frame_index=frame_index)

        viewport_height = self._get_viewport_height()
        if viewport_height is None:
            return result  # 取不到视口高度就没法判断"底部区域"，直接走 fallback

        bottom_threshold = viewport_height * bottom_ratio

        # 带引号 = 精确文本匹配（不是子串），先排除 "3D (46)" 这类整体文本不相等的元素
        candidates = self.page.locator(f'text="{label}"').all()
        result.text_candidates = len(candidates)

        visible_boxes = []
        for el in candidates:
            try:
                if not el.is_visible():
                    continue
                box = el.bounding_box()
            except Exception:
                continue
            if box is not None:
                visible_boxes.append(box)
        result.visible_candidates = len(visible_boxes)

        bottom_boxes = [b for b in visible_boxes if b["y"] > bottom_threshold]
        result.bottom_candidates = len(bottom_boxes)

        if not bottom_boxes:
            return result

        # 多个候选时，选最靠近页面底部（y 最大）的那个——最贴近时间轴区域
        target = max(bottom_boxes, key=lambda b: b["y"])
        click_x = target["x"] + target["width"] / 2 + offset_x
        click_y = target["y"] + target["height"] / 2 + offset_y

        try:
            self.page.mouse.click(click_x, click_y)
        except Exception:
            return result  # 点击失败也交给 fallback 处理，这里不抛异常

        result.clicked_x = click_x
        result.clicked_y = click_y
        return result

    def _fallback_to_coordinates(self, frame_index: int, prev_result: NavigationResult) -> NavigationResult:
        """可见文本 + 底部过滤没找到候选时的兜底：直接按坐标点击（不看 DOM/选择器）。"""
        fallback_mode = self.nav_cfg.get("fallback_mode", "coordinates")
        if fallback_mode != "coordinates":
            raise FrameNavigationError(
                f"第 {frame_index} 帧：可见文本+底部过滤没找到候选元素"
                f"（文本候选 {prev_result.text_candidates}，可见 {prev_result.visible_candidates}，"
                f"底部区域 {prev_result.bottom_candidates}），且 navigation.fallback_mode 不是 "
                f"'coordinates'（当前是 {fallback_mode!r}），无法继续切帧。"
            )

        try:
            start_x = self.nav_cfg["timeline_start_x"]
            y = self.nav_cfg["timeline_y"]
            step_px = self.nav_cfg["frame_step_px"]
        except KeyError as exc:
            raise FrameNavigationError(
                f"第 {frame_index} 帧：fallback 坐标点击需要 config.yaml 里配置 "
                "navigation.timeline_start_x / timeline_y / frame_step_px，见 README。"
            ) from exc

        x = start_x + (frame_index - self.start_frame_index) * step_px
        try:
            self.page.mouse.click(x, y)
        except Exception as exc:
            raise FrameNavigationError(
                f"第 {frame_index} 帧：可见文本+底部过滤没找到候选，fallback 坐标点击也失败了"
                f"（x={x}, y={y}）: {exc}"
            ) from exc

        prev_result.clicked_x = x
        prev_result.clicked_y = y
        prev_result.used_fallback = True
        prev_result.warning = (
            f"可见文本+底部过滤没找到帧号 {frame_index} 的候选元素"
            f"（文本候选 {prev_result.text_candidates} 个，可见 {prev_result.visible_candidates} 个，"
            f"底部区域 {prev_result.bottom_candidates} 个），已 fallback 到坐标点击 ({x:.0f}, {y:.0f})。"
        )
        return prev_result

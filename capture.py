"""
capture.py
负责"截图保存"。判断一帧是否加载完成不在这里做，见 frame_ready.py
（这里只管截图这一个动作，"什么时候截图"由调用方决定）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Page


@dataclass
class CaptureResult:
    """一帧截图操作的结果。"""
    frame_index: int
    screenshot_path: str
    timestamp: str
    success: bool
    error_message: str = ""


def take_frame_screenshot(
    page: Page,
    scene_id: str,
    frame_index: int,
    screenshot_cfg: dict,
) -> CaptureResult:
    """对当前帧整页截图，保存到 screenshot_cfg['output_dir']，返回 CaptureResult。"""
    output_dir = Path(screenshot_cfg["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    fmt = screenshot_cfg.get("format", "png").lower()
    safe_scene_id = scene_id or "unknown_scene"
    filename = f"{safe_scene_id}_frame_{frame_index:03d}.{fmt}"
    filepath = output_dir / filename

    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    screenshot_kwargs = {
        "path": str(filepath),
        "full_page": screenshot_cfg.get("full_page", True),
    }
    if fmt == "jpeg":
        screenshot_kwargs["type"] = "jpeg"
        screenshot_kwargs["quality"] = screenshot_cfg.get("jpeg_quality", 90)

    try:
        page.screenshot(**screenshot_kwargs)
        return CaptureResult(
            frame_index=frame_index,
            screenshot_path=str(filepath),
            timestamp=timestamp,
            success=True,
        )
    except Exception as exc:
        return CaptureResult(
            frame_index=frame_index,
            screenshot_path=str(filepath),
            timestamp=timestamp,
            success=False,
            error_message=str(exc),
        )

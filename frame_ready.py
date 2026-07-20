"""
frame_ready.py
判断"这一帧是否真的加载完成、可以截图了"，而不是简单 sleep 固定时间。

等待条件（全部满足才算 ready）：
  1. 抓到期望数量的 .pcd（默认 1 个，frame_ready.wait_for_pcd / expected_pcd_count 控制）
  2. 抓到期望数量的 .jpg/.jpeg（默认 5 个，frame_ready.wait_for_jpg / expected_jpg_count 控制）
  3. 网络连续 quiet_ms 毫秒没有新的 PCD/JPG response 到达（说明这一批资源基本加载完了，
     不会出现"数量刚好够了但其实还有资源在路上"的情况）
  4. 在满足以上条件之后，再额外等待 render_settle_ms 毫秒，给 WebGL/Canvas/3D BBox 的
     前端渲染留出稳定时间（网络加载完成不等于画面已经渲染稳定）

超时不会抛异常，只会返回 ready_status="timeout"，由调用方决定要不要照样截图、
记录 warning——绝不会让整个 81 帧的流程因为某一帧没就绪就中断。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from playwright.sync_api import Page

from network_recorder import NetworkRecorder


@dataclass
class FrameReadyResult:
    ready_status: str   # "ok" | "timeout"
    pcd_count: int
    jpg_count: int
    elapsed_ms: int
    reason: str = ""     # timeout 时简单说明卡在哪个条件，方便调参


def wait_for_frame_ready(page: Page, recorder: NetworkRecorder, frame_ready_cfg: dict) -> FrameReadyResult:
    """轮询直到"数量达标 + 网络安静 + 渲染稳定"，或超时。不抛异常。"""
    wait_for_pcd = frame_ready_cfg.get("wait_for_pcd", True)
    expected_pcd = frame_ready_cfg.get("expected_pcd_count", 1)
    wait_for_jpg = frame_ready_cfg.get("wait_for_jpg", True)
    expected_jpg = frame_ready_cfg.get("expected_jpg_count", 5)
    timeout_s = frame_ready_cfg.get("timeout_seconds", 15)
    quiet_ms = frame_ready_cfg.get("quiet_ms", 800)
    render_settle_ms = frame_ready_cfg.get("render_settle_ms", 500)
    poll_ms = frame_ready_cfg.get("poll_interval_ms", 50)

    start = time.time()
    deadline = start + timeout_s

    settled = False
    while time.time() < deadline:
        pcd_count, jpg_count = recorder.current_counts()
        counts_ok = (not wait_for_pcd or pcd_count >= expected_pcd) and (not wait_for_jpg or jpg_count >= expected_jpg)

        if counts_ok and recorder.idle_ms() >= quiet_ms:
            settled = True
            break

        page.wait_for_timeout(poll_ms)

    pcd_count, jpg_count = recorder.current_counts()

    if not settled:
        elapsed_ms = int((time.time() - start) * 1000)
        reason = (
            f"超时（{timeout_s}s）：pcd={pcd_count}/{expected_pcd if wait_for_pcd else '-'}, "
            f"jpg={jpg_count}/{expected_jpg if wait_for_jpg else '-'}, "
            f"idle_ms={recorder.idle_ms():.0f}（需要 >= {quiet_ms}）"
        )
        return FrameReadyResult("timeout", pcd_count, jpg_count, elapsed_ms, reason)

    # 数量 + 网络静默都满足了，再等一段固定时间让 WebGL/Canvas/BBox 渲染稳定
    page.wait_for_timeout(render_settle_ms)

    elapsed_ms = int((time.time() - start) * 1000) + render_settle_ms
    return FrameReadyResult("ok", pcd_count, jpg_count, elapsed_ms)

"""
network_recorder.py
监听浏览器的网络请求，记录并验证每一帧是否精准加载了 1 个 PCD + 5 个 JPG。

只做被动监听（page.on("response")），不拦截、不修改、不重放任何请求。

用法：
  1. NetworkRecorder(page, config) 构造时挂上监听器，开始持续、不间断地收集
     所有 .pcd / .jpg 响应（整个运行期间都不会丢弃已收到的数据）。
  2. 每帧开始时调用 mark_frame_start()，打一个"起点标记"（不是真的清空缓冲区——
     如果真清空，遇到平台提前预加载下一帧资源的情况，就会把提前到达、还没来得及
     处理的数据误删掉，之前真实测试里就踩过这个坑：截图明明成功了，
     network_assets.csv 却记录成 0 个 PCD + 0 个 JPG）。
  3. frame_ready.py 通过 current_counts() / idle_ms() 判断"这一帧目前新增了多少
     资源、网络有没有安静下来"，用来决定要不要结束等待。
  4. 截图之后调用 snapshot(scene_id, frame_index) 取出这一帧应该归属的资源：
     内部按 URL 里资源自带的时间戳分组认领（config.yaml -> network.grouping_mode），
     不依赖 mark_frame_start() 的调用时机，即使资源提前到达也不会丢、不会记错帧。
  5. 全部帧处理完后调用 write_asset_csv() 写出 network_assets.csv。
"""

from __future__ import annotations

import csv
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Page, Response

ASSET_CSV_FIELDS = [
    "scene_id",
    "frame_index",
    "pcd_url",
    "jpg_url_1",
    "jpg_url_2",
    "jpg_url_3",
    "jpg_url_4",
    "jpg_url_5",
    "pcd_count",
    "jpg_count",
    "status",
    "warning",
]


@dataclass
class AssetRecord:
    """一帧的网络资源捕获结果，对应 network_assets.csv 的一行。"""
    scene_id: str
    frame_index: int
    pcd_urls: list = field(default_factory=list)
    jpg_urls: list = field(default_factory=list)

    @property
    def pcd_count(self) -> int:
        return len(self.pcd_urls)

    @property
    def jpg_count(self) -> int:
        return len(self.jpg_urls)

    def status(self, expected_pcd: int, expected_jpg: int) -> str:
        if self.pcd_count == expected_pcd and self.jpg_count == expected_jpg:
            return "ok"
        return "warning"

    def warning_text(self, expected_pcd: int, expected_jpg: int) -> str:
        if self.status(expected_pcd, expected_jpg) == "ok":
            return ""
        return f"期望 {expected_pcd} PCD + {expected_jpg} JPG，实际抓到 {self.pcd_count} PCD + {self.jpg_count} JPG"

    def as_row(self, expected_pcd: int, expected_jpg: int) -> list:
        # jpg_url_1..5：按认领顺序填入前 5 个；不足 5 个的留空，超过 5 个的仍计入 jpg_count
        jpg_slots = (self.jpg_urls + [""] * 5)[:5]
        # 多个 PCD 的情况理论上不应该发生，但为了不丢数据，全部用分号拼在一个单元格里
        pcd_cell = ";".join(self.pcd_urls)
        return [
            self.scene_id,
            self.frame_index,
            pcd_cell,
            *jpg_slots,
            self.pcd_count,
            self.jpg_count,
            self.status(expected_pcd, expected_jpg),
            self.warning_text(expected_pcd, expected_jpg),
        ]


class NetworkRecorder:
    """挂在一个 Page 上，被动记录 .pcd / .jpg 请求的 URL。"""

    def __init__(self, page: Page, network_cfg: dict):
        self.page = page
        self.cfg = network_cfg
        self._pcd_re = re.compile(network_cfg.get("pcd_url_pattern", r"\.pcd(\?|$)"), re.IGNORECASE)
        self._jpg_re = re.compile(network_cfg.get("jpg_url_pattern", r"\.jpe?g(\?|$)"), re.IGNORECASE)

        self.grouping_mode = network_cfg.get("grouping_mode", "timestamp")
        self._ts_re = re.compile(network_cfg.get("timestamp_pattern", r"/(\d+\.\d+)\.(?:pcd|jpe?g)(?:\?|$)"))

        # 持续累积，整个运行期间不清空；[{"url", "type": "pcd"|"jpg", "group_key", "claimed"}]
        self._buffer: list[dict] = []
        self._frame_start_len = 0  # mark_frame_start() 时的 buffer 长度，标记"这一帧的统计起点"
        self._last_asset_ts: float | None = None  # 最近一次收到 pcd/jpg response 的时间（time.time()）

        # 匹配 pcd/jpg 模式、但最终请求失败（网络层面没拿到 response，比如超时/连接被重置/CORS 失败）
        # 的记录 —— 这类请求在 DevTools Network 面板里能看到，但 page.on("response") 永远不会触发，
        # 之前 PCD 一直是 0 但实际存在这类请求时，只有靠这个才能诊断出来。
        self._failed: list[dict] = []

        page.on("response", self._on_response)
        page.on("requestfailed", self._on_request_failed)

    def _on_response(self, response: Response) -> None:
        try:
            url = response.url
        except Exception:
            return

        if self._pcd_re.search(url):
            kind = "pcd"
        elif self._jpg_re.search(url):
            kind = "jpg"
        else:
            return

        try:
            status = response.status
        except Exception:
            status = None
        if status is not None and status >= 400:
            print(f"[警告] {kind.upper()} 请求返回了错误状态码 {status}: {url}")

        group_key = None
        if self.grouping_mode == "timestamp":
            m = self._ts_re.search(url)
            group_key = m.group(1) if m else None

        self._buffer.append({"url": url, "type": kind, "group_key": group_key, "claimed": False})
        self._last_asset_ts = time.time()

    def _on_request_failed(self, request) -> None:
        """网络层面直接失败的请求（超时/连接中断/CORS 等），不会触发 page.on("response")。
        单独记录下来，方便诊断"DevTools 里看得到请求、但脚本抓不到"的情况。"""
        try:
            url = request.url
        except Exception:
            return

        if not (self._pcd_re.search(url) or self._jpg_re.search(url)):
            return

        try:
            failure = request.failure
            reason = failure.get("errorText", str(failure)) if isinstance(failure, dict) else str(failure)
        except Exception:
            reason = "未知原因"

        self._failed.append({"url": url, "reason": reason})
        print(f"[警告] 请求失败（未产生 response，原因: {reason}）: {url}")

    def mark_frame_start(self) -> None:
        """标记"这一帧从这里开始统计"。非破坏性——不会丢弃之前已经到达、还没被认领的数据，
        只是给 current_counts() / idle_ms() 一个参照起点，供 frame_ready.py 判断"这一帧目前
        新增了多少资源"。最终归属统计（snapshot）不依赖这个标记，靠时间戳分组认领。"""
        self._frame_start_len = len(self._buffer)

    def total_failed_requests(self) -> list[dict]:
        """整个运行期间，匹配 pcd/jpg 模式但网络层面直接失败的请求列表（跑完后在 main.py 里汇总打印）。"""
        return list(self._failed)

    def current_counts(self) -> tuple[int, int]:
        """自最近一次 mark_frame_start() 以来，新增了多少 pcd / jpg（不看是否已被认领）。
        供 frame_ready.py 判断"数量是否已经达标"用。"""
        new_items = self._buffer[self._frame_start_len:]
        pcd_count = sum(1 for a in new_items if a["type"] == "pcd")
        jpg_count = sum(1 for a in new_items if a["type"] == "jpg")
        return pcd_count, jpg_count

    def idle_ms(self) -> float:
        """距离最近一次收到 pcd/jpg response 过去了多少毫秒。
        还没收到过任何资源时返回 inf（代表"当然还没安静下来"）。"""
        if self._last_asset_ts is None:
            return float("inf")
        return (time.time() - self._last_asset_ts) * 1000

    def snapshot(self, scene_id: str, frame_index: int) -> AssetRecord:
        """取出这一帧应归属的资源，构造成一条 AssetRecord。"""
        if self.grouping_mode == "timestamp":
            return self._snapshot_by_timestamp(scene_id, frame_index)
        return self._snapshot_by_window(scene_id, frame_index)

    def _snapshot_by_timestamp(self, scene_id: str, frame_index: int) -> AssetRecord:
        """在所有"尚未被认领"的资源里，取时间戳最早的一组（一个 pcd + 与它同时间戳的 jpg）
        认领给当前这一帧。不管这些资源是提前到达还是刚到达，只要时间戳相同就会被认领在一起，
        认领后标记 claimed，不会再被后续帧重复取用。"""
        unclaimed = [a for a in self._buffer if not a["claimed"]]

        pcd_keys = sorted(
            {a["group_key"] for a in unclaimed if a["type"] == "pcd" and a["group_key"] is not None},
            key=float,
        )
        if pcd_keys:
            target_key = pcd_keys[0]
            claim = [a for a in unclaimed if a["group_key"] == target_key]
        else:
            # 没有任何带得出时间戳的未认领 pcd：可能是收尾，也可能是这个平台的 URL 里
            # 提取不出时间戳（timestamp_pattern 没匹配上），此时退化为"认领全部未认领项"，尽力而为。
            claim = unclaimed

        for a in claim:
            a["claimed"] = True

        pcd_urls = [a["url"] for a in claim if a["type"] == "pcd"]
        jpg_urls = [a["url"] for a in claim if a["type"] == "jpg"]
        return AssetRecord(scene_id=scene_id, frame_index=frame_index, pcd_urls=pcd_urls, jpg_urls=jpg_urls)

    def _snapshot_by_window(self, scene_id: str, frame_index: int) -> AssetRecord:
        """认领自 mark_frame_start() 以来新增、且尚未被认领的资源（老式窗口法，
        只有 timestamp_pattern 在目标平台上完全提取不出时间戳时才建议用这个模式）。"""
        new_items = [a for a in self._buffer[self._frame_start_len:] if not a["claimed"]]
        for a in new_items:
            a["claimed"] = True
        pcd_urls = [a["url"] for a in new_items if a["type"] == "pcd"]
        jpg_urls = [a["url"] for a in new_items if a["type"] == "jpg"]
        return AssetRecord(scene_id=scene_id, frame_index=frame_index, pcd_urls=pcd_urls, jpg_urls=jpg_urls)


def write_asset_csv(records: list[AssetRecord], network_cfg: dict, report_cfg: dict) -> str:
    """把所有帧的 AssetRecord 写成 CSV，返回文件路径。"""
    expected_pcd = network_cfg.get("expected_pcd_count", 1)
    expected_jpg = network_cfg.get("expected_jpg_count", 5)

    output_dir = Path(report_cfg["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / network_cfg.get("output_filename", "network_assets.csv")

    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(ASSET_CSV_FIELDS)
        for record in records:
            writer.writerow(record.as_row(expected_pcd, expected_jpg))

    return str(filepath)

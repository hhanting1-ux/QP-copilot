"""
report.py
定义 FrameRecord（capture_report.csv 的一行）以及这份报告的读写。

capture_report.csv 同时承担两个角色：
  1. 人可以直接打开看的抓取结果报告（每帧切帧状态、就绪状态、PCD/JPG 数量、警告）。
  2. "抓取"和"判断分析"两个阶段之间的交接点：以后调整规则引擎 / AI 视觉模型时，
     analyze.py 直接读这份 CSV、重新跑分析、原地更新 ai_status 等字段，
     不需要重新打开浏览器、重新在 QP 平台里翻一遍。

第一版不接 AI 视觉模型，所有行的 ai_status 固定写 "pending"，
issue_type / description / confidence 留空，reviewer_note 留空给人工填写。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, fields
from pathlib import Path

REPORT_FIELDS = [
    "scene_id",
    "frame_index",
    "screenshot_path",
    "navigation_status",
    "ready_status",
    "pcd_count",
    "jpg_count",
    "warning",
    "ai_status",
    "issue_type",
    "description",
    "confidence",
    "reviewer_note",
]

# CSV 里应该被当整数读回来的字段（其余字段一律当字符串处理）
_INT_FIELDS = {"frame_index", "pcd_count", "jpg_count"}


@dataclass
class FrameRecord:
    """capture_report.csv 的一行，对应一帧的抓取 + 判断分析结果。"""
    scene_id: str
    frame_index: int
    screenshot_path: str = ""
    navigation_status: str = "ok"      # ok | warning（切帧失败时）
    ready_status: str = "ok"            # ok | timeout（frame_ready 判断结果）
    pcd_count: int = 0
    jpg_count: int = 0
    warning: str = ""                    # 任何一步出问题时的简要说明，汇总展示给人工看
    ai_status: str = "pending"            # 第一版固定为 pending，预留给未来的 AI 视觉分析结果
    issue_type: str = ""
    description: str = ""
    confidence: str = ""
    reviewer_note: str = ""

    def as_row(self) -> list:
        return [getattr(self, name) for name in REPORT_FIELDS]


def build_frame_record(
    scene_id: str,
    frame_index: int,
    screenshot_path: str,
    navigation_status: str,
    ready_status: str,
    pcd_count: int,
    jpg_count: int,
    warning: str,
) -> FrameRecord:
    """根据一帧的抓取结果，构造一条报告记录（ai_status 恒为 pending，判断分析见 analyzers.py）。"""
    return FrameRecord(
        scene_id=scene_id,
        frame_index=frame_index,
        screenshot_path=screenshot_path,
        navigation_status=navigation_status,
        ready_status=ready_status,
        pcd_count=pcd_count,
        jpg_count=jpg_count,
        warning=warning,
    )


def write_report(records: list[FrameRecord], report_cfg: dict) -> str:
    """把记录列表写入 capture_report.csv，返回文件路径。"""
    output_dir = Path(report_cfg["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / report_cfg.get("capture_report_filename", "capture_report.csv")

    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(REPORT_FIELDS)
        for record in records:
            writer.writerow(record.as_row())

    return str(filepath)


def read_report(filepath: str) -> list[FrameRecord]:
    """从 capture_report.csv 读回 FrameRecord 列表，供 analyze.py 使用。"""
    valid_names = {f.name for f in fields(FrameRecord)}

    records = []
    with open(filepath, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            kwargs = {k: v for k, v in row.items() if k in valid_names}
            for int_field in _INT_FIELDS:
                if int_field in kwargs:
                    kwargs[int_field] = int(kwargs[int_field] or 0)
            records.append(FrameRecord(**kwargs))

    return records

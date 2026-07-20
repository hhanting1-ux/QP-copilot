"""
analyze.py
"判断分析"阶段的独立入口 —— 完全不需要浏览器 / 不需要连接 QP 平台。

用途：main.py 抓取一次 scene（浏览 81 帧 + 截图）之后，会生成一份 capture_report.csv。
以后新增或调整规则引擎 / AI 视觉模型时，反复迭代 config.yaml 里 analysis.enabled_analyzers
和对应分析器的逻辑，然后用本脚本对着同一份 capture_report.csv 重新跑分析、原地更新
ai_status / issue_type / description / confidence 这几列即可，不用每次都重新在 QP
平台里翻一遍 81 帧。navigation_status / ready_status / pcd_count / jpg_count /
warning（抓取阶段的结果）保持不变。

用法：
    python analyze.py                                   # 默认读 outputs/reports/capture_report.csv
    python analyze.py outputs/reports/capture_report.csv  # 也可以显式指定路径
"""

from __future__ import annotations

import sys

import yaml

from analyzers import run_analyzers
from report import read_report, write_report


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def run(report_path: str | None = None, config_path: str = "config.yaml") -> None:
    config = load_config(config_path)
    report_cfg = config["report"]

    if report_path is None:
        report_path = f"{report_cfg['output_dir']}/{report_cfg.get('capture_report_filename', 'capture_report.csv')}"

    records = read_report(report_path)
    print(f"[信息] 从 capture_report 读取到 {len(records)} 条记录: {report_path}")

    enabled = config.get("analysis", {}).get("enabled_analyzers", []) or []
    print(f"[信息] 已启用的分析器: {enabled if enabled else '（无，第一版尚未接入规则/AI）'}")

    records = [run_analyzers(r, config) for r in records]

    written_path = write_report(records, report_cfg)
    print(f"[信息] 报告已更新: {written_path}")


if __name__ == "__main__":
    arg_path = sys.argv[1] if len(sys.argv) > 1 else None
    run(arg_path)

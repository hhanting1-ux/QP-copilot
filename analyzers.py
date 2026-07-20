"""
analyzers.py
判断模块的可插拔入口（第一版为空实现）。

设计目的：抓取（浏览+截图）和判断（规则引擎 / AI 视觉）分成两个独立阶段：
  - main.py 只负责抓取，产出截图 + manifest.json（每帧的原始记录）
  - analyze.py 负责判断，读 manifest.json，不需要打开浏览器/连接 QP 平台，
    可以反复调整规则或 AI 提示词并重新跑，不会影响 QP 平台上的真实数据。

新增一个分析器（无论是规则引擎还是 AI 视觉模型）时：
  1. 写一个函数 `def my_analyzer(record: FrameRecord, config: dict) -> FrameRecord`
  2. 在 ANALYZER_REGISTRY 里注册，例如 "rules": my_analyzer
  3. 在 config.yaml 的 analysis.enabled_analyzers 里加上对应名字并启用

分析器函数应该只读 record.screenshot_path 对应的图片（以及需要的话读 config），
更新并返回 record 的 ai_status / issue_type / description / confidence，
不应该有任何写回 QP 平台的副作用。
"""

from __future__ import annotations

from report import FrameRecord

# 分析器注册表：name -> 处理函数
# 第一版为空，后续会依次加入 "rules"（规则引擎）和 "ai_vision"（AI 视觉模型）
ANALYZER_REGISTRY = {
    # "rules": rules.run_rule_checks,
    # "ai_vision": ai_vision.run_ai_vision_checks,
}


def run_analyzers(record: FrameRecord, config: dict) -> FrameRecord:
    """按 config.yaml 中 analysis.enabled_analyzers 的顺序依次跑分析器。

    第一版 enabled_analyzers 默认为空列表，此函数原样返回 record（ai_status 仍为 pending）。
    """
    enabled = config.get("analysis", {}).get("enabled_analyzers", []) or []

    for name in enabled:
        fn = ANALYZER_REGISTRY.get(name)
        if fn is None:
            print(f"[警告] analysis.enabled_analyzers 中的 '{name}' 尚未实现，已跳过。")
            continue
        record = fn(record, config)

    return record

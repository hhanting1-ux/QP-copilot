"""
rule_engine.py
质检规则引擎：读取 bbox_data.csv，跑一批跨帧/跨目标的质检规则，输出 rule_report.csv
和 rule_summary.json。只读 bbox_data.csv，不连浏览器、不改任何平台数据。

---------------------------------------------------------------------------
目标唯一标识
---------------------------------------------------------------------------
不重新实现"用哪个字段跨帧标识同一个目标"的优先级判断——bbox_data.csv 里的
resolved_target_key 列已经是 bbox_extractor.py 按 track_id > target_id > uuid >
object_id > bbox_index 算好的结果，这里直接复用这一列做分组，规则引擎本身不碰
优先级逻辑，也不需要改 bbox_data.csv 的导出。

---------------------------------------------------------------------------
新增一条规则怎么做
---------------------------------------------------------------------------
1. 写一个函数 `def my_rule(ctx: RuleContext) -> list[RuleFinding]`
   ctx 里有：
     ctx.scene_id                 当前场景 ID
     ctx.frames                   {frame_index: [box_dict, ...]}，按帧分组
     ctx.tracks                   {resolved_target_key: [(frame_index, box_dict), ...]}，
                                   按目标分组，组内已经按 frame_index 排好序
     ctx.expected_frame_indices   本次场景理论上应该采集的完整帧号列表（用来判断
                                   "整帧缺失/空帧"，不是只看 bbox_data.csv 里实际出现过的帧）
     ctx.config                   完整的 config.yaml 内容，规则自己从 config["rule_engine"] 取参数
   box_dict 的键就是 bbox_data.csv 的列名，值全是字符串，数值字段用 _to_float() 转换。
2. 在 RULE_REGISTRY 里注册：RULE_REGISTRY["my_rule_id"] = my_rule
3. 在 config.yaml 的 rule_engine.enabled_rules 里加上这个名字

不需要改 main.py、bbox_extractor.py、navigator.py、capture.py 或任何其它模块。

---------------------------------------------------------------------------
容错：抓取阶段单帧 BBox 读取失败
---------------------------------------------------------------------------
main.py 抓取阶段如果某一帧 BBox 读取失败（页面未加载完成/JS 异常/网络延迟等），
不会中断采集，而是调用本模块的 missing_bbox_finding() 构造一条 rule_id=MissingBBox
的 finding，跟其它规则命中的 finding 一起写进 rule_report.csv。这跟下面 8 条
质检规则是两回事：MissingBBox 是"抓取失败"，8 条规则是"抓到了、但内容有问题"。

用法（独立于 main.py 重新跑，不需要重新打开浏览器）：
    python rule_engine.py                                  # 默认读 outputs/reports/bbox_data.csv
    python rule_engine.py outputs/reports/bbox_data.csv       # 也可以显式指定路径
    （独立重跑时不知道本次场景"理论上应该有多少帧"，expected_frame_indices 会退化成
     "bbox_data.csv 里实际出现过的帧号"，EmptyFrame 在两端边界帧缺失时可能检测不到——
     这是不 touch bbox_data.csv 格式前提下的已知限制，main.py 里正常跑没有这个问题，
     因为它知道真实的 frame_count/start_frame_index）
"""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml

RULE_REPORT_FIELDS = [
    "scene_id",
    "frame_index",
    "first_frame",
    "last_frame",
    "occurrence_count",
    "track_id",
    "bbox_index",
    "label",
    "rule_id",
    "severity",
    "message",
    "evidence",
]


@dataclass
class RuleFinding:
    """rule_report.csv 的一行，对应一个"持续问题"（同一 track_id 的同一种 rule_id，
    如果在连续帧里一直命中，只会有一行，不是每帧一行——见模块末尾
    _merge_persistent_findings() 的说明）。

    这里的 track_id 字段存的是"目标唯一标识的解析结果"（bbox_data.csv 里的
    resolved_target_key），不是原始 track_id 列本身——按需求优先用 track_id，
    track_id 为空时依次退到 target_id/uuid/object_id/bbox_index，这个退化链条
    已经在 bbox_extractor.py 里算好了，这里直接拿来用。

    frame_index / first_frame 是同一个值（第一次发现问题的那一帧，只记录这个位置）；
    last_frame 是这个问题持续到的最后一帧；occurrence_count 是连续命中了多少次
    （帧数不连续就不算同一个持续问题，会拆成两条独立记录）。
    """
    scene_id: str
    frame_index: int
    track_id: str = ""
    bbox_index: str = ""
    label: str = ""
    rule_id: str = ""
    severity: str = "Warning"
    message: str = ""
    evidence: str = ""
    first_frame: int = 0
    last_frame: int = 0
    occurrence_count: int = 1

    def __post_init__(self) -> None:
        # 大部分规则函数只关心 frame_index，不用挨个手动填 first_frame/last_frame——
        # 没显式传的话默认就是"只出现了这一帧"，_merge_persistent_findings() 处理完
        # 连续帧合并之后会覆盖成真实的 first_frame/last_frame/occurrence_count。
        if not self.first_frame:
            self.first_frame = self.frame_index
        if not self.last_frame:
            self.last_frame = self.frame_index

    def as_row(self) -> list:
        return [getattr(self, name) for name in RULE_REPORT_FIELDS]


def missing_bbox_finding(scene_id: str, frame_index: int, reason: str = "") -> RuleFinding:
    """构造"这一帧 BBox 读取失败"的 finding。main.py 在抓取阶段遇到单帧 BBox 提取失败时调用，
    不是常规质检规则命中，是抓取阶段容错的记录方式，跟 8 条规则的 finding 一起写进
    同一份 rule_report.csv（字段结构完全一样，rule_id 固定是 "MissingBBox"）。"""
    evidence = f"frame_index={frame_index}"
    if reason:
        evidence += f", reason={reason}"
    return RuleFinding(
        scene_id=scene_id,
        frame_index=frame_index,
        rule_id="MissingBBox",
        severity="Warning",
        message="Failed to read bbox data.",
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# 数值/角度小工具
# ---------------------------------------------------------------------------

def _to_float(value) -> float | None:
    """bbox_data.csv 读回来的值全是字符串，空字符串/None 统一当"没有值"处理。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _angle_diff(a: float, b: float) -> float:
    """角度差处理环绕：比如 -3.13 rad 和 3.13 rad 实际只差约 0.02 rad，不是 6.26 rad。"""
    d = (a - b + math.pi) % (2 * math.pi) - math.pi
    return abs(d)


def _distance(p1: tuple[float, float, float], p2: tuple[float, float, float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(p1, p2)))


def _position(box: dict) -> tuple[float, float, float] | None:
    x, y, z = _to_float(box.get("position_x")), _to_float(box.get("position_y")), _to_float(box.get("position_z"))
    if x is None or y is None or z is None:
        return None
    return (x, y, z)


def _scale(box: dict) -> tuple[float, float, float] | None:
    x, y, z = _to_float(box.get("scale_x")), _to_float(box.get("scale_y")), _to_float(box.get("scale_z"))
    if x is None or y is None or z is None:
        return None
    return (x, y, z)


def _label_of(box: dict) -> str:
    """bbox_data.csv 里 label 列这个平台上一直是空的，className 才是真实拿到的类别，
    规则报告里的 label 字段统一用 className 填充，跟实际有数据的字段对齐。"""
    return box.get("className") or box.get("label") or ""


def _target_key(box: dict) -> str:
    """跨帧标识同一个目标用的 key，直接读 bbox_data.csv 已经算好的 resolved_target_key；
    万一这一列缺失（比如手动拼的旧数据），退化到 bbox_index，保证不会崩。"""
    return box.get("resolved_target_key") or f"bbox_index:{box.get('bbox_index', '')}"


# ---------------------------------------------------------------------------
# 数据加载：按帧分组 / 按目标分组
# ---------------------------------------------------------------------------

def load_bbox_data(bbox_csv_path: str) -> dict[int, list[dict]]:
    """读 bbox_data.csv，按 frame_index 分组，返回 {frame_index: [box_dict, ...]}。
    注意：一帧如果 0 个框，bbox_data.csv 里根本没有这一帧的行，这个 dict 里就不会有
    这个 key——EmptyFrame 这类规则要正确处理"帧存在但 0 个框"，需要靠
    RuleContext.expected_frame_indices 补全完整帧号范围，不能只看这个 dict 的 key。"""
    frames: dict[int, list[dict]] = defaultdict(list)
    with open(bbox_csv_path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            frame_index = int(row["frame_index"])
            frames[frame_index].append(row)
    return dict(frames)


def build_tracks(frames: dict[int, list[dict]]) -> dict[str, list[tuple[int, dict]]]:
    """按 resolved_target_key 把所有帧的 box 分组，组内按 frame_index 排序，
    用于跨帧比较同一个目标（LabelConsistency/PositionJump/RotationJump/SizeConsistency/BrokenTrack）。"""
    tracks: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for frame_index, boxes in frames.items():
        for box in boxes:
            tracks[_target_key(box)].append((frame_index, box))
    for key in tracks:
        tracks[key].sort(key=lambda item: item[0])
    return dict(tracks)


@dataclass
class RuleContext:
    """传给每条规则函数的统一上下文，新增规则只需要从这里取需要的数据，
    不需要改动 run_rules() 或者任何其它模块。"""
    scene_id: str
    frames: dict[int, list[dict]]
    tracks: dict[str, list[tuple[int, dict]]]
    expected_frame_indices: list[int]
    config: dict

    def rule_cfg(self) -> dict:
        return self.config.get("rule_engine", {}) or {}


# ---------------------------------------------------------------------------
# 8 条质检规则
# ---------------------------------------------------------------------------

def rule_label_consistency(ctx: RuleContext) -> list[RuleFinding]:
    """同一目标在连续帧中，label/className 不能发生变化。
    "连续帧"按目标自己的出现顺序比较相邻两次出现，不要求 frame_index 正好差 1
    （目标偶尔漏检几帧、之后又出现，仍然拿它前后两次真实出现的类别做比较）。"""
    findings = []
    for key, entries in ctx.tracks.items():
        prev_label = None
        prev_frame = None
        for frame_index, box in entries:
            label = _label_of(box)
            if prev_label is not None and label != prev_label:
                findings.append(RuleFinding(
                    scene_id=ctx.scene_id, frame_index=frame_index,
                    track_id=key, bbox_index=box.get("bbox_index", ""), label=label,
                    rule_id="LabelConsistency", severity="Warning",
                    message=f"类别从 '{prev_label}' 变成 '{label}'",
                    evidence=f"track={key}, prev_frame={prev_frame}(label={prev_label}), "
                             f"cur_frame={frame_index}(label={label})",
                ))
            prev_label, prev_frame = label, frame_index
    return findings


def rule_position_jump(ctx: RuleContext) -> list[RuleFinding]:
    """同一目标，连续帧 position 距离变化超过阈值报警。

    "连续帧"要求 frame_index 正好差 1——如果目标中间跳过了几帧才再出现（不管是因为
    采集时那一帧没读到 BBox，还是目标本身被遮挡了几帧），中间累积的真实位移会被
    误判成"一帧内的跳变"，量级被放大好几倍，是假报警。用真实数据核对过：几个被
    误报的目标轨迹其实是连续平滑的，只是中间空了几帧，按实际帧数换算车速完全正常。
    所以这里必须限定 frame_index 差值正好是 1，跳帧的情况不比较（BrokenTrack 规则
    已经在专门处理"消失又出现"这件事，这里不用重复报）。"""
    threshold = ctx.rule_cfg().get("position_jump_threshold", 10.0)
    findings = []
    for key, entries in ctx.tracks.items():
        prev_pos, prev_frame = None, None
        for frame_index, box in entries:
            pos = _position(box)
            if prev_pos is not None and pos is not None and frame_index - prev_frame == 1:
                dist = _distance(prev_pos, pos)
                if dist > threshold:
                    findings.append(RuleFinding(
                        scene_id=ctx.scene_id, frame_index=frame_index,
                        track_id=key, bbox_index=box.get("bbox_index", ""), label=_label_of(box),
                        rule_id="PositionJump", severity="Warning",
                        message=f"位置跳变 {dist:.2f}m，超过阈值 {threshold}m",
                        evidence=f"track={key}, prev_frame={prev_frame}(pos={prev_pos}), "
                                 f"cur_frame={frame_index}(pos={pos})",
                    ))
            if pos is not None:
                prev_pos, prev_frame = pos, frame_index
    return findings


def rule_rotation_jump(ctx: RuleContext) -> list[RuleFinding]:
    """同一目标，连续帧 rotation_z 变化超过阈值报警（处理了角度环绕）。

    跟 PositionJump 一样，要求 frame_index 正好差 1 才比较——跳帧之后再出现的转向变化
    是好几帧累积的，不是"一帧内"的跳变，会被同样的量级放大问题误报，理由见
    rule_position_jump() 的说明。"""
    threshold = ctx.rule_cfg().get("rotation_jump_threshold", 1.0)
    findings = []
    for key, entries in ctx.tracks.items():
        prev_rot, prev_frame = None, None
        for frame_index, box in entries:
            rot = _to_float(box.get("rotation_z"))
            if prev_rot is not None and rot is not None and frame_index - prev_frame == 1:
                diff = _angle_diff(rot, prev_rot)
                if diff > threshold:
                    findings.append(RuleFinding(
                        scene_id=ctx.scene_id, frame_index=frame_index,
                        track_id=key, bbox_index=box.get("bbox_index", ""), label=_label_of(box),
                        rule_id="RotationJump", severity="Warning",
                        message=f"rotation_z 跳变 {diff:.3f}rad，超过阈值 {threshold}rad",
                        evidence=f"track={key}, prev_frame={prev_frame}(rotation_z={prev_rot}), "
                                 f"cur_frame={frame_index}(rotation_z={rot})",
                    ))
            if rot is not None:
                prev_rot, prev_frame = rot, frame_index
    return findings


def rule_size_consistency(ctx: RuleContext) -> list[RuleFinding]:
    """同一目标，连续帧 scale_x/y/z 任一轴变化比例超过阈值报警（默认 30%）。

    跟 PositionJump 一样，要求 frame_index 正好差 1 才比较，理由见 rule_position_jump()
    的说明——跳帧之后的尺寸对比不代表"突变"，可能只是这中间几帧本来就没读到数据。"""
    threshold = ctx.rule_cfg().get("size_change_threshold", 0.3)
    findings = []
    for key, entries in ctx.tracks.items():
        prev_scale, prev_frame = None, None
        for frame_index, box in entries:
            scale = _scale(box)
            if prev_scale is not None and scale is not None and frame_index - prev_frame == 1:
                for axis_name, cur_v, prev_v in zip(("scale_x", "scale_y", "scale_z"), scale, prev_scale):
                    if prev_v == 0:
                        continue  # 避免除零，前一帧该轴尺寸是 0（不应该发生，跳过不判断）
                    ratio = abs(cur_v - prev_v) / abs(prev_v)
                    if ratio > threshold:
                        findings.append(RuleFinding(
                            scene_id=ctx.scene_id, frame_index=frame_index,
                            track_id=key, bbox_index=box.get("bbox_index", ""), label=_label_of(box),
                            rule_id="SizeConsistency", severity="Warning",
                            message=f"{axis_name} 变化比例 {ratio:.1%}，超过阈值 {threshold:.0%}",
                            evidence=f"track={key}, prev_frame={prev_frame}({axis_name}={prev_v}), "
                                     f"cur_frame={frame_index}({axis_name}={cur_v})",
                        ))
            if scale is not None:
                prev_scale, prev_frame = scale, frame_index
    return findings


def _zero_box_frames(ctx: RuleContext) -> set[int]:
    """"整帧 0 个框"的帧号集合——不管是这一帧真的没有标注物（EmptyFrame），
    还是 BBox 提取失败（main.py 抓取阶段的 MissingBBox），从 bbox_data.csv 里看
    都是"这个 frame_index 完全没有行"，没法区分，但对 BrokenTrack 来说处理方式
    是一样的：这是帧级问题，不该怪到每一个 track 头上，见 rule_broken_track()。"""
    return {fi for fi in ctx.expected_frame_indices if len(ctx.frames.get(fi, [])) == 0}


def rule_broken_track(ctx: RuleContext) -> list[RuleFinding]:
    """同一目标连续消失 <= N 帧（默认 3）后又重新出现，报警
    （消失太久，比如真的离开了画面，不算 broken track，不报警）。

    消失区间如果覆盖了"整帧 0 个框"的帧（EmptyFrame 或 MissingBBox，这是一帧级别
    的问题，见 _zero_box_frames()），不再对每个 track 都报一遍 BrokenTrack——
    实测过一次真实数据：一帧整体 0 个框，会让当时活跃的 27~30 个 track 同时报
    "断轨"，其实是同一个帧级问题被重复算了 27~30 遍，不是 27~30 个真实的追踪问题。
    这种情况只保留 EmptyFrame（或 MissingBBox）那一条帧级报警就够了，跳过这里的
    逐 track 报警。只有消失区间跟"整帧 0 个框"完全无关（这个 track 自己漏检了
    几帧，其它 track 在那几帧都好好的）才继续按下面的逻辑判断。

    上面已经把"整帧 0 个框"（EmptyFrame/MissingBBox）造成的假断轨过滤掉了，
    走到这里的都是这个 track 自己的问题（同一时刻其它 track 都好好的），
    所以默认就是 severity=Warning——第 1 次断轨就报，不用等第 2 次。
    命中以下任一条件都会在 message 里附带具体原因：
      1. 断轨次数（第几次，多次说明这个目标反复出问题，比只出现一次更可疑）
      2. 重新出现时位置发生明显跳变（复用 position_jump_threshold 衡量"明显"）
      3. 重新出现时类别（label/className）发生了变化
    """
    gap_threshold = ctx.rule_cfg().get("broken_track_gap", 3)
    position_threshold = ctx.rule_cfg().get("position_jump_threshold", 25.0)
    zero_box_frames = _zero_box_frames(ctx)
    findings = []
    for key, entries in ctx.tracks.items():
        break_count = 0
        for i in range(1, len(entries)):
            prev_frame, prev_box = entries[i - 1]
            cur_frame, cur_box = entries[i]
            gap = cur_frame - prev_frame - 1
            if not (1 <= gap <= gap_threshold):
                continue

            gap_frames = set(range(prev_frame + 1, cur_frame))
            if gap_frames & zero_box_frames:
                # 消失区间里有整帧 0 个框的帧，是帧级问题，不是这个 track 单独的追踪
                # 问题，不重复报 BrokenTrack；也不计入 break_count（不算这个 track
                # 真的断轨过一次，避免帧级问题顺带把后面的"第 2 次断轨"escalation 带偏）。
                continue

            break_count += 1
            reasons = []

            if break_count >= 1:
                reasons.append(f"同一目标第 {break_count} 次断轨")

            prev_pos, cur_pos = _position(prev_box), _position(cur_box)
            dist = _distance(prev_pos, cur_pos) if prev_pos is not None and cur_pos is not None else None
            if dist is not None and dist > position_threshold:
                reasons.append(f"重新出现后位置跳变 {dist:.2f}m（阈值 {position_threshold}m）")

            prev_label, cur_label = _label_of(prev_box), _label_of(cur_box)
            if prev_label != cur_label:
                reasons.append(f"重新出现后类别从 '{prev_label}' 变成 '{cur_label}'")

            severity = "Warning" if reasons else "Info"
            message = f"目标消失 {gap} 帧后重新出现"
            if reasons:
                message += "（" + "；".join(reasons) + "）"

            findings.append(RuleFinding(
                scene_id=ctx.scene_id, frame_index=cur_frame,
                track_id=key, bbox_index=cur_box.get("bbox_index", ""), label=cur_label,
                rule_id="BrokenTrack", severity=severity,
                message=message,
                evidence=f"track={key}, 消失区间=frame {prev_frame + 1}~{cur_frame - 1}, "
                         f"消失前帧={prev_frame}, 重新出现帧={cur_frame}",
            ))
    return findings


def rule_empty_frame(ctx: RuleContext) -> list[RuleFinding]:
    """某帧 bbox 数量为 0，报警。按 expected_frame_indices 判断，不是只看
    bbox_data.csv 里出现过的帧号（0 个框的帧在 bbox_data.csv 里本来就没有行）。"""
    findings = []
    ordered_frames = sorted(ctx.expected_frame_indices) or sorted(ctx.frames.keys())
    for frame_index in ordered_frames:
        if len(ctx.frames.get(frame_index, [])) == 0:
            findings.append(RuleFinding(
                scene_id=ctx.scene_id, frame_index=frame_index,
                track_id="", bbox_index="", label="",
                rule_id="EmptyFrame", severity="Warning",
                message="这一帧没有任何 BBox",
                evidence=f"frame_index={frame_index}",
            ))
    return findings


def rule_size_outlier(ctx: RuleContext) -> list[RuleFinding]:
    """BBox 任一尺寸 < size_outlier_min（默认 0.1m）或 > size_outlier_max（默认 30m）报警。"""
    size_min = ctx.rule_cfg().get("size_outlier_min", 0.1)
    size_max = ctx.rule_cfg().get("size_outlier_max", 30.0)
    findings = []
    for frame_index, boxes in ctx.frames.items():
        for box in boxes:
            scale = _scale(box)
            if scale is None:
                continue
            for axis_name, value in zip(("scale_x", "scale_y", "scale_z"), scale):
                if value < size_min or value > size_max:
                    findings.append(RuleFinding(
                        scene_id=ctx.scene_id, frame_index=frame_index,
                        track_id=_target_key(box), bbox_index=box.get("bbox_index", ""), label=_label_of(box),
                        rule_id="SizeOutlier", severity="Warning",
                        message=f"{axis_name}={value:.3f}m 超出合理范围 [{size_min}, {size_max}]",
                        evidence=f"frame_index={frame_index}, {axis_name}={value}",
                    ))
    return findings


def rule_static_object_position(ctx: RuleContext) -> list[RuleFinding]:
    """静止目标（灯塔/塔桥/场桥/雪糕筒等，见 static_object_classes 配置）理论上不会动，
    连续帧 position 变化超过阈值（默认 0.5m）报警——阈值统一用一个数字，不按类别区分。

    跟 PositionJump 一样，要求 frame_index 正好差 1 才比较，避免跳帧被当成"一帧内的
    位移"，原因见 rule_position_jump() 的说明。"""
    threshold = ctx.rule_cfg().get("static_object_position_threshold", 0.5)
    static_classes = set(ctx.rule_cfg().get("static_object_classes", []))
    findings = []
    for key, entries in ctx.tracks.items():
        static_entries = [(fi, box) for fi, box in entries if _label_of(box) in static_classes]
        prev_pos, prev_frame = None, None
        for frame_index, box in static_entries:
            pos = _position(box)
            if prev_pos is not None and pos is not None and frame_index - prev_frame == 1:
                dist = _distance(prev_pos, pos)
                if dist > threshold:
                    findings.append(RuleFinding(
                        scene_id=ctx.scene_id, frame_index=frame_index,
                        track_id=key, bbox_index=box.get("bbox_index", ""), label=_label_of(box),
                        rule_id="StaticObjectPosition", severity="Warning",
                        message="Static object position changed.",
                        evidence=f"track={key}, label={_label_of(box)}, prev_frame={prev_frame}(pos={prev_pos}), "
                                 f"cur_frame={frame_index}(pos={pos}), dist={dist:.3f}m（阈值 {threshold}m）",
                    ))
            if pos is not None:
                prev_pos, prev_frame = pos, frame_index
    return findings


# ---------------------------------------------------------------------------
# Persistent Issue 去重
# ---------------------------------------------------------------------------

def _merge_persistent_findings(findings: list[RuleFinding]) -> list[RuleFinding]:
    """同一个 track_id 的同一种 rule_id，如果在连续帧（frame_index 正好差 1）里持续命中，
    合并成一条记录：只保留第一次发现时的 message/evidence/severity/label/bbox_index，
    first_frame 是第一次发现的帧，last_frame 是持续到的最后一帧，occurrence_count 是
    连续命中了多少次。只要中间断了（frame_index 不连续），就切成新的一段——问题消失后
    再出现，算一条新记录，不会跟之前的合并在一起。

    不区分具体是哪条规则、统一按 (track_id, rule_id) 分组处理，新增规则不需要自己
    实现去重逻辑，这里是所有规则跑完之后统一做的后处理，也不需要改任何一条规则函数。
    EmptyFrame 这类没有 track_id 的规则，track_id 是空字符串，同样能正确分组
    （相当于把"连续几帧都是空帧"合并成一条，中间断开的空帧段算两条）。
    """
    groups: dict[tuple[str, str], list[RuleFinding]] = defaultdict(list)
    for finding in findings:
        groups[(finding.track_id, finding.rule_id)].append(finding)

    merged: list[RuleFinding] = []
    for items in groups.values():
        items.sort(key=lambda f: f.frame_index)
        run: list[RuleFinding] = []
        for finding in items:
            if run and finding.frame_index - run[-1].frame_index > 1:
                merged.append(_collapse_run(run))
                run = []
            run.append(finding)
        if run:
            merged.append(_collapse_run(run))

    merged.sort(key=lambda f: (f.frame_index, f.track_id, f.rule_id))
    return merged


def _collapse_run(run: list[RuleFinding]) -> RuleFinding:
    """一段连续帧的同一个持续问题，折叠成一条记录（保留第一条的 message/evidence 等，
    只补上 first_frame/last_frame/occurrence_count）。"""
    first = run[0]
    first.first_frame = run[0].frame_index
    first.last_frame = run[-1].frame_index
    first.occurrence_count = len(run)
    return first


# 规则注册表：规则名 -> 处理函数，签名统一是 (ctx: RuleContext) -> list[RuleFinding]。
RULE_REGISTRY = {
    "LabelConsistency": rule_label_consistency,
    "PositionJump": rule_position_jump,
    "RotationJump": rule_rotation_jump,
    "SizeConsistency": rule_size_consistency,
    "BrokenTrack": rule_broken_track,
    "EmptyFrame": rule_empty_frame,
    "SizeOutlier": rule_size_outlier,
    "StaticObjectPosition": rule_static_object_position,
}


def run_rules(
    bbox_csv_path: str,
    scene_id: str,
    config: dict,
    expected_frame_indices: list[int] | None = None,
) -> list[RuleFinding]:
    """跑一遍 config.yaml -> rule_engine.enabled_rules 里启用的规则，返回所有命中的 finding。
    expected_frame_indices 是本次场景理论上应该采集的完整帧号列表（main.py 传进来，
    独立重跑时不传，退化成 bbox_data.csv 里实际出现过的帧号，见模块开头的用法说明）。"""
    frames = load_bbox_data(bbox_csv_path)
    tracks = build_tracks(frames)
    ctx = RuleContext(
        scene_id=scene_id,
        frames=frames,
        tracks=tracks,
        expected_frame_indices=list(expected_frame_indices) if expected_frame_indices else sorted(frames.keys()),
        config=config,
    )

    enabled_rules = config.get("rule_engine", {}).get("enabled_rules", []) or []

    findings: list[RuleFinding] = []
    for rule_name in enabled_rules:
        fn = RULE_REGISTRY.get(rule_name)
        if fn is None:
            print(f"[警告] rule_engine.enabled_rules 中的 '{rule_name}' 尚未实现，已跳过。")
            continue
        findings.extend(fn(ctx) or [])

    # 同一 track_id 的同一种问题如果连续帧持续出现，合并成一条记录（只保留第一次发现的
    # 位置，附带 first_frame/last_frame/occurrence_count），不逐帧重复输出。
    return _merge_persistent_findings(findings)


def write_rule_report(findings: list[RuleFinding], report_cfg: dict, rule_engine_cfg: dict) -> str:
    """把所有 finding 写成 rule_report.csv，返回文件路径。没有 finding 时也会写出只有表头的空文件。"""
    output_dir = Path(report_cfg["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / rule_engine_cfg.get("output_filename", "rule_report.csv")

    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(RULE_REPORT_FIELDS)
        for finding in findings:
            writer.writerow(finding.as_row())

    return str(filepath)


def build_rule_summary(
    bbox_csv_path: str,
    findings: list[RuleFinding],
    missing_bbox_frames: list[int],
    scene_id: str,
    expected_frame_indices: list[int] | None = None,
) -> dict:
    """汇总 rule_summary.json 的内容（返回 dict）。total_frames 用 expected_frame_indices
    （main.py 传进来的真实帧数），不传时退化成 bbox_data.csv 里实际出现过的帧号数量。"""
    frames = load_bbox_data(bbox_csv_path)
    total_bbox = sum(len(boxes) for boxes in frames.values())
    total_frames = len(expected_frame_indices) if expected_frame_indices else len(frames)

    warnings_by_rule: dict[str, int] = {}
    frames_with_issues: set[int] = set()
    tracks_with_issues: set[str] = set()
    total_warnings = 0
    for finding in findings:
        if finding.severity == "Warning":
            total_warnings += 1
        warnings_by_rule[finding.rule_id] = warnings_by_rule.get(finding.rule_id, 0) + 1
        frames_with_issues.add(finding.frame_index)
        if finding.track_id:
            tracks_with_issues.add(finding.track_id)

    return {
        "scene_id": scene_id,
        "total_frames": total_frames,
        "total_bbox": total_bbox,
        "total_warnings": total_warnings,
        "warnings_by_rule": warnings_by_rule,
        "frames_with_issues": sorted(frames_with_issues),
        "tracks_with_issues": sorted(tracks_with_issues),
        # ---- 以下是抓取阶段容错功能（见模块开头说明）额外携带的统计，不在最少必需字段列表里 ----
        "missing_bbox_frames": sorted(set(missing_bbox_frames)),
        "missing_bbox_count": len(set(missing_bbox_frames)),
    }


def write_rule_summary(
    bbox_csv_path: str,
    findings: list[RuleFinding],
    missing_bbox_frames: list[int],
    scene_id: str,
    report_cfg: dict,
    expected_frame_indices: list[int] | None = None,
) -> tuple[str, dict]:
    """把 build_rule_summary() 的内容写成 outputs/reports/rule_summary.json，
    返回 (文件路径, summary dict) —— 调用方可以直接拿 summary dict 打印终端汇总，
    不用再重新读一遍文件。"""
    output_dir = Path(report_cfg["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / "rule_summary.json"

    summary = build_rule_summary(bbox_csv_path, findings, missing_bbox_frames, scene_id, expected_frame_indices)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    return str(filepath), summary


def print_rule_summary(summary: dict, max_list_items: int = 20) -> None:
    """把 rule_summary.json 的关键内容打印到终端，不用打开文件就能看结果。"""
    def _truncated(items: list) -> str:
        shown = items[:max_list_items]
        text = ", ".join(str(x) for x in shown)
        if len(items) > max_list_items:
            text += f" ...（还有 {len(items) - max_list_items} 个未列出）"
        return text if text else "（无）"

    print()
    print("=" * 60)
    print(f"[规则引擎汇总] scene_id={summary['scene_id']}")
    print("-" * 60)
    print(f"  总帧数: {summary['total_frames']}    总框数: {summary['total_bbox']}    "
          f"总告警数: {summary['total_warnings']}")

    warnings_by_rule = summary.get("warnings_by_rule", {})
    if warnings_by_rule:
        print("  按规则统计:")
        for rule_id in sorted(warnings_by_rule, key=lambda r: -warnings_by_rule[r]):
            print(f"    {rule_id:<20} {warnings_by_rule[rule_id]}")
    else:
        print("  按规则统计: （没有任何命中）")

    print(f"  有问题的帧号: {_truncated(summary.get('frames_with_issues', []))}")
    print(f"  有问题的目标: {_truncated(summary.get('tracks_with_issues', []))}")

    missing = summary.get("missing_bbox_frames", [])
    if missing:
        print(f"  BBox 读取失败的帧号: {_truncated(missing)}")

    print("=" * 60)


# ----------------------------------------------------------------------
# 独立入口：main.py 抓取阶段已经跑过一次的 bbox_data.csv，可以在这里反复重跑规则，
# 不需要重新打开浏览器、重新采集。
# ----------------------------------------------------------------------

def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main(bbox_csv_path: str | None = None, config_path: str = "config.yaml") -> None:
    config = load_config(config_path)
    report_cfg = config["report"]
    rule_engine_cfg = config.get("rule_engine", {})

    if bbox_csv_path is None:
        bbox_extract_cfg = config.get("bbox_extract", {})
        bbox_csv_path = f"{report_cfg['output_dir']}/{bbox_extract_cfg.get('output_filename', 'bbox_data.csv')}"

    print(f"[信息] 从 {bbox_csv_path} 读取 BBox 数据...")
    frames = load_bbox_data(bbox_csv_path)
    first_row = next(iter(next(iter(frames.values()), [])), None)
    scene_id = first_row["scene_id"] if first_row else "scene"

    findings = run_rules(bbox_csv_path, scene_id, config)
    print(f"[信息] 规则引擎运行结束，共命中 {len(findings)} 条 finding。")

    report_path = write_rule_report(findings, report_cfg, rule_engine_cfg)
    print(f"[信息] 规则报告已生成: {report_path}")

    summary_path, summary = write_rule_summary(bbox_csv_path, findings, [], scene_id, report_cfg)
    print(f"[信息] 规则汇总已生成: {summary_path}（独立重跑，没有 missing_bbox_frames 信息）")
    print_rule_summary(summary)


if __name__ == "__main__":
    arg_path = sys.argv[1] if len(sys.argv) > 1 else None
    main(arg_path)

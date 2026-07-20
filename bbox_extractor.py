"""
bbox_extractor.py
在 window.viewer 的 Three.js 场景图里递归查找 BoxVolume 节点，读取每一帧真实的
BBox 全部可获得字段（身份标识、类别、位置/朝向/尺寸、可见性等），只读属性、
不调用任何函数、不修改任何数据。

背景：bbox_probe.py 是广撒网式的关键词扫描，用来"探测 BBox 数据大概在哪"；
本模块是确认了确切位置之后，针对性做的干净提取，产出可以直接喂给 rule_engine.py 的
结构化数据（bbox_data.csv）。两者相互独立，互不影响，bbox_probe.py 的行为没有任何改动。

踩过的坑：
  1. 一开始以为 window.viewer.recycleVolumes 是"当前帧生效的框"，用真实画面上明确有框
     的一帧验证时发现读到的是空数组——recycleVolumes 更像是"待复用的对象池"，不代表
     当前渲染的框；window.viewer.annotationGroup 这个引用当时也是空的（可能会被平台
     重新赋值，读到的是过期引用）。真正可靠的做法是不认定某个固定属性名，而是在
     window.viewer 全部 Object3D 类型的属性下递归查找 constructor.name === 'BoxVolume'
     的节点——这是场景图里真正被渲染出来的框。
  2. 检查过 viewer.volumesData / viewer.cloudData / viewer.prevFrameData / viewer.data
     这些"看起来像独立数据源"的字段，确认都不含框级别的额外身份信息（前两个是空的，
     prevFrameData 只有 points/frameId）。BoxVolume 节点自己就是唯一、完整的数据源，
     没有另外一份更全的表可以关联，不需要跨结构拼接。
  3. BoxVolume 节点上没有字面意义的 target_id / object_id / label 字段；trackId /
     className / uuid / name / visible 是确认存在的。_mark.id 看起来是标注数据模型
     自己的 ID（区别于 Three.js 自动生成的 uuid），作为额外字段留存，不覆盖标准列。

字段来源对照（BoxVolume 节点上的原始属性名 -> bbox_data.csv 列名）：
  uuid -> uuid（Three.js 场景对象的 UUID，同一帧内唯一；注意：不保证跨帧稳定，
          如果平台每帧重建 Three.js 对象，同一个真实物体在不同帧的 uuid 可能不同）
  trackId -> track_id（确认是跨帧稳定的跟踪 ID，推荐作为目标唯一标识，见 resolve_target_key）
  className -> class_name（类别中文名，如"轿车"）
  _classId -> class_id（类别英文代码，如"car"，额外字段）
  name -> name（Three.js Object3D.name，实测是"box_0"这种按索引生成的通用名，不是语义标签）
  position/rotation/scale -> position_x/y/z、rotation_x/y/z（Euler 角）、scale_x/y/z
  visible -> visible
  isHide/isSelected -> is_hide/is_selected（额外字段，UI 状态）
  _mark.id -> mark_id（额外字段，标注数据模型自己的 ID，跟 uuid 是两回事）
  points -> point_count（额外字段，框内点云点数，可用于"点数太少可能是误标"这类规则）
  startIndex -> start_index（额外字段）

target_id / object_id / label 这三个字段在这个平台的 BoxVolume 节点上找不到对应的原始属性，
按需求保留这三列、值留空，不删除列。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, fields
from pathlib import Path

from playwright.sync_api import Page

# capture_report.csv 风格的必需列（用户明确列出的最少字段）+ 额外发现的有用字段。
# 任何一个字段在当前平台上找不到对应数据时，列依然保留，值为空字符串，不删除列。
BBOX_CSV_FIELDS = [
    "scene_id",
    "frame_index",
    "bbox_index",
    "track_id",
    "target_id",
    "uuid",
    "object_id",
    "label",
    "className",
    "name",
    "position_x",
    "position_y",
    "position_z",
    "rotation_x",
    "rotation_y",
    "rotation_z",
    "scale_x",
    "scale_y",
    "scale_z",
    "visible",
    # ---- 以下是平台上额外发现、对质检规则可能有用的字段（不在最少必需列表里）----
    "class_id",
    "mark_id",
    "is_hide",
    "is_selected",
    "point_count",
    "start_index",
    # ---- 目标唯一标识解析结果（按 track_id > target_id > uuid > object_id > bbox_index 优先级）----
    "resolved_target_field",
    "resolved_target_key",
]

# 按优先级尝试用哪个字段跨帧标识"同一个目标"：track_id 最优，一路 fallback 到 bbox_index。
TARGET_KEY_PRIORITY = ["track_id", "target_id", "uuid", "object_id"]

# 在浏览器里执行的只读提取脚本：在 window.viewer 所有 Object3D 属性的场景图里递归查找
# constructor.name === 'BoxVolume' 的节点，尽量读出所有可能有用的字段。
# 对 target_id / object_id / label 这几个不确定是否存在的字段名做了防御性尝试
# （node.targetId / node.target_id / node.objectId / node.object_id / node.label），
# 目前这个平台上都读不到，但保留这些尝试，万一其它类别的框有不同字段结构也能捕获到。
_EXTRACT_JS = """
() => {
  function safeKeys(obj) { try { return Object.keys(obj); } catch (e) { return []; } }
  function vec3(v) { return v ? [v.x, v.y, v.z] : [null, null, null]; }
  function euler(v) { return v ? [v._x, v._y, v._z] : [null, null, null]; }
  function firstDefined(...vals) {
    for (const v of vals) { if (v !== undefined && v !== null && v !== '') return v; }
    return null;
  }

  const viewer = window.viewer;
  // 明确区分"window.viewer 不存在"（提取失败，比如页面还没加载完成）和
  // "viewer 存在但这一帧确实没有框"（合法的空结果）：前者返回 null，后者返回 []。
  if (!viewer) return null;

  const results = [];
  const visited = new Set();

  function walk(node, depth) {
    if (!node || typeof node !== 'object' || depth > 6) return;
    if (visited.has(node)) return;
    visited.add(node);

    const ctor = node.constructor ? node.constructor.name : '';
    if (ctor === 'BoxVolume') {
      let markId = null;
      try { markId = node._mark ? node._mark.id : null; } catch (e) {}

      results.push({
        uuid: node.uuid || '',
        trackId: firstDefined(node.trackId, node.track_id),
        targetId: firstDefined(node.targetId, node.target_id),
        objectId: firstDefined(node.objectId, node.object_id),
        label: firstDefined(node.label),
        className: node.className,
        classId: node._classId,
        name: node.name,
        position: vec3(node.position),
        rotation: euler(node.rotation),
        scale: vec3(node.scale),
        visible: node.visible,
        isSelected: !!node.isSelected,
        isHide: !!node.isHide,
        markId: markId,
        pointCount: (typeof node.points === 'number') ? node.points : null,
        startIndex: (typeof node.startIndex === 'number') ? node.startIndex : null,
      });
      return; // BoxVolume 节点本身不会再嵌套别的 BoxVolume，找到就不用继续往下钻
    }

    if (Array.isArray(node.children)) {
      node.children.forEach(c => walk(c, depth + 1));
    }
  }

  // 从 viewer 的每一个一级属性开始找，只钻看起来像 Three.js 容器的属性
  // （isObject3D 为 true，或者有 children 数组），不假设具体是哪个属性名。
  for (const key of safeKeys(viewer)) {
    let val;
    try { val = viewer[key]; } catch (e) { continue; }
    if (val && typeof val === 'object' && (val.isObject3D || Array.isArray(val.children))) {
      walk(val, 0);
    }
  }

  return results;
}
"""


@dataclass
class BBoxRecord:
    """bbox_data.csv 的一行，对应某一帧里的一个 BBox。"""
    scene_id: str
    frame_index: int
    bbox_index: int
    track_id: str = ""
    target_id: str = ""
    uuid: str = ""
    object_id: str = ""
    label: str = ""
    className: str = ""
    name: str = ""
    position_x: float | None = None
    position_y: float | None = None
    position_z: float | None = None
    rotation_x: float | None = None
    rotation_y: float | None = None
    rotation_z: float | None = None
    scale_x: float | None = None
    scale_y: float | None = None
    scale_z: float | None = None
    visible: bool | None = None
    class_id: str = ""
    mark_id: str = ""
    is_hide: bool = False
    is_selected: bool = False
    point_count: int | None = None
    start_index: int | None = None
    resolved_target_field: str = ""
    resolved_target_key: str = ""

    def as_row(self) -> list:
        return [getattr(self, name) for name in BBOX_CSV_FIELDS]


def resolve_target_key(record: BBoxRecord) -> tuple[str, str]:
    """按 track_id > target_id > uuid > object_id > bbox_index 的优先级，
    返回 (用的哪个字段, 具体值)，供跨帧标识"同一个目标"用。"""
    for field_name in TARGET_KEY_PRIORITY:
        value = getattr(record, field_name, "")
        if value:
            return field_name, str(value)
    return "bbox_index", str(record.bbox_index)


@dataclass
class BBoxExtractionResult:
    """某一帧 BBox 提取的结果。success=False 表示这一帧读取失败（不是"这一帧恰好没有框"），
    上层（main.py）应该把 frame_index + error 记录进 missing_bbox_frames，
    继续处理下一帧，而不是中断整个程序。"""
    success: bool
    records: list[BBoxRecord]
    error: str = ""


def extract_bbox(page: Page, scene_id: str, frame_index: int) -> BBoxExtractionResult:
    """只读提取当前帧的 BBox 全部可获得字段。

    容错设计：页面未加载完成 / window.viewer 不存在 / JS 执行异常（比如 Vue 数据还没渲染出来、
    网络延迟导致 evaluate 超时）都会被这里捕获，返回 success=False + 具体原因，
    不会抛异常、不会影响后续帧的采集。"这一帧确实一个框都没有"（viewer 存在，只是空场景）
    跟"这一帧读取失败"是两回事，用 success 字段区分，不会混淆。
    """
    try:
        raw = page.evaluate(_EXTRACT_JS)
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        print(f"  [警告] 第 {frame_index} 帧 BBox 数据提取失败（不影响主流程）: {error_msg}")
        return BBoxExtractionResult(success=False, records=[], error=error_msg)

    if raw is None:
        error_msg = "window.viewer 不存在（页面可能未加载完成，或者当前不是点云查看页面）"
        print(f"  [警告] 第 {frame_index} 帧 BBox 数据提取失败（不影响主流程）: {error_msg}")
        return BBoxExtractionResult(success=False, records=[], error=error_msg)

    records = []
    for i, b in enumerate(raw or []):
        px, py, pz = b.get("position") or [None, None, None]
        rx, ry, rz = b.get("rotation") or [None, None, None]
        sx, sy, sz = b.get("scale") or [None, None, None]

        record = BBoxRecord(
            scene_id=scene_id,
            frame_index=frame_index,
            bbox_index=i,
            track_id=_as_str(b.get("trackId")),
            target_id=_as_str(b.get("targetId")),
            uuid=b.get("uuid", "") or "",
            object_id=_as_str(b.get("objectId")),
            label=_as_str(b.get("label")),
            className=b.get("className") or "",
            name=b.get("name") or "",
            position_x=px, position_y=py, position_z=pz,
            rotation_x=rx, rotation_y=ry, rotation_z=rz,
            scale_x=sx, scale_y=sy, scale_z=sz,
            visible=b.get("visible"),
            class_id=b.get("classId") or "",
            mark_id=_as_str(b.get("markId")),
            is_hide=bool(b.get("isHide")),
            is_selected=bool(b.get("isSelected")),
            point_count=b.get("pointCount"),
            start_index=b.get("startIndex"),
        )
        record.resolved_target_field, record.resolved_target_key = resolve_target_key(record)
        records.append(record)

    return BBoxExtractionResult(success=True, records=records)


def _as_str(value) -> str:
    """None/undefined 统一转成空字符串，其它值转成字符串（trackId 等字段平台上可能是数字）。"""
    if value is None:
        return ""
    return str(value)


def write_bbox_csv(records: list[BBoxRecord], report_cfg: dict, bbox_extract_cfg: dict) -> str:
    """把所有帧的 BBoxRecord 写成 bbox_data.csv，返回文件路径。"""
    output_dir = Path(report_cfg["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / bbox_extract_cfg.get("output_filename", "bbox_data.csv")

    with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(BBOX_CSV_FIELDS)
        for record in records:
            writer.writerow(record.as_row())

    return str(filepath)


def build_mapping_report(records: list[BBoxRecord]) -> str:
    """生成 mapping_report.txt 的文本内容：
      1. 实际导出了哪些字段（每个字段的非空填充率）
      2. 哪些字段整场下来一直为空
      3. 建议后续 Rule Engine 用哪个字段作为目标唯一标识（基于 resolved_target_field 的实际分布）
    """
    lines = []
    lines.append("BBox 数据字段映射报告（mapping_report.txt）")
    lines.append("=" * 60)
    lines.append(f"总记录数（所有帧的 box 之和）: {len(records)}")
    lines.append("")

    lines.append("1. 各字段填充情况（非空数量 / 总数）")
    lines.append("-" * 60)
    total = len(records)
    if total == 0:
        lines.append("  （没有任何 BBox 记录——可能所有帧都提取失败了，看 rule_summary.json 里的")
        lines.append("   missing_bbox_frames，或者这批帧真的全部没有标注物）")
        always_empty = []
    else:
        always_empty = []
        field_names = [f.name for f in fields(BBoxRecord)]
        for field_name in field_names:
            non_empty = sum(1 for r in records if _is_filled(getattr(r, field_name, None)))
            lines.append(f"  {field_name:<24} {non_empty}/{total}")
            if non_empty == 0:
                always_empty.append(field_name)

        lines.append("")
        lines.append("2. 整场下来一直为空的字段（平台上没有对应数据，列保留，值留空）")
        lines.append("-" * 60)
        if always_empty:
            for field_name in always_empty:
                lines.append(f"  - {field_name}")
        else:
            lines.append("  （没有，所有字段至少有部分记录填充了值）")

    lines.append("")
    lines.append("3. 目标唯一标识建议")
    lines.append("-" * 60)
    lines.append("优先级：track_id > target_id > uuid > object_id > bbox_index")
    if records:
        usage_count: dict[str, int] = {}
        for r in records:
            usage_count[r.resolved_target_field] = usage_count.get(r.resolved_target_field, 0) + 1
        for field_name in TARGET_KEY_PRIORITY + ["bbox_index"]:
            count = usage_count.get(field_name, 0)
            if count:
                lines.append(f"  实际使用 {field_name} 作为标识的记录数: {count}/{len(records)}")

        dominant_field = max(usage_count, key=usage_count.get)
        lines.append("")
        lines.append(f"建议：Rule Engine 使用 resolved_target_field / resolved_target_key 这两列，")
        lines.append(f"不需要自己重新判断优先级——本次数据里 {dominant_field} 覆盖了")
        lines.append(f"{usage_count[dominant_field]}/{len(records)} 条记录")
        if dominant_field != "track_id":
            lines.append(
                f"  [注意] track_id 不是本次的主要标识来源，跨帧关联同一目标的准确性可能受影响，"
                f"建议人工确认 {dominant_field} 是否真的能稳定标识同一个目标。"
            )
        if "uuid" in usage_count and usage_count.get("uuid", 0) > 0:
            lines.append(
                "  [提醒] uuid 是 Three.js 场景对象的 UUID，只保证同一帧内唯一，"
                "不保证跨帧对应同一个真实物体（如果平台每帧重建渲染对象，uuid 可能每帧都变）。"
                "如果 resolved_target_field 大量落在 uuid 上，Rule Engine 跨帧比对时要谨慎。"
            )
    else:
        lines.append("  （没有任何记录，无法判断）")

    lines.append("")
    return "\n".join(lines)


def _is_filled(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True  # 数字/布尔值只要不是 None 就算"有值"（包括 0 / False 这些合法取值）


def save_mapping_report(records: list[BBoxRecord], report_cfg: dict) -> str:
    """把 build_mapping_report() 的内容写成 outputs/reports/mapping_report.txt，返回文件路径。"""
    output_dir = Path(report_cfg["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / "mapping_report.txt"

    text = build_mapping_report(records)
    filepath.write_text(text, encoding="utf-8")

    return str(filepath)

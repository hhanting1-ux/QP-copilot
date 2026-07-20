"""
cleanup_scene.py
按 scene_id 清理 outputs/ 目录下这个场景产生的本地文件（截图/报告/BBox 数据/网络记录等）。

用于人工判断完一个 scene 之后，清掉这个 scene 的本地测试数据，给下一个 scene 腾地方。
只读扫描 + 删除文件：
  - 只在 outputs/reports、outputs/screenshots、outputs/assets 这三个目录里找
    （目录路径优先读 config.yaml，读不到才退回默认值，见 _load_scan_dirs()）
  - 只删文件，绝不删除 outputs/ 或其子目录本身
  - 不会碰任何项目代码/配置文件（.py/.yaml/.md 等）——因为压根不扫描 outputs/ 以外的地方
  - 不连浏览器、不改采集/规则逻辑，是一个完全独立的小工具

匹配规则（文件名 或 文件内容包含 scene_id 就算相关）：
  - 图片文件（.png/.jpg/.jpeg）：只按文件名匹配（截图命名是 {scene_id}_frame_NNN.png，
    内容是二进制，扫描内容既没意义也慢）
  - 其它文本文件（.csv/.json/.txt 等）：文件名包含 scene_id，或者文件内容里包含
    scene_id 都算匹配——现在大部分报告文件（capture_report.csv、bbox_data.csv、
    rule_report.csv、rule_summary.json、network_assets.csv、bbox_probe_report.json）
    文件名是固定的、不带 scene_id，只能靠内容里的 scene_id 列/字段判断是不是这个场景的数据。
  - 兜底（SCENE_AGNOSTIC_COMPANION_FILES）：极少数报告文件内容里完全不带 scene_id
    （比如 mapping_report.txt，是"这次抓取整体质量"的统计，不是某个场景自己的数据），
    文件名/内容都匹配不上。这种文件不改它的生成逻辑，而是在这里认：如果它所在的目录
    已经因为别的文件命中了这个 scene_id，说明这一批报告确实是这次场景生成的（同一次
    main.py 运行会把这些报告文件一起写出来），就把它也一并纳入清理范围。

用法：
    python cleanup_scene.py              # 交互式：列出将删除的文件，输入 DELETE 二次确认才真删
    python cleanup_scene.py --dry-run     # 只列出会删除哪些文件，不会真的删除，不需要输入 DELETE

scene_id 留空直接回车 -> 什么都不做，直接退出。
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

# 图片按文件名匹配即可，不读二进制内容（既没意义又浪费时间）
BINARY_EXTENSIONS = {".png", ".jpg", ".jpeg"}

# 内容匹配时单个文件的读取大小上限（字节），避免不小心扫到异常巨大的文件卡住
MAX_CONTENT_SCAN_BYTES = 20 * 1024 * 1024  # 20MB

# 找不到 config.yaml 或读取失败时的默认目录（跟 config.yaml 里的默认值保持一致）
DEFAULT_SCAN_DIRS = ["outputs/reports", "outputs/screenshots", "outputs/assets"]

# 内容里不带 scene_id、没法直接按文件名/内容匹配的报告文件——如果它所在目录已经有
# 别的文件因为这个 scene_id 匹配上了，就一并纳入清理（见模块开头「匹配规则」的说明）。
SCENE_AGNOSTIC_COMPANION_FILES = {"mapping_report.txt"}


def _load_scan_dirs(config_path: str = "config.yaml") -> list[str]:
    """优先读 config.yaml 里配置的实际输出目录，读不到就用默认值——不写死路径，
    避免以后有人在 config.yaml 里改了 output_dir，这个工具却删错地方（或者删不到）。"""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return DEFAULT_SCAN_DIRS

    dirs = []
    report_dir = config.get("report", {}).get("output_dir")
    screenshot_dir = config.get("screenshot", {}).get("output_dir")
    assets_dir = config.get("network", {}).get("assets_dir")
    for d in (report_dir, screenshot_dir, assets_dir):
        if d:
            dirs.append(d)

    return dirs or DEFAULT_SCAN_DIRS


def find_matching_files(scene_id: str, scan_dirs: list[str]) -> list[tuple[Path, str]]:
    """在 scan_dirs 里找文件名或内容包含 scene_id 的文件，返回 [(路径, 匹配原因), ...]。
    另外把 SCENE_AGNOSTIC_COMPANION_FILES 里那种"内容不带 scene_id 但跟其它匹配文件
    同批次生成"的文件也纳入（前提是它所在目录确实有其它文件匹配上了这个 scene_id，
    不会凭空把一个没有任何关联证据的目录里的文件也算进来）。"""
    matches: list[tuple[Path, str]] = []
    matched_parent_dirs: set[Path] = set()

    for scan_dir in scan_dirs:
        base = Path(scan_dir)
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if path.name == ".gitkeep":
                continue  # 目录占位文件，不是数据，不参与匹配

            if scene_id in path.name:
                matches.append((path, "文件名匹配"))
                matched_parent_dirs.add(path.parent)
                continue

            if path.suffix.lower() in BINARY_EXTENSIONS:
                continue  # 图片不扫内容

            try:
                if path.stat().st_size > MAX_CONTENT_SCAN_BYTES:
                    continue
                content = path.read_text(encoding="utf-8-sig", errors="ignore")
            except (OSError, UnicodeDecodeError):
                continue

            if scene_id in content:
                matches.append((path, "内容匹配"))
                matched_parent_dirs.add(path.parent)

    matched_paths = {p for p, _ in matches}
    for parent_dir in matched_parent_dirs:
        for name in SCENE_AGNOSTIC_COMPANION_FILES:
            companion = parent_dir / name
            if companion.is_file() and companion not in matched_paths:
                matches.append((companion, "同批次报告文件（内容不含 scene_id，随其它匹配文件一起清理）"))
                matched_paths.add(companion)

    return matches


def delete_files(matches: list[tuple[Path, str]]) -> int:
    """实际删除文件，返回成功删除的数量。只删文件（path.unlink），不删任何目录。"""
    deleted = 0
    for path, _reason in matches:
        try:
            path.unlink()
            deleted += 1
        except OSError as exc:
            print(f"[警告] 删除失败: {path} ({exc})")
    return deleted


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    scene_id = input("请输入 scene_id: ").strip()
    if not scene_id:
        print("[信息] scene_id 为空，退出，没有做任何操作。")
        return

    scan_dirs = _load_scan_dirs()
    matches = find_matching_files(scene_id, scan_dirs)

    print()
    if not matches:
        print(f"[信息] 没有找到跟 scene_id={scene_id!r} 相关的文件。")
        return

    print(f"将删除（scene_id={scene_id!r}，共 {len(matches)} 个文件）：")
    for path, reason in matches:
        print(f"  {path.as_posix()}（{reason}）")

    if dry_run:
        print()
        print(f"[信息] dry-run 模式，以上 {len(matches)} 个文件不会被真正删除。")
        return

    print()
    confirm = input("确认删除请输入 DELETE: ").strip()
    if confirm != "DELETE":
        print("[信息] 输入不是 DELETE，已取消，没有删除任何文件。")
        return

    deleted = delete_files(matches)
    print()
    print(f"[信息] 已删除 {deleted} / {len(matches)} 个文件。")


if __name__ == "__main__":
    main()

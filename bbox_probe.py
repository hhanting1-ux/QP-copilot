"""
bbox_probe.py
只读探测：BBox 数据到底能不能从前端页面里爬到。

背景：Network 面板里没找到 BBox JSON，PCD 里只有 x/y/z/intensity，
但页面确实画出了 3D BBox，所以数据大概率是：
  - 在页面加载时随某个大的初始状态/JS bundle一次性塞进了前端（比如 window.__INITIAL_STATE__）
  - 存在 Vue/Pinia/Vuex store 里
  - 存在 Three.js 的 scene graph（Object3D.userData）或某个全局的 viewer/scene 对象上

本模块的探测方式：在浏览器 console context 里执行只读 JS，
按关键词扫描 window 上的全局变量（以及浅层嵌套属性），
再尝试识别 Three.js 场景对象并读取 children 的 userData。

严格只读：
  - 不调用任何函数（只读取属性，不 invoke，避免任何副作用）
  - 不写入/修改 window 上的任何值
  - 不点击、不提交、不改变平台上的任何数据
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from playwright.sync_api import Page

# 在浏览器里执行的只读探测脚本。
# 输入参数 (keywords, maxDepth) 通过 page.evaluate 的第二个参数传入。
_PROBE_JS = """
([keywords, maxDepth]) => {
  function safeKeys(obj) {
    try { return Object.keys(obj); } catch (e) { return []; }
  }

  function matchesKeyword(key) {
    const k = String(key).toLowerCase();
    return keywords.some(kw => k.includes(kw));
  }

  // 排除掉明显不可能承载 BBox 数据的类型（null/undefined/function），
  // 减少浏览器内置属性（如 cookieStore、onstorage 之类事件句柄）造成的噪音。
  function isNoise(described) {
    return described.type === 'null' || described.type === 'undefined' || described.type === 'function';
  }

  function describeValue(val) {
    if (val === null) return { type: 'null' };
    const t = typeof val;
    if (t === 'function') return { type: 'function' };
    if (t !== 'object') return { type: t, sample: val };
    if (Array.isArray(val)) {
      return {
        type: 'array',
        length: val.length,
        sampleKeys: val.length > 0 ? safeKeys(val[0]).slice(0, 20) : [],
      };
    }
    const ctorName = (val.constructor && val.constructor.name) ? val.constructor.name : 'Object';
    return { type: 'object', constructor: ctorName, keys: safeKeys(val).slice(0, 30) };
  }

  // ---- 1. 扫描 window 顶层 + 浅层嵌套（命中关键词的 key） ----
  const windowMatches = [];
  const topKeys = safeKeys(window);

  for (const key of topKeys) {
    if (!matchesKeyword(key)) continue;
    let val;
    try { val = window[key]; } catch (e) { continue; }
    if (val === undefined) continue;

    const described = describeValue(val);
    if (!isNoise(described)) {
      windowMatches.push({ path: 'window.' + key, ...described });
    }

    if (val && typeof val === 'object' && !Array.isArray(val)) {
      for (const subKey of safeKeys(val)) {
        if (!matchesKeyword(subKey)) continue;
        let subVal;
        try { subVal = val[subKey]; } catch (e) { continue; }
        const subDescribed = describeValue(subVal);
        if (isNoise(subDescribed)) continue;
        windowMatches.push({ path: `window.${key}.${subKey}`, ...subDescribed });
      }
    }
  }

  // ---- 2. Three.js 场景遍历：找 isScene / children 是 Object3D 的对象 ----
  function tryTraverseThree(pathStr, root, depth) {
    if (!root || typeof root !== 'object' || depth > maxDepth) return null;
    const isSceneLike = root.isScene === true;
    const hasObject3DChildren = Array.isArray(root.children) && root.children.length > 0
      && root.children[0] && typeof root.children[0] === 'object' && root.children[0].isObject3D === true;

    if (!isSceneLike && !hasObject3DChildren) return null;

    const withUserData = root.children.filter(c => c && c.userData && safeKeys(c.userData).length > 0);
    return {
      path: pathStr,
      childCount: root.children.length,
      childrenWithUserDataCount: withUserData.length,
      sampleUserData: withUserData.slice(0, 3).map(c => c.userData),
    };
  }

  const threeJsCandidates = [];
  for (const key of topKeys) {
    if (!matchesKeyword(key)) continue;
    let val;
    try { val = window[key]; } catch (e) { continue; }

    const found = tryTraverseThree('window.' + key, val, 0);
    if (found) threeJsCandidates.push(found);

    if (val && typeof val === 'object') {
      for (const subKey of safeKeys(val)) {
        if (!matchesKeyword(subKey)) continue;
        let subVal;
        try { subVal = val[subKey]; } catch (e) { continue; }
        const foundSub = tryTraverseThree(`window.${key}.${subKey}`, subVal, 1);
        if (foundSub) threeJsCandidates.push(foundSub);
      }
    }
  }

  return { windowMatches, threeJsCandidates };
}
"""


def probe_frame(page: Page, frame_index: int, bbox_cfg: dict) -> dict:
    """对当前帧执行一次只读探测，返回这一帧的探测结果（不抛异常，失败也会返回结果字典）。"""
    keywords = [str(k).lower() for k in bbox_cfg.get("keywords", [])]
    max_depth = bbox_cfg.get("max_depth", 2)

    try:
        raw = page.evaluate(_PROBE_JS, [keywords, max_depth])
        return {
            "frame_index": frame_index,
            "success": True,
            "error": "",
            "window_matches": raw.get("windowMatches", []),
            "threejs_candidates": raw.get("threeJsCandidates", []),
        }
    except Exception as exc:
        return {
            "frame_index": frame_index,
            "success": False,
            "error": str(exc),
            "window_matches": [],
            "threejs_candidates": [],
        }


def _has_findings(probe_result: dict) -> bool:
    return bool(probe_result["window_matches"] or probe_result["threejs_candidates"])


def build_probe_report(probe_results: list[dict], scene_id: str) -> dict:
    """把多帧的探测结果汇总成一份报告，包含结论和（找不到时的）下一步建议。"""
    found_any = any(_has_findings(r) for r in probe_results)

    report = {
        "scene_id": scene_id,
        "probed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "probed_frames": [r["frame_index"] for r in probe_results],
        "found_candidates": found_any,
        "frames": probe_results,
    }

    if found_any:
        report["conclusion"] = (
            "在 window 全局变量或 Three.js 场景中找到了疑似 BBox 相关的字段/对象，"
            "请人工核对 frames[].window_matches / frames[].threejs_candidates 里的字段名和样例结构，"
            "确认是否真的是 BBox（位置/朝向/尺寸）数据。"
        )
        report["next_steps"] = [
            "对着找到的 path（例如 window.viewer.xxx）在浏览器 Console 里手动展开，确认字段含义",
            "确认数据结构后，可以把对应的读取逻辑固化成一个专用的抓取函数，替代本探测脚本",
        ]
    else:
        report["conclusion"] = "未能在 window 全局变量或常见 Three.js 场景结构中找到疑似 BBox 数据。"
        report["possible_reasons"] = [
            "BBox 数据可能通过 WebSocket 或其它非 fetch/XHR 的方式传输，不会出现在 window 全局变量里",
            "前端框架使用了模块作用域变量（ES Module 闭包），没有挂载到 window 上，无法从外部读取",
            "数据可能封装在 iframe 或 Web Worker 里，主 window 探测不到，需要在对应的 frame/worker context 里探测",
            "变量命名没有匹配到 config.yaml -> bbox_probe.keywords 里的关键词",
        ]
        report["next_steps"] = [
            "在浏览器 DevTools 的 Sources 面板全局搜索 'psr' / 'bbox' / 'annotation' 等字符串，定位实际变量名",
            "找到变量名后，加入 config.yaml -> bbox_probe.keywords，重新运行探测",
            "检查 DevTools Network 面板的 WS（WebSocket）分类，看 BBox 是否通过 WebSocket 推送",
            "如果确认数据在 iframe 里，需要扩展本模块支持在对应 frame context 里执行探测（当前版本只探测主 window）",
        ]

    return report


def save_probe_report(report: dict, bbox_cfg: dict, report_cfg: dict) -> str:
    """把探测报告保存为 JSON，返回文件路径。"""
    output_dir = Path(report_cfg["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / bbox_cfg.get("output_filename", "bbox_probe_report.json")

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    return str(filepath)

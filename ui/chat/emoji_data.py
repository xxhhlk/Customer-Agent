"""
表情（方括号占位符）数据层
==========================
拼多多表情 = 方括号文本占位符，如 ``[玫瑰]``，发送走纯文本接口，PDD 服务端自行渲染。
本模块只负责：
- 维护本地表情名库（data/emoji.json）
- 名称 → emoji 字符的预览映射（仅用于面板显示 + 气泡本地预览，与发送内容无关）
- 气泡渲染：把已知 ``[xxx]`` 替换为 emoji 字符

所有操作均在主线程完成，无 DB 操作，不涉及项目 QThread 铁律。
"""

import json
import os
import re
import threading
from copy import deepcopy
from pathlib import Path
from typing import Optional

from utils.logger_loguru import get_logger

logger = get_logger("EmojiData")

# 名称 → emoji 字符，仅用于「面板显示 + 气泡本地预览」。
# 只保留数据库实测 PDD 可渲染（verified）的 14 个；其余一律不猜。
# 映射不到的名字在渲染时保留原方括号文本（仍可辨认是表情）。
EMOJI_UNICODE: dict[str, str] = {
    # 已实测 PDD 能渲染（verified，共 14 个）
    "玫瑰": "🌹",
    "抱拳": "🙏",
    "微笑": "😊",
    "点赞": "👍",
    "流泪": "😭",
    "汗": "😓",
    "撇嘴": "😖",
    "捂脸": "🤦",
    "大哭": "😢",
    "敲打": "🔨",
    "笑哭": "😂",
    "上火了": "😤",
    "可怜": "🥺",
    "呆住": "😳",
}

# 内置默认表（首次启动生成；用户可直接手改 emoji.json）
# 默认只放数据库实测可渲染的 14 个，不预置任何猜测的微信表情分组。
# 全量官方表情请按 docs/emoji_feature_plan.md §6 从拼多多客户端导入。
DEFAULT_LIBRARY: dict = {
    "version": 1,
    # 实测确认 PDD 能渲染的表情名（置顶 + 面板打 ✅）
    "verified": [
        "玫瑰", "抱拳", "微笑", "点赞", "流泪", "汗", "撇嘴", "捂脸",
        "大哭", "敲打", "笑哭", "上火了", "可怜", "呆住",
    ],
    # 最近使用，最多 24 个，最新的在前
    "recent": [],
    "groups": [
        # 常用 = 已验证的 14 个，面板中均为亮色（✅）
        {"name": "常用", "items": [
            "玫瑰", "抱拳", "微笑", "点赞", "流泪", "汗", "撇嘴", "捂脸",
            "大哭", "敲打", "笑哭", "上火了", "可怜", "呆住",
        ]},
        # 自定义：初始为空，§6 导入官方全量后自动填充
        {"name": "自定义", "items": []},
    ],
}

# 方括号占位符正则：长度 ≤10 且不含空白/换行，避免误伤 [20]、商品链接等
PLACEHOLDER_RE = re.compile(r"\[([^\[\]\s]{1,10})\]")

_RECENT_CAP = 24
_lock = threading.Lock()


def _library_path() -> Path:
    """emoji.json 落点：开发环境=项目根/data，打包环境=exe 目录/data"""
    from utils.runtime_path import get_base_path

    return get_base_path() / "data" / "emoji.json"


def _validate(lib: dict) -> None:
    """逐字段校验，缺字段从默认表补齐；结构非法则抛异常触发回退"""
    if not isinstance(lib, dict):
        raise ValueError("library root not a dict")
    if "version" not in lib:
        lib["version"] = DEFAULT_LIBRARY["version"]
    if not isinstance(lib.get("verified"), list):
        lib["verified"] = list(DEFAULT_LIBRARY["verified"])
    if not isinstance(lib.get("recent"), list):
        lib["recent"] = []
    if not isinstance(lib.get("groups"), list):
        lib["groups"] = deepcopy(DEFAULT_LIBRARY["groups"])
    for g in lib["groups"]:
        if not isinstance(g, dict) or "name" not in g or not isinstance(g.get("items"), list):
            raise ValueError("group schema invalid")
        g["items"] = [str(x) for x in g["items"]]


def load_library() -> dict:
    """读 data/emoji.json，不存在则写默认表；损坏则备份坏文件并回退默认表"""
    path = _library_path()
    if not path.exists():
        lib = deepcopy(DEFAULT_LIBRARY)
        save_library(lib)
        return lib
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _validate(data)
        return data
    except Exception as e:  # noqa: BLE001 - 任何解析/校验异常都回退
        logger.error(f"emoji.json 读取/校验失败，回退默认表: {e}")
        try:
            if path.exists():
                bak = path.with_suffix(".json.bak")
                os.replace(path, bak)
                logger.warning(f"坏文件已备份为 {bak}")
        except Exception as e2:  # noqa: BLE001
            logger.error(f"备份坏文件失败: {e2}")
        lib = deepcopy(DEFAULT_LIBRARY)
        save_library(lib)
        return lib


def save_library(lib: dict) -> None:
    """原子写：tmp + os.replace"""
    path = _library_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with _lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(lib, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


def all_names(lib: dict) -> list[str]:
    """全部表情名（去重，保序）"""
    seen: set[str] = set()
    out: list[str] = []
    for name in lib.get("verified", []):
        if name not in seen:
            seen.add(name)
            out.append(name)
    for g in lib.get("groups", []):
        for name in g.get("items", []):
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


def placeholder(name: str) -> str:
    """名称 → 方括号占位符，如 '玫瑰' -> '[玫瑰]'"""
    return f"[{name}]"


def render_text(text: str, lib: Optional[dict] = None) -> str:
    """把已知 ``[xxx]`` 渲染为 emoji 字符；未知占位符原样保留。

    - 正则仅替换「在映射表内」的名称（EMOJI_UNICODE 是渲染可用的权威集合）
    - 额外兼容：整条 content 恰好等于某个已知表情名（收端 emotion 裸名形态）时也渲染
    """
    if not text:
        return text

    def repl(m: re.Match) -> str:
        name = m.group(1)
        ch = EMOJI_UNICODE.get(name)
        return ch if ch else m.group(0)

    out = PLACEHOLDER_RE.sub(repl, text)

    # 兼容收端 emotion 消息：整条内容就是裸表情名（无括号）
    s = text.strip()
    if s in EMOJI_UNICODE:
        return EMOJI_UNICODE[s]
    return out


def add_recent(lib: dict, name: str) -> None:
    """最近使用，上限 24，最新在前，去重"""
    recent = lib.get("recent", [])
    if name in recent:
        recent.remove(name)
    recent.insert(0, name)
    lib["recent"] = recent[:_RECENT_CAP]


def import_from_text(lib: dict, raw: str) -> list[str]:
    """从文本正则提取 [xxx]，去重、覆盖写入「自定义」组，返回新增项。

    设计取舍：导入的官方全量列表覆盖「自定义」组（符合 §6 一键拿到全量），
    保留 verified / recent 与其余预设分组不破坏。
    """
    found: list[str] = []
    seen: set[str] = set()
    for name in PLACEHOLDER_RE.findall(raw):
        if name not in seen:
            seen.add(name)
            found.append(name)

    grp = next((g for g in lib.get("groups", []) if g.get("name") == "自定义"), None)
    if grp is None:
        grp = {"name": "自定义", "items": []}
        lib.setdefault("groups", []).append(grp)

    existing = set(grp.get("items", []))
    added = [n for n in found if n not in existing]
    # 覆盖写入自定义组（官方全量）
    grp["items"] = found
    return added

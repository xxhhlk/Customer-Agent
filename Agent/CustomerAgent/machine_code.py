"""机器码 / 设备编号的格式判定（单一事实来源）。

奥维的「机器码」是硬件唯一标识，格式固定：

    50378B2F - D563EA05              电脑端（8 位十六进制 - 8 位十六进制）
    4296292C-5258A423                无空格写法
    808ACA82 - DE714A55 - 6E9FE0FD   个别设备为三段（8-8-8）

而纯数字串是「设备编号 / 本机编号」，**不是机器码**：

    113317104
    14313890                          （奥维官方文档中的本机编号示例）

拿设备编号去授权必然失败。2026-11-11 买家发「机器编号是这个 / 113317104」，
AI 直接回「收到 机器码 113317104 已经记下 我这边安排授权」—— 承诺了无法兑现的授权。

判定必须区分以下三种串：

- 合法机器码：8 位十六进制 - 8 位十六进制（可再带一段）
- 设备编号：纯数字 6~12 位
- 订单号：6 位日期 - 15 位数字（``261111-283891515713620``）—— 不是编号，别误伤

本模块只做纯函数判定，不碰网络与数据库。
"""

import re
from typing import List, Tuple

# 合法机器码：8 位十六进制 - 8 位十六进制（可选第三段）
MACHINE_CODE_RE = re.compile(
    r"[0-9A-Fa-f]{8}\s*-\s*[0-9A-Fa-f]{8}(?:\s*-\s*[0-9A-Fa-f]{8})?"
)

# 设备编号 / 本机编号：纯数字
DEVICE_ID_RE = re.compile(r"\d{6,12}")

# 拼多多订单号：6 位日期(YYMMDD) - 15 位数字
ORDER_SN_RE = re.compile(r"\d{6}-\d{15}")

# 判定违规时向两侧扩展的上下文窗口（字符）
_VIOLATION_WINDOW = 25

# 从数字 token 右端继续匹配 "- 8位十六进制"，用于识别
# 「12345678 - 87654321」这种全数字但合法的机器码（不该被当成设备编号）
_TAIL_MACHINE_CODE_RE = re.compile(r"(?:\s*-\s*[0-9A-Fa-f]{8}){1,2}")

# 命中「编号」时还要出现这些接受性动词，才算「把编号收下了」
_ACCEPT_VERBS = ("收到", "记下", "记录", "安排授权", "去授权", "授权", "开通")


def is_machine_code(text: str) -> bool:
    """整段文本是否为合法机器码。"""
    if not text:
        return False
    return bool(MACHINE_CODE_RE.fullmatch(text.strip()))


def is_device_id(text: str) -> bool:
    """整段文本是否为设备编号（纯数字 6~12 位）。"""
    if not text:
        return False
    return bool(DEVICE_ID_RE.fullmatch(text.strip()))


def is_order_sn(text: str) -> bool:
    """整段文本是否为拼多多订单号。"""
    if not text:
        return False
    return bool(ORDER_SN_RE.fullmatch(text.strip()))


def is_device_id_only(text: str) -> bool:
    """整条买家消息是否只有一个设备编号（用于入站拦截）。

    只在「消息整体就是一个编号」时成立：买家若同时说了别的事，
    不能拦掉整条消息，应交给模型判断。
    """
    if not text:
        return False
    stripped = text.strip()
    return bool(DEVICE_ID_RE.fullmatch(stripped))


def _protected_spans(text: str) -> List[Tuple[int, int]]:
    """订单号与合法机器码所占的区间——这些串不能被当成编号。"""
    spans: List[Tuple[int, int]] = []
    for m in ORDER_SN_RE.finditer(text):
        spans.append((m.start(), m.end()))
    for m in MACHINE_CODE_RE.finditer(text):
        spans.append((m.start(), m.end()))
    return spans


def _numeric_tokens(text: str) -> List[Tuple[str, List[Tuple[int, int]]]]:
    """文本中的纯数字 token 及其出现位置（已排除订单号与合法机器码内部）。

    「12345678 - 87654321」这类全数字机器码：左段是纯数字，但右端能接上
    "- 8位十六进制"，整体是合法机器码，不算设备编号。
    """
    if not text:
        return []
    protected = _protected_spans(text)
    tokens: dict = {}
    for m in DEVICE_ID_RE.finditer(text):
        start, end = m.span()
        # 与受保护区间重叠的片段属于订单号/机器码的一部分，跳过
        if any(start < pe and ps < end for ps, pe in protected):
            continue
        # 右端能接上 "- 8位十六进制" → 整体是合法机器码的左半段
        tail = _TAIL_MACHINE_CODE_RE.match(text, end)
        if tail and MACHINE_CODE_RE.fullmatch(text[start:tail.end()]):
            continue
        tokens.setdefault(m.group(0), []).append((start, end))
    return list(tokens.items())


def detect_machine_code_violation(reply_text: str) -> List[str]:
    """检测回复里「把设备编号当机器码收下了」的表述。

    命中两种模式：

    1. 「机器码」附近出现一个非法编号（纯数字 6~12 位）—— 典型的
       「收到 机器码 113317104 已经记下」
    2. 「编号」附近出现非法编号，且同窗口内还有接受性动词 —— 典型的
       「收到 设备编号 113317104 我记下了」

    Returns:
        命中的编号列表（去重）。空列表表示未发现违规。
    """
    if not reply_text:
        return []

    hits: List[str] = []
    for token, positions in _numeric_tokens(reply_text):
        for start, end in positions:
            window = reply_text[max(0, start - _VIOLATION_WINDOW): end + _VIOLATION_WINDOW]
            if "机器码" in window:
                hits.append(token)
                break
            if "编号" in window and any(v in window for v in _ACCEPT_VERBS):
                hits.append(token)
                break
    return list(dict.fromkeys(hits))


__all__ = [
    "MACHINE_CODE_RE",
    "DEVICE_ID_RE",
    "ORDER_SN_RE",
    "is_machine_code",
    "is_device_id",
    "is_order_sn",
    "is_device_id_only",
    "detect_machine_code_violation",
]

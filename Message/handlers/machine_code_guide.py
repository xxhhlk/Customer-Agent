"""机器码收取口径的固定话术与判定。

背景（2026-11-11 生产事故）：买家发「113317104 / 机器编号是这个」，
AI 回「收到 机器码 113317104 已经记下 我这边安排授权」——把**设备编号**
当成了机器码。这个码拿去授权必然失败，买家等不到开通就会投诉。

本模块提供：

- 格式判定：转调 :mod:`Agent.CustomerAgent.machine_code`（单一事实来源）
- 入站拦截话术：买家发来设备编号时直接给这段话，不让模型自由发挥
- 出站兜底话术：模型仍然把编号说成机器码时用它替换

口径：**只收机器码，不收设备编号。** 设备编号不是无效信息——它说明买家
已经在 VIP管理 里找到了那个界面，只是点错了按钮，因此话术要指路而不是
否定买家。
"""

from typing import List, Optional

from Agent.CustomerAgent.machine_code import (
    detect_machine_code_violation,
    is_device_id_only,
    is_machine_code,
)
from utils.logger_loguru import get_logger

logger = get_logger("MachineCodeGuide")

# 买家发来设备编号时的固定回复。
# 覆盖手机与电脑两条获取路径，与知识库《手机端获取机器码》《电脑端获取机器码》
# 的操作步骤一致；不含链接，避免编造。
DEVICE_ID_GUIDE = (
    "这个是设备编号哦，不是机器码，我们授权只认机器码的～\n"
    "麻烦重新复制一下机器码发我：\n"
    "手机：更多→VIP管理→离线授权→点排在上面的复制\n"
    "电脑：菜单栏最右边 帮助→VIP管理→复制机器码"
)

# 模型重生成时的约束提示：把「为什么错」和「该怎么说」讲清楚。
# {codes} 会被替换成命中的编号列表。
REGENERATE_CONSTRAINT = (
    "[系统约束] 买家发来的 {codes} 是【设备编号】，不是机器码。"
    "奥维的机器码是 8位十六进制-8位十六进制 的形式(如 50378B2F - D563EA05)。"
    "禁止把它说成机器码，禁止说「已记下」「已收到机器码」「安排授权」。"
    "请回复：这是设备编号不是机器码，请按教程重新复制机器码发我。"
)


def guide_for_device_id() -> str:
    """设备编号拦截话术。"""
    return DEVICE_ID_GUIDE


def is_pure_device_id_message(text: Optional[str]) -> bool:
    """买家整条消息是否只有一个设备编号。

    只在「消息整体就是那个数字」时成立：买家若同时说了别的事，
    不能整条拦掉，交由模型按提示词处理。
    """
    return is_device_id_only(text or "")


def reply_mentions_accepted_code(reply: Optional[str]) -> List[str]:
    """回复里是否把设备编号当机器码收下了。

    Returns:
        命中的编号列表；空列表表示没有违规。
    """
    return detect_machine_code_violation(reply or "")


__all__ = [
    "DEVICE_ID_GUIDE",
    "REGENERATE_CONSTRAINT",
    "guide_for_device_id",
    "is_pure_device_id_message",
    "reply_mentions_accepted_code",
    "is_machine_code",
]

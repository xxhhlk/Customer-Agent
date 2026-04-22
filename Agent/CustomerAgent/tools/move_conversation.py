from agno.run import RunContext
from Channel.pinduoduo.utils.API.send_message import SendMessage
from agno.tools import tool
from utils.logger_loguru import get_logger

logger = get_logger("TransferConversationTool")

@tool(name="transfer_conversation", description="将当前会话转接给人工客服。")
def transfer_conversation(shop_id: str, user_id: str, recipient_uid: str) -> str:
    """
    将当前会话转接给人工客服。
    """
    try:

        if not all([shop_id, user_id, recipient_uid]):
            return f"转接失败：缺少必要的会话信息 (shop_id={shop_id}, user_id={user_id}, recipient_uid={recipient_uid})"

        sender = SendMessage(shop_id, user_id)
        cs_list = sender.getAssignCsList()
        my_cs_uid = f"cs_{shop_id}_{user_id}"
        if cs_list and isinstance(cs_list, dict):
            # 过滤掉自己，不转接给自己
            available_cs_uids = [uid for uid in cs_list.keys() if uid != my_cs_uid]

            if available_cs_uids:
                # 选择第一个可用的客服
                cs_uid = available_cs_uids[0]
                # 转移会话
                transfer_result = sender.move_conversation(recipient_uid, cs_uid)

                if transfer_result and transfer_result.get('success'):
                    return f"转接成功，已转接给客服 {cs_uid}"
                else:
                    return f"转接失败：转移操作未成功"
            else:
                return "转接失败：没有可用的人工客服"

        return "转接失败：无法获取客服列表"

    except Exception as e:
        return f"转接过程中发生错误: {str(e)}"

"""测试系统消息识别修复"""
import json
from Channel.pinduoduo.pdd_message import PDDChatMessage

# 测试用例1: type=31 的系统提示消息（不应该被识别为客服回复）
system_message = {
    "message": {
        "content": "为保障您的购物权益和资金安全，请联系商家客服使用小额打款或小额收款。请勿在其他平台与商家进行资金往来！",
        "from": {
            "csid": "技术图源服务",
            "mall_id": "277169009",
            "role": "mall_cs",
            "uid": "277169009"
        },
        "msg_id": "1777863417019",
        "to": {
            "role": "user",
            "uid": "2924390443483"
        },
        "ts": "1777863417",
        "type": 31,  # 系统提示消息
    },
    "response": "push"
}

# 测试用例2: type=0 的真实客服回复（应该被识别为客服回复）
real_staff_message = {
    "message": {
        "content": "一般推荐高清就可以",
        "from": {
            "csid": "技术图源服务",
            "mall_id": "277169009",
            "role": "mall_cs",
            "uid": "277169009"
        },
        "msg_id": "1777872464125",
        "to": {
            "role": "user",
            "uid": "2924390443483"
        },
        "ts": "1777872464",
        "type": 0,  # 文本消息
        "manual_reply": 1  # 人工回复标记
    },
    "response": "push"
}

# 测试用例3: 平台机器人回复（也应该被识别为客服回复）
robot_message = {
    "message": {
        "content": "您好，请问有什么可以帮您？",
        "from": {
            "csid": "技术图源服务",
            "mall_id": "277169009",
            "role": "mall_cs",
            "uid": "277169009"
        },
        "msg_id": "1777872464126",
        "to": {
            "role": "user",
            "uid": "2924390443483"
        },
        "ts": "1777872465",
        "type": 0,  # 文本消息
        "manual_reply": 0  # 或者没有这个字段，表示机器人回复
    },
    "response": "push"
}

# 测试用例4: 其他类型的客服消息（如图片，也应该被识别为客服回复）
image_staff_message = {
    "message": {
        "content": "https://example.com/image.jpg",
        "from": {
            "csid": "技术图源服务",
            "mall_id": "277169009",
            "role": "mall_cs",
            "uid": "277169009"
        },
        "msg_id": "1777872464127",
        "to": {
            "role": "user",
            "uid": "2924390443483"
        },
        "ts": "1777872466",
        "type": 1,  # 图片消息
    },
    "response": "push"
}

print("=" * 60)
print("测试用例1: type=31 的系统提示消息")
print("=" * 60)
msg1 = PDDChatMessage(system_message)
print(f"消息类型: {msg1.user_msg_type}")
print(f"消息内容: {msg1.content}")
print(f"预期: SYSTEM_HINT")
print(f"结果: {'✓ 通过' if msg1.user_msg_type.value == 'system_hint' else '✗ 失败'}")
print()

print("=" * 60)
print("测试用例2: type=0 的真实客服回复")
print("=" * 60)
msg2 = PDDChatMessage(real_staff_message)
print(f"消息类型: {msg2.user_msg_type}")
print(f"消息内容: {msg2.content}")
print(f"预期: MALL_CS")
print(f"结果: {'✓ 通过' if msg2.user_msg_type.value == 'mall_cs' else '✗ 失败'}")
print()

print("=" * 60)
print("测试用例3: 平台机器人回复")
print("=" * 60)
msg3 = PDDChatMessage(robot_message)
print(f"消息类型: {msg3.user_msg_type}")
print(f"消息内容: {msg3.content}")
print(f"预期: MALL_CS")
print(f"结果: {'✓ 通过' if msg3.user_msg_type.value == 'mall_cs' else '✗ 失败'}")
print()

print("=" * 60)
print("测试用例4: 其他类型的客服消息（图片）")
print("=" * 60)
msg4 = PDDChatMessage(image_staff_message)
print(f"消息类型: {msg4.user_msg_type}")
print(f"消息内容: {msg4.content}")
print(f"预期: MALL_CS")
print(f"结果: {'✓ 通过' if msg4.user_msg_type.value == 'mall_cs' else '✗ 失败'}")
print()

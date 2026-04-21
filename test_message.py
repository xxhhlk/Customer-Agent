#!/usr/bin/env python3
"""
直接调用AI Bot测试脚本
"""
import sys
import json
from bridge.context import Context, ContextType, ChannelType
from Agent.bot_factory import create_bot

def main():
    """主函数"""
    # 获取消息
    if len(sys.argv) > 1:
        text = sys.argv[1]
    else:
        text = input("请输入消息: ").strip()
        if not text:
            text = "你好"
    
    print(f"消息: {text}")
    print("正在调用AI...")
    
    # 创建Bot
    bot = create_bot()
    
    # 构造消息内容（JSON格式）
    content = json.dumps([{"type": "text", "text": text}], ensure_ascii=False)
    
    # 创建Context，添加必要的参数
    context = Context(
        type=ContextType.TEXT,
        content=content,
        channel_type=ChannelType.PINDUODUO,
        kwargs={
            'shop_id': 'test_shop_1',
            'user_id': 'test_user_1',
            'from_uid': 'test_from_1',
            'username': '测试店铺',
            'nickname': '测试用户'
        }
    )
    
    # 调用Bot
    reply = bot.reply(context)
    
    # 输出回复
    if reply:
        print(f"\nAI回复: {reply.content}")
    else:
        print("\n未获取到回复")

if __name__ == "__main__":
    main()

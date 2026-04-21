#!/usr/bin/env python3
"""
直接调用AI Bot测试脚本
"""
import sys
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
    
    # 创建Context
    context = Context(
        type=ContextType.TEXT,
        content=text,
        channel_type=ChannelType.PINDUODUO,
        kwargs={}
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

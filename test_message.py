#!/usr/bin/env python3
"""
手动模拟消息测试脚本
用于测试AI自动回复功能
"""
import asyncio
import sys
import argparse
from bridge.context import Context, ContextType, ChannelType
from Message.message_queue import message_queue_manager
from Message.message_consumer import message_consumer_manager
from Message.message_handler import handler_chain
from utils.logger import get_logger

logger = get_logger("TestMessage")

async def send_test_message(text="你好，请问这个商品怎么卖？", 
                           shop_id="test_shop_1",
                           user_id="test_user_1",
                           from_uid="test_from_1",
                           username="测试店铺",
                           nickname="测试用户",
                           msg_type=ContextType.TEXT):
    """
    发送测试消息
    
    Args:
        text: 消息内容
        shop_id: 店铺ID
        user_id: 用户ID
        from_uid: 发送者UID
        username: 店铺名称
        nickname: 用户昵称
        msg_type: 消息类型
    """
    # 创建Context对象
    context = Context(
        type=msg_type,
        content=text,
        channel_type=ChannelType.PINDUODUO,
        kwargs={
            'shop_id': shop_id,
            'user_id': user_id,
            'from_uid': from_uid,
            'username': username,
            'nickname': nickname
        }
    )
    
    # 获取或创建队列
    queue = message_queue_manager.get_or_create_queue("pinduoduo")
    
    # 放入消息
    message_id = await queue.put(context)
    logger.info(f"测试消息已发送, ID: {message_id}")
    logger.info(f"消息类型: {msg_type.name}, 内容: {text}")
    return message_id

async def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='拼多多智能客服 - 测试消息发送工具')
    parser.add_argument('message', nargs='?', help='测试消息内容')
    parser.add_argument('--no-ai', action='store_true', help='禁用AI回复（仅测试关键词）')
    parser.add_argument('--shop-id', default='test_shop_1', help='店铺ID')
    parser.add_argument('--user-id', default='test_user_1', help='用户ID')
    parser.add_argument('--from-uid', default='test_from_1', help='发送者UID')
    parser.add_argument('--username', default='测试店铺', help='店铺名称')
    parser.add_argument('--nickname', default='测试用户', help='用户昵称')
    parser.add_argument('--wait-time', type=int, default=30, help='等待处理时间（秒）')
    
    args = parser.parse_args()
    
    print("=" * 50)
    print("拼多多智能客服 - 测试消息发送工具")
    print("=" * 50)
    print(f"AI回复: {'禁用' if args.no_ai else '启用'}")
    print(f"等待时间: {args.wait_time}秒")
    print()
    
    # 初始化处理器链
    handlers = handler_chain(use_ai=not args.no_ai)
    
    # 创建并启动消费者
    consumer = message_consumer_manager.create_consumer("pinduoduo")
    for handler in handlers:
        consumer.add_handler(handler)
    
    # 启动消费者
    consumer_task = asyncio.create_task(consumer.start())
    await asyncio.sleep(0.5)  # 等待消费者启动
    
    # 获取测试消息
    if args.message:
        test_text = args.message
    else:
        test_text = input("请输入测试消息内容（默认为'你好，请问这个商品怎么卖？'）: ").strip()
        if not test_text:
            test_text = "你好，请问这个商品怎么卖？"
    
    # 发送测试消息
    await send_test_message(
        test_text,
        shop_id=args.shop_id,
        user_id=args.user_id,
        from_uid=args.from_uid,
        username=args.username,
        nickname=args.nickname
    )
    
    # 保持运行一段时间
    print(f"\n消息已发送，等待处理 {args.wait_time} 秒...（按Ctrl+C退出）")
    try:
        await asyncio.sleep(args.wait_time)
    except KeyboardInterrupt:
        print("\n正在停止...")
    
    # 停止消费者
    await consumer.stop()
    if not consumer_task.done():
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass

if __name__ == "__main__":
    asyncio.run(main())

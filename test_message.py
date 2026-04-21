#!/usr/bin/env python3
"""
手动模拟消息测试脚本 - 简化版
仅测试AI对话回复
"""
import asyncio
import sys
from bridge.context import Context, ContextType, ChannelType
from Message.message_queue import message_queue_manager
from Message.message_consumer import message_consumer_manager
from Message.message_handler import handler_chain
from utils.logger import get_logger

logger = get_logger("TestMessage")

async def send_test_message(text):
    """发送测试消息"""
    # 创建Context对象
    context = Context(
        type=ContextType.TEXT,
        content=text,
        channel_type=ChannelType.PINDUODUO,
        kwargs={
            'shop_id': 'test_shop_1',
            'user_id': 'test_user_1',
            'from_uid': 'test_from_1',
            'username': '测试店铺',
            'nickname': '测试用户'
        }
    )
    
    # 获取或创建队列并放入消息
    queue = message_queue_manager.get_or_create_queue("pinduoduo")
    message_id = await queue.put(context)
    logger.info(f"消息已发送: {text}")
    return message_id

async def main():
    """主函数"""
    print("=" * 50)
    print("拼多多智能客服 - AI对话测试")
    print("=" * 50)
    
    # 初始化处理器链（启用AI）
    handlers = handler_chain(use_ai=True)
    
    # 创建并启动消费者
    consumer = message_consumer_manager.create_consumer("pinduoduo")
    for handler in handlers:
        consumer.add_handler(handler)
    
    # 启动消费者
    consumer_task = asyncio.create_task(consumer.start())
    await asyncio.sleep(0.5)
    
    # 获取测试消息
    if len(sys.argv) > 1:
        test_text = sys.argv[1]
    else:
        test_text = input("请输入测试消息: ").strip()
        if not test_text:
            test_text = "你好"
    
    # 发送测试消息
    await send_test_message(test_text)
    
    # 等待处理
    print("\n等待AI回复...（按Ctrl+C退出）")
    try:
        await asyncio.sleep(60)
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

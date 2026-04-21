"""
Message模块核心功能
包含简化的消息队列、消费者和处理器基类
"""

from .queue import SimpleMessageQueue, QueueManager
from .consumer import MessageConsumer
from .enhanced_consumer import EnhancedMessageConsumer, enhanced_message_consumer_manager
from .handlers import MessageHandler, TypeBasedHandler, ChannelBasedHandler

__all__ = [
    'SimpleMessageQueue',
    'QueueManager',
    'MessageConsumer',
    'EnhancedMessageConsumer',
    'enhanced_message_consumer_manager',
    'MessageHandler',
    'TypeBasedHandler',
    'ChannelBasedHandler'
]
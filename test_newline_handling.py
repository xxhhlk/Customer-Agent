#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试换行符在知识库和AI回复流程中的处理
"""

import json
import sys
import os

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from Agent.CustomerAgent.agent_knowledge import KnowledgeManager
from Agent.CustomerAgent.agent import CustomerAgent
from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
import asyncio
import tempfile
import shutil


def test_newline_preservation():
    """测试换行符是否在各个处理环节中得到保留"""
    
    # 1. 测试内容（包含换行符）
    test_content = """这是第一行内容。
这是第二行内容，包含一些特殊符号：!@#$%^&*()
这是第三行内容，包含中文标点符号：，。！？；：

接下来是一些列表项：
- 第一项
- 第二项
- 第三项

最后一行内容。"""
    
    print("原始内容:")
    print(repr(test_content))
    print("\n原始内容显示:")
    print(test_content)
    
    # 2. 测试JSON序列化/反序列化
    print("\n=== 测试JSON处理 ===")
    doc_data = {
        "content": test_content,
        "title": "测试文档",
        "metadata": {"source": "test"}
    }
    
    # 序列化
    json_str = json.dumps(doc_data, ensure_ascii=False, indent=2)
    print("JSON序列化后:")
    print(json_str)
    
    # 反序列化
    restored_data = json.loads(json_str)
    restored_content = restored_data["content"]
    print("\n反序列化后内容:")
    print(repr(restored_content))
    
    # 比较
    if test_content == restored_content:
        print("✓ JSON处理保留了换行符")
    else:
        print("✗ JSON处理丢失了换行符")
        print("差异:")
        for i, (orig, rest) in enumerate(zip(test_content, restored_content)):
            if orig != rest:
                print(f"  位置{i}: 原始='{repr(orig)}' 恢复='{repr(rest)}'")
    
    # 3. 测试HTML转义
    print("\n=== 测试HTML转义 ===")
    escaped_content = test_content.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    print("转义后内容:")
    print(repr(escaped_content))
    
    # 使用<pre>标签包装
    html_content = f"<pre>{escaped_content}</pre>"
    print("HTML包装后:")
    print(html_content)
    
    # 4. 测试拼多多消息发送格式
    print("\n=== 测试拼多多消息格式 ===")
    message_data = {
        "data": {
            "cmd": "send_message",
            "request_id": "test_12345",
            "message": {
                "to": {
                    "role": "user",
                    "uid": "test_user"
                },
                "from": {
                    "role": "mall_cs"
                },
                "content": test_content,
                "msg_id": None,
                "type": 0,
                "is_aut": 0,
                "manual_reply": 1,
            },
        },
        "client": "WEB"
    }
    
    message_json = json.dumps(message_data, ensure_ascii=False)
    print("消息JSON:")
    print(message_json)
    
    # 检查内容是否保留
    restored_message = json.loads(message_json)
    message_content = restored_message["data"]["message"]["content"]
    
    if test_content == message_content:
        print("✓ 拼多多消息格式保留了换行符")
    else:
        print("✗ 拼多多消息格式丢失了换行符")


async def test_knowledge_base_newlines():
    """测试知识库中的换行符处理"""
    print("\n=== 测试知识库换行符处理 ===")
    
    # 创建临时目录用于测试
    temp_dir = tempfile.mkdtemp(prefix="knowledge_test_")
    print(f"使用临时目录: {temp_dir}")
    
    try:
        # 创建测试内容
        test_title = "换行符测试文档"
        test_content = """这是第一行。
这是第二行。

这是第四行，前面有一空行。

列表项：
- 项目一
- 项目二
- 项目三

结尾行。"""
        
        print("测试内容:")
        print(repr(test_content))
        
        # 这里可以添加更多关于知识库处理的测试
        print("知识库测试占位符 - 需要实际的知识库实例来进行完整测试")
        
    finally:
        # 清理临时目录
        shutil.rmtree(temp_dir)
        print(f"已清理临时目录: {temp_dir}")


if __name__ == "__main__":
    print("开始测试换行符处理...")
    test_newline_preservation()
    asyncio.run(test_knowledge_base_newlines())
    print("\n测试完成。")
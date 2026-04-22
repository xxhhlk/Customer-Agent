"""
测试关键词匹配功能
"""

import sys
import os

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Message.handlers.keyword_matcher import matcher_factory
from database.db_manager import db_manager


def test_matchers():
    """测试各种匹配器"""
    print("=" * 60)
    print("测试关键词匹配器功能")
    print("=" * 60)
    
    # 测试用例
    test_cases = [
        # (匹配类型, 关键词, 消息, 预期结果)
        ("partial", "人工", "我想转人工客服", True),
        ("partial", "人工", "我想找客服", False),
        ("exact", "好评", "好评！", True),
        ("exact", "好评", "好评好评", True),
        ("exact", "好评", "给个好评吧", False),
        ("regex", r"退款|退货", "我要退款", True),
        ("regex", r"退款|退货", "我要退货", True),
        ("regex", r"退款|退货", "我要换货", False),
        ("wildcard", "转*客服", "转人工客服", True),
        ("wildcard", "转*客服", "转售后客服", True),
        ("wildcard", "转*客服", "我要转人工", False),
    ]
    
    passed = 0
    failed = 0
    
    for match_type, keyword, message, expected in test_cases:
        matcher = matcher_factory.get_matcher(match_type)
        result = matcher.match(keyword, message)
        status = "[PASS]" if result == expected else "[FAIL]"
        
        if result == expected:
            passed += 1
        else:
            failed += 1
        
        print(f"{status} [{match_type:8}] keyword: '{keyword:15}' message: '{message:20}' "
              f"expected: {expected} actual: {result}")
    
    print("=" * 60)
    print(f"测试结果: 通过 {passed}/{len(test_cases)}, 失败 {failed}/{len(test_cases)}")
    print("=" * 60)
    
    return failed == 0


def test_database_keywords():
    """测试数据库关键词加载"""
    print("\n" + "=" * 60)
    print("测试数据库关键词加载")
    print("=" * 60)
    
    keywords = db_manager.get_all_keywords()
    print(f"从数据库加载了 {len(keywords)} 个关键词")
    
    if keywords:
        print("\n前5个关键词:")
        for i, kw in enumerate(keywords[:5], 1):
            print(f"{i}. 关键词: {kw['keyword']:15} "
                  f"分组: {kw.get('group_name', 'default'):10} "
                  f"匹配类型: {kw.get('match_type', 'partial'):10} "
                  f"优先级: {kw.get('priority', 0)}")
    
    return len(keywords) > 0


def test_keyword_handler():
    """测试关键词处理器"""
    print("\n" + "=" * 60)
    print("测试关键词处理器")
    print("=" * 60)
    
    from Message.handlers.keyword_handler import KeywordDetectionHandler
    
    handler = KeywordDetectionHandler()
    print(f"处理器初始化完成，加载了 {handler.get_keyword_count()} 个关键词")
    
    # 测试匹配
    test_messages = [
        "我想转人工",
        "给个好评",
        "我要退款",
        "你好",
    ]
    
    print("\n测试消息匹配:")
    for msg in test_messages:
        matched = handler.match_keyword(msg)
        if matched:
            print(f"[MATCH] message: '{msg:20}' keyword: '{matched['keyword']}' "
                  f"(type: {matched['match_type']}, group: {matched['group_name']})")
        else:
            print(f"[NO MATCH] message: '{msg:20}' no keyword matched")
    
    return True


if __name__ == "__main__":
    print("\n开始测试关键词匹配功能...\n")
    
    # 运行测试
    test1 = test_matchers()
    test2 = test_database_keywords()
    test3 = test_keyword_handler()
    
    print("\n" + "=" * 60)
    if test1 and test2 and test3:
        print("[SUCCESS] All tests passed!")
        print("=" * 60)
        sys.exit(0)
    else:
        print("[FAILED] Some tests failed!")
        print("=" * 60)
        sys.exit(1)

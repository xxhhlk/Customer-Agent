# -*- coding: utf-8 -*-
"""Simple test for keyword matcher"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Message.handlers.keyword_matcher import matcher_factory

# Test basic matchers
print("Testing keyword matchers...")

matcher = matcher_factory.get_matcher('partial')
result = matcher.match('人工', '我想转人工客服')
print(f"Partial match test: {result}")

matcher = matcher_factory.get_matcher('exact')
result = matcher.match('好评', '好评！')
print(f"Exact match test: {result}")

print("Basic tests completed!")

# 关键词匹配器模块 - 支持多种匹配类型
# 基于上游新架构重实现

import re
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from utils.logger_loguru import get_logger


class KeywordMatcher(ABC):
    """关键词匹配器基类"""

    @abstractmethod
    def match(self, keyword: str, message: str) -> bool:
        """检查关键词是否匹配消息

        Args:
            keyword: 关键词模式
            message: 用户消息

        Returns:
            bool: 是否匹配
        """
        pass

    @abstractmethod
    def get_match_type(self) -> str:
        """返回匹配类型标识"""
        pass


class ExactMatcher(KeywordMatcher):
    """完全匹配器 - 忽略标点符号、空格、大小写"""

    # 用于清理消息的正则：只保留文字、数字和中文
    _CLEAN_RE = re.compile(r'[^\w\u4e00-\u9fff]')

    def match(self, keyword: str, message: str) -> bool:
        # 清理关键词和消息：去掉所有符号，转小写
        clean_kw = self._CLEAN_RE.sub('', keyword).lower()
        clean_msg = self._CLEAN_RE.sub('', message).lower()
        return clean_msg == clean_kw

    def get_match_type(self) -> str:
        return 'exact'


class PartialMatcher(KeywordMatcher):
    """部分匹配器 - 关键词是消息的子串（现有行为）"""

    def match(self, keyword: str, message: str) -> bool:
        return keyword.lower() in message.lower()

    def get_match_type(self) -> str:
        return 'partial'


class RegexMatcher(KeywordMatcher):
    """正则表达式匹配器"""

    def __init__(self):
        self._compiled_cache: Dict[str, re.Pattern] = {}
        self.logger = get_logger(__name__)

    def match(self, keyword: str, message: str) -> bool:
        try:
            # 缓存编译后的正则表达式
            if keyword not in self._compiled_cache:
                self._compiled_cache[keyword] = re.compile(keyword, re.IGNORECASE)
            pattern = self._compiled_cache[keyword]
            return bool(pattern.search(message))
        except re.error as e:
            self.logger.warning(f"正则表达式 '{keyword}' 编译失败: {e}")
            return False

    def get_match_type(self) -> str:
        return 'regex'


class WildcardMatcher(KeywordMatcher):
    """通配符匹配器 - *匹配任意字符, ?匹配单个字符"""

    def __init__(self):
        self._compiled_cache: Dict[str, re.Pattern] = {}
        self.logger = get_logger(__name__)

    @staticmethod
    def _wildcard_to_regex(pattern: str) -> str:
        """将通配符模式转换为正则表达式"""
        # 先转义正则特殊字符，然后替换通配符
        regex = re.escape(pattern)
        regex = regex.replace(r'\*', '.*')  # * -> 任意字符序列
        regex = regex.replace(r'\?', '.')    # ? -> 单个字符
        return f'^{regex}$'

    def match(self, keyword: str, message: str) -> bool:
        try:
            if keyword not in self._compiled_cache:
                regex_pattern = self._wildcard_to_regex(keyword)
                self._compiled_cache[keyword] = re.compile(regex_pattern, re.IGNORECASE)
            pattern = self._compiled_cache[keyword]
            return bool(pattern.search(message))
        except re.error as e:
            self.logger.warning(f"通配符模式 '{keyword}' 转换失败: {e}")
            return False

    def get_match_type(self) -> str:
        return 'wildcard'


class MatcherFactory:
    """匹配器工厂 - 单例模式"""

    _instance = None
    _matchers: Dict[str, KeywordMatcher] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._matchers = {
            'exact': ExactMatcher(),
            'partial': PartialMatcher(),
            'regex': RegexMatcher(),
            'wildcard': WildcardMatcher()
        }
        self._initialized = True

    def get_matcher(self, match_type: str) -> KeywordMatcher:
        """获取对应类型的匹配器

        Args:
            match_type: 匹配类型 (exact/partial/regex/wildcard)

        Returns:
            KeywordMatcher: 匹配器实例，默认为PartialMatcher
        """
        return self._matchers.get(match_type, self._matchers['partial'])


# 全局工厂实例
matcher_factory = MatcherFactory()

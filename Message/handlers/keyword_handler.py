"""
关键词检测处理器 - 检测转人工关键词并触发转人工流程
"""
import re
from typing import Dict, Any, Optional, List
from bridge.context import Context, ContextType
from .base import BaseHandler
from .keyword_matcher import matcher_factory
from database.db_manager import db_manager
from utils.logger_loguru import get_logger
from Channel.pinduoduo.utils.API.send_message import SendMessage

class KeywordDetectionHandler(BaseHandler):
    """关键词检测处理器 - 检测转人工关键词并触发转人工流程"""

    def __init__(self):
        super().__init__("KeywordDetectionHandler")
        self.logger = get_logger("KeywordDetectionHandler")
        self.keywords = self._load_keywords()
        self.matcher_factory = matcher_factory

        # 记录加载的关键词数量
        self.logger.info(f"关键词检测处理器初始化完成，加载了 {len(self.keywords)} 个关键词")

    def _load_keywords(self) -> List[dict]:
        """从数据库加载关键词，返回带完整信息的列表"""
        try:
            keywords_data = db_manager.get_all_keywords()
            # 按优先级排序（已在数据库查询中排序）
            self.logger.debug(f"从数据库加载关键词: {len(keywords_data)} 个")
            return keywords_data
        except Exception as e:
            self.logger.error(f"加载关键词失败: {e}")
            return []

    def can_handle(self, context: Context) -> bool:
        """检查消息是否包含关键词"""
        # 只处理文本类型的消息
        if context.type != ContextType.TEXT:
            return False

        # 检查消息内容是否存在且为字符串
        if not context.content or not isinstance(context.content, str):
            return False

        # 检查是否包含任何关键词（优先级已在数据库查询时排序）
        for kw in self.keywords:
            keyword = kw.get('keyword', '')
            match_type = kw.get('match_type', 'partial')
            
            if not keyword:
                continue
                
            # 使用匹配器工厂获取对应的匹配器
            matcher = self.matcher_factory.get_matcher(match_type)
            if matcher.match(keyword, context.content):
                self.logger.debug(f"检测到关键词: '{keyword}' (匹配类型: {match_type}) 在消息: '{context.content}'")
                return True

        return False
    
    def match_keyword(self, message: str) -> Optional[dict]:
        """匹配消息中的关键词，返回匹配结果
        
        Args:
            message: 用户消息
            
        Returns:
            匹配的关键词信息，包含:
            - keyword: 关键词
            - group_name: 分组名称
            - match_type: 匹配类型
            - reply_content: 回复内容
            - transfer_to_human: 是否转人工
            - priority: 优先级
            如果没有匹配则返回 None
        """
        if not message:
            return None
            
        # 按优先级匹配（数据库查询时已排序）
        for kw in self.keywords:
            keyword = kw.get('keyword', '')
            match_type = kw.get('match_type', 'partial')
            
            if not keyword:
                continue
                
            # 使用匹配器工厂获取对应的匹配器
            matcher = self.matcher_factory.get_matcher(match_type)
            if matcher.match(keyword, message):
                return {
                    'keyword': kw.get('keyword'),
                    'group_name': kw.get('group_name', 'default'),
                    'match_type': kw.get('match_type', 'partial'),
                    'reply_content': kw.get('reply_content'),
                    'transfer_to_human': kw.get('transfer_to_human', False),
                    'pass_to_ai': kw.get('pass_to_ai', False),
                    'priority': kw.get('priority', 0)
                }
        
        return None

    async def handle(self, context: Context, metadata: Dict[str, Any]) -> bool:
        """处理关键词匹配结果"""
        try:
            shop_id = context.kwargs.shop_id
            user_id = context.kwargs.user_id
            from_uid = context.kwargs.from_uid
            
            self.logger.debug(f"关键词处理器收到参数: shop_id={shop_id}, user_id={user_id}, from_uid={from_uid}")
            
            # 检查必要参数（空字符串也视为无效）
            if not shop_id or not user_id or not from_uid:
                self.logger.warning(f"关键词处理器缺少必要参数: shop_id={shop_id}, user_id={user_id}, from_uid={from_uid}")
                return False
            
            # 匹配关键词
            matched = self.match_keyword(context.content or "")
            if not matched:
                self.logger.debug(f"未匹配到关键词: {context.content}")
                return False
            
            self.logger.info(f"匹配到关键词: {matched}")
            
            # 如果需要转人工
            if matched.get('transfer_to_human', False):
                self.logger.info(f"转人工: {matched.get('keyword')}")
                return await self._transfer_to_human(shop_id, user_id, from_uid)
            
            # 如果有回复内容，发送回复
            reply_content = matched.get('reply_content')
            if reply_content:
                self.logger.info(f"发送关键词回复: {reply_content}")
                sender = SendMessage(shop_id, user_id)
                sender.send_text(from_uid, reply_content)
                self.logger.info(f"已发送关键词回复: {reply_content}")
                
                # 如果 pass_to_ai 为 True，发送回复后继续传递给AI
                if matched.get('pass_to_ai', False):
                    self.logger.info(f"关键词 pass_to_ai=True，检查是否有有意义的内容传递给AI")
                    
                    # 从消息中移除关键词，用于判断是否有有意义的内容
                    original_content = context.content or ""
                    cleaned_content = self._remove_keyword_from_message(
                        original_content,
                        matched.get('keyword', ''),
                        matched.get('match_type', 'partial')
                    )
                    
                    # 检查移除后是否还有有意义的内容
                    if self._has_meaningful_content(cleaned_content):
                        self.logger.info(f"移除关键词后仍有有意义的内容，传递给AI（原始消息）: '{original_content}'")
                        # 不修改 context.content，传递原始完整消息给AI
                        return False  # 继续传递给AI
                    else:
                        self.logger.info(f"移除关键词后无有意义的内容，不传递给AI")
                        return True  # 终止处理，不传递给AI
                
                return True
            
            self.logger.warning(f"关键词匹配成功但没有回复内容: {matched.get('keyword')}")
            return False
            
        except Exception as e:
            self.logger.error(f"关键词处理失败: {e}")
            return False
    
    def _remove_keyword_from_message(self, message: str, keyword: str, match_type: str) -> str:
        """从消息中移除关键词
        
        Args:
            message: 原始消息
            keyword: 匹配的关键词
            match_type: 匹配类型
            
        Returns:
            移除关键词后的消息
        """
        if not message or not keyword:
            return message
        
        try:
            if match_type == 'exact':
                # 完全匹配：直接替换整个消息
                return ""
            elif match_type == 'partial':
                # 部分匹配：替换消息中的关键词部分
                result = message.replace(keyword, "")
                return result.strip()
            elif match_type == 'regex':
                # 正则匹配：使用正则替换
                result = re.sub(keyword, "", message, flags=re.IGNORECASE)
                return result.strip()
            elif match_type == 'wildcard':
                # 通配符匹配：将通配符转换为正则，然后替换
                pattern = self._wildcard_to_regex(keyword)
                result = re.sub(pattern, "", message, flags=re.IGNORECASE)
                return result.strip()
            else:
                # 默认：部分匹配
                result = message.replace(keyword, "")
                return result.strip()
        except Exception as e:
            self.logger.warning(f"移除关键词失败: {e}，返回原始消息")
            return message
    
    def _wildcard_to_regex(self, pattern: str) -> str:
        """将通配符模式转换为正则表达式
        
        Args:
            pattern: 通配符模式（* 匹配任意字符，? 匹配单个字符）
            
        Returns:
            正则表达式字符串
        """
        # 转义正则特殊字符
        result = re.escape(pattern)
        # 将转义后的通配符转换回正则
        result = result.replace(r'\*', '.*').replace(r'\?', '.')
        return f'^{result}$'
    
    def _has_meaningful_content(self, content: str) -> bool:
        """检查内容是否有意义（非空白、非纯标点等）
        
        Args:
            content: 待检查的内容
            
        Returns:
            是否有意义
        """
        if not content:
            return False
        
        # 去除空白字符
        stripped = content.strip()
        if not stripped:
            return False
        
        # 检查是否全是标点符号或特殊字符
        # 中文标点：，。！？、；：""''（）【】《》
        # 英文标点：,.!?;:'"()[]{}
        punctuation_pattern = r'^[，。！？、；：""''（）【】《》\s,.!?;:\'"()\[\]{}]+$'
        if re.match(punctuation_pattern, stripped):
            return False
        
        # 至少包含一个字母、数字或中文字符
        meaningful_pattern = r'[a-zA-Z0-9\u4e00-\u9fa5]'
        if re.search(meaningful_pattern, stripped):
            return True
        
        return False
    
    async def _transfer_to_human(self, shop_id: str, user_id: str, from_uid: str) -> bool:
        """转接到人工客服"""
        try:
            # 获取可用的客服列表
            sender = SendMessage(shop_id, user_id)
            cs_list = sender.getAssignCsList()
            my_cs_uid = f"cs_{shop_id}_{user_id}"
            
            if cs_list and isinstance(cs_list, dict):
                # 过滤掉自己，不转接给自己
                available_cs_uids = [uid for uid in cs_list.keys() if uid != my_cs_uid]

                if available_cs_uids:
                    # 选择第一个可用的客服
                    cs_uid = available_cs_uids[0]
                    target_cs = cs_list[cs_uid]
                    cs_name = target_cs.get('username', '客服')
                    
                    # 转移会话
                    transfer_result = sender.move_conversation(from_uid, cs_uid)
                    
                    if transfer_result and transfer_result.get('success'):
                        self.logger.info(f"会话已成功转接给 {cs_name} ({cs_uid})")
                        return True
                    else:
                        self.logger.error("会话转接失败")
                else:
                    self.logger.warning("没有其他可用的客服进行转接")
                    sender.send_text(from_uid, "抱歉，当前没有其他客服在线，请您稍后再试。")
            
            return False
            
        except Exception as e:
            self.logger.error(f"客服转接处理失败: {e}")
            return False
            
    def reload_keywords(self) -> None:
        """重新加载关键词（用于管理员更新关键词后刷新）"""
        old_count = len(self.keywords)
        self.keywords = self._load_keywords()
        new_count = len(self.keywords)
        self.logger.info(f"关键词重新加载完成: {old_count} -> {new_count}")

    def get_keyword_count(self) -> int:
        """获取当前关键词数量"""
        return len(self.keywords)

    def get_keywords(self) -> List[dict]:
        """获取当前关键词列表"""
        return self.keywords.copy()
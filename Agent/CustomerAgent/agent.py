import asyncio

from agno import tools
from Agent.bot import Bot
from agno.agent import Agent, RunOutput

from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from agno.models.openai import OpenAILike
from agno.db.sqlite import SqliteDb
from Agent.CustomerAgent.agent_knowledge import KnowledgeManager
from Agent.CustomerAgent.tools.move_conversation import transfer_conversation
from Agent.CustomerAgent.tools.get_product_list import get_shop_products
from Agent.CustomerAgent.tools.send_goods_link import send_goods_link
from config import get_config
from typing import Any, Optional
from utils.logger_loguru import get_logger
from pydantic import BaseModel, Field
from typing import Dict


class CustomerAgent(Bot):
    knowledge_manager: KnowledgeManager

    def __init__(self, knowledge_manager: Optional['KnowledgeManager'] = None):
        super().__init__()
        # 从 DI 容器获取 KnowledgeManager（如果未传入）
        if knowledge_manager is None:
            from core.di_container import container
            try:
                knowledge_manager = container.get(KnowledgeManager)
            except ValueError:
                # 容器中未注册时直接创建
                knowledge_manager = KnowledgeManager()
        self.knowledge_manager = knowledge_manager  # pyright: ignore[reportAttributeAccessIssue]
        self._agent: Optional[Agent] = None  # 延迟初始化
        self.logger = get_logger("CustomerAgent")
        self._is_initialized = False

    async def initialize_async(self) -> bool:
        """初始化CustomerAgent"""
        if self._is_initialized:
            return True

        try:
            # 获取配置
            db_path = get_config("db_path", "./temp/agent.db")
            model_name = get_config("llm.model_name", "gpt-3.5-turbo")
            api_key = get_config("llm.api_key", "")
            api_base = get_config("llm.api_base", "")
            description = get_config("prompt.description", "")
            instructions = get_config("prompt.instructions", [])
            additional_context = get_config("prompt.additional_context", "")
            thinking_config = get_config("llm.thinking", None)

            # 验证必要配置
            if not api_key:
                raise ValueError("LLM API密钥未配置")

            # 构建 extra_body 参数（用于火山引擎 thinking 配置）
            extra_body = None
            if thinking_config:
                extra_body = {"thinking": thinking_config}

            # 创建Agent实例
            self._agent = Agent(
                db=SqliteDb(db_file=db_path),
                knowledge=self.knowledge_manager.knowledge,
                model=OpenAILike(
                    id=model_name,
                    api_key=api_key,
                    base_url=api_base,
                    temperature=0.7,
                    extra_body=extra_body,
                ),
                tools=[transfer_conversation, send_goods_link],
                search_knowledge= True,
                description=description,
                instructions=instructions,
                additional_context=additional_context,
                add_history_to_context=True,
                add_dependencies_to_context=True,
                add_datetime_to_context=True,
                timezone_identifier="Asia/Shanghai"
            )

            self.logger.info("CustomerAgent初始化成功")
            return True

        except Exception as e:
            self.logger.error(f"CustomerAgent初始化失败: {e}")
            return False

    async def async_reply(self, query: str, context: Optional[Context] = None) -> Reply:
        """异步回复接口 - 确保返回Reply对象"""
        if not self._agent:
            if not await self.initialize_async():
                return Reply(ReplyType.TEXT, "AI客服初始化失败")

        if context is None:
            return Reply(ReplyType.TEXT, "缺少上下文信息")

        try:
            assert self._agent is not None, "Agent未初始化"
            # 确保session_id是字符串
            session_id = f"{context.channel_type}{context.kwargs.user_id}"
            # 确保dependencies中的值是安全的类型
            dependencies = {
                "shop_name": str(context.kwargs.shop_name),
                "channel_type": str(context.channel_type.value if context.channel_type else ""),
                "shop_id": str(context.kwargs.shop_id),
                "user_id": str(context.kwargs.user_id),
                "from_uid": str(context.kwargs.from_uid),
            }
            
            response: RunOutput = await self._agent.arun(
                user_id=context.kwargs.user_id, 
                session_id=session_id, 
                input=query, 
                dependencies=dependencies
            )
            return Reply(ReplyType.TEXT, response.content)
        except Exception as e:
            self.logger.error(f"CustomerAgent异步回复失败: {e}")
            return Reply(ReplyType.TEXT, "抱歉，我现在无法回复，请稍后再试。")
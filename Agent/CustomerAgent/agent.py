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

# ---------------------------------------------------------------------------
# agno monkey-patch: 同步 DB 操作在 async 上下文中必须走 asyncio.to_thread()
#
# agno >= 2.3.4 将 _storage/_session/_init 模块合并到了 Agent 类的实例方法中，
# 但 _aread_or_create_session / asave_session 在同步 DB（SqliteDb）路径上
# 仍然直接同步调用 SQLAlchemy 操作，阻塞 asyncio 事件循环。当阻塞超过数秒时，
# Qt 主线程的事件处理也会被冻结，期间 paintEvent 读取不一致的主题状态
# 可能导致 QSvgRenderer access violation 崩溃。
#
# 解决方案：monkey-patch Agent 类的实例方法，让同步 DB 操作通过
# asyncio.to_thread() 在后台线程池执行，不再阻塞事件循环。
# ---------------------------------------------------------------------------

_agno_patched = False  # 全局标志，确保只 patch 一次


def _patch_agno_async_db():
    """Patch agno Agent 实例方法，让同步 DB 操作走 to_thread()"""
    global _agno_patched
    if _agno_patched:
        return
    _agno_patched = True

    from agno.agent.agent import Agent

    # 保存原始方法引用（用于反向引用内部方法）
    _original_aread_or_create = Agent._aread_or_create_session
    _original_asave_session = Agent.asave_session

    async def _patched_aread_or_create_session(
        self: Agent, session_id: str, user_id: Optional[str] = None
    ) -> "AgentSession":
        """_aread_or_create_session 的安全版本：同步 DB 读取走 to_thread()"""
        from time import time
        from uuid import uuid4
        from typing import cast
        from agno.db.types import AgentSession
        from agno.utils.log import log_debug
        from agno.run.output import RunOutput
        from agno.run.message import Message

        # 返回缓存 session
        if (
            self._cached_session is not None
            and self._cached_session.session_id == session_id
        ):
            return self._cached_session

        # 从数据库加载
        agent_session = None
        if self.db is not None and self.team_id is None and self.workflow_id is None:
            log_debug(f"Reading AgentSession: {session_id}")
            if self._has_async_db():
                agent_session = cast(AgentSession, await self._aread_session(session_id=session_id))
            else:
                # 关键修复：同步 DB 读取走 to_thread()
                agent_session = cast(AgentSession, await asyncio.to_thread(
                    self._read_session, session_id=session_id
                ))

        if agent_session is None:
            log_debug(f"Creating new AgentSession: {session_id}")
            session_data = {}
            if self.session_state is not None:
                from copy import deepcopy
                session_data["session_state"] = deepcopy(self.session_state)
            agent_session = AgentSession(
                session_id=session_id,
                agent_id=self.id,
                user_id=user_id,
                agent_data=self._get_agent_data(),
                session_data=session_data,
                metadata=self.metadata,
                created_at=int(time()),
            )
            if self.introduction is not None:
                agent_session.upsert_run(
                    RunOutput(
                        run_id=str(uuid4()),
                        session_id=session_id,
                        agent_id=self.id,
                        agent_name=self.name,
                        user_id=user_id,
                        content=self.introduction,
                        messages=[
                            Message(role=self.model.assistant_message_role, content=self.introduction)
                        ],
                    )
                )

        if self.cache_session:
            self._cached_session = agent_session

        return agent_session

    async def _patched_asave_session(
        self: Agent, session: "Union[AgentSession, TeamSession, WorkflowSession]"
    ) -> None:
        """asave_session 的安全版本：同步 DB 操作走 to_thread()"""
        from agno.utils.log import log_debug

        if (
            self.db is not None
            and self.team_id is None
            and self.workflow_id is None
            and session.session_data is not None
        ):
            if session.session_data is not None and isinstance(session.session_data.get("session_state"), dict):
                session.session_data["session_state"].pop("current_session_id", None)
                session.session_data["session_state"].pop("current_user_id", None)
                session.session_data["session_state"].pop("current_run_id", None)
            if self._has_async_db():
                await self._aupsert_session(session=session)
            else:
                # 关键修复：同步 upsert_session 走 to_thread()
                await asyncio.to_thread(self._upsert_session, session=session)
            log_debug(f"Created or updated AgentSession record: {session.session_id}")

    # 替换 Agent 类的实例方法
    Agent._aread_or_create_session = _patched_aread_or_create_session
    Agent.asave_session = _patched_asave_session


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

        # Patch agno 框架的 async DB 函数（幂等，全局只执行一次）
        _patch_agno_async_db()

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
                num_history_runs=8,
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

        # 限流检查 - 在处理AI请求之前检查用户是否超出限流阈值
        try:
            from_uid = context.kwargs.from_uid if hasattr(context, 'kwargs') else None
            if from_uid:
                # 获取限流器实例
                from Message.handlers.rate_limiter import coze_rate_limiter
                if coze_rate_limiter.is_rate_limited(from_uid):
                    self.logger.warning(f"用户 {from_uid} 已超出限流阈值，等待人工回复")

                    # 等待人工客服回复，与普通消息一致
                    import asyncio
                    from Message.handlers.staff_reply_event import staff_reply_event_manager
                    from config import get_config

                    staff_wait_config = get_config("staff_reply_wait", {})
                    enable_staff_wait = staff_wait_config.get("enable", True)
                    wait_seconds = staff_wait_config.get("wait_seconds", 30)

                    if enable_staff_wait and isinstance(from_uid, str):
                        event_id = staff_reply_event_manager.start_waiting(from_uid)
                        try:
                            staff_replied = await staff_reply_event_manager.wait_for_staff_reply(
                                from_uid, event_id, timeout=wait_seconds
                            )
                            if staff_replied:
                                self.logger.info(f"用户 {from_uid} 限流期间人工客服已回复，跳过兜底回复")
                                return Reply(ReplyType.TEXT, "")  # 返回空内容跳过后续处理
                        finally:
                            staff_reply_event_manager.stop_waiting(from_uid, event_id)

                    # 人工回复超时，发送兜底回复
                    rate_limit_config = get_config("rate_limit", {})
                    fallback_replies = rate_limit_config.get("fallback_reply", [])

                    if not fallback_replies:
                        fallback_replies = ["亲，感谢您的咨询！客服正在为您处理，请稍等片刻。"]

                    import random
                    reply_text = random.choice(fallback_replies)
                    return Reply(ReplyType.TEXT, reply_text)
        except Exception as e:
            self.logger.error(f"限流检查时出错: {e}")

        try:
            assert self._agent is not None, "Agent未初始化"

            # 查询人工客服消息上下文
            staff_context = ""
            if context and hasattr(context, 'kwargs'):
                from_uid = context.kwargs.from_uid
                if from_uid:
                    from Message.handlers.staff_message_cache import staff_message_cache
                    staff_messages = staff_message_cache.get_messages(from_uid)
                    if staff_messages:
                        staff_context = "\n[人工客服已回复]\n" + "\n".join(
                            f"客服({time_str}): {content}" for time_str, content in staff_messages
                        )

            # 拼接客服消息到 input
            final_input = query
            if staff_context:
                final_input = f"{query}{staff_context}"

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

            # 预读 session 到缓存，避免 _aread_or_create_session 中的同步 DB 读取阻塞事件循环
            # arun() → _arun() → _aread_or_create_session() 在同步 DB 路径上会通过
            # asyncio.to_thread 读取，这里在线程池中预读并设置 agent._cached_session，
            # 让后续 _aread_or_create_session 命中缓存直接返回，省一次线程池调度。
            try:
                if self._agent.db is not None:
                    # 在线程池中执行同步 DB 读取（_read_or_create_session 是 Agent 实例方法）
                    _pre_session = await asyncio.to_thread(
                        self._agent._read_or_create_session, session_id=session_id,
                        user_id=context.kwargs.user_id
                    )
                    # 设置缓存，让 _aread_or_create_session 命中缓存
                    self._agent._cached_session = _pre_session
            except Exception:
                pass  # 预读失败不影响主流程，patch 后的 _aread_or_create_session 也能正常工作

            response: RunOutput = await self._agent.arun(
                user_id=context.kwargs.user_id,
                session_id=session_id,
                input=final_input,
                dependencies=dependencies
            )
            return Reply(ReplyType.TEXT, response.content)
        except Exception as e:
            self.logger.error(f"CustomerAgent异步回复失败: {e}")
            return Reply(ReplyType.TEXT, "抱歉，我现在无法回复，请稍后再试。")
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
# agno 框架的 async 版本函数（aupsert_session / aread_or_create_session /
# asave_session）在检测到同步 DB（SqliteDb，非 AsyncBaseDb）时，会直接
# 调用同步 SQLAlchemy 操作，阻塞 asyncio 事件循环。当阻塞超过数秒时，
# Qt 主线程的事件处理也会被冻结，期间 paintEvent 读取不一致的主题状态
# 可能导致 QSvgRenderer access violation 崩溃。
#
# 解决方案：monkey-patch 这些函数，让同步 DB 操作通过 asyncio.to_thread()
# 在后台线程池执行，不再阻塞事件循环。
# ---------------------------------------------------------------------------

_agno_patched = False  # 全局标志，确保只 patch 一次


def _patch_agno_async_db():
    """Patch agno 框架的 async DB 函数，让同步 DB 操作走 to_thread()"""
    global _agno_patched
    if _agno_patched:
        return
    _agno_patched = True

    # 直接导入子模块（agno >= 2.x 不再从 __init__ 导出这些内部模块）
    import agno.agent._storage as _storage
    import agno.agent._session as _session
    import agno.agent._init as _init

    # 保存原始函数引用
    _original_aupsert_session = _storage.aupsert_session
    _original_aread_or_create_session = _storage.aread_or_create_session
    _original_asave_session = _session.asave_session

    async def _patched_aupsert_session(agent, session):
        """aupsert_session 的安全版本：同步 DB 操作走 to_thread()"""
        try:
            if not agent.db:
                raise ValueError("Db not initialized")
            if _init.has_async_db(agent):
                return await agent.db.upsert_session(session=session)
            else:
                # 关键修复：同步 DB 操作不直接调用，而是投到线程池
                return await asyncio.to_thread(agent.db.upsert_session, session=session)
        except Exception as e:
            import traceback
            traceback.print_exc(limit=3)
            from agno.utils.log import log_warning
            log_warning(f"Error upserting session into db: {str(e)}")
            return None

    async def _patched_aread_or_create_session(agent, session_id, user_id=None):
        """aread_or_create_session 的安全版本：同步 DB 读取走 to_thread()"""
        from time import time
        from uuid import uuid4
        from typing import cast
        from agno.db.types import AgentSession
        from agno.agent._storage import read_session, get_agent_data
        from agno.utils.log import log_debug
        from agno.run.output import RunOutput
        from agno.run.message import Message

        # 返回缓存 session
        if (
            agent._cached_session is not None
            and agent._cached_session.session_id == session_id
            and (user_id is None or agent._cached_session.user_id == user_id)
        ):
            return agent._cached_session

        # 从数据库加载
        agent_session = None
        if agent.db is not None and agent.team_id is None and agent.workflow_id is None:
            log_debug(f"Reading AgentSession: {session_id}")
            if _init.has_async_db(agent):
                agent_session = cast(AgentSession, await _storage.aread_session(agent, session_id=session_id, user_id=user_id))
            else:
                # 关键修复：同步 DB 读取走 to_thread()
                agent_session = cast(AgentSession, await asyncio.to_thread(
                    _storage.read_session, agent, session_id=session_id, user_id=user_id
                ))

        if agent_session is None:
            log_debug(f"Creating new AgentSession: {session_id}")
            session_data = {}
            if agent.session_state is not None:
                from copy import deepcopy
                session_data["session_state"] = deepcopy(agent.session_state)
            agent_session = AgentSession(
                session_id=session_id,
                agent_id=agent.id,
                user_id=user_id,
                agent_data=get_agent_data(agent),
                session_data=session_data,
                metadata=agent.metadata,
                created_at=int(time()),
            )
            if agent.introduction is not None:
                agent_session.upsert_run(
                    RunOutput(
                        run_id=str(uuid4()),
                        session_id=session_id,
                        agent_id=agent.id,
                        agent_name=agent.name,
                        user_id=user_id,
                        content=agent.introduction,
                        messages=[
                            Message(role=agent.model.assistant_message_role, content=agent.introduction)
                        ],
                    )
                )

        if agent.cache_session:
            agent._cached_session = agent_session

        return agent_session

    async def _patched_asave_session(agent, session):
        """asave_session 的安全版本：同步 DB 操作走 to_thread()"""
        if (
            agent.db is not None
            and agent.team_id is None
            and agent.workflow_id is None
            and session.session_data is not None
        ):
            if session.session_data is not None and isinstance(session.session_data.get("session_state"), dict):
                session.session_data["session_state"].pop("current_session_id", None)
                session.session_data["session_state"].pop("current_user_id", None)
                session.session_data["session_state"].pop("current_run_id", None)
            if _init.has_async_db(agent):
                await _storage.aupsert_session(agent, session=session)
            else:
                # 关键修复：同步 upsert_session 走 to_thread()
                await asyncio.to_thread(_storage.upsert_session, agent, session=session)
            from agno.utils.log import log_debug
            log_debug(f"Created or updated AgentSession record: {session.session_id}")

    # 替换模块级函数
    _storage.aupsert_session = _patched_aupsert_session
    _storage.aread_or_create_session = _patched_aread_or_create_session
    _session.asave_session = _patched_asave_session


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

            # 预读 session 到缓存，避免 arun_dispatch 中的同步 read_or_create_session 阻塞事件循环
            # arun_dispatch（非 async 函数）内部会同步调用 read_or_create_session 读取 pre_session，
            # 对同步 DB 这会直接阻塞 asyncio 事件循环。通过提前在线程池中预读并设置
            # agent._cached_session，让 read_or_create_session 命中缓存直接返回，不触发 DB I/O。
            try:
                if self._agent.db is not None:
                    from agno.agent._storage import read_or_create_session as _sync_read_or_create
                    # 在线程池中执行同步 DB 读取
                    _pre_session = await asyncio.to_thread(
                        _sync_read_or_create, self._agent, session_id=session_id, user_id=context.kwargs.user_id
                    )
                    # 设置缓存，让 arun_dispatch 中的 read_or_create_session 命中缓存
                    self._agent._cached_session = _pre_session
            except Exception:
                pass  # 预读失败不影响主流程，最坏情况 arun_dispatch 做同步读取

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
import asyncio
import random
import threading

from agno import tools
from Agent.bot import Bot
from agno.agent import Agent
from agno.run.agent import RunOutput
from agno.models.message import Message
from agno.session import AgentSession
from agno.session.team import TeamSession
from agno.session.workflow import WorkflowSession

from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from agno.models.openai import OpenAILike
from agno.db.sqlite import SqliteDb
from Agent.CustomerAgent.agent_knowledge import KnowledgeManager
from Agent.CustomerAgent.tools.move_conversation import transfer_conversation
from Agent.CustomerAgent.tools.get_product_list import get_shop_products
from Agent.CustomerAgent.tools.send_goods_link import send_goods_link
from config import get_config
from typing import Any, Optional, Union, cast
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
    from agno.session.agent import AgentSession

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
        from agno.utils.log import log_debug

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
                messages = []
                if self.model is not None:
                    messages.append(Message(role=self.model.assistant_message_role, content=self.introduction))
                agent_session.upsert_run(
                    RunOutput(
                        run_id=str(uuid4()),
                        session_id=session_id,
                        agent_id=self.id,
                        agent_name=self.name,
                        user_id=user_id,
                        content=self.introduction,
                        messages=messages,
                    )
                )

        if self.cache_session:
            self._cached_session = agent_session

        return agent_session

    async def _patched_asave_session(
        self: Agent, session: Union[AgentSession, TeamSession, WorkflowSession]
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

    # ---------------------------------------------------------------------------
    # Patch AgentSession.upsert_run：截断 runs 列表，防止无限增长
    #
    # 根因：agno 的 upsert_run 只 append，从不删除旧 run。每次 arun 都要
    # 从 SQLite 读取整个 runs JSON → json.loads → 对每条 message 做 pydantic
    # 反序列化。当 runs 积累到数百条（每条 run 因 add_history_to_context=True
    # 携带完整历史快照，单条 150KB+）后，runs JSON 达到数十 MB，单次
    # _read_session 耗时 6+ 秒，阻塞 asyncio 线程池，导致主线程冻结，最终进程崩溃。
    #
    # 修复：upsert_run 后只保留最近 MAX_SESSION_RUNS 条 run。
    # num_history_runs=8 读取历史时只取最近 8 条，保留 15 条绰绰有余。
    # ---------------------------------------------------------------------------
    _MAX_SESSION_RUNS = 15
    _original_upsert_run = AgentSession.upsert_run

    def _patched_upsert_run(self: AgentSession, run):
        _original_upsert_run(self, run)
        # 截断：只保留最近 N 条 run
        if self.runs and len(self.runs) > _MAX_SESSION_RUNS:
            self.runs = self.runs[-_MAX_SESSION_RUNS:]

    AgentSession.upsert_run = _patched_upsert_run


class CustomerAgent(Bot):
    knowledge_manager: KnowledgeManager

    # 类级别锁：防止重连时两个线程同时初始化 Agent / LanceDB / KnowledgeManager
    # 这是崩溃根因修复之一 —— 04:36 那次崩溃中两个线程在 3 秒内并发创建了
    # knowledge_enhanced 实例（包含 LanceDB 向量数据库 + agno Agent），
    # 共用同一个 LanceDB 数据目录导致 access violation。
    _init_lock = threading.Lock()

    # 全局单例 KnowledgeManager（所有 CustomerAgent 实例共享）
    _shared_knowledge_manager: Optional['KnowledgeManager'] = None
    _km_lock = threading.Lock()

    def __init__(self, knowledge_manager: Optional['KnowledgeManager'] = None):
        super().__init__()
        # 使用线程安全的全局单例 KnowledgeManager，避免两个线程同时创建
        # LanceDB/LanceDbWithProgress 导致 C++ 级 access violation
        if knowledge_manager is None:
            with CustomerAgent._km_lock:
                if CustomerAgent._shared_knowledge_manager is None:
                    from core.di_container import container
                    try:
                        knowledge_manager = container.get(KnowledgeManager)
                    except ValueError:
                        knowledge_manager = KnowledgeManager()
                    CustomerAgent._shared_knowledge_manager = knowledge_manager
                knowledge_manager = CustomerAgent._shared_knowledge_manager
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

        # 线程锁：防止重连时多个 AutoReplyThread 并发初始化 Agent/LanceDB
        # 在锁内完成所有可能操作向量数据库的操作（KnowledgeManager + Agent 创建）
        with CustomerAgent._init_lock:
            # 双重检查：可能另一个线程在等锁时已经初始化完了
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
                    num_history_runs=8,
                    add_dependencies_to_context=True,
                    add_datetime_to_context=True,
                    timezone_identifier="Asia/Shanghai"
                )

                self._is_initialized = True
                self.logger.info("CustomerAgent初始化成功")
                return True

            except Exception as e:
                self.logger.error(f"CustomerAgent初始化失败: {e}")
                return False

    async def async_reply(self, query: str, context: Optional[Context] = None) -> Reply:
        """异步回复接口 - 确保返回Reply对象"""
        self.logger.info("[async_reply] 开始处理，进入初始化检查")
        if not self._agent:
            if not await self.initialize_async():
                return Reply(ReplyType.TEXT, "AI客服初始化失败")
        self.logger.info("[async_reply] Agent 已就绪")

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
            self.logger.info("[async_reply] 开始预读 session")
            try:
                if self._agent.db is not None:
                    # 使用同步的 _read_session 在线程池中读取（注意：_read_or_create_session 已被 patch 为 async，
                    # 不能在线程池中直接调用；_read_session 仍是同步方法）
                    _pre_session = await asyncio.to_thread(
                        self._agent._read_session, session_id=session_id
                    )
                    # 设置缓存，让 _aread_or_create_session 命中缓存
                    # _read_session 返回类型是 Union[AgentSession, TeamSession, WorkflowSession, None]，
                    # 但我们用的是单 agent 模式，实际只会返回 AgentSession，用 cast 安抚类型检查器
                    self._agent._cached_session = cast(Optional[AgentSession], _pre_session)
                    self.logger.info("[async_reply] 预读 session 完成")
            except Exception as e:
                self.logger.warning(f"[async_reply] 预读 session 失败（将继续）: {e}")

            self.logger.info("[async_reply] 开始调用 arun")
            # 给 arun 加 60 秒超时，避免某个步骤无限挂起导致事件循环冻结
            response: RunOutput = await asyncio.wait_for(
                self._agent.arun(
                    user_id=context.kwargs.user_id,
                    session_id=session_id,
                    input=final_input,
                    dependencies=dependencies
                ),
                timeout=60.0
            )
            self.logger.info("[async_reply] arun 调用完成")
            return Reply(ReplyType.TEXT, response.content)
        except Exception as e:
            self.logger.error(f"CustomerAgent异步回复失败: {e}")
            # 异常兜底：优先使用设置中配置的兜底回复话术（rate_limit.fallback_reply，
            # 即设置面板里的“兜底回复”），随机抽一条；未配置或列表为空时回退到默认文案，
            # 避免发送空消息。
            _fallbacks = get_config("rate_limit.fallback_reply", []) or []
            if _fallbacks:
                return Reply(ReplyType.TEXT, random.choice(_fallbacks))
            return Reply(ReplyType.TEXT, "抱歉，我现在无法回复，请稍后再试。")
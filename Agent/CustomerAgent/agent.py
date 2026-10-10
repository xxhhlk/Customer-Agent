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
from agno.media import Image
from agno.db.sqlite import SqliteDb
from Agent.CustomerAgent.tools.move_conversation import transfer_conversation
from Agent.CustomerAgent.tools.get_product_list import get_shop_products
from Agent.CustomerAgent.tools.send_goods_link import send_goods_link
from config import get_config
from typing import Any, Optional, Union, cast, TYPE_CHECKING
from utils.logger_loguru import get_logger
from pydantic import BaseModel, Field
from typing import Dict

if TYPE_CHECKING:
    # 仅用于类型注解（下方均为字符串注解，运行时不需要真实类型）。
    # 严禁在主进程真实导入 agent_knowledge：它会连带 import knowledge_enhanced
    # → agno.vectordb.lancedb → lancedb（lance/arrow/tantivy C 扩展 + 后台线程），
    # 击穿 LanceDB 子进程隔离，导致退出期解释器拆解 C 线程时 ntdll 堆 access violation。
    from Agent.CustomerAgent.agent_knowledge import KnowledgeManager

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

# 历史图片重放上限：只把最近 N 条带图历史消息的图片还原给模型，更早的剥离。
# 对应 JC0v0「仅还原最近若干条历史图」的取舍：买家发图后追问时 AI 仍能引用
# 最近的图，但不会让 8 轮窗口内所有旧图随每轮请求重复携带（token 成本线性增长）。
_HISTORY_MEDIA_KEEP_RECENT = 2

# 人工接管轮次在会话历史中的占位回复：这类轮次不经模型，但买家消息必须留在
# 历史里，否则买家已提供过的机器码、订单号等关键信息在后续轮次中缺失。
_STAFF_TAKEOVER_CONTENT = "（本轮由人工客服直接回复）"


def _strip_old_history_images(messages, keep_recent: int = _HISTORY_MEDIA_KEEP_RECENT) -> None:
    """剥离较旧历史消息里的图片（原地修改），只保留最近 keep_recent 条带图消息。"""
    with_images = [m for m in messages if getattr(m, "images", None)]
    for msg in with_images[:-keep_recent]:
        msg.images = None


def _patch_agno_history_media() -> None:
    """Patch AgentSession.get_messages：历史消息只保留最近 N 张图片。

    背景（实测，agno 2.3.4）：get_messages 装载的历史 Message 若带 images，
    会被 OpenAIChat._format_message 无条件转成 image_url 内容块发给模型，
    且不受 send_media_to_model 控制（后者只管当前轮与工具产出媒体）；
    配合 add_history_to_context + num_history_runs=8，每轮请求都会重放
    窗口内所有历史图片。此补丁幂等。
    """
    try:
        from agno.session.agent import AgentSession
    except Exception:
        return
    if getattr(AgentSession.get_messages, "_cb_history_media_patched", False):
        return

    _orig_get_messages = AgentSession.get_messages

    def _get_messages_patched(self, *args, **kwargs):
        messages = _orig_get_messages(self, *args, **kwargs)
        try:
            _strip_old_history_images(messages)
        except Exception:
            # 剥离失败不影响主流程（代价只是多带几张旧图）
            pass
        return messages

    _get_messages_patched._cb_history_media_patched = True  # type: ignore[attr-defined]
    AgentSession.get_messages = _get_messages_patched  # type: ignore[method-assign]


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


# ---------------------------------------------------------------------------
# reasoning_effort（思考强度）模型兼容判断
#
# 火山方舟 Chat API 的 reasoning_effort 字段并非所有模型都支持：
# 支持的模型（doubao-seed 2.x / 1.8、deepseek-v4、glm-5-2、doubao-seed-1-6-251015 等）
# 接受全部 7 档取值（none/minimal/low/medium/high/xhigh/max），服务端自动映射；
# 不支持的模型（如 doubao-seed-1-6-flash-*、doubao-seed-1-6-vision-*、glm-4-7 等）
# 传入未知字段可能报错。因此只在确认支持的模型上透传该参数，其余模型自动忽略，
# 保证"各种模型都能正常回复"。
# ---------------------------------------------------------------------------
_REASONING_EFFORT_MODEL_PREFIXES = (
    "doubao-seed-2-0-",   # 2.0 全系（lite/mini/pro/code-preview 各版本）均支持
    "doubao-seed-2-1-",   # 2.1 全系（pro/turbo）均支持
    "doubao-seed-1-8-",   # 1.8 全系支持，后续版本大概率继续支持
    "deepseek-v4-",       # v4 全系（pro/flash 各版本）均支持
)
_REASONING_EFFORT_MODEL_EXACT = {
    "doubao-seed-evolving",
    "doubao-seed-1-6-251015",   # 注意：doubao-seed-1-6-250615 / -flash-* / -vision-* 不支持，必须精确匹配
    "doubao-seed-character-260628",
    "glm-5-2-260617",           # glm-4-7 不支持，不能按 glm- 前缀匹配
}


def _model_supports_reasoning_effort(model_name: str) -> bool:
    """判断模型是否支持 reasoning_effort（思考强度）参数，不支持的模型不传该字段"""
    if not model_name:
        return False
    name = model_name.strip().lower()
    if name in _REASONING_EFFORT_MODEL_EXACT:
        return True
    return any(name.startswith(prefix) for prefix in _REASONING_EFFORT_MODEL_PREFIXES)


class CustomerAgent(Bot):
    knowledge_manager: Optional['KnowledgeManager']

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
        # lancedb 已隔离到独立子进程，主进程堆不会被损坏。
        # 可以提前启动子进程并初始化，不必等到收到消息才创建。
        self._explicit_knowledge_manager = knowledge_manager
        self.knowledge_manager: Optional['KnowledgeManager'] = None
        self._agent: Optional[Agent] = None
        self.logger = get_logger("CustomerAgent")
        self._is_initialized = False
        # 同一买家会话串行锁：防止同一买家消息并发交错进入 Agent（会话历史/工具调用互相污染）
        self._conversation_locks: Dict[str, asyncio.Lock] = {}

    async def initialize_async(self) -> bool:
        """初始化CustomerAgent"""
        if self._is_initialized:
            return True

        # Patch agno 框架的 async DB 函数（幂等，全局只执行一次）
        _patch_agno_async_db()
        # Patch 历史图片重放（幂等；只保留最近 N 条历史图，防每轮重复携带）
        _patch_agno_history_media()

        # 线程锁：防止重连时多个 AutoReplyThread 并发初始化 Agent/LanceDB
        # 在锁内完成所有可能操作向量数据库的操作（KnowledgeManager + Agent 创建）
        with CustomerAgent._init_lock:
            # 双重检查：可能另一个线程在等锁时已经初始化完了
            if self._is_initialized:
                return True

            try:
                # 延迟创建 KnowledgeManager 代理（lancedb 子进程），到真正需要 AI 回复时才初始化。
                # lancedb 的 C 扩展在独立子进程中运行，其后台线程破坏的堆只影响子进程，
                # 主进程堆保持干净，避免点聊天 tab 时堆崩溃（0xfc0）。
                if self.knowledge_manager is None:
                    if self._explicit_knowledge_manager is not None:
                        self.knowledge_manager = self._explicit_knowledge_manager
                    else:
                        with CustomerAgent._km_lock:
                            if CustomerAgent._shared_knowledge_manager is None:
                                from Agent.CustomerAgent.lancedb_proxy import get_knowledge_manager_proxy
                                CustomerAgent._shared_knowledge_manager = get_knowledge_manager_proxy()
                            self.knowledge_manager = CustomerAgent._shared_knowledge_manager

                # 获取配置
                db_path = get_config("db_path", "./temp/agent.db")
                model_name = get_config("llm.model_name", "gpt-3.5-turbo")
                api_key = get_config("llm.api_key", "")
                api_base = get_config("llm.api_base", "")
                description = get_config("prompt.description", "")
                instructions = get_config("prompt.instructions", [])
                additional_context = get_config("prompt.additional_context", "")
                thinking_config = get_config("llm.thinking", None)
                reasoning_effort = get_config("llm.reasoning_effort", "") or ""

                # 验证必要配置
                if not api_key:
                    raise ValueError("LLM API密钥未配置")

                # 构建 extra_body 参数（火山引擎 thinking / reasoning_effort 配置）
                # 思考强度仅对支持的模型透传，避免不支持的模型因未知参数报错
                extra_body = {}
                if thinking_config:
                    extra_body["thinking"] = thinking_config
                if reasoning_effort and _model_supports_reasoning_effort(model_name):
                    extra_body["reasoning_effort"] = reasoning_effort
                extra_body = extra_body or None

                # 创建Agent实例；给 agno 的 SQLite 引擎补 WAL + busy_timeout，
                # 缓解多账号并发写同一 db 时的 SQLITE_BUSY 争用
                _agent_db = SqliteDb(db_file=db_path)
                try:
                    from utils.db_pragma import setup_sqlite_pragmas
                    setup_sqlite_pragmas(_agent_db.db_engine)
                except Exception:
                    pass

                self._agent = Agent(
                    db=_agent_db,
                    knowledge=self.knowledge_manager.knowledge,
                    model=OpenAILike(
                        id=model_name,
                        api_key=api_key,
                        base_url=api_base,
                        temperature=0.7,
                        extra_body=extra_body,
                    ),
                    tools=[transfer_conversation, send_goods_link],
                    # search_knowledge: 给模型挂 search_knowledge_base 工具（模型自主决定是否调用）
                    # add_knowledge_to_context: 每轮无条件检索并在用户消息中注入 <references> 保底，
                    # 实测模型会漏调工具（2026-10-10 排查），双通道并存互不冲突
                    search_knowledge=True,
                    add_knowledge_to_context=True,
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
        """异步回复接口 - 确保返回Reply对象；同一买家会话串行处理，防止并发交错"""
        if context is not None:
            session_id = self._make_session_id(context)
            lock = self._conversation_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                return await self._async_reply_locked(query, context)
        return await self._async_reply_locked(query, context)

    def _make_session_id(self, context: Context) -> str:
        """会话键 = 渠道 + 客服账号 + 买家；避免同店多个买家共用同一份 AI 历史"""
        from_uid = ""
        if hasattr(context, "kwargs") and context.kwargs is not None:
            from_uid = str(getattr(context.kwargs, "from_uid", "") or "")
        return f"{context.channel_type}{context.kwargs.user_id}_{from_uid}"

    async def record_staff_turn(self, query: str, context: Optional[Context] = None) -> None:
        """人工接管本轮时，把买家消息补写进会话历史。

        被人接管的轮次不走模型，历史里只会留下 AI 参与过的对话。若不补写，
        买家已发出的机器码、订单号、图片等关键信息对后续每轮都不可见，
        AI 会重复索要买家已经提供过的内容。
        """
        if self._agent is None or context is None:
            return
        text = (query or "").strip()
        if not text:
            return

        session_id = self._make_session_id(context)
        user_id = str(context.kwargs.user_id)
        lock = self._conversation_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            try:
                from uuid import uuid4
                from agno.run.base import RunStatus

                assert self._agent is not None
                session = cast(
                    AgentSession,
                    await self._agent._aread_or_create_session(
                        session_id=session_id, user_id=user_id
                    ),
                )
                session.upsert_run(
                    RunOutput(
                        run_id=str(uuid4()),
                        session_id=session_id,
                        agent_id=self._agent.id,
                        agent_name=self._agent.name,
                        user_id=user_id,
                        content=_STAFF_TAKEOVER_CONTENT,
                        status=RunStatus.completed,
                        messages=[
                            Message(role="user", content=text),
                            Message(
                                role=getattr(self._agent.model, "assistant_message_role", "assistant"),
                                content=_STAFF_TAKEOVER_CONTENT,
                            ),
                        ],
                    )
                )
                await self._agent.asave_session(session)
            except Exception as e:
                self.logger.warning(f"[record_staff_turn] 补写人工接管轮次失败: {e}")

    @staticmethod
    def _is_safe_media_url(url: str) -> bool:
        """校验媒体 URL：仅允许 http(s) 且指向公网地址（防 SSRF）。

        视觉模型/agno 会主动抓取该 URL，必须拒绝内网 / 回环 / 链路本地 /
        元数据地址，以及带凭据的 URL。
        """
        try:
            import ipaddress
            from urllib.parse import urlsplit
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https"):
                return False
            if parts.username or parts.password:
                return False
            host = parts.hostname
            if not host:
                return False
            try:
                ip = ipaddress.ip_address(host)
            except ValueError:
                # 域名场景放行（DNS 层防护不在本层职责内）
                return True
            return not (
                ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified
            )
        except Exception:
            return False

    async def _async_reply_locked(self, query: str, context: Optional[Context] = None) -> Reply:
        """异步回复实现（在会话锁内执行）"""
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

            # 会话键含买家 from_uid，避免同店多个买家共用历史（防串话）
            session_id = self._make_session_id(context)
            # 确保dependencies中的值是安全的类型
            dependencies = {
                "shop_name": str(context.kwargs.shop_name),
                "channel_type": str(context.channel_type.value if context.channel_type else ""),
                "shop_id": str(context.kwargs.shop_id),
                "user_id": str(context.kwargs.user_id),
                "from_uid": str(context.kwargs.from_uid),
            }

            # 图片/视频消息传给视觉大模型（llm.send_image_to_ai 开关实时读取，免重启热开关）
            # 图片：URL 直传优先，走 agno images 参数（自动转 image_url 内容块）；
            # 视频：agno OpenAILike 不支持 videos 参数，构造带 video_url 内容块的 Message 原样透传。
            # 两者 URL 直传失败时均由 _arun 内的重试逻辑下载转 base64 再试一次。
            images: Optional[list] = None
            image_url: Optional[str] = None
            video_message: Optional[Message] = None
            video_url: Optional[str] = None
            _send_media = get_config("llm.send_image_to_ai", True)
            if context.type == ContextType.IMAGE and _send_media and context.content:
                _img = str(context.content).strip()
                if _img.startswith(("http://", "https://")):
                    if self._is_safe_media_url(_img):
                        images = [Image(url=_img)]
                        image_url = _img
                    else:
                        self.logger.warning(
                            f"[async_reply] 图片 URL 未通过安全校验，改为纯文本处理: {_img[:100]}"
                        )
                elif _img.startswith("data:image"):
                    # 已是 base64 编码的 data URL，直接走 agno 的 base64 通道
                    images = [Image(url=_img)]
            elif context.type == ContextType.VIDEO and _send_media and context.content:
                _video = str(context.content).strip()
                if _video.startswith(("http://", "https://", "data:video")):
                    video_url = _video
                    _fps = get_config("llm.video_fps", 1.0)
                    video_message = Message(role="user", content=[
                        {"type": "text", "text": final_input},
                        {"type": "video_url", "video_url": {"url": _video, "fps": _fps}},
                    ])

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
            # input_msg/ images 均为 None 时与旧行为完全一致；带图片/视频时 URL 直传优先，
            # 直传失败（HTTP 错误 / 超时）则下载转 base64 重试一次，仍失败 re-raise 走外层兜底
            async def _arun(input_msg: Union[str, Message], images: Optional[list] = None) -> RunOutput:
                assert self._agent is not None, "Agent未初始化"
                return await asyncio.wait_for(
                    self._agent.arun(
                        user_id=context.kwargs.user_id,
                        session_id=session_id,
                        input=input_msg,
                        dependencies=dependencies,
                        images=images
                    ),
                    timeout=60.0
                )

            run_input = video_message if video_message is not None else final_input
            try:
                response = await _arun(run_input, images)
            except Exception:
                if image_url:
                    b64_image = await self._download_media_as_base64(
                        image_url, 10 * 1024 * 1024, "image/jpeg"
                    )
                    if b64_image is not None:
                        self.logger.info("[async_reply] 图片 URL 直传失败，改用 base64 重试")
                        try:
                            response = await _arun(run_input, [Image(url=b64_image)])
                        except Exception:
                            # 模型仍然拒绝图片：单次降级为纯文本，保证买家仍能收到回复。
                            # 不预判、不缓存"该模型不支持图片"——错误码无法区分
                            # "模型不接受"与"这张图抓不到"，缓存会让偶发失败静默降级到重启。
                            self.logger.warning(
                                "[async_reply] base64 重试仍失败，降级为纯文本重试"
                            )
                            response = await _arun(run_input, None)
                    else:
                        self.logger.warning("[async_reply] 图片下载失败，降级为纯文本重试")
                        response = await _arun(run_input, None)
                elif video_url and video_url.startswith(("http://", "https://")):
                    b64_video = await self._download_media_as_base64(
                        video_url, 45 * 1024 * 1024, "video/mp4"
                    )
                    if b64_video is not None:
                        self.logger.info("[async_reply] 视频 URL 直传失败，改用 base64 重试")
                        _fps = get_config("llm.video_fps", 1.0)
                        retry_msg = Message(role="user", content=[
                            {"type": "text", "text": final_input},
                            {"type": "video_url", "video_url": {"url": b64_video, "fps": _fps}},
                        ])
                        response = await _arun(retry_msg, None)
                    else:
                        raise
                else:
                    raise
            self.logger.info("[async_reply] arun 调用完成")
            return Reply(ReplyType.TEXT, response.content)
        except Exception as e:
            self.logger.error(f"CustomerAgent异步回复失败: {e}", exc_info=True)
            # 异常兜底：优先使用设置中配置的兜底回复话术（rate_limit.fallback_reply，
            # 即设置面板里的“兜底回复”），随机抽一条；未配置或列表为空时回退到默认文案，
            # 避免发送空消息。
            _fallbacks = get_config("rate_limit.fallback_reply", []) or []
            if _fallbacks:
                return Reply(ReplyType.TEXT, random.choice(_fallbacks))
            return Reply(ReplyType.TEXT, "抱歉，我现在无法回复，请稍后再试。")

    async def _download_media_as_base64(
        self, url: str, max_bytes: int, fallback_mime: str
    ) -> Optional[str]:
        """下载媒体文件并转 base64 data URL。

        供图片/视频 URL 直传失败时回退使用：下载走线程池不阻塞事件循环；
        下载失败 / 媒体类型不符 / 超过 max_bytes 均返回 None，由调用方 re-raise 走外层兜底。
        方舟限制：图片单图 10MB、base64 视频 50MB 且请求体 64MB（base64 膨胀 4/3，
        故视频上限取 45MB 留余量）。
        """
        import base64
        import mimetypes
        import requests

        def _fetch() -> Optional[str]:
            from utils.proxy_config import get_media_proxies
            resp = requests.get(url, timeout=15, proxies=get_media_proxies())
            resp.raise_for_status()
            data = resp.content
            if len(data) > max_bytes:
                return None
            mime = resp.headers.get("Content-Type", "").split(";")[0].strip()
            if not mime.lower().startswith(("image/", "video/")):
                mime = mimetypes.guess_type(url)[0] or fallback_mime
            if not mime.lower().startswith(("image/", "video/")):
                return None
            return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"

        try:
            return await asyncio.to_thread(_fetch)
        except Exception as e:
            self.logger.warning(f"[async_reply] 媒体下载失败: {e}")
            return None
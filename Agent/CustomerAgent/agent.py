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

# 单次回复允许的知识库工具调用总次数上限（agno 跨轮累计，超出后返回错误结果逼模型收敛）。
# 背景：2026-10-26 一次换机器码咨询里模型连续检索 4 次，叠加深度思考把单次 arun
# 拖过 60s 被超时打断。实测正常咨询 1-3 次足够（提示词已要求先关键词后语义），
# 这里封顶 4 次，避免无限检索。改这里需要重启客户端（构造 Agent 时传入）。
_AGENT_TOOL_CALL_LIMIT = 4


def _reply_timeout_seconds() -> float:
    """单次 AI 回复超时（秒）：读 llm.reply_timeout 并夹取到 [20, 300]。

    模型带深度思考时一次回复常需 40-70s，多轮知识库检索还会更久；
    写死 60s 会把正常回复打成超时兜底（2026-10-26 实测，买家换机器码那轮
    模型检索 4 次后仍被切断）。配置项在设置页可调，每轮实时读取。
    """
    try:
        value = float(get_config("llm.reply_timeout", 90))
    except (TypeError, ValueError):
        return 90.0
    return max(20.0, min(300.0, value))


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
        # 当前生效的 LLM 配置指纹，用于运行时检测配置变更并热替换模型
        self._model_fp: Optional[tuple] = None
        # 当前生效的提示词指纹（description / additional_context / instructions）
        self._prompt_fp: Optional[tuple] = None
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
                                CustomerAgent._shared_knowledge_manager = cast('KnowledgeManager', get_knowledge_manager_proxy())
                            self.knowledge_manager = CustomerAgent._shared_knowledge_manager

                # 获取配置
                db_path = get_config("db_path", "./temp/agent.db")
                description = get_config("prompt.description", "")
                instructions = get_config("prompt.instructions", [])
                additional_context = get_config("prompt.additional_context", "")

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
                    model=self._build_model(),
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
                    timezone_identifier="Asia/Shanghai",
                    # 知识库工具调用总次数封顶，防止模型反复检索把单次回复拖到超时
                    tool_call_limit=_AGENT_TOOL_CALL_LIMIT,
                )

                self._is_initialized = True
                self._model_fp = self._config_fingerprint()
                self._prompt_fp = self._prompt_fingerprint()
                self.logger.info("CustomerAgent初始化成功")
                return True

            except Exception as e:
                self.logger.error(f"CustomerAgent初始化失败: {e}")
                return False

    @staticmethod
    def _llm_model_options() -> Dict[str, Any]:
        """按当前配置构造 OpenAILike 参数（初始化与热更新共用）。

        api_key 缺失时抛 ValueError，与初始化时的必填校验行为一致。
        """
        model_name = get_config("llm.model_name", "gpt-3.5-turbo")
        api_key = get_config("llm.api_key", "")
        api_base = get_config("llm.api_base", "")
        if not api_key:
            raise ValueError("LLM API密钥未配置")

        # extra_body 透传火山引擎的 thinking / reasoning_effort；
        # reasoning_effort 仅对支持的模型下发，避免不支持的模型因未知参数报错
        extra_body: Dict[str, Any] = {}
        thinking_config = get_config("llm.thinking", None)
        if thinking_config:
            extra_body["thinking"] = thinking_config
        reasoning_effort = get_config("llm.reasoning_effort", "") or ""
        if reasoning_effort and _model_supports_reasoning_effort(model_name):
            extra_body["reasoning_effort"] = reasoning_effort

        return {
            "id": model_name,
            "api_key": api_key,
            "base_url": api_base,
            "temperature": 0.7,
            "extra_body": extra_body or None,
        }

    @classmethod
    def _build_model(cls) -> OpenAILike:
        return OpenAILike(**cls._llm_model_options())

    @staticmethod
    def _config_fingerprint() -> tuple:
        """影响模型连接方式的配置指纹。embedder 不在其中：向量模型变更需要
        重建 lancedb 子进程与索引，只能重启生效。"""
        return (
            get_config("llm.model_name", "gpt-3.5-turbo"),
            get_config("llm.api_base", ""),
            get_config("llm.api_key", ""),
            repr(get_config("llm.thinking", None)),
            get_config("llm.reasoning_effort", "") or "",
        )

    def _refresh_model_if_changed(self) -> None:
        """LLM 配置变更后热替换模型实例，免重启。

        只换 model 不重建 Agent：CustomerAgent 是 DI 全局单例，被多个账号线程
        （各自独立事件循环）共用，重建会出现「_agent 为 None」的中间态，此刻
        其他线程正在 arun 就会崩。属性赋值是原子的，最坏情况是本次请求仍用旧
        模型，下一次生效。
        """
        if self._agent is None:
            return
        fp = self._config_fingerprint()
        if fp == self._model_fp:
            return
        try:
            new_model = self._build_model()
        except Exception as e:
            self.logger.error(f"[模型热更新] 构造新模型失败，继续使用旧配置: {e}")
            return
        self._agent.model = new_model
        # agno 初始化时把 model 复制给了这几个管理器，不同步会让
        # 会话摘要 / 上下文压缩继续打旧地址
        for attr in ("memory_manager", "session_summary_manager",
                     "compression_manager", "culture_manager"):
            mgr = getattr(self._agent, attr, None)
            if mgr is not None:
                try:
                    mgr.model = new_model
                except Exception:
                    pass
        self._model_fp = fp
        self.logger.info(f"[模型热更新] 已切换到 {fp[0]} @ {fp[1]}")

    # 提示词指纹字段顺序，与 _prompt_fingerprint() 的元组一一对应
    _PROMPT_FP_FIELDS = ("description", "additional_context", "instructions")

    @staticmethod
    def _prompt_fingerprint() -> tuple:
        """提示词配置指纹。instructions 可能是 list 或 str，统一 repr 后比较。"""
        return (
            repr(get_config("prompt.description", "")),
            repr(get_config("prompt.additional_context", "")),
            repr(get_config("prompt.instructions", [])),
        )

    def _refresh_prompt_if_changed(self) -> None:
        """提示词变更后热替换 agno Agent 的 system message 来源，免重启。

        与模型热更新同理只做属性赋值：agno 在 get_system_message() /
        aget_system_message() 里每次 run 才读 self.description、
        self.instructions、self.additional_context
        （agno/agent/agent.py:6942 / 7035 / 7074），赋值即对下一条消息生效。
        不重建 Agent，避免多账号线程看到「_agent 为 None」的中间态。
        """
        if self._agent is None:
            return
        fp = self._prompt_fingerprint()
        if fp == self._prompt_fp:
            return
        old = self._prompt_fp or ("",) * len(self._PROMPT_FP_FIELDS)
        changed = [
            name for name, before, after in zip(self._PROMPT_FP_FIELDS, old, fp)
            if before != after
        ]
        self._agent.description = get_config("prompt.description", "")
        self._agent.instructions = get_config("prompt.instructions", [])
        self._agent.additional_context = get_config("prompt.additional_context", "")
        self._prompt_fp = fp
        self.logger.info(f"[提示词热更新] 已应用最新配置，变更项: {', '.join(changed) or 'unknown'}")

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

    async def record_staff_turn(
        self,
        query: str,
        context: Optional[Context] = None,
        placeholder: Optional[str] = None,
    ) -> None:
        """本轮不走 AI（人工接管 / 系统自动回复）时，把买家消息补写进会话历史。

        这类轮次不走模型，历史里只会留下 AI 参与过的对话。若不补写，
        买家已发出的机器码、订单号、图片等关键信息对后续每轮都不可见，
        AI 会重复索要买家已经提供过的内容。

        placeholder 为 None 时使用人工接管默认文案；关键词等系统直接回复
        场景传入对应占位（如「本轮由系统自动回复」）。
        """
        if self._agent is None or context is None:
            return
        text = (query or "").strip()
        if not text:
            return
        reply_content = placeholder or _STAFF_TAKEOVER_CONTENT

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
                        content=reply_content,
                        status=RunStatus.completed,
                        messages=[
                            Message(role="user", content=text),
                            Message(
                                role=getattr(self._agent.model, "assistant_message_role", "assistant"),
                                content=reply_content,
                            ),
                        ],
                    )
                )
                await self._agent.asave_session(session)
            except Exception as e:
                self.logger.warning(f"[record_staff_turn] 补写会话轮次失败: {e}")

    @staticmethod
    def _load_recent_history_block(shop_id: str, buyer_uid: str, current_content: str, limit: int = 30) -> str:
        """读取该买家近期聊天记录并格式化为上下文块；无记录时返回空串。

        同步 DB 访问，调用方需用 asyncio.to_thread 包装。
        """
        from services.message_persistence import message_persistence_service

        rows = message_persistence_service.get_messages_by_uid(shop_id, buyer_uid, limit=limit + 5)
        if not rows:
            return ""
        current = (current_content or "").strip()
        lines = []
        for row in rows[-limit:]:
            msg_type = row.get("msg_type") or ""
            if msg_type == "image":
                content = "[图片]"
            elif msg_type == "video":
                content = "[视频]"
            else:
                content = (row.get("content") or "").strip()
            if not content or (current and content == current):
                continue
            ts = (row.get("timestamp") or "")[:16].replace("T", " ")
            who = "买家" if row.get("direction") == "inbound" else "客服"
            lines.append(f"{who}({ts}): {content[:200]}")
        if not lines:
            return ""
        return "[历史聊天记录]\n" + "\n".join(lines) + "\n"

    @staticmethod
    def _load_order_block(shop_id: str, account_user_id: str, buyer_uid: str) -> str:
        """读取买家近期订单（状态/商品/物流）并格式化为上下文块；无订单返回空串。

        同步 HTTP 访问（含 cookies 读取），调用方需用 asyncio.to_thread 包装。
        结果在 order_context 内部按 (shop, buyer) 缓存，短时间多轮对话不重复请求。
        """
        from Agent.CustomerAgent.order_context import load_order_block

        return load_order_block(shop_id, account_user_id, buyer_uid)

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

    @staticmethod
    def _video_cover_url(context: Context) -> Optional[str]:
        """取 PDD 视频消息自带的封面图 URL（message.info.preview.url）。

        抽帧失败时的降级素材：只有首帧画面，但零成本、不需要解码。
        """
        try:
            raw = getattr(context.kwargs, "raw_data", None) or {}
            msg = raw.get("message", raw) if isinstance(raw, dict) else {}
            info = (msg.get("info") or {}) if isinstance(msg, dict) else {}
            preview = info.get("preview") or {}
            url = preview.get("url") if isinstance(preview, dict) else None
            return str(url) if url else None
        except Exception:
            return None

    def _prepare_video_frames(self, url: str, context: Context) -> list:
        """下载视频并本地抽帧，返回 data URL 列表（供 asyncio.to_thread 调用）。

        耗时较长（1080p/113s 实测约 30s 解码），必须在事件循环外执行。
        内部有全局 ffmpeg 串行锁，多账号并发时排队而非同时解码。
        """
        from utils.video_frames import prepare_from_url

        # 时长优先取 PDD 给的 info.duration（省一次 probe），单位可能是毫秒
        duration: Optional[float] = None
        try:
            raw = getattr(context.kwargs, "raw_data", None) or {}
            msg = raw.get("message", raw) if isinstance(raw, dict) else {}
            info = (msg.get("info") or {}) if isinstance(msg, dict) else {}
            d = info.get("duration")
            if isinstance(d, (int, float)) and d > 0:
                duration = float(d) / 1000.0 if d > 1000 else float(d)
        except Exception:
            duration = None

        return prepare_from_url(
            url,
            duration=duration,
            target_frames=int(get_config("llm.video_max_frames", 80)),
            width=int(get_config("llm.video_frame_width", 768)),
            threshold=float(get_config("llm.video_scene_threshold", 0.02)),
        )

    async def _async_reply_locked(self, query: str, context: Optional[Context] = None) -> Reply:
        """异步回复实现（在会话锁内执行）"""
        self.logger.info("[async_reply] 开始处理，进入初始化检查")
        if not self._agent:
            if not await self.initialize_async():
                return Reply(ReplyType.TEXT, "AI客服初始化失败")
        else:
            # 模型地址/密钥/思考参数可能在设置页刚改过，这里比对指纹按需热替换
            self._refresh_model_if_changed()
            # 提示词同样按指纹热更新（不重建 Agent，只改 system message 来源）
            self._refresh_prompt_if_changed()
        self.logger.info("[async_reply] Agent 已就绪")

        if context is None:
            return Reply(ReplyType.TEXT, "缺少上下文信息")

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

            # 预读 session 到缓存，避免 _aread_or_create_session 中的同步 DB 读取阻塞事件循环
            # arun() → _arun() → _aread_or_create_session() 在同步 DB 路径上会通过
            # asyncio.to_thread 读取，这里在线程池中预读并设置 agent._cached_session，
            # 让后续 _aread_or_create_session 命中缓存直接返回，省一次线程池调度。
            self.logger.info("[async_reply] 开始预读 session")
            _pre_session: Optional[AgentSession] = None
            try:
                if self._agent.db is not None:
                    # 使用同步的 _read_session 在线程池中读取（注意：_read_or_create_session 已被 patch 为 async，
                    # 不能在线程池中直接调用；_read_session 仍是同步方法）
                    _pre_session = cast(Optional[AgentSession], await asyncio.to_thread(
                        self._agent._read_session, session_id=session_id
                    ))
                    # 设置缓存，让 _aread_or_create_session 命中缓存
                    # _read_session 返回类型是 Union[AgentSession, TeamSession, WorkflowSession, None]，
                    # 但我们用的是单 agent 模式，实际只会返回 AgentSession，用 cast 安抚类型检查器
                    self._agent._cached_session = _pre_session
                    self.logger.info("[async_reply] 预读 session 完成")
            except Exception as e:
                self.logger.warning(f"[async_reply] 预读 session 失败（将继续）: {e}")

            # 会话还没有任何 run（首次 AI 介入）时，从消息库补近期聊天记录：
            # 早期人工/网页端的沟通（已下单、已授权、买家发过的机器码等）不在
            # agno session 里，不补的话模型会当作从未发生过
            if not getattr(_pre_session, "runs", None):
                try:
                    _history_block = await asyncio.to_thread(
                        self._load_recent_history_block,
                        str(context.kwargs.shop_id),
                        str(context.kwargs.from_uid),
                        str(context.content or ""),
                    )
                    if _history_block:
                        final_input = _history_block + final_input
                        self.logger.info("[async_reply] 已注入近期聊天记录")
                except Exception as e:
                    self.logger.warning(f"[async_reply] 加载历史聊天记录失败（将继续）: {e}")

            # 注入买家近期订单（状态/商品/金额/物流），供回答订单进度类问题。
            # 每轮都带（内部 60s 缓存），订单状态会变化，不能只在首轮注入。
            try:
                _order_block = await asyncio.to_thread(
                    self._load_order_block,
                    str(context.kwargs.shop_id),
                    str(context.kwargs.user_id),
                    str(context.kwargs.from_uid),
                )
                if _order_block:
                    final_input = _order_block + final_input
                    self.logger.info("[async_reply] 已注入买家订单信息")
            except Exception as e:
                self.logger.warning(f"[async_reply] 加载买家订单信息失败（将继续）: {e}")

            # 图片/视频消息传给视觉大模型（llm.send_image_to_ai 开关实时读取，免重启热开关）
            # 图片：URL 直传优先，走 agno images 参数（自动转 image_url 内容块）；
            # 视频：按 llm.video_input_mode 分流
            #   native → 构造带 video_url 内容块的 Message 原样透传（模型需支持）
            #   frames → 本地抽帧成多图，走 images 通道（模型只支持图片时用）
            #   off    → 不分析
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
                _mode = str(get_config("llm.video_input_mode", "native") or "native").lower()
                if _mode == "frames":
                    if _video.startswith(("http://", "https://")):
                        _frames = await asyncio.to_thread(
                            self._prepare_video_frames, _video, context
                        )
                    else:
                        _frames = []
                    if _frames:
                        images = [Image(url=u) for u in _frames]
                        self.logger.info(
                            f"[async_reply] 视频本地抽帧完成，共 {len(_frames)} 帧"
                        )
                    else:
                        # 抽帧失败：退到 PDD 给的视频封面图（info.preview.url），
                        # 只能体现首帧画面，但比完全没有视觉信息好
                        _cover = self._video_cover_url(context)
                        if _cover and self._is_safe_media_url(_cover):
                            images = [Image(url=_cover)]
                            image_url = _cover
                            self.logger.info("[async_reply] 抽帧失败，改用视频封面图")
                        else:
                            self.logger.warning("[async_reply] 抽帧与封面图均不可用，按纯文本处理")
                elif _mode != "off" and _video.startswith(("http://", "https://", "data:video")):
                    video_url = _video
                    _fps = get_config("llm.video_fps", 1.0)
                    video_message = Message(role="user", content=[
                        {"type": "text", "text": final_input},
                        {"type": "video_url", "video_url": {"url": _video, "fps": _fps}},
                    ])

            self.logger.info("[async_reply] 开始调用 arun")
            # 给 arun 加超时，避免某个步骤无限挂起导致事件循环冻结。
            # 阈值可配置（llm.reply_timeout，默认 90s）：模型带深度思考时一次回复
            # 常要 40-70s，写死 60s 会把正常回复打成超时兜底。每轮实时读取，改了即时生效。
            # input_msg/ images 均为 None 时与旧行为完全一致；带图片/视频时 URL 直传优先，
            # 直传失败（HTTP 错误 / 超时）则下载转 base64 重试一次，仍失败 re-raise 走外层兜底
            _reply_timeout = _reply_timeout_seconds()

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
                    timeout=_reply_timeout
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
            # TimeoutError 的 str() 是空字符串，只打 {e} 会得到「异步回复失败: 」这种
            # 无法排查的日志，必须带上异常类型名。
            # loguru 不认 exc_info 参数，要打堆栈必须走 opt(exception=True)。
            self.logger.opt(exception=True).error(
                f"CustomerAgent异步回复失败: {type(e).__name__}: {e}"
            )
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
"""
聊天记录页面 - 主界面
双栏布局：左侧会话列表 + 右侧聊天区域，顶部店铺筛选
"""

from datetime import date

from PyQt6.QtCore import Qt, pyqtSignal, QEvent, QTimer, QThread
from PyQt6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QSplitter, QComboBox,
    QSizePolicy, QWidget,
)
from PyQt6.QtGui import QFont
from qfluentwidgets import BodyLabel, InfoBar, InfoBarPosition, isDarkTheme

from ui.chat.conversation_list import ConversationListPanel
from ui.chat.chat_area import ChatAreaPanel
from utils.logger_loguru import get_logger

logger = get_logger("ChatUI")


class _ShopLoader(QThread):
    """后台线程加载店铺列表"""
    result = pyqtSignal(list)  # shops

    def run(self):
        try:
            from database.db_manager import get_db_manager
            db = get_db_manager()
            shops = db.get_all_shops()
        except Exception:
            shops = []
        self.result.emit(shops)


class _ConversationLoader(QThread):
    """后台线程加载会话列表"""
    result = pyqtSignal(list)  # conversations

    def __init__(self, shop_id: str | None = None, limit: int = 100, parent=None):
        super().__init__(parent)
        self._shop_id = shop_id
        self._limit = limit

    def run(self):
        try:
            from services.message_persistence import message_persistence_service
            convs = message_persistence_service.get_conversations(shop_id=self._shop_id, limit=self._limit)
        except Exception:
            convs = []
        self.result.emit(convs)


class _MaterialSendWorker(QThread):
    """后台发送素材空间中的图片/视频，并持久化到消息库。

    - 图片：`send_image(uid, url)`（type=1，content=图片 URL）
    - 视频：`send_video(uid, download_url, info)`，info 由
      `MaterialSpace.build_video_info()` 生成（含 file_id/download_url/size/status，
      缺这些字段会 result=ok 但静默不投递）
    """

    done = pyqtSignal(bool, str, dict)  # success, error_msg, msg_dict|None

    def __init__(self, shop_id, user_id, buyer_uid, item: dict, parent=None):
        super().__init__(parent)
        self._sid = shop_id
        self._uid = user_id
        self._buid = buyer_uid
        self._item = item

    def run(self):
        try:
            from Channel.pinduoduo.utils.API.material_space import MaterialSpace
            from Channel.pinduoduo.utils.API.send_message import SendMessage

            payload = MaterialSpace.build_send_payload(self._item)
            ctx_type = payload["context_type"]
            url = payload["url"]
            info = payload["info"]

            if not url:
                self.done.emit(False, "素材没有可用的 URL", None)
                return

            sender = SendMessage(str(self._sid), str(self._uid))
            if ctx_type == "video":
                logger.info("[MATERIAL] 发送视频: file_id=%s, has_info=%s",
                            (info or {}).get("file_id"), bool(info))
                result = sender.send_video(str(self._buid), url, info=info)
            else:
                logger.info("[MATERIAL] 发送图片: name=%s", self._item.get("name"))
                result = sender.send_image(str(self._buid), url)

            ok, err = self._judge(result)
            if not ok:
                self.done.emit(False, err, None)
                return

            self._notify_staff_intervention(ctx_type)

            msg_dict = self._persist(ctx_type, url, payload.get("media_meta"))
            self.done.emit(True, "", msg_dict or {})
        except Exception as e:
            logger.error(f"[MATERIAL] 发送素材异常: {e}", exc_info=True)
            self.done.emit(False, str(e), None)

    @staticmethod
    def _judge(result) -> tuple[bool, str]:
        """判定发送是否成功：success + result.result != fail + 无 error_code"""
        if not isinstance(result, dict) or not result.get("success"):
            return False, str(result)
        inner = result.get("result") or {}
        if inner.get("result") == "fail":
            return False, f"param error: {inner.get('reason')}"
        if inner.get("error_code") and inner.get("error_code") != 0:
            return False, f"error_code={inner.get('error_code')} {inner.get('error', '')}"
        return True, ""

    def _notify_staff_intervention(self, ctx_type: str):
        """发送素材属人工介入：取消正在等待的 AI 流程 + 写入人工消息缓存"""
        try:
            from Message.handlers.staff_reply_event import staff_reply_event_manager
            staff_reply_event_manager.notify_staff_reply(self._buid)
        except Exception:
            pass
        try:
            from Message.handlers.staff_message_cache import staff_message_cache
            staff_message_cache.add_message(
                self._buid, "[图片]" if ctx_type == "image" else "[视频]"
            )
        except Exception:
            pass

    def _persist(self, ctx_type: str, url: str, media_meta):
        """写入消息库（成功后 UI 立即显示气泡）"""
        try:
            from services.message_persistence import message_persistence_service
            return message_persistence_service.save_outbound_message(
                shop_id=self._sid,
                user_id=self._uid,
                buyer_uid=self._buid,
                reply_content=url,
                reply_source="manual",
                context_type=ctx_type,
                media_meta=media_meta,
            )
        except Exception as e:
            logger.error(f"[MATERIAL] 持久化失败: {e}")
            return None


class ChatUI(QFrame):
    """聊天记录页面"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChatUI")
        self._shops: list[dict] = []
        self._shop_loader = None   # _ShopLoader 引用
        self._conv_loader = None   # _ConversationLoader 引用
        self._shops_loaded = False  # 店铺列表是否已加载过
        self._persist_workers: list = []  # 持久化 worker 列表（支持并发）
        self._forward_workers: list = []  # 转发 worker 列表（支持并发）
        self._material_workers: list = []  # 素材发送 worker 列表（支持并发）
        logger.info("[ChatUI] __init__ 开始")
        self._init_ui()
        logger.info("[ChatUI] _init_ui 完成")
        self._apply_theme()
        logger.info("[ChatUI] _apply_theme 完成")
        # lancedb 已隔离到独立子进程，主进程堆不会被损坏，
        # 恢复在 __init__ 里提前加载数据（500ms 延迟让 UI 先渲染）。
        self._data_loaded = False
        QTimer.singleShot(500, self._initial_load)
        # 跨天检测：常驻期间日期变化时刷新聊天内日期标签与会话列表时间
        self._seen_date: date = date.today()
        self._day_watch_timer = QTimer(self)
        self._day_watch_timer.setInterval(60000)
        self._day_watch_timer.timeout.connect(self._on_day_watch)
        self._day_watch_timer.start()
        logger.info("[ChatUI] __init__ 完成，已安排 500ms 后加载数据")

    def _on_day_watch(self):
        """每分钟检查一次系统日期，跨天则重算相对时间文案"""
        today = date.today()
        if today == self._seen_date:
            return
        self._seen_date = today
        try:
            self.chat_area.refresh_day_labels()
            self.conversation_list.refresh_times()
        except RuntimeError:
            pass

    def _initial_load(self):
        """首次加载: 同时加载店铺列表和会话列表"""
        logger.info("[ChatUI] _initial_load 开始")
        self._load_shops()
        logger.info("[ChatUI] _load_shops 已启动")
        self._load_conversations(None)
        logger.info("[ChatUI] _load_conversations 已启动")

    def showEvent(self, event):
        """窗口显示事件（数据已在 __init__ 里安排加载）"""
        super().showEvent(event)

    def _load_shops(self):
        """后台加载店铺列表（仅在首次或需要刷新时调用）"""
        if self._shop_loader is not None:
            try:
                self._shop_loader.result.disconnect(self._on_shops_loaded)
            except (TypeError, RuntimeError):
                pass
            self._shop_loader.quit()
            self._shop_loader.wait(500)
            self._shop_loader = None

        self._shop_loader = _ShopLoader(self)
        self._shop_loader.result.connect(self._on_shops_loaded)
        self._shop_loader.start()

    def _on_shops_loaded(self, shops: list[dict]):
        """店铺列表加载完成"""
        self._shops = shops
        self._shops_loaded = True

        self.shop_combo.blockSignals(True)
        self.shop_combo.clear()
        self.shop_combo.addItem("全部店铺", None)
        for shop in shops:
            display = f"{shop['shop_name']} ({shop['shop_id']})"
            self.shop_combo.addItem(display, shop["shop_id"])
        self.shop_combo.blockSignals(False)

        if len(shops) <= 1:
            self.shop_filter_container.hide()
        else:
            self.shop_filter_container.show()

    def _load_conversations(self, shop_id: str | None):
        """后台加载会话列表"""
        if self._conv_loader is not None:
            try:
                self._conv_loader.result.disconnect(self._on_conversations_loaded)
            except (TypeError, RuntimeError):
                pass
            self._conv_loader.quit()
            self._conv_loader.wait(500)
            self._conv_loader = None

        self._conv_loader = _ConversationLoader(shop_id=shop_id, limit=100, parent=self)
        self._conv_loader.result.connect(self._on_conversations_loaded)
        self._conv_loader.start()

    def _on_conversations_loaded(self, convs: list[dict]):
        """会话列表加载完成"""
        logger.info(f"[ChatUI] _on_conversations_loaded: {len(convs)} 条会话")
        # 同步 _current_shop_filter（修复新消息过滤不一���）
        current_filter = self.shop_combo.currentData() if self._shops_loaded else None
        self.conversation_list._current_shop_filter = current_filter
        self.conversation_list._all_data = convs
        self.conversation_list._rebuild_cards(convs)
        logger.info(f"[ChatUI] _rebuild_cards 完成")

    def _on_shop_changed(self, index: int):
        """店铺筛选切换 — 只重新加载会话，不重建店铺列表"""
        shop_id = self.shop_combo.currentData()
        logger.info(f"[ChatUI] _on_shop_changed: index={index}, shop_id={shop_id}")
        # 同步 filter 状态
        self.conversation_list._current_shop_filter = shop_id
        self.conversation_list._rebuild_cards([])  # 先清空
        self._load_conversations(shop_id)

    def _apply_theme(self):
        dark = isDarkTheme()
        # ChatUI 容器透明，继承 FluentWindow 背景
        self.setStyleSheet(f"""
            #ChatUI {{
                background-color: transparent;
                border: none;
            }}
        """)
        # 店铺筛选栏背景
        self.shop_filter_container.setStyleSheet(f"background-color: transparent;")

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        # ---- 店铺筛选栏 ----
        self.shop_filter_container = QWidget()
        filter_layout = QHBoxLayout(self.shop_filter_container)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.setSpacing(8)

        shop_label = BodyLabel("店铺:")
        shop_label.setFixedWidth(40)
        filter_layout.addWidget(shop_label)

        self.shop_combo = QComboBox()
        self.shop_combo.setMinimumWidth(200)
        self.shop_combo.setMaximumWidth(300)
        self.shop_combo.currentIndexChanged.connect(self._on_shop_changed)
        filter_layout.addWidget(self.shop_combo)
        filter_layout.addStretch()

        main_layout.addWidget(self.shop_filter_container)

        # ---- 双栏分割器 ----
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(2)

        # 左侧：会话列表
        self.conversation_list = ConversationListPanel()
        self.conversation_list.conversation_selected.connect(self._on_conversation_selected)
        splitter.addWidget(self.conversation_list)

        # 右侧：聊天区域
        self.chat_area = ChatAreaPanel()
        self.chat_area.send_manual_reply.connect(self._send_manual_reply)
        self.chat_area.forward_message.connect(self._on_forward_message)
        self.chat_area.send_material.connect(self._on_send_material)
        splitter.addWidget(self.chat_area)

        # 初始比例 1:3
        splitter.setSizes([250, 750])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)

        main_layout.addWidget(splitter, 1)

        # ---- 连接实时消息信号 ----
        try:
            from services.message_persistence import message_persistence_service
            message_persistence_service.signals.new_message.connect(self._on_new_message)
        except Exception as e:
            logger.warning(f"连接消息信号失败: {e}")

    def _on_conversation_selected(self, shop_id: str, buyer_uid: str):
        """选中会话"""
        logger.info(f"[ChatUI] _on_conversation_selected: shop_id={shop_id}, buyer_uid={buyer_uid}")
        # 一并下发店铺名，供素材面板标注「当前是哪个账号的素材库」
        self.chat_area.load_messages(shop_id, buyer_uid, shop_name=self._shop_name_of(shop_id))
        logger.info("[ChatUI] _on_conversation_selected: chat_area.load_messages 返回")

    def _shop_name_of(self, shop_id: str) -> str:
        """从已加载的店铺列表取店铺名（纯内存，不查库，避免阻塞 UI 线程）"""
        sid = str(shop_id or "")
        for s in (self._shops or []):
            if str(s.get("shop_id")) == sid:
                return str(s.get("shop_name") or "")
        return ""

    def _on_new_message(self, msg_data: dict):
        """收到新消息"""
        # 检查店铺筛选
        current_filter = self.shop_combo.currentData()
        if current_filter and msg_data.get("shop_id") != current_filter:
            return

        # 增量更新会话列表（不全部重建）
        self.conversation_list.on_new_message(msg_data)

        # 追加到当前聊天
        self.chat_area.append_message(msg_data)

    def _send_manual_reply(self, shop_id: str, user_id: str, text: str, buyer_uid: str):
        """发送手动回复

        整个发送链路（send_text + notify + 持久化）在 QThread 后台执行，
        避免同步 send_text 网络请求阻塞主线程（实测曾导致主线程卡顿 16.7s）。
        """
        try:
            from PyQt6.QtCore import QThread

            class _ManualReplyWorker(QThread):
                done = pyqtSignal(dict)  # 持久化完成的消息 dict

                def __init__(self, sid, uid, buid, txt):
                    super().__init__()
                    self._sid = sid
                    self._uid = uid
                    self._buid = buid
                    self._txt = txt

                def run(self):
                    try:
                        from Channel.pinduoduo.utils.API.send_message import SendMessage
                        sender = SendMessage(str(self._sid), str(self._uid))
                        result = sender.send_text(str(self._buid), self._txt)

                        if not (isinstance(result, dict) and result.get("success")):
                            logger.warning(f"发送手动回复失败: {result}")
                            return

                        # 通知客服回复事件管理器：手动回复等同于人工客服回复，
                        # 需要取消正在等待的AI处理流程
                        try:
                            from Message.handlers.staff_reply_event import staff_reply_event_manager
                            staff_reply_event_manager.notify_staff_reply(self._buid)
                        except Exception:
                            pass

                        # 缓存客服消息，供AI后续轮次作为上下文
                        try:
                            from Message.handlers.staff_message_cache import staff_message_cache
                            staff_message_cache.add_message(self._buid, self._txt)
                        except Exception:
                            pass

                        # 持久化
                        try:
                            from services.message_persistence import message_persistence_service
                            msg_dict = message_persistence_service.save_manual_reply(
                                shop_id=self._sid, user_id=self._uid,
                                buyer_uid=self._buid, text=self._txt
                            )
                            if msg_dict:
                                self.done.emit(msg_dict)
                        except Exception:
                            pass
                    except Exception as e:
                        logger.error(f"发送手动回复异常: {e}")

            worker = _ManualReplyWorker(shop_id, user_id, buyer_uid, text)

            def _on_persist_done(msg_dict: dict):
                try:
                    from services.message_persistence import message_persistence_service
                    message_persistence_service.notify_new_message(msg_dict)
                except Exception:
                    pass

            worker.done.connect(_on_persist_done)
            worker.start()
            # 保持引用防止 GC（使用列表支持并发 worker）
            self._persist_workers.append(worker)
            # 清理已完成的 worker
            def _cleanup_persist(_w=worker):
                try:
                    if _w in self._persist_workers:
                        self._persist_workers.remove(_w)
                    _w.deleteLater()
                except Exception:
                    pass
            worker.done.connect(lambda *_: _cleanup_persist())
        except Exception as e:
            logger.error(f"发送手动回复异常: {e}")

    def _on_forward_message(self, msg_data: dict, target_buyer_uid: str):
        """转发消息到目标会话"""
        shop_id = msg_data.get("shop_id", "")
        user_id = msg_data.get("user_id", "")
        content = msg_data.get("content", "")
        context_type = msg_data.get("context_type", "text") or "text"

        if not shop_id or not user_id or not content:
            logger.warning("转发消息缺少必要参数")
            return

        logger.info(f"[FORWARD] 开始转发: shop_id={shop_id}, user_id={user_id}, "
                    f"target_buyer_uid={target_buyer_uid}, context_type={context_type}, "
                    f"content_len={len(content) if content else 0}")

        class _ForwardWorker(QThread):
            done = pyqtSignal(bool, str)  # success, error_msg

            def __init__(self, sid, uid, target_uid, cnt, ctx_type, media_meta=None):
                super().__init__()
                self._sid = sid
                self._uid = uid
                self._target_uid = target_uid
                self._cnt = cnt
                self._ctx_type = ctx_type
                self._media_meta = media_meta

            def run(self):
                try:
                    from Channel.pinduoduo.utils.API.send_message import SendMessage
                    sender = SendMessage(str(self._sid), str(self._uid))
                    logger.info(f"[FORWARD] SendMessage 已创建: shop_id={self._sid}, user_id={self._uid}, "
                                f"account_name={sender.account_name}, has_cookies={bool(sender.cookies)}")

                    if self._ctx_type == "image":
                        logger.info(f"[FORWARD] 调用 send_image: target={self._target_uid}")
                        result = sender.send_image(str(self._target_uid), self._cnt)
                    elif self._ctx_type == "video":
                        # 从 media_meta 还原 PDD 要求的 info 字段。
                        # 实测结论（docs/material-space-send-research-2026-10-11.md §3.2）：
                        # info 必须含 download_url / file_id / size / status，
                        # 只传 preview+duration 时 result=ok 但消息静默不投递。
                        info = None
                        if self._media_meta:
                            try:
                                import json
                                meta = json.loads(self._media_meta) if isinstance(self._media_meta, str) else self._media_meta
                                raw_info = meta.get("raw_info") if isinstance(meta, dict) else None
                                if raw_info and isinstance(raw_info, dict) and raw_info.get("file_id"):
                                    # 最优路径：原样回传历史消息的 info（含它自己的 file_id）
                                    info = dict(raw_info)
                                    logger.info(
                                        "[FORWARD] 视频 info 原样回传: file_id=%s, "
                                        "has_download_url=%s, size=%s, status=%s",
                                        info.get("file_id"), bool(info.get("download_url")),
                                        info.get("size"), info.get("status"),
                                    )
                                else:
                                    # 回退：老版本入库缺少完整 raw_info，尽力用 cover/duration 拼装
                                    cover_url = meta.get("cover_url")
                                    cover_size = meta.get("cover_size")
                                    duration = meta.get("duration")
                                    if cover_url:
                                        info = {}
                                        preview = {"url": cover_url}
                                        if cover_size:
                                            preview["size"] = cover_size
                                        info["preview"] = preview
                                        if duration is not None:
                                            info["duration"] = duration
                                        logger.warning(
                                            "[FORWARD] 视频 info 用旧字段拼凑（缺少完整 raw_info，"
                                            "可能静默不投递）: cover_url=%s..., duration=%s",
                                            cover_url[:80], duration,
                                        )
                                    else:
                                        logger.warning("[FORWARD] media_meta 缺少 cover_url: %s", list(meta.keys()))
                            except Exception as e:
                                logger.warning(f"构造视频 info 失败: {e}")
                        else:
                            logger.warning("[FORWARD] 视频消息缺少 media_meta，将不带 info 字段发送")
                        logger.info(f"[FORWARD] 调用 send_video: target={self._target_uid}, has_info={bool(info)}")
                        result = sender.send_video(str(self._target_uid), self._cnt, info=info)
                    elif self._ctx_type == "goods_card":
                        # 商品卡片尝试提取 goods_id
                        try:
                            import json
                            meta = json.loads(self._cnt) if self._cnt.startswith("{") else {}
                            goods_id = meta.get("goods_id", self._cnt)
                            result = sender.send_mallGoodsCard(str(self._target_uid), str(goods_id))
                        except Exception:
                            result = sender.send_text(str(self._target_uid), self._cnt)
                    else:
                        logger.info(f"[FORWARD] 调用 send_text: target={self._target_uid}")
                        result = sender.send_text(str(self._target_uid), self._cnt)

                    logger.info(f"[FORWARD] API 响应: success={isinstance(result, dict) and result.get('success')}, "
                                f"result_keys={list(result.keys()) if isinstance(result, dict) else type(result).__name__}")
                    if isinstance(result, dict):
                        inner = result.get("result", {})
                        error_code = inner.get("error_code")
                        if error_code:
                            logger.warning(f"[FORWARD] API 返回 error_code={error_code}, "
                                          f"error={inner.get('error')}")
                        # PDD 视频: result.result="fail" 但 success=True（参数错误等）
                        if inner.get("result") == "fail":
                            logger.warning(f"[FORWARD] API 返回 result=fail, reason={inner.get('reason')}")

                    # 发送成功需同时满足: success=True, result.result != "fail", 无 error_code
                    if isinstance(result, dict) and result.get("success"):
                        inner = result.get("result", {})
                        if inner.get("result") == "fail":
                            logger.error(f"[FORWARD] 发送失败(param error): reason={inner.get('reason')}")
                            self.done.emit(False, f"param error: {inner.get('reason')}")
                            return
                        if inner.get("error_code") and inner.get("error_code") != 0:
                            logger.error(f"[FORWARD] 发送失败(error_code): code={inner.get('error_code')}")
                            self.done.emit(False, str(inner))
                            return
                        # 持久化
                        try:
                            from services.message_persistence import message_persistence_service
                            # 对于视频/图片消息，持久化时携带 media_meta
                            out_media_meta = None
                            if self._ctx_type in ("video", "image") and self._media_meta:
                                out_media_meta = self._media_meta if isinstance(self._media_meta, str) else json.dumps(self._media_meta, ensure_ascii=False)
                            msg_dict = message_persistence_service.save_outbound_message(
                                shop_id=self._sid,
                                user_id=self._uid,
                                buyer_uid=self._target_uid,
                                reply_content=self._cnt,
                                reply_source="manual",
                                context_type=self._ctx_type,
                                media_meta=out_media_meta,
                            )
                            if msg_dict:
                                message_persistence_service.notify_new_message(msg_dict)
                                logger.info(f"[FORWARD] 持久化成功: buyer={self._target_uid}, type={self._ctx_type}")
                        except Exception as e:
                            logger.error(f"[FORWARD] 持久化失败: {e}")
                        self.done.emit(True, "")
                    else:
                        logger.error(f"[FORWARD] 发送失败: {result}")
                        self.done.emit(False, str(result))
                except Exception as e:
                    logger.error(f"[FORWARD] 转发异常: {e}", exc_info=True)
                    self.done.emit(False, str(e))

        worker = _ForwardWorker(shop_id, user_id, target_buyer_uid, content, context_type, media_meta=msg_data.get("media_meta"))
        worker.done.connect(
            lambda success, err: logger.info(f"转发消息完成: success={success}, err={err}")
            if not success else None
        )
        worker.start()
        # 保持引用防止 GC（使用列表支持并发 worker）
        self._forward_workers.append(worker)
        # 清理已完成的 worker
        def _cleanup_forward(_w=worker):
            try:
                if _w in self._forward_workers:
                    self._forward_workers.remove(_w)
                _w.deleteLater()
            except Exception:
                pass
        worker.done.connect(lambda *_: _cleanup_forward())

    # ------------------------------------------------------------------ #
    # 素材空间发送（图片空间 / 视频空间）
    # ------------------------------------------------------------------ #

    def _on_send_material(self, shop_id: str, user_id: str, item: dict, buyer_uid: str):
        """从素材空间选中素材 → 发送给当前买家（后台线程执行）"""
        if not item:
            return
        if not shop_id or not user_id or not buyer_uid:
            logger.warning("[MATERIAL] 缺少必要参数，取消发送")
            return

        worker = _MaterialSendWorker(shop_id, user_id, buyer_uid, item)
        worker.done.connect(self._on_material_send_done)
        worker.start()
        self._material_workers.append(worker)

        def _cleanup_material(_w=worker):
            try:
                if _w in self._material_workers:
                    self._material_workers.remove(_w)
                _w.deleteLater()
            except Exception:
                pass

        worker.done.connect(lambda *_: _cleanup_material())

    def _on_material_send_done(self, success: bool, err: str, msg_dict: dict):
        """素材发送结果回调（主线程）"""
        if success:
            logger.info("[MATERIAL] 素材发送成功")
            if msg_dict:
                try:
                    from services.message_persistence import message_persistence_service
                    message_persistence_service.notify_new_message(msg_dict)
                except Exception as e:
                    logger.warning(f"[MATERIAL] 通知新消息失败: {e}")
            return

        logger.error(f"[MATERIAL] 素材发送失败: {err}")
        try:
            InfoBar.error(
                title="素材发送失败",
                content=f"图片/视频未能发出：{err[:200]}",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=6000,
                parent=self,
            )
        except Exception:
            pass

    def cleanup(self):
        """清理资源"""
        try:
            self._day_watch_timer.stop()
        except Exception:
            pass
        try:
            from services.message_persistence import message_persistence_service
            message_persistence_service.signals.new_message.disconnect(self._on_new_message)
        except Exception:
            pass

    def changeEvent(self, event):
        if event.type() == QEvent.Type.PaletteChange:
            # 防抖：避免 setStyleSheet → PaletteChange → singleShot 乒乓循环
            if not getattr(self, '_palette_pending', False):
                self._palette_pending = True
                from PyQt6.QtCore import QTimer
                QTimer.singleShot(100, self._do_palette_update)
        super().changeEvent(event)

    def _do_palette_update(self):
        """实际执行调色板更新"""
        # 先执行更新，再延迟重置标志 —— 避免 setStyleSheet 触发的 PaletteChange
        # 在标志仍为 True 时被忽略，从而打破乒乓循环
        try:
            self._apply_theme()
        finally:
            QTimer.singleShot(200, self._reset_palette_pending)

    def _reset_palette_pending(self):
        """重置调色板更新标志"""
        self._palette_pending = False

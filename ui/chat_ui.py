"""
聊天记录页面 - 主界面
双栏布局：左侧会话列表 + 右侧聊天区域，顶部店铺筛选
"""

from PyQt6.QtCore import Qt, pyqtSignal, QEvent, QTimer, QThread
from PyQt6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QSplitter, QComboBox,
    QSizePolicy, QWidget,
)
from PyQt6.QtGui import QFont
from qfluentwidgets import BodyLabel, isDarkTheme

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
        logger.info("[ChatUI] __init__ 开始")
        self._init_ui()
        logger.info("[ChatUI] _init_ui 完成")
        self._apply_theme()
        logger.info("[ChatUI] _apply_theme 完成")
        # 不在 __init__ 里加载数据！改为 showEvent 触发，
        # 只有用户真正切到聊天 tab 时才创建卡片。
        # 之前在 __init__ 里 QTimer.singleShot(500, _initial_load)，
        # 导致 ChatUI 不可见时就创建了 30 个卡片 widget，
        # 与后台 PDD 消息循环线程竞争，切 tab 显示时触发堆崩���。
        self._data_loaded = False
        logger.info("[ChatUI] __init__ 完成，数据加载延迟到 showEvent")

    def _initial_load(self):
        """首次加载: 同时加载店铺列表和会话列表"""
        logger.info("[ChatUI] _initial_load 开始")
        self._load_shops()
        logger.info("[ChatUI] _load_shops 已启动")
        self._load_conversations(None)
        logger.info("[ChatUI] _load_conversations 已启动")

    def showEvent(self, event):
        """窗口显示时首次加载数据 (与知识库 tab 一致的模式)

        之前在 __init__ 里 QTimer.singleShot(500, _initial_load),
        导致 ChatUI 不可见时就创建了 30 个卡片 widget,
        与后台 PDD 消息循环线程竞争, 切 tab 显示时触发堆崩溃.
        改为 showEvent 触发, 只有用户真正切到聊天 tab 时才创建.
        """
        super().showEvent(event)
        if not self._data_loaded:
            self._data_loaded = True
            QTimer.singleShot(200, self._initial_load)

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
        self.chat_area.load_messages(shop_id, buyer_uid)
        logger.info("[ChatUI] _on_conversation_selected: chat_area.load_messages 返回")

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
        """发送手动回复"""
        try:
            from Channel.pinduoduo.utils.API.send_message import SendMessage
            sender = SendMessage(str(shop_id), str(user_id))
            result = sender.send_text(str(buyer_uid), text)

            if isinstance(result, dict) and result.get("success"):
                # 通知客服回复事件管理器：手动回复等同于人工客服回复，
                # 需要取消正在等待的AI处理流程
                try:
                    from Message.handlers.staff_reply_event import staff_reply_event_manager
                    staff_reply_event_manager.notify_staff_reply(buyer_uid)
                except Exception:
                    pass

                # 在后台线程持久化，避免阻塞主线程
                from PyQt6.QtCore import QThread

                class _PersistWorker(QThread):
                    done = pyqtSignal(dict)

                    def __init__(self, sid, uid, buid, txt):
                        super().__init__()
                        self._sid = sid
                        self._uid = uid
                        self._buid = buid
                        self._txt = txt

                    def run(self):
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

                worker = _PersistWorker(shop_id, user_id, buyer_uid, text)

                def _on_persist_done(msg_dict: dict):
                    from services.message_persistence import message_persistence_service
                    message_persistence_service.notify_new_message(msg_dict)

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
            else:
                logger.warning(f"发送手动回复失败: {result}")
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
                        # 从 media_meta 构造 PDD 要求的 info 字段
                        info = None
                        if self._media_meta:
                            try:
                                import json
                                meta = json.loads(self._media_meta) if isinstance(self._media_meta, str) else self._media_meta
                                raw_info = meta.get("raw_info")
                                if raw_info and isinstance(raw_info, dict):
                                    # PDD send_video 需要的 info 字段: preview + duration
                                    # 注意: download_url 会导致 40003；仅 preview+duration 时
                                    # result=ok 但视频静默不投递，因此视频转发按钮已禁用
                                    info = {}
                                    preview = raw_info.get("preview")
                                    if preview:
                                        info["preview"] = preview
                                    if raw_info.get("duration") is not None:
                                        info["duration"] = raw_info["duration"]
                                    logger.info(f"[FORWARD] 视频 info（preview+duration）: "
                                                f"duration={info.get('duration')}, "
                                                f"has_preview={bool(info.get('preview'))}")
                                else:
                                    # 回退：用旧字段拼凑（老版本入库的没有 raw_info）
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
                                        logger.warning(f"[FORWARD] 视频 info 用旧字段拼凑（缺少 raw_info）: "
                                                       f"cover_url={cover_url[:80]}..., duration={duration}")
                                    else:
                                        logger.warning(f"[FORWARD] media_meta 缺少 cover_url: {list(meta.keys())}")
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

    def cleanup(self):
        """清理资源"""
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

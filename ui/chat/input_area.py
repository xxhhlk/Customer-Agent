"""
输入区域组件 - 消息输入框 + 发送按钮
支持快捷语录自动联想：无需斜杠，输入即联想；"/" 为显式触发
"""

from PyQt6.QtCore import Qt, pyqtSignal, QEvent, QTimer, QPoint
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QTextEdit, QLabel, QApplication
from PyQt6.QtGui import QFont, QKeyEvent, QTextCursor, QInputMethodEvent
from qfluentwidgets import PrimaryPushButton, isDarkTheme

from ui.chat.slash_popup import SlashKnowledgePopup
from utils.logger_loguru import get_logger

logger = get_logger("InputArea")


class InputArea(QWidget):
    """聊天输入区域"""

    send_message = pyqtSignal(str)  # 发送消息信号

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("InputArea")
        self.setFixedHeight(120)
        self._slash_active = False  # 显式斜杠检索模式
        self._slash_start_pos = 0  # 斜杠位置
        self._query_start_pos = 0  # 当前联想片段起始位置（替换时用）
        self._min_query_len = 1  # 触发联想的最小字符数
        self._max_query_len = 12  # 参与匹配的查询长度（取末尾片段）
        self._init_ui()
        self._init_slash_popup()
        self._apply_theme()

    def _apply_theme(self):
        dark = isDarkTheme()
        bg = "#2b2b2b" if dark else "#f5f5f5"
        border = "#3a3a3a" if dark else "#e0e0e0"
        input_bg = "#1e1e1e" if dark else "#ffffff"
        input_color = "#e0e0e0" if dark else "#333333"
        self.setStyleSheet(f"""
            #InputArea {{
                background-color: {bg};
                border-top: 1px solid {border};
            }}
        """)
        self.text_edit.setStyleSheet(f"""
            QTextEdit {{
                background-color: {input_bg};
                color: {input_color};
                border: 1px solid {border};
                border-radius: 6px;
                padding: 6px 8px;
            }}
            QTextEdit:focus {{
                border-color: #4a90d9;
            }}
        """)

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 8)
        layout.setSpacing(4)

        # 输入框
        self.text_edit = QTextEdit()
        self.text_edit.setPlaceholderText("输入消息...  (直接输入即联想快捷语录，/ 显式触发)")
        self.text_edit.setFont(QFont("Microsoft YaHei", 10))
        self.text_edit.setMaximumHeight(68)
        self.text_edit.setMinimumHeight(60)
        self.text_edit.installEventFilter(self)
        layout.addWidget(self.text_edit)

        # 按钮行
        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(8)

        self.hint_label = QLabel()
        self.hint_label.setFont(QFont("Microsoft YaHei", 9))
        self.hint_label.setStyleSheet("color: #999;")
        btn_layout.addWidget(self.hint_label)
        btn_layout.addStretch()

        self.send_btn = PrimaryPushButton("发送 (Enter)")
        self.send_btn.setFixedWidth(110)
        self.send_btn.clicked.connect(self._on_send_clicked)
        btn_layout.addWidget(self.send_btn)

        layout.addLayout(btn_layout)

    def _init_slash_popup(self):
        """初始化斜杠检索浮窗"""
        self._slash_popup = SlashKnowledgePopup(self)
        self._slash_popup.item_selected.connect(self._on_slash_item_selected)
        self._slash_popup.position_requested.connect(self._update_popup_position)
        # 安装全局鼠标事件过滤器，用于点击浮窗外部时关闭浮窗
        self._install_global_click_filter()

    # ========== 斜杠检索逻辑 ==========

    def _check_slash_trigger(self):
        """检查是否需要触发/继续/退出快捷语录联想

        触发规则：
        - 显式：输入 "/" → 自动选中首项，Enter 直接插入
        - 隐式：普通输入（无需斜杠）→ 只展示候选，Enter 仍发送，
                需 ↑/↓ 或点击 / Tab 才插入
        """
        # 输入框失焦（如程序写入文本、AI 自动填充）时不联想
        if not self.text_edit.hasFocus():
            self._cancel_slash()
            return

        cursor = self.text_edit.textCursor()
        pos = cursor.position()
        text = self.text_edit.toPlainText()

        if self._slash_active:
            # 显式斜杠模式：校验斜杠是否还在
            if pos <= self._slash_start_pos or pos > len(text):
                self._cancel_slash()
                return
            if self._slash_start_pos >= len(text) or text[self._slash_start_pos] != '/':
                self._cancel_slash()
                return

            query = text[self._slash_start_pos + 1:pos]
            if '\n' in query:
                self._cancel_slash()
                return

            self._query_start_pos = self._slash_start_pos
            self._slash_popup.search(query, auto_select=True)
            if self._slash_popup.isVisible():
                self._update_popup_position()
            return

        # 未进入显式模式：以「光标所在行」为片段
        if pos <= 0 or pos > len(text):
            self._cancel_slash()
            return

        line_start = text.rfind('\n', 0, pos) + 1
        segment = text[line_start:pos]

        # 行内出现斜杠 → 转为显式模式（斜杠后内容作为 query）
        stripped = segment.lstrip()
        if stripped.startswith('/'):
            slash_idx = line_start + (len(segment) - len(stripped))
            self._slash_active = True
            self._slash_start_pos = slash_idx
            self._query_start_pos = slash_idx
            query = text[slash_idx + 1:pos]
            self._slash_popup.search(query, auto_select=True)
            if self._slash_popup.isVisible():
                self._update_popup_position()
            return

        # 隐式联想：普通文本
        query = segment.strip()
        if len(query) > self._max_query_len:
            query = query[-self._max_query_len:]
        if len(query) < self._min_query_len:
            self._cancel_slash()
            return

        self._query_start_pos = line_start
        self._slash_popup.search(query, auto_select=False)
        if self._slash_popup.isVisible():
            self._update_popup_position()

    def _update_popup_position(self):
        """将浮窗定位到输入框上方"""
        # 获取输入框在屏幕中的全局坐标
        text_rect = self.text_edit.rect()
        bottom_left = self.text_edit.mapToGlobal(text_rect.bottomLeft())
        popup_height = self._slash_popup.height()
        if popup_height <= 0:
            popup_height = 200  # 默认高度
        x = bottom_left.x()
        y = bottom_left.y() - popup_height - 4
        self._slash_popup.move(x, y)

    def _on_slash_item_selected(self, content: str):
        """选中知识库条目，替换当前联想片段（斜杠 + 关键词 或 整段输入）"""
        cursor = self.text_edit.textCursor()
        text = self.text_edit.toPlainText()

        start_pos = self._query_start_pos
        if start_pos < len(text):
            # 找到片段结束位置（到下一个换行或文本末尾）
            end_pos = len(text)
            cursor_pos = cursor.position()
            for i in range(cursor_pos, len(text)):
                if text[i] == '\n':
                    end_pos = i
                    break

            cursor.setPosition(start_pos)
            cursor.setPosition(end_pos, QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(content)

        self._slash_active = False
        # 抑制刚插入的内容，避免浮窗立刻对自身内容再次联想
        self._slash_popup.suppress(content[: self._max_query_len])
        self.text_edit.setFocus()

    def _cancel_slash(self):
        """取消联想模式"""
        self._slash_active = False
        self._slash_popup.cancel()
        self._slash_popup.hide()

    def _dismiss_popup(self):
        """用户主动关闭浮窗（Esc / 点击外部）：记住 query，避免立刻重弹"""
        self._slash_popup.dismiss()

    # ========== 事件处理 ==========

    def eventFilter(self, obj, event):
        """拦截键盘事件 + 输入法事件 + 全局鼠标点击（关闭浮窗）"""
        # 全局鼠标点击：如果点在浮窗外，关闭浮窗
        if event.type() == QEvent.Type.MouseButtonPress:
            if self._slash_popup.isVisible():
                # 检查点击是否在浮窗外
                popup_rect = self._slash_popup.geometry()
                if not popup_rect.contains(event.globalPosition().toPoint()):
                    self._dismiss_popup()
            return False

        # 处理输入法事件（中文输入法候选词确认后触发）
        if obj is self.text_edit and event.type() == QEvent.Type.InputMethod:
            # IME 输入完成后，文本已插入 QTextEdit，延迟检查斜杠触发
            result = super().eventFilter(obj, event)
            QTimer.singleShot(0, self._check_slash_trigger)
            return result

        if obj is self.text_edit and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            modifiers = event.modifiers()

            # 浮窗可见时的键盘导航
            if self._slash_popup.isVisible():
                if key == Qt.Key.Key_Down:
                    self._slash_popup.select_next()
                    return True
                elif key == Qt.Key.Key_Up:
                    # 已进入导航才拦截；否则放行，避免多行输入时无法上移光标
                    if self._slash_popup.is_nav_active():
                        self._slash_popup.select_prev()
                        return True
                elif key == Qt.Key.Key_Escape:
                    # ESC 关闭联想（记住 query，输入变化前不再弹）
                    self._dismiss_popup()
                    return True
                elif key == Qt.Key.Key_Tab:
                    # Tab 确认选择（有选中项才插入）
                    if self._slash_popup.confirm_selection():
                        return True
                elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    if not (modifiers & Qt.KeyboardModifier.ShiftModifier):
                        # 仅在已进入导航态时插入；否则继续走发送逻辑
                        if self._slash_popup.confirm_selection():
                            return True
                        self._slash_popup.hide()

            # 常规按键：Enter 发送 / Shift+Enter 换行
            if key == Qt.Key.Key_Return or key == Qt.Key.Key_Enter:
                if modifiers & Qt.KeyboardModifier.ShiftModifier:
                    # Shift+Enter → 换行，放行让 QTextEdit 处理
                    result = super().eventFilter(obj, event)
                    QTimer.singleShot(0, self._check_slash_trigger)
                    return result
                else:
                    # Enter → 发送（先关闭浮窗）
                    if self._slash_active:
                        self._cancel_slash()
                    self._on_send_clicked()
                    return True

            # 其他按键：放行让 QTextEdit 处理，然后延迟检查斜杠
            result = super().eventFilter(obj, event)
            QTimer.singleShot(0, self._check_slash_trigger)
            return result

        return super().eventFilter(obj, event)

    def _install_global_click_filter(self):
        """安装全局鼠标事件过滤器，用于点击外部关闭浮窗"""
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def _remove_global_click_filter(self):
        """移除全局鼠标事件过滤器"""
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

    def _on_send_clicked(self):
        """发送按钮 / Enter 触发"""
        # 发送前关闭联想浮窗
        self._cancel_slash()

        text = self.text_edit.toPlainText().strip()
        if not text:
            return
        self.send_message.emit(text)
        self.text_edit.clear()

    def set_enabled(self, enabled: bool):
        """启用/禁用输入框"""
        self.text_edit.setEnabled(enabled)
        self.send_btn.setEnabled(enabled)
        if not enabled:
            self.hint_label.setText("请先选择一个会话")
        else:
            self.hint_label.setText("")

    def clear(self):
        self.text_edit.clear()
        self._slash_active = False
        self._slash_popup.cancel()

    def invalidate_knowledge_cache(self):
        """知识库内容变更后调用，清空浮窗缓存"""
        self._slash_popup.invalidate_cache()

    def cleanup(self):
        """清理资源"""
        self._slash_popup.cleanup()
        self._remove_global_click_filter()

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
        try:
            self._apply_theme()
            self._slash_popup.refresh_theme()
        finally:
            QTimer.singleShot(200, self._reset_palette_pending)

    def _reset_palette_pending(self):
        """重置调色板更新标志"""
        self._palette_pending = False

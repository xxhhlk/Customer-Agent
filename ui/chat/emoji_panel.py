"""
表情面板（EmojiPopup）
======================
浮层，挂在聊天输入框上方。分组网格展示表情，点击后在光标处插入 ``[表情名]``。
- 未验证项（不在 verified 列表）淡色显示
- 支持「最近使用」分组
- 底部「从文本导入」：粘贴拼多多输入框复制的表情文本，自动提取官方全量
- 全部主线程完成，无 DB 操作

禁用 QMessageBox（项目铁律）：导入对话框使用自定义 QDialog。
"""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QTabWidget, QScrollArea, QWidget,
    QGridLayout, QToolButton, QDialog, QPlainTextEdit, QLabel,
)
from qfluentwidgets import PushButton, PrimaryPushButton, isDarkTheme

from ui.chat.emoji_data import (
    load_library, save_library, add_recent, import_from_text,
    EMOJI_UNICODE, placeholder,
)
from utils.logger_loguru import get_logger

logger = get_logger("EmojiPanel")


class EmojiImportDialog(QDialog):
    """从文本导入表情的对话框（替代不存在的 TextDialog）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("从文本导入表情")
        self.setMinimumWidth(360)
        self.resize(380, 240)

        layout = QVBoxLayout(self)
        hint = QLabel(
            "粘贴从拼多多输入框复制的表情文本，如 [微笑][撇嘴][色]... "
            "程序会自动提取、去重并覆盖写入「自定义」分组。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._edit = QPlainTextEdit()
        self._edit.setPlaceholderText("[微笑][撇嘴][色]...")
        layout.addWidget(self._edit, 1)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = PushButton("取消")
        ok_btn = PrimaryPushButton("确定")
        cancel_btn.clicked.connect(self.reject)
        ok_btn.clicked.connect(self.accept)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

    def text(self) -> str:
        return self._edit.toPlainText()


class EmojiPopup(QFrame):
    """表情浮窗：Popup + 无边框，Tab 分组 + 8 列网格"""

    emoji_selected = pyqtSignal(str)

    _GRID_COLS = 8

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("EmojiPopup")
        self.setFixedSize(420, 280)
        self._lib = load_library()
        self._build_ui()
        self.refresh_theme()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        self._tabs = QTabWidget()
        layout.addWidget(self._tabs, 1)

        import_btn = PushButton("从文本导入")
        import_btn.clicked.connect(self._on_import)
        layout.addWidget(import_btn)

        self._rebuild_tabs()

    def _rebuild_tabs(self):
        """重建所有分组标签"""
        self._tabs.clear()
        groups = list(self._lib.get("groups", []))
        recent = self._lib.get("recent", [])
        if recent:
            groups = [{"name": "最近", "items": recent}] + groups
        for g in groups:
            self._tabs.addTab(self._build_grid(g.get("items", [])), g.get("name", ""))

    def _build_grid(self, items: list[str]) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget()
        grid = QGridLayout(container)
        grid.setSpacing(4)
        grid.setContentsMargins(4, 4, 4, 4)

        verified = set(self._lib.get("verified", []))
        for i, name in enumerate(items):
            btn = QToolButton()
            btn.setText(EMOJI_UNICODE.get(name, name))
            btn.setToolTip(placeholder(name))
            btn.setFixedSize(42, 42)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            # 闭包捕获 name
            btn.clicked.connect(lambda _checked=False, n=name: self._on_pick(n))
            if name not in verified:
                btn.setProperty("_unverified", True)
            grid.addWidget(btn, i // self._GRID_COLS, i % self._GRID_COLS)

        # 让网格左对齐，右侧留白
        grid.setColumnStretch(self._GRID_COLS, 1)
        scroll.setWidget(container)
        return scroll

    def _on_pick(self, name: str):
        """点击表情：记最近使用 + 持久化 + 传出占位符 + 收起"""
        add_recent(self._lib, name)
        save_library(self._lib)
        self.emoji_selected.emit(placeholder(name))
        self.hide()

    def _on_import(self):
        """从文本导入官方全量列表"""
        dlg = EmojiImportDialog(self)
        if dlg.exec():
            raw = dlg.text()
            if raw.strip():
                added = import_from_text(self._lib, raw)
                save_library(self._lib)
                self._rebuild_tabs()
                logger.info(f"从文本导入表情完成，新增 {len(added)} 个")

    def reload(self):
        """外部（如主题切换/数据变更）重新加载库并重建"""
        self._lib = load_library()
        self._rebuild_tabs()

    def refresh_theme(self):
        """主题变化时刷新样式；未验证项淡色显示"""
        dark = isDarkTheme()
        bg = "#2b2b2b" if dark else "#ffffff"
        border = "#3a3a3a" if dark else "#e0e0e0"
        text_color = "#e0e0e0" if dark else "#333333"
        hover_bg = "#3a3a3a" if dark else "#f0f7ff"

        self.setStyleSheet(f"""
            EmojiPopup {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 8px;
            }}
            QTabWidget::pane {{
                border: none;
                background-color: {bg};
            }}
            QTabBar::tab {{
                color: {text_color};
                padding: 4px 8px;
                background: transparent;
            }}
            QTabBar::tab:selected {{
                color: #4a90d9;
            }}
            QScrollArea {{
                border: none;
                background-color: {bg};
            }}
            QToolButton {{
                background-color: transparent;
                border: none;
                font-size: 20px;
                color: {text_color};
                border-radius: 6px;
            }}
            QToolButton:hover {{
                background-color: {hover_bg};
            }}
            QToolButton[_unverified="true"] {{
                opacity: 0.5;
            }}
        """)

        # QTabWidget 子控件需在 setStyleSheet 后单独刷新
        self._tabs.setStyleSheet(f"background-color: {bg}; color: {text_color};")

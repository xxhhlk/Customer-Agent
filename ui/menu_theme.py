"""右键菜单（QMenu）主题样式

qfluentwidgets 的主题只管自家控件，原生 ``QMenu`` 会跟随系统深色模式渲染：
应用强制浅色时就会出现「黑底黑字」的菜单（关键词分组右键菜单踩过）。

统一在这里显式着色，凡是新建 ``QMenu`` 的地方调用 ``style_context_menu(menu)`` 即可。
"""

from PyQt6.QtWidgets import QMenu
from qfluentwidgets import isDarkTheme

_LIGHT_QSS = """
QMenu {
    background-color: #ffffff;
    border: 1px solid #e0e0e0;
    padding: 4px;
    color: #000000;
}
QMenu::item {
    padding: 6px 26px 6px 18px;
    background-color: transparent;
    color: #000000;
}
QMenu::item:selected {
    background-color: #0078d4;
    color: #ffffff;
}
QMenu::item:disabled {
    color: #9e9e9e;
}
QMenu::separator {
    height: 1px;
    background: #e8e8e8;
    margin: 4px 8px;
}
"""

_DARK_QSS = """
QMenu {
    background-color: #2b2b2b;
    border: 1px solid #3d3d3d;
    padding: 4px;
    color: #ffffff;
}
QMenu::item {
    padding: 6px 26px 6px 18px;
    background-color: transparent;
    color: #ffffff;
}
QMenu::item:selected {
    background-color: #0078d4;
    color: #ffffff;
}
QMenu::item:disabled {
    color: #7a7a7a;
}
QMenu::separator {
    height: 1px;
    background: #3d3d3d;
    margin: 4px 8px;
}
"""


def style_context_menu(menu: QMenu) -> QMenu:
    """给右键菜单套上当前主题的配色，返回入参便于链式调用"""
    menu.setStyleSheet(_DARK_QSS if isDarkTheme() else _LIGHT_QSS)
    return menu


__all__ = ["style_context_menu"]

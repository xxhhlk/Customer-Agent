"""
数据库模块初始化文件

此模块导出数据库管理器代理，确保整个应用程序使用 DI 容器中的同一实例。
通过 _DIProxy 提供向后兼容，底层转发到 DI 容器。
"""

from .db_manager import DatabaseManager, get_db_manager
from core.service_providers import _create_proxy

# 创建 DI 代理：现有代码 `from database import db_manager` 无需修改
# 同时支持 `from database.db_manager import db_manager`（通过 db_manager.py 导出的 get_db_manager）
db_manager = _create_proxy(DatabaseManager)

__all__ = ["get_db_manager", "db_manager", "DatabaseManager"]

"""
Coze API 限流器
固定窗口计数：按 from_uid（买家ID）全局计数，窗口到期后重置

基于上游新架构重实现
"""

import time
from dataclasses import dataclass, field
from typing import Dict, Optional
from threading import Lock
from utils.logger_loguru import get_logger

logger = get_logger(__name__)


@dataclass
class UserWindow:
    """单个用户的固定窗口"""
    count: int = 0                # 当前窗口内的请求计数
    window_start: float = 0.0     # 窗口起始时间（time.time()）
    window_size: float = 4 * 3600  # 窗口大小（秒），默认4小时
    max_requests: int = 10        # 窗口内最大请求数


class CozeRateLimiter:
    """
    Coze API 请求限流器

    固定窗口策略：
    - 从用户第一次请求开始计时，窗口内最多允许 max_requests 次请求
    - 窗口到期后自动重置计数并重新开始计时
    - 按 from_uid 全局计数，跨店铺共享
    """

    DEFAULT_WINDOW_SIZE = 4 * 3600   # 默认4小时
    DEFAULT_MAX_REQUESTS = 10        # 默认最多10次请求

    def __init__(self, window_size: float = None, max_requests: int = None):
        """
        初始化限流器

        Args:
            window_size: 窗口大小（秒），默认4小时
            max_requests: 窗口内最大请求数，默认10次
        """
        self._window_size = window_size or self.DEFAULT_WINDOW_SIZE
        self._max_requests = max_requests or self.DEFAULT_MAX_REQUESTS
        self._users: Dict[str, UserWindow] = {}
        self._lock = Lock()

    @property
    def window_size(self) -> float:
        """当前窗口大小（秒）"""
        return self._window_size

    @window_size.setter
    def window_size(self, value: float):
        self._window_size = value

    @property
    def max_requests(self) -> int:
        """当前最大请求数"""
        return self._max_requests

    @max_requests.setter
    def max_requests(self, value: int):
        self._max_requests = value

    def configure(self, window_size: float = None, max_requests: int = None):
        """
        动态更新限流配置（不会重置已有用户的窗口计数）

        Args:
            window_size: 窗口大小（秒）
            max_requests: 最大请求数
        """
        with self._lock:
            if window_size is not None:
                self._window_size = window_size
                logger.info(f"限流器窗口大小已更新为 {window_size} 秒")
            if max_requests is not None:
                self._max_requests = max_requests
                logger.info(f"限流器最大请求数已更新为 {max_requests}")

    def is_rate_limited(self, from_uid: str) -> bool:
        """
        检查用户是否被限流，并递增计数

        Args:
            from_uid: 买家ID

        Returns:
            True: 已超限，应返回兜底回复
            False: 未超限，计数已递增
        """
        now = time.time()

        with self._lock:
            user = self._users.get(from_uid)

            if user is None:
                # 首次请求：创建新窗口，计数=1
                self._users[from_uid] = UserWindow(
                    count=1,
                    window_start=now,
                    window_size=self._window_size,
                    max_requests=self._max_requests
                )
                logger.info(f"用户 {from_uid} 首次请求，创建新窗口")
                return False

            # 检查窗口是否已过期
            elapsed = now - user.window_start
            if elapsed >= user.window_size:
                # 窗口过期，重置
                user.count = 1
                user.window_start = now
                user.window_size = self._window_size
                user.max_requests = self._max_requests
                logger.info(f"用户 {from_uid} 窗口已过期（{elapsed:.0f}s），重置计数")
                return False

            # 窗口内：检查是否超限
            user.count += 1
            if user.count > self._max_requests:
                logger.warning(
                    f"用户 {from_uid} 已超限：{user.count}/{self._max_requests}，"
                    f"窗口剩余 {(user.window_size - elapsed) / 3600:.1f}h"
                )
                return True

            logger.debug(f"用户 {from_uid} 计数 {user.count}/{self._max_requests}")
            return False

    def get_user_status(self, from_uid: str) -> Optional[Dict]:
        """
        获取用户的限流状态（用于调试/展示）

        Args:
            from_uid: 买家ID

        Returns:
            dict: 用户状态信息，如果用户不存在返回None
        """
        with self._lock:
            user = self._users.get(from_uid)
            if not user:
                return None

            now = time.time()
            elapsed = now - user.window_start
            remaining = max(0, user.window_size - elapsed)

            return {
                'from_uid': from_uid,
                'count': user.count,
                'max_requests': user.max_requests,
                'window_size_hours': user.window_size / 3600,
                'elapsed_hours': elapsed / 3600,
                'remaining_hours': remaining / 3600,
                'is_limited': user.count > user.max_requests,
            }

    def get_all_users_count(self) -> int:
        """获取当前跟踪的用户数"""
        with self._lock:
            return len(self._users)

    def cleanup_expired_users(self, max_age_hours: float = 24) -> int:
        """
        清理过期的用户记录，释放内存

        Args:
            max_age_hours: 最大保留时间（小时），默认24小时

        Returns:
            int: 清理的用户数量
        """
        now = time.time()
        max_age_seconds = max_age_hours * 3600
        cleaned = 0

        with self._lock:
            expired_uids = []
            for uid, user in self._users.items():
                elapsed = now - user.window_start
                # 窗口过期超过max_age的才清理
                if elapsed > user.window_size + max_age_seconds:
                    expired_uids.append(uid)

            for uid in expired_uids:
                del self._users[uid]
                cleaned += 1

        if cleaned > 0:
            logger.info(f"限流器清理了 {cleaned} 个过期用户记录")

        return cleaned


# 全局限流器实例
coze_rate_limiter = CozeRateLimiter()

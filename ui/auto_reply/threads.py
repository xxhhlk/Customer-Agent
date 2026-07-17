# 后台线程模块
import asyncio
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QPixmap, QPainter, QPainterPath
from PyQt6.QtCore import Qt
import requests
from utils.logger_loguru import get_logger


class LogoLoaderThread(QThread):
    """异步加载Logo的线程

    ⚠️ 线程安全规则：
    - QPixmap/QPainter 不能在非 GUI 线程创建
    - 本线程只负责下载图片数据（bytes），通过信号传到主线程
    - 主线程回调中创建 QPixmap + QPainter 并绘制圆形头像
    - 信号携带 bytes 而非 QPixmap，避免 C++ 级 GUI 资源在线程间传递
    """
    # 信号改为携带 bytes（图片原始数据），而非 QPixmap
    logo_loaded = pyqtSignal(bytes)

    def __init__(self, url):
        super().__init__()
        self.url = url
        self.setObjectName("LogoLoaderThread")

    def run(self):
        """后台线程：只下载图片数据，不做任何 GUI 操作"""
        try:
            response = requests.get(self.url, timeout=10)
            response.raise_for_status()
            # 只传递 bytes，不在后台线程创建 QPixmap
            self.logo_loaded.emit(response.content)
        except Exception as e:
            get_logger().error(f"Failed to load logo from {self.url}: {e}")
            self.logo_loaded.emit(b'')  # 失败时发射空 bytes


class AutoReplyThread(QThread):
    """自动回复线程 - 每个账号独立的WebSocket连接线程"""

    connection_success = pyqtSignal()  # 连接成功信号
    connection_failed = pyqtSignal(str)  # 连接失败信号

    # 失败后自动重启配置
    _MAX_RESTART = 3       # 最大重启次数
    _RESTART_DELAY = 60   # 重启间隔（秒）

    def __init__(self, account_data: dict):
        super().__init__()
        self.account_data = account_data
        self.channel = None
        self.logger = get_logger("AutoReplyThread")
        self.loop = None
        self._stop_requested = False
        self._restart_count = 0
        # 设置线程对象名，便于调试
        self.setObjectName(f"AutoReplyThread-{account_data.get('username', 'unknown')}")

    def run(self):
        """启动后端 PDDChannel 引擎，失败后自动重启"""
        from Channel.pinduoduo.pdd_channel import PDDChannel

        while not self._stop_requested:
            try:
                # 为当前线程创建并设置新的事件循环
                self.loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self.loop)

                # 创建 PDDChannel 实例
                self.channel = PDDChannel()
                # 将事件循环引用传递给PDDChannel，使其能线程安全地操作事件循环
                self.channel.loop = self.loop

                # 定义成功和失败的回调函数
                # 注意：这些回调在 asyncio 事件循环中被调用（非主 Qt 线程），
                # 直接 emit 是安全的：Qt 自动使用 QueuedConnection 将信号投递到主线程
                def on_success():
                    self.connection_success.emit()

                def on_failure(error_msg):
                    self.connection_failed.emit(error_msg)

                # 启动引擎，并传递回调
                task = self.loop.create_task(
                    self.channel.start_account(
                        shop_id=self.account_data['shop_id'],
                        user_id=self.account_data['user_id'],
                        on_success=on_success,
                        on_failure=on_failure
                    )
                )

                # 运行事件循环，直到任务完成或被停止
                try:
                    self.loop.run_until_complete(task)
                except asyncio.CancelledError:
                    self.logger.debug("主任务被取消")
                except RuntimeError:
                    # loop.stop() 被调用时会触发此异常
                    self.logger.debug("事件循环被外部停止")
                except Exception as e:
                    self.logger.error(f"主任务执行出错: {e}")

            except Exception as e:
                self.logger.error(f"自动回复线程启动失败: {e}")
                self.connection_failed.emit(str(e))
            finally:
                # 确保事件循环正确关闭
                if self.loop and not self.loop.is_closed():
                    try:
                        # 取消所有未完成的任务
                        pending = asyncio.all_tasks(self.loop)
                        for task in pending:
                            if not task.done():
                                task.cancel()

                        # 运行事件循环让取消的任务完成清理
                        if pending:
                            try:
                                self.loop.run_until_complete(
                                    asyncio.gather(*pending, return_exceptions=True)
                                )
                            except Exception:
                                pass

                        # 关闭事件循环
                        self.loop.close()
                        self.logger.debug("事件循环已关闭")
                    except Exception as e:
                        self.logger.error(f"关闭事件循环失败: {e}")

                self.channel = None
                self.loop = None

            # 检查是否需要重启
            if self._stop_requested:
                break

            if self._restart_count < self._MAX_RESTART:
                self._restart_count += 1
                self.logger.info(
                    f"连接结束，{self._RESTART_DELAY}秒后自动重启 "
                    f"(第{self._restart_count}/{self._MAX_RESTART}次): "
                    f"{self.account_data.get('username', 'unknown')}"
                )
                # 可中断的延迟等待
                import time as _time
                for _ in range(self._RESTART_DELAY):
                    if self._stop_requested:
                        break
                    _time.sleep(1)
            else:
                self.logger.error(
                    f"已达到最大重启次数 ({self._MAX_RESTART})，停止重试: "
                    f"{self.account_data.get('username', 'unknown')}"
                )
                break

    def stop(self):
        """停止后端引擎 - 线程安全版本

        关键时序：
        1. 先设停止标志（_stop_requested）— 让 run() 内部循环不会再重启新 loop
        2. channel.request_stop() — 通知业务层停止，内部 _stop_event 的 set 是线程安全的
        3. 等一个微小延时（10ms）让 channel 内部的协程有机会进入收尾，
           避免在 channel 仍持有 loop 强引用时 loop 已被 close
        4. 最后 loop.call_soon_threadsafe(loop.stop) — 让事件循环自身优雅退出

        千万不要先 close loop 再让 channel 写：channel 协程若在已 close 的 loop 上
        调度回调会触发 access violation（C 级 trap，Python except 抓不住）。
        """
        try:
            self._stop_requested = True
            self.requestInterruption()

            # 通知channel停止（设置_stop_event）
            if self.channel:
                self.channel.request_stop()

            # 线程安全地停止事件循环
            # call_soon_threadsafe 是唯一安全的跨线程事件循环操作
            if self.loop and not self.loop.is_closed():
                try:
                    self.loop.call_soon_threadsafe(self.loop.stop)
                    self.logger.debug("已请求停止事件循环")
                except RuntimeError:
                    # 事件循环已经关闭
                    pass

        except Exception as e:
            self.logger.error(f"停止自动回复线程失败: {e}")

    def is_running(self) -> bool:
        """检查线程是否在运行"""
        # 实际的运行状态由 PDDChannel 内部管理，这里仅表示线程是否已启动
        return self.isRunning()

    def wait(self, msecs: int = 10000) -> bool:
        """等待线程结束"""
        # 调用父类的wait方法
        return super().wait(msecs)


class SetStatusThread(QThread):
    """设置账号状态的线程"""

    status_set_success = pyqtSignal(dict, int)  # 设置成功信号
    status_set_failed = pyqtSignal(dict, str)   # 设置失败信号

    def __init__(self, account_data: dict, target_status: int):
        super().__init__()
        self.account_data = account_data
        self.target_status = target_status
        self.logger = get_logger()
        self.setObjectName("SetStatusThread")

    def run(self):
        """在后台线程中执行状态更新"""
        from Channel.pinduoduo.utils.API.Set_up_online import AccountMonitor
        from database.db_manager import db_manager

        try:
            # 1. 调用API设置平台状态
            cookies = self.account_data.get("cookies")
            if not cookies:
                raise ValueError("账号缺少cookies，无法设置状态")

            account_monitor = AccountMonitor(
                cookies=cookies,
                shop_id=self.account_data.get("shop_id"),
                user_id=self.account_data.get("user_id"),
                channel_name=self.account_data.get("channel_name", "pinduoduo")
            )

            api_success = account_monitor.set_csstatus(str(self.target_status))

            if not api_success:
                # API调用失败
                self.status_set_failed.emit(self.account_data, "平台状态设置失败")
                return

            # 2. 更新数据库状态
            db_success = db_manager.update_account_status(
                channel_name=self.account_data["channel_name"],
                shop_id=self.account_data["shop_id"],
                user_id=self.account_data["user_id"],
                status=self.target_status
            )

            if db_success:
                # 发射成功信号
                self.status_set_success.emit(self.account_data, self.target_status)
            else:
                # 发射失败信号
                self.status_set_failed.emit(self.account_data, "数据库状态更新失败")

        except KeyError:
            # 如果缺少 'user_id' 等关键信息
            self.status_set_failed.emit(self.account_data, "账号数据不完整，无法设置状态")
        except Exception as e:
            # 其他异常
            self.status_set_failed.emit(self.account_data, str(e))


__all__ = ['LogoLoaderThread', 'AutoReplyThread', 'SetStatusThread']

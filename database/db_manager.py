import os
import json
import asyncio
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.pool import QueuePool
from sqlalchemy.exc import SQLAlchemyError
from typing import List, Dict, Any, Optional, Union
from utils.logger import get_logger
from utils.resource_manager import ThreadResourceManager
from database.models import Base, Channel, Shop, Account, Keyword, KeywordGroup

class DatabaseManager:
    """数据库管理类，提供数据库操作的封装 - 优化版本支持连接池"""
    _instance = None

    def __new__(cls, *args, **kwargs):
        """单例模式实现"""
        if cls._instance is None:
            cls._instance = super(DatabaseManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, db_path: str = 'database/channel_shop.db', pool_size: int = 10, max_overflow: int = 20):
        """初始化数据库连接 - 优化版本支持连接池

        Args:
            db_path: 数据库文件路径
            pool_size: 连接池大小
            max_overflow: 连接池溢出大小
        """
        if self._initialized:
            return

        # 确保数据库目录存在
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        # 创建数据库引擎 - 配置连接池
        self.engine = create_engine(
            f'sqlite:///{db_path}',
            poolclass=QueuePool,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,  # 连接健康检查
            pool_recycle=3600,   # 连接回收时间（秒）
            echo=False  # 生产环境关闭SQL日志
        )

        # 使用scoped_session确保线程安全
        self.Session = scoped_session(sessionmaker(bind=self.engine))

        # 创建表结构
        Base.metadata.create_all(self.engine)

        self._initialized = True
        self.logger = get_logger()

        # 资源管理
        self.resource_manager = ThreadResourceManager()
        self.resource_manager.register_thread_pool(
            self.engine.pool,
            f"数据库连接池(size={pool_size}, overflow={max_overflow})"
        )

        # 初始化数据库
        self.init_db()

        self.logger.info(f"数据库连接池已初始化: pool_size={pool_size}, max_overflow={max_overflow}")

    def __del__(self):
        """析构函数，确保连接池被正确关闭"""
        try:
            if hasattr(self, 'Session'):
                self.Session.remove()
            if hasattr(self, 'resource_manager'):
                asyncio.create_task(self.resource_manager.cleanup_all())
        except Exception as e:
            if hasattr(self, 'logger'):
                self.logger.error(f"清理数据库资源失败: {e}")

    def init_db(self):
        """初始化渠道信息 + 数据库迁移"""
        channel_name = "pinduoduo"
        description = "拼多多"
        self.add_channel(channel_name, description)

        # 数据库迁移：为keywords表添加group_id列
        self._migrate_keywords_table()


    def _migrate_keywords_table(self):
        """迁移keywords表：添加group_id列，将孤立关键词归入默认分组"""
        try:
            import sqlite3
            conn = sqlite3.connect(self.engine.url.database)
            cursor = conn.cursor()

            # 检查keywords表是否存在group_id列
            cursor.execute("PRAGMA table_info(keywords)")
            columns = [col[1] for col in cursor.fetchall()]

            if 'group_id' not in columns:
                self.logger.info("正在迁移keywords表：添加group_id列...")

                # 添加group_id列，默认值为0（临时值）
                cursor.execute("ALTER TABLE keywords ADD COLUMN group_id INTEGER DEFAULT 0")
                conn.commit()

                # 创建默认的"转人工"分组
                cursor.execute(
                    "INSERT INTO keyword_groups (group_name, reply, is_transfer) "
                    "VALUES ('转人工', '稍等，我帮您转接人工客服，请稍候~', 1)"
                )
                group_id = cursor.lastrowid

                # 将所有孤立关键词归入该分组
                cursor.execute("UPDATE keywords SET group_id = ? WHERE group_id = 0 OR group_id IS NULL",
                             (group_id,))
                affected = cursor.rowcount

                conn.commit()
                self.logger.info(f"keywords表迁移完成：{affected}个关键词已归入默认分组(ID={group_id})")

            # 检查keyword_groups表是否有pass_to_ai列
            cursor.execute("PRAGMA table_info(keyword_groups)")
            kg_columns = [col[1] for col in cursor.fetchall()]
            if 'pass_to_ai' not in kg_columns:
                self.logger.info("正在迁移keyword_groups表：添加pass_to_ai列...")
                cursor.execute("ALTER TABLE keyword_groups ADD COLUMN pass_to_ai INTEGER DEFAULT 0")
                conn.commit()
                self.logger.info("keyword_groups表迁移完成：已添加pass_to_ai列")

            conn.close()
        except Exception as e:
            self.logger.error(f"迁移keywords表失败: {e}")

    def get_session(self):
        """获取数据库会话 - 线程安全版本"""
        return self.Session()

    def get_connection_pool_stats(self) -> Dict[str, Any]:
        """获取连接池统计信息"""
        if hasattr(self.engine.pool, 'status'):
            return {
                'pool_size': self.engine.pool.size(),
                'checked_in': self.engine.pool.checkedin(),
                'checked_out': self.engine.pool.checkedout(),
                'overflow': self.engine.pool.overflow(),
                'invalid': self.engine.pool.invalid()
            }
        return {}

    async def close_all_connections(self):
        """关闭所有数据库连接"""
        try:
            self.Session.remove()
            self.engine.dispose()
            self.logger.info("所有数据库连接已关闭")
        except Exception as e:
            self.logger.error(f"关闭数据库连接失败: {e}")
    
    # 渠道相关操作
    def add_channel(self, channel_name: str, description: str = None) -> bool:
        """添加渠道
        
        Args:
            channel_name: 渠道名称
            description: 渠道描述
            
        Returns:
            bool: 是否添加成功
        """
        session = self.get_session()
        try:
            # 检查渠道是否已存在
            existing = session.query(Channel).filter(Channel.channel_name == channel_name).first()
            if existing:
                return True
                
            # 创建新渠道
            channel = Channel(channel_name=channel_name, description=description)
            session.add(channel)
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"添加渠道失败: {str(e)}")
            return False
        finally:
            session.close()
    
    def get_channel(self, channel_name: str) -> Optional[Dict[str, Any]]:
        """获取渠道信息
        
        Args:
            channel_name: 渠道名称
            
        Returns:
            Optional[Dict]: 渠道信息或None
        """
        session = self.get_session()
        try:
            channel = session.query(Channel).filter(Channel.channel_name == channel_name).first()
            if not channel:
                return None
                
            return {
                'id': channel.id,
                'channel_name': channel.channel_name,
                'description': channel.description
            }
        except SQLAlchemyError as e:
            self.logger.error(f"获取渠道失败: {str(e)}")
            return None
        finally:
            session.close()
    
    def get_all_channels(self) -> List[Dict[str, Any]]:
        """获取所有渠道
        
        Returns:
            List[Dict]: 渠道列表
        """
        session = self.get_session()
        try:
            channels = session.query(Channel).all()
            return [
                {
                    'id': channel.id,
                    'channel_name': channel.channel_name,
                    'description': channel.description
                }
                for channel in channels
            ]
        except SQLAlchemyError as e:
            self.logger.error(f"获取渠道列表失败: {str(e)}")
            return []
        finally:
            session.close()
    
    def delete_channel(self, channel_name: str) -> bool:
        """删除渠道
        
        Args:
            channel_name: 渠道名称
            
        Returns:
            bool: 是否删除成功
        """
        session = self.get_session()
        try:
            channel = session.query(Channel).filter(Channel.channel_name == channel_name).first()
            if not channel:
                self.logger.warning(f"渠道 {channel_name} 不存在")
                return False
                
            session.delete(channel)
            session.commit()
            self.logger.info(f"成功删除渠道: {channel_name}")
            return True
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"删除渠道失败: {str(e)}")
            return False
        finally:
            session.close()
    
    # 店铺相关操作
    def add_shop(self, channel_name: str, shop_id: str, shop_name: str, shop_logo: str, description: str = None) -> bool:
        """添加店铺
        
        Args:
            channel_name: 渠道名称
            shop_id: 店铺ID
            shop_name: 店铺名称
            shop_logo: 店铺logo
            description: 店铺描述
            
        Returns:
            bool: 是否添加成功
        """
        session = self.get_session()
        try:
            # 获取对应渠道
            channel = session.query(Channel).filter(Channel.channel_name == channel_name).first()
            if not channel:
                self.logger.error(f"添加店铺失败: 渠道 {channel_name} 不存在")
                return False
            
            # 检查店铺是否已存在
            existing = session.query(Shop).filter(
                Shop.channel_id == channel.id,
                Shop.shop_id == shop_id
            ).first()
            
            if existing:
                self.logger.warning(f"店铺 {shop_id} 已存在于渠道 {channel_name}")
                return False
            
            # 创建新店铺
            shop = Shop(
                channel_id=channel.id,
                shop_id=shop_id,
                shop_name=shop_name,
                shop_logo=shop_logo,
                description=description
            )
            
            session.add(shop)
            session.commit()
            self.logger.info(f"成功添加店铺: {shop_name}({shop_id}) 到渠道 {channel_name}")
            return True
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"添加店铺失败: {str(e)}")
            return False
        finally:
            session.close()
    
    def get_shop(self, channel_name: str, shop_id: str) -> Optional[Dict[str, Any]]:
        """获取店铺信息
        
        Args:
            channel_name: 渠道名称
            shop_id: 店铺ID
            
        Returns:
            Optional[Dict]: 店铺信息或None
        """
        session = self.get_session()
        try:
            channel = session.query(Channel).filter(Channel.channel_name == channel_name).first()
            if not channel:
                return None
                
            shop = session.query(Shop).filter(
                Shop.channel_id == channel.id,
                Shop.shop_id == shop_id
            ).first()
            
            if not shop:
                return None
                
            return {
                'id': shop.id,
                'channel_id': shop.channel_id,
                'channel_name': channel_name,
                'shop_id': shop.shop_id,
                'shop_name': shop.shop_name,
                'shop_logo': shop.shop_logo,
                'description': shop.description,
            }
        except SQLAlchemyError as e:
            self.logger.error(f"获取店铺失败: {str(e)}")
            return None
        finally:
            session.close()
    
    def get_shops_by_channel(self, channel_name: str) -> List[Dict[str, Any]]:
        """获取指定渠道下的所有店铺
        
        Args:
            channel_name: 渠道名称
            
        Returns:
            List[Dict]: 店铺列表
        """
        session = self.get_session()
        try:
            channel = session.query(Channel).filter(Channel.channel_name == channel_name).first()
            if not channel:
                return []
                
            shops = session.query(Shop).filter(Shop.channel_id == channel.id).all()
            return [
                {
                    'id': shop.id,
                    'channel_id': shop.channel_id,
                    'channel_name': channel_name,
                    'shop_id': shop.shop_id,
                    'shop_name': shop.shop_name,
                    'shop_logo': shop.shop_logo,
                    'description': shop.description
                }
                for shop in shops
            ]
        except SQLAlchemyError as e:
            self.logger.error(f"获取店铺列表失败: {str(e)}")
            return []
        finally:
            session.close()
    
    def update_shop_info(self, channel_name: str, shop_id: str, shop_name: str = None, shop_logo: str = None, description: str = None) -> bool:
        """更新店铺信息
        
        Args:
            channel_name: 渠道名称
            shop_id: 新的店铺ID
            shop_name: 新的店铺名称
            shop_logo: 新的店铺logo
            description: 新的店铺描述
            
        Returns:
            bool: 是否更新成功
        """
        session = self.get_session()
        try:
            channel = session.query(Channel).filter(Channel.channel_name == channel_name).first()
            if not channel:
                return False
                
            shop = session.query(Shop).filter(
                Shop.channel_id == channel.id,
                Shop.shop_id == shop_id
            ).first()
            
            if not shop:
                return False
            
            if shop_id is not None:
                shop.shop_id = shop_id
            if shop_name is not None:
                shop.shop_name = shop_name
            if shop_logo is not None:
                shop.shop_logo = shop_logo
            if description is not None:
                shop.description = description
                
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"更新店铺信息失败: {str(e)}")
            return False
        finally:
            session.close()
    
    def delete_shop(self, channel_name: str, shop_id: str) -> bool:
        """删除店铺
        
        Args:
            channel_name: 渠道名称
            shop_id: 店铺ID
        Returns:
            bool: 是否删除成功
        """
        session = self.get_session()
        try:
            channel = session.query(Channel).filter(Channel.channel_name == channel_name).first()
            if not channel:
                return False
                
            shop = session.query(Shop).filter(
                Shop.channel_id == channel.id,
                Shop.shop_id == shop_id
            ).first()
            
            if not shop:
                return False
                
            session.delete(shop)
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"删除店铺失败: {str(e)}")
            return False
        finally:
            session.close()

    # 账号相关操作
    def add_account(self, channel_name: str, shop_id: str, user_id: str, username: str, password: str, cookies: str = None) -> bool:
        """添加账号
        
        Args:
            channel_name: 渠道名称
            shop_id: 店铺ID
            user_id: 用户ID
            username: 登录用户名
            password: 登录密码
            cookies: cookies JSON字符串
            
        Returns:
            bool: 是否添加成功
        """
        session = self.get_session()
        try:
            # 获取对应店铺
            channel = session.query(Channel).filter(Channel.channel_name == channel_name).first()
            if not channel:
                self.logger.error(f"添加账号失败: 渠道 {channel_name} 不存在")
                return False
                
            shop = session.query(Shop).filter(
                Shop.channel_id == channel.id,
                Shop.shop_id == shop_id
            ).first()
            
            if not shop:
                self.logger.error(f"添加账号失败: 店铺 {shop_id} 不存在")
                return False
            
            # 检查账号是否已存在
            existing = session.query(Account).filter(
                Account.shop_id == shop.id,
                Account.username == username
            ).first()
            
            if existing:
                self.logger.warning(f"账号 {username} 已存在于店铺 {shop_id}")
                return False
            
            # 创建新账号
            account = Account(
                shop_id=shop.id,
                user_id=user_id,
                username=username,
                password=password,
                cookies=cookies,
                status=None
            )
            
            session.add(account)
            session.commit()
            self.logger.info(f"成功添加账号: {username} 到店铺 {shop_id}")
            return True
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"添加账号失败: {str(e)}")
            return False
        finally:
            session.close()
    
    def get_account(self, channel_name: str, shop_id: str,user_id: str) -> Optional[Dict[str, Any]]:
        """获取账号信息
        
        Args:
            channel_name: 渠道名称
            shop_id: 店铺ID
            user_id: 用户ID
        Returns:
            Optional[Dict]: 账号信息或None
        """
        session = self.get_session()
        try:
            channel = session.query(Channel).filter(Channel.channel_name == channel_name).first()
            if not channel:
                self.logger.warning(f"未找到渠道: {channel_name}")
                return None
                
            shop = session.query(Shop).filter(
                Shop.channel_id == channel.id,
                Shop.shop_id == shop_id
            ).first()
            
            if not shop:
                self.logger.warning(f"未找到店铺: {shop_id} (渠道: {channel_name})")
                return None
                
            account = session.query(Account).filter(
                Account.shop_id == shop.id,
                Account.user_id == user_id
            ).first()
            
            if not account:
                self.logger.warning(f"未找到账户: {user_id} (店铺 ID: {shop_id})")
                return None
                
            return {
                'id': account.id,
                'shop_id': account.shop_id,
                'user_id': account.user_id,
                'username': account.username,
                'password': account.password,
                'cookies': account.cookies,
                'status': account.status
            }
        except SQLAlchemyError as e:
            self.logger.error(f"获取账号失败: {str(e)}")
            return None
        finally:
            session.close()
    
    def update_account_info(self, channel_name: str, shop_id: str, user_id: str, username: Optional[str] = None, password: Optional[str] = None, cookies: Optional[str] = None, status: Optional[int] = None) -> bool:
        """更新账号信息
        
        Args:
            channel_name: 渠道名称
            shop_id: 店铺ID
            user_id: 用户ID
            username: 登录用户名
            password: 登录密码
            cookies: cookies JSON字符串
            status: 账号状态
        Returns:
            bool: 是否更新成功
        """
        session = self.get_session()
        try:
            channel = session.query(Channel).filter(Channel.channel_name == channel_name).first()
            if not channel:
                self.logger.error(f"更新账号失败: 渠道 {channel_name} 不存在")
                return False
                
            shop = session.query(Shop).filter(
                Shop.channel_id == channel.id,
                Shop.shop_id == shop_id
            ).first()
            
            if not shop:
                self.logger.error(f"更新账号失败: 店铺 {shop_id} 不存在于渠道 {channel_name}")
                return False
                
            account = session.query(Account).filter(
                Account.shop_id == shop.id,
                Account.user_id == user_id
            ).first()
            
            if not account:
                self.logger.error(f"更新账号失败: 账号 {user_id} 不存在于店铺 {shop_id}")
                return False
                
            # 更新账号信息
            if username is not None:
                account.username = username
            if password is not None:
                account.password = password
            if cookies is not None:
                account.cookies = cookies
            if status is not None:
                account.status = status

            session.commit()
            self.logger.info(f"成功更新账号信息: {username} (用户ID: {user_id})")
            return True
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"更新账号信息失败: {str(e)}")
            return False
        finally:
            session.close()
                

    def get_accounts_by_shop(self, channel_name: str, shop_id: str) -> List[Dict[str, Any]]:
        """获取指定店铺下的所有账号
        
        Args:
            channel_name: 渠道名称
            shop_id: 店铺ID
            
        Returns:
            List[Dict]: 账号列表
        """
        session = self.get_session()
        try:
            channel = session.query(Channel).filter(Channel.channel_name == channel_name).first()
            if not channel:
                return []
                
            shop = session.query(Shop).filter(
                Shop.channel_id == channel.id,
                Shop.shop_id == shop_id
            ).first()
            
            if not shop:
                return []
                
            accounts = session.query(Account).filter(Account.shop_id == shop.id).all()
            return [
                {
                    'id': account.id,
                    'shop_id': account.shop_id,
                    'user_id': account.user_id,
                    'username': account.username,
                    'password': account.password,
                    'cookies': account.cookies,
                    'status': account.status
                }
                for account in accounts
            ]
        except SQLAlchemyError as e:
            self.logger.error(f"获取账号列表失败: {str(e)}")
            return []
        finally:
            session.close()
    
    def update_account_status(self, channel_name: str, shop_id: str, user_id: str, status: int) -> bool:
        """更新账号状态
        
        Args:
            channel_name: 渠道名称
            shop_id: 店铺ID
            user_id: 用户ID
            status: 状态值 (0-未验证, 1-正常, 2-异常)
            
        Returns:
            bool: 是否更新成功
        """
        session = self.get_session()
        try:
            channel = session.query(Channel).filter(Channel.channel_name == channel_name).first()
            if not channel:
                return False
                
            shop = session.query(Shop).filter(
                Shop.channel_id == channel.id,
                Shop.shop_id == shop_id
            ).first()
            
            if not shop:
                return False
                
            account = session.query(Account).filter(
                Account.shop_id == shop.id,
                Account.user_id == user_id
            ).first()
            
            if not account:
                return False
                
            account.status = status
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"更新账号状态失败: {str(e)}")
            return False
        finally:
            session.close()
    
    def update_account_cookies(self, channel_name: str, shop_id: str, user_id: str, cookies: str) -> bool:
        """更新账号cookies
        
        Args:
            channel_name: 渠道名称
            shop_id: 店铺ID
            user_id: 用户ID
            cookies: cookies JSON字符串
            
        Returns:
            bool: 是否更新成功
        """
        session = self.get_session()
        try:
            channel = session.query(Channel).filter(Channel.channel_name == channel_name).first()
            if not channel:
                return False
                
            shop = session.query(Shop).filter(
                Shop.channel_id == channel.id,
                Shop.shop_id == shop_id
            ).first()
            
            if not shop:
                return False
                
            account = session.query(Account).filter(
                Account.shop_id == shop.id,
                Account.user_id == user_id
            ).first()
            
            if not account:
                return False
                
            account.cookies = cookies
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"更新账号cookies失败: {str(e)}")
            return False
        finally:
            session.close()
    
    def delete_account(self, channel_name: str, shop_id: str, user_id: str) -> bool:
        """删除账号
        
        Args:
            channel_name: 渠道名称
            shop_id: 店铺ID
            user_id: 用户ID
            
        Returns:
            bool: 是否删除成功
        """
        session = self.get_session()
        try:
            channel = session.query(Channel).filter(Channel.channel_name == channel_name).first()
            if not channel:
                return False
                
            shop = session.query(Shop).filter(
                Shop.channel_id == channel.id,
                Shop.shop_id == shop_id
            ).first()
            
            if not shop:
                return False
                
            account = session.query(Account).filter(
                Account.shop_id == shop.id,
                Account.user_id == user_id
            ).first()
            
            if not account:
                return False
                
            session.delete(account)
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"删除账号失败: {str(e)}")
            return False
        finally:
            session.close()

    # ========== 关键词分组相关操作 ==========
    def add_keyword_group(self, group_name: str, reply: str = None, is_transfer: int = 0, pass_to_ai: int = 0) -> bool:
        """添加关键词分组

        Args:
            group_name: 分组名称
            reply: 自动回复内容，为空则仅匹配不回复
            is_transfer: 是否转人工 (0-仅回复, 1-转人工)
            pass_to_ai: 是否传递给AI (0-不传递, 1-匹配后剩余内容传给AI)

        Returns:
            bool: 是否添加成功
        """
        session = self.get_session()
        try:
            group = KeywordGroup(group_name=group_name, reply=reply, is_transfer=is_transfer, pass_to_ai=pass_to_ai)
            session.add(group)
            session.commit()
            self.logger.info(f"成功添加关键词分组: {group_name}")
            return True
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"添加关键词分组失败: {str(e)}")
            return False
        finally:
            session.close()

    def get_keyword_group(self, group_id: int) -> Optional[Dict[str, Any]]:
        """获取关键词分组（含关键词列表）"""
        session = self.get_session()
        try:
            group = session.query(KeywordGroup).filter(KeywordGroup.id == group_id).first()
            if not group:
                return None
            return {
                'id': group.id,
                'group_name': group.group_name,
                'reply': group.reply,
                'is_transfer': group.is_transfer,
                'pass_to_ai': group.pass_to_ai,
                'keywords': [kw.keyword for kw in group.keywords]
            }
        except SQLAlchemyError as e:
            self.logger.error(f"获取关键词分组失败: {str(e)}")
            return None
        finally:
            session.close()

    def get_all_keyword_groups(self) -> List[Dict[str, Any]]:
        """获取所有关键词分组（含关键词列表）"""
        session = self.get_session()
        try:
            groups = session.query(KeywordGroup).all()
            return [
                {
                    'id': g.id,
                    'group_name': g.group_name,
                    'reply': g.reply,
                    'is_transfer': g.is_transfer,
                    'pass_to_ai': g.pass_to_ai,
                    'keywords': [kw.keyword for kw in g.keywords]
                }
                for g in groups
            ]
        except SQLAlchemyError as e:
            self.logger.error(f"获取关键词分组列表失败: {str(e)}")
            return []
        finally:
            session.close()

    def update_keyword_group(self, group_id: int, group_name: str = None, reply: str = None, is_transfer: int = None, pass_to_ai: int = None) -> bool:
        """更新关键词分组"""
        session = self.get_session()
        try:
            group = session.query(KeywordGroup).filter(KeywordGroup.id == group_id).first()
            if not group:
                self.logger.warning(f"关键词分组 {group_id} 不存在")
                return False
            if group_name is not None:
                group.group_name = group_name
            if reply is not None:
                group.reply = reply
            if is_transfer is not None:
                group.is_transfer = is_transfer
            if pass_to_ai is not None:
                group.pass_to_ai = pass_to_ai
            session.commit()
            self.logger.info(f"成功更新关键词分组: {group_id}")
            return True
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"更新关键词分组失败: {str(e)}")
            return False
        finally:
            session.close()

    def delete_keyword_group(self, group_id: int) -> bool:
        """删除关键词分组（级联删除关键词）"""
        session = self.get_session()
        try:
            group = session.query(KeywordGroup).filter(KeywordGroup.id == group_id).first()
            if not group:
                self.logger.warning(f"关键词分组 {group_id} 不存在")
                return False
            session.delete(group)
            session.commit()
            self.logger.info(f"成功删除关键词分组: {group_id}")
            return True
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"删除关键词分组失败: {str(e)}")
            return False
        finally:
            session.close()

    # ========== 关键词相关操作（分组版） ==========
    def add_keyword_to_group(self, keyword: str, group_id: int) -> bool:
        """添加关键词到指定分组

        Args:
            keyword: 关键词
            group_id: 分组ID

        Returns:
            bool: 是否添加成功
        """
        session = self.get_session()
        try:
            # 检查分组是否存在
            group = session.query(KeywordGroup).filter(KeywordGroup.id == group_id).first()
            if not group:
                self.logger.warning(f"分组 {group_id} 不存在")
                return False

            # 检查关键词是否已在该分组中
            existing = session.query(Keyword).filter(
                Keyword.keyword == keyword, Keyword.group_id == group_id
            ).first()
            if existing:
                self.logger.warning(f"关键词 '{keyword}' 已存在于分组 {group_id}")
                return False

            keyword_obj = Keyword(keyword=keyword, group_id=group_id)
            session.add(keyword_obj)
            session.commit()
            self.logger.info(f"成功添加关键词 '{keyword}' 到分组 '{group.group_name}'")
            return True
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"添加关键词失败: {str(e)}")
            return False
        finally:
            session.close()

    def get_all_keywords(self) -> List[Dict[str, Any]]:
        """获取所有关键词（含分组信息）"""
        session = self.get_session()
        try:
            keywords = session.query(Keyword).all()
            return [
                {
                    'id': kw.id,
                    'keyword': kw.keyword,
                    'group_id': kw.group_id
                }
                for kw in keywords
            ]
        except SQLAlchemyError as e:
            self.logger.error(f"获取关键词列表失败: {str(e)}")
            return []
        finally:
            session.close()

    def get_keywords_by_group(self, group_id: int) -> List[str]:
        """获取指定分组的所有关键词"""
        session = self.get_session()
        try:
            keywords = session.query(Keyword).filter(Keyword.group_id == group_id).all()
            return [kw.keyword for kw in keywords]
        except SQLAlchemyError as e:
            self.logger.error(f"获取分组关键词失败: {str(e)}")
            return []
        finally:
            session.close()

    def update_keyword(self, keyword_id: int, new_keyword: str = None, new_group_id: int = None) -> bool:
        """更新关键词（按ID）"""
        session = self.get_session()
        try:
            keyword_obj = session.query(Keyword).filter(Keyword.id == keyword_id).first()
            if not keyword_obj:
                self.logger.warning(f"关键词ID {keyword_id} 不存在")
                return False
            if new_keyword is not None:
                # 检查同分组下是否已存在同名关键词
                existing = session.query(Keyword).filter(
                    Keyword.keyword == new_keyword,
                    Keyword.group_id == keyword_obj.group_id,
                    Keyword.id != keyword_id
                ).first()
                if existing:
                    self.logger.warning(f"关键词 '{new_keyword}' 已存在于该分组")
                    return False
                keyword_obj.keyword = new_keyword
            if new_group_id is not None:
                keyword_obj.group_id = new_group_id
            session.commit()
            self.logger.info(f"成功更新关键词ID {keyword_id}")
            return True
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"更新关键词失败: {str(e)}")
            return False
        finally:
            session.close()

    def delete_keyword(self, keyword_id: int) -> bool:
        """删除关键词（按ID）"""
        session = self.get_session()
        try:
            keyword_obj = session.query(Keyword).filter(Keyword.id == keyword_id).first()
            if not keyword_obj:
                self.logger.warning(f"关键词ID {keyword_id} 不存在")
                return False
            session.delete(keyword_obj)
            session.commit()
            self.logger.info(f"成功删除关键词ID {keyword_id}")
            return True
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"删除关键词失败: {str(e)}")
            return False
        finally:
            session.close()

    def get_keyword_reply_rules(self) -> List[Dict[str, Any]]:
        """获取所有关键词回复规则（供业务层使用）

        Returns:
            List[Dict]: [{'keywords': [...], 'reply': '...', 'is_transfer': 0/1, 'group_name': '...'}]
        """
        session = self.get_session()
        try:
            groups = session.query(KeywordGroup).all()
            return [
                {
                    'group_id': g.id,
                    'group_name': g.group_name,
                    'keywords': [kw.keyword for kw in g.keywords],
                    'reply': g.reply,
                    'is_transfer': g.is_transfer,
                    'pass_to_ai': g.pass_to_ai
                }
                for g in groups if g.keywords  # 只返回有关键词的分组
            ]
        except SQLAlchemyError as e:
            self.logger.error(f"获取关键词回复规则失败: {str(e)}")
            return []
        finally:
            session.close()

# 创建全局数据库管理器实例
db_manager = DatabaseManager() 
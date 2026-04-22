import os
import json
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import SQLAlchemyError
from typing import List, Dict, Any, Optional, Union
from utils.logger_loguru import get_logger
from database.models import Base, Channel, Shop, Account, Keyword

class DatabaseManager:
    """数据库管理类，提供数据库操作的封装"""
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        """单例模式实现"""
        if cls._instance is None:
            cls._instance = super(DatabaseManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, db_path: str = './temp/channel_shop.db'):
        """初始化数据库连接
        
        Args:
            db_path: 数据库文件路径
        """
        if self._initialized:
            return
        
        # 打印实际使用的数据库路径
        print(f"[DatabaseManager] 使用数据库路径: {db_path}")
        
        # 确保数据库目录存在
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        
        # 创建数据库引擎
        self.engine = create_engine(f'sqlite:///{db_path}')
        self.Session = sessionmaker(bind=self.engine)
        
        # 创建表结构
        Base.metadata.create_all(self.engine)
        
        self._initialized = True
        self.logger = get_logger()    
        # 初始化数据库
        self.init_db()

    def init_db(self):
        """初始化渠道信息"""
        channel_name = "pinduoduo"
        description = "拼多多"
        self.add_channel(channel_name, description)


    def get_session(self):
        """获取数据库会话"""
        return self.Session()
    
    # 渠道相关操作
    def add_channel(self, channel_name: str, description: Optional[str] = None) -> bool:
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
    def add_shop(self, channel_name: str, shop_id: str, shop_name: str, shop_logo: str, description: Optional[str] = None) -> bool:
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
    
    def update_shop_info(self, channel_name: str, shop_id: str, shop_name: Optional[str] = None, shop_logo: Optional[str] = None, description: Optional[str] = None) -> bool:
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
    def add_account(self, channel_name: str, shop_id: str, user_id: str, username: str, password: str, cookies: Optional[str] = None) -> bool:
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

    # 关键词相关操作
    def add_keyword(self, keyword: str, group_name: str = 'default', match_type: str = 'partial',
                    reply_content: Optional[str] = None, transfer_to_human: bool = False, priority: int = 0) -> bool:
        """添加关键词
        
        Args:
            keyword: 关键词
            group_name: 分组名称
            match_type: 匹配类型 (exact/partial/regex/wildcard)
            reply_content: 回复内容
            transfer_to_human: 是否转人工
            priority: 优先级
            
        Returns:
            bool: 是否添加成功
        """
        session = self.get_session()
        try:
            # 检查关键词是否已存在
            existing = session.query(Keyword).filter(Keyword.keyword == keyword).first()
            if existing:
                self.logger.warning(f"关键词 {keyword} 已存在")
                return False
                
            # 创建新关键词
            keyword_obj = Keyword(
                keyword=keyword,
                group_name=group_name,
                match_type=match_type,
                reply_content=reply_content,
                transfer_to_human=1 if transfer_to_human else 0,
                priority=priority
            )
            session.add(keyword_obj)
            session.commit()
            self.logger.info(f"成功添加关键词: {keyword} (分组: {group_name})")
            return True
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"添加关键词失败: {str(e)}")
            return False
        finally:
            session.close()
    
    def get_keyword(self, keyword: str) -> Optional[Dict[str, Any]]:
        """获取关键词信息
        
        Args:
            keyword: 关键词
            
        Returns:
            Optional[Dict]: 关键词信息或None
        """
        session = self.get_session()
        try:
            keyword_obj = session.query(Keyword).filter(Keyword.keyword == keyword).first()
            if not keyword_obj:
                return None
                
            return {
                'id': keyword_obj.id,
                'keyword': keyword_obj.keyword,
                'group_name': keyword_obj.group_name,
                'reply_content': keyword_obj.reply_content,
                'transfer_to_human': bool(keyword_obj.transfer_to_human),
                'priority': keyword_obj.priority,
                'created_at': keyword_obj.created_at.isoformat() if keyword_obj.created_at else None,
                'updated_at': keyword_obj.updated_at.isoformat() if keyword_obj.updated_at else None
            }
        except SQLAlchemyError as e:
            self.logger.error(f"获取关键词失败: {str(e)}")
            return None
        finally:
            session.close()
    
    def get_all_keywords(self) -> List[Dict[str, Any]]:
        """获取所有关键词
        
        Returns:
            List[Dict]: 关键词列表
        """
        session = self.get_session()
        try:
            keywords = session.query(Keyword).order_by(Keyword.priority.desc()).all()
            return [
                {
                    'id': keyword.id,
                    'keyword': keyword.keyword,
                    'group_name': keyword.group_name,
                    'match_type': keyword.match_type,
                    'reply_content': keyword.reply_content,
                    'transfer_to_human': bool(keyword.transfer_to_human),
                    'priority': keyword.priority,
                    'created_at': keyword.created_at.isoformat() if keyword.created_at else None,
                    'updated_at': keyword.updated_at.isoformat() if keyword.updated_at else None
                }
                for keyword in keywords
            ]
        except SQLAlchemyError as e:
            self.logger.error(f"获取关键词列表失败: {str(e)}")
            return []
        finally:
            session.close()
    
    def get_keywords_by_group(self, group_name: str) -> List[Dict[str, Any]]:
        """获取指定分组的关键词
        
        Args:
            group_name: 分组名称
            
        Returns:
            List[Dict]: 关键词列表
        """
        session = self.get_session()
        try:
            keywords = session.query(Keyword).filter(
                Keyword.group_name == group_name
            ).order_by(Keyword.priority.desc()).all()
            return [
                {
                    'id': keyword.id,
                    'keyword': keyword.keyword,
                    'group_name': keyword.group_name,
                    'reply_content': keyword.reply_content,
                    'transfer_to_human': bool(keyword.transfer_to_human),
                    'priority': keyword.priority
                }
                for keyword in keywords
            ]
        except SQLAlchemyError as e:
            self.logger.error(f"获取分组关键词失败: {str(e)}")
            return []
        finally:
            session.close()
    
    def update_keyword(self, old_keyword: str, new_keyword: str, group_name: Optional[str] = None,
                       match_type: Optional[str] = None, reply_content: Optional[str] = None, 
                       transfer_to_human: Optional[bool] = None, priority: Optional[int] = None) -> bool:
        """更新关键词
        
        Args:
            old_keyword: 原关键词
            new_keyword: 新关键词
            group_name: 分组名称
            match_type: 匹配类型 (exact/partial/regex/wildcard)
            reply_content: 回复内容
            transfer_to_human: 是否转人工
            priority: 优先级
            
        Returns:
            bool: 是否更新成功
        """
        session = self.get_session()
        try:
            # 检查原关键词是否存在
            keyword_obj = session.query(Keyword).filter(Keyword.keyword == old_keyword).first()
            if not keyword_obj:
                self.logger.warning(f"关键词 {old_keyword} 不存在")
                return False
            
            # 检查新关键词是否已存在（如果不是同一个关键词）
            if old_keyword != new_keyword:
                existing = session.query(Keyword).filter(Keyword.keyword == new_keyword).first()
                if existing:
                    self.logger.warning(f"关键词 {new_keyword} 已存在")
                    return False
                
            # 更新关键词
            keyword_obj.keyword = new_keyword
            if group_name is not None:
                keyword_obj.group_name = group_name
            if match_type is not None:
                keyword_obj.match_type = match_type
            if reply_content is not None:
                keyword_obj.reply_content = reply_content
            if transfer_to_human is not None:
                keyword_obj.transfer_to_human = transfer_to_human
            if priority is not None:
                keyword_obj.priority = priority
                
            session.commit()
            self.logger.info(f"成功更新关键词: {old_keyword} -> {new_keyword}")
            return True
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"更新关键词失败: {str(e)}")
            return False
        finally:
            session.close()
    
    def delete_keyword(self, keyword: str) -> bool:
        """删除关键词
        
        Args:
            keyword: 关键词
            
        Returns:
            bool: 是否删除成功
        """
        session = self.get_session()
        try:
            keyword_obj = session.query(Keyword).filter(Keyword.keyword == keyword).first()
            if not keyword_obj:
                self.logger.warning(f"关键词 {keyword} 不存在")
                return False
                
            session.delete(keyword_obj)
            session.commit()
            self.logger.info(f"成功删除关键词: {keyword}")
            return True
        except SQLAlchemyError as e:
            session.rollback()
            self.logger.error(f"删除关键词失败: {str(e)}")
            return False
        finally:
            session.close()
    
    def get_all_keyword_groups(self) -> List[str]:
        """获取所有关键词分组名称
        
        Returns:
            List[str]: 分组名称列表
        """
        session = self.get_session()
        try:
            from sqlalchemy import distinct
            groups = session.query(distinct(Keyword.group_name)).all()
            return [g[0] for g in groups if g[0]]
        except SQLAlchemyError as e:
            self.logger.error(f"获取关键词分组失败: {str(e)}")
            return []
        finally:
            session.close()

_db_instance = None

def get_db_manager() -> "DatabaseManager":
    global _db_instance
    if _db_instance is None:
        _db_instance = DatabaseManager()
    return _db_instance

class _LazyDBProxy:
    def __getattr__(self, name):
        return getattr(get_db_manager(), name)

db_manager = _LazyDBProxy()

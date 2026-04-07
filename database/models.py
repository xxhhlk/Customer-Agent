from pyclbr import Class
from sqlalchemy import Column, Integer, String, Text, ForeignKey, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker

Base = declarative_base()

class Channel(Base):
    """渠道表，存储电商渠道基本信息"""
    __tablename__ = 'channels'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_name = Column(String(50), unique=True, nullable=False, comment='渠道名称')
    description = Column(String(255), comment='渠道描述')
    
    # 关联关系 - 一个渠道可以有多个店铺
    shops = relationship('Shop', back_populates='channel', cascade='all, delete-orphan')
    
    def __repr__(self):
        return f"<Channel(channel_name='{self.channel_name}')>"


class Shop(Base):
    """店铺表，存储店铺基本信息"""
    __tablename__ = 'shops'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_id = Column(Integer, ForeignKey('channels.id'), nullable=False)
    shop_id = Column(String(100), nullable=False, comment='店铺ID')
    shop_name = Column(String(100), nullable=False, comment='店铺名称')
    shop_logo = Column(String(255), nullable=True, comment='店铺logo')
    description = Column(String(255), comment='店铺描述')
    
    # 关联关系 - 多个店铺属于一个渠道，一个店铺可以有多个账号
    channel = relationship('Channel', back_populates='shops')
    accounts = relationship('Account', back_populates='shop', cascade='all, delete-orphan')

    def __repr__(self):
        return f"<Shop(shop_id='{self.shop_id}', shop_name='{self.shop_name}', channel='{self.channel.channel_name if self.channel else None}')>" 


class Account(Base):
    """账号表，存储店铺账号信息"""
    __tablename__ = 'accounts'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    shop_id = Column(Integer, ForeignKey('shops.id'), nullable=False)
    user_id = Column(String(100), nullable=False, comment='用户ID')
    username = Column(String(100), nullable=False, comment='登录用户名')
    password = Column(String(255), nullable=False, comment='登录密码')
    cookies = Column(Text, comment='存储登录cookies信息的JSON字符串')
    status = Column(Integer, default=None, comment='账号状态: None-未验证, 0-休息,1-在线, 3-离线')
    
    # 关联关系 - 多个账号属于一个店铺
    shop = relationship('Shop', back_populates='accounts')
    
    def __repr__(self):
        return f"<Account(username='{self.username}', password='{self.password}', shop='{self.shop.shop_name if self.shop else None}')>"

    
class KeywordGroup(Base):
    """关键词分组表，多个关键词可对应同一个回复"""
    __tablename__ = 'keyword_groups'

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_name = Column(String(100), nullable=False, comment='分组名称')
    reply = Column(Text, nullable=True, comment='自动回复内容，为空则转人工')
    is_transfer = Column(Integer, default=0, comment='是否转人工: 0-仅回复, 1-转人工')
    pass_to_ai = Column(Integer, default=0, comment='是否传递给AI: 0-不传递, 1-匹配后剩余内容传给AI处理')

    # 关联关系 - 一个分组包含多个关键词
    keywords = relationship('Keyword', back_populates='group', cascade='all, delete-orphan')

    def __repr__(self):
        return f"<KeywordGroup(group_name='{self.group_name}', reply='{self.reply[:20] if self.reply else None}')>"


class Keyword(Base):
    """关键词表，存储关键词信息"""
    __tablename__ = 'keywords'

    id = Column(Integer, primary_key=True, autoincrement=True)
    keyword = Column(String(100), nullable=False, comment='关键词')
    group_id = Column(Integer, ForeignKey('keyword_groups.id'), nullable=False, comment='所属分组ID')

    # 关联关系
    group = relationship('KeywordGroup', back_populates='keywords')

    def __repr__(self):
        return f"<Keyword(keyword='{self.keyword}', group='{self.group.group_name if self.group else None}')>"
from typing import List, Optional, TYPE_CHECKING
from sqlalchemy import Column, Integer, String, Text, ForeignKey, create_engine, DateTime, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker, Mapped, mapped_column
from datetime import datetime
import json

Base = declarative_base()

class Channel(Base):
    """渠道表，存储电商渠道基本信息"""
    __tablename__ = 'channels'
    __allow_unmapped__ = True  # 允许 pyright 正确处理 ORM 赋值

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, comment='渠道名称')
    description: Mapped[Optional[str]] = mapped_column(String(255), comment='渠道描述')

    # 关联关系 - 一个渠道可以有多个店铺
    shops: Mapped[List['Shop']] = relationship('Shop', back_populates='channel', cascade='all, delete-orphan')

    def __repr__(self):
        return f"<Channel(channel_name='{self.channel_name}')>"


class Shop(Base):
    """店铺表，存储店铺基本信息"""
    __tablename__ = 'shops'
    __allow_unmapped__ = True

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(Integer, ForeignKey('channels.id'), nullable=False)
    shop_id: Mapped[str] = mapped_column(String(100), nullable=False, comment='店铺ID')
    shop_name: Mapped[str] = mapped_column(String(100), nullable=False, comment='店铺名称')
    shop_logo: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment='店铺logo')
    description: Mapped[Optional[str]] = mapped_column(String(255), comment='店铺描述')

    # 关联关系 - 多个店铺属于一个渠道，一个店铺可以有多个账号
    channel: Mapped['Channel'] = relationship('Channel', back_populates='shops')
    accounts: Mapped[List['Account']] = relationship('Account', back_populates='shop', cascade='all, delete-orphan')

    def __repr__(self):
        return f"<Shop(shop_id='{self.shop_id}', shop_name='{self.shop_name}', channel='{self.channel.channel_name if self.channel else None}')>"


class Account(Base):
    """账号表，存储店铺账号信息"""
    __tablename__ = 'accounts'
    __allow_unmapped__ = True

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(Integer, ForeignKey('shops.id'), nullable=False)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False, comment='用户ID')
    username: Mapped[str] = mapped_column(String(100), nullable=False, comment='登录用户名')
    password: Mapped[str] = mapped_column(String(255), nullable=False, comment='登录密码')
    cookies: Mapped[Optional[str]] = mapped_column(Text, comment='存储登录cookies信息的JSON字符串')
    status: Mapped[Optional[int]] = mapped_column(Integer, default=None, comment='账号状态: None-未验证, 0-休息,1-在线, 3-离线')

    # 关联关系 - 多个账号属于一个店铺
    shop: Mapped['Shop'] = relationship('Shop', back_populates='accounts')

    def __repr__(self):
        return f"<Account(username='{self.username}', password='{self.password}', shop='{self.shop.shop_name if self.shop else None}')>"


class KeywordGroup(Base):
    """关键词分组表，存储分组信息和回复内容"""
    __tablename__ = 'keyword_groups'
    __allow_unmapped__ = True

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, comment='分组名称')
    reply: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment='回复内容')
    is_transfer: Mapped[Optional[int]] = mapped_column(Integer, default=0, comment='是否转人工: 0-否, 1-是')
    pass_to_ai: Mapped[Optional[int]] = mapped_column(Integer, default=0, comment='是否传递给AI: 0-否, 1-是')
    priority: Mapped[int] = mapped_column(Integer, default=0, comment='优先级，数值越大优先级越高')

    # 关联关系 - 一个分组可以有多个关键词
    keywords: Mapped[List['Keyword']] = relationship('Keyword', back_populates='group', cascade='all, delete-orphan')

    def __repr__(self):
        return f"<KeywordGroup(id={self.id}, name='{self.group_name}', reply='{self.reply[:20] if self.reply else None}...')>"


class Keyword(Base):
    """关键词表，存储关键词信息"""
    __tablename__ = 'keywords'
    __allow_unmapped__ = True

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    keyword: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, comment='关键词')
    group_id: Mapped[int] = mapped_column(Integer, ForeignKey('keyword_groups.id'), nullable=False, comment='分组ID')
    match_type: Mapped[str] = mapped_column(String(20), nullable=False, default='partial', comment='匹配类型: exact-完全匹配, partial-部分匹配, regex-正则匹配, wildcard-通配符匹配')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment='创建时间')
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, comment='更新时间')

    # 关联关系 - 多个关键词属于一个分组
    group: Mapped['KeywordGroup'] = relationship('KeywordGroup', back_populates='keywords')

    def __repr__(self):
        return f"<Keyword(keyword='{self.keyword}', group_id={self.group_id}, match_type='{self.match_type}')>"

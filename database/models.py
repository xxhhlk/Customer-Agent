from typing import List, Optional, TYPE_CHECKING
from sqlalchemy import Column, Integer, String, Text, ForeignKey, create_engine, DateTime, Float, Index, Boolean, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker, Mapped, mapped_column
from datetime import datetime
import json
import uuid

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


class ChatMessageRecord(Base):
    """聊天消息记录表，存储买家-客服对话历史"""
    __tablename__ = 'chat_message_records'
    __allow_unmapped__ = True

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    msg_id: Mapped[str] = mapped_column(String(100), nullable=False, comment='平台消息ID/手动生成ID')
    shop_id: Mapped[str] = mapped_column(String(100), nullable=False, comment='店铺ID')
    user_id: Mapped[str] = mapped_column(String(100), nullable=False, comment='登录账号user_id（用于手动发送）')
    shop_name: Mapped[Optional[str]] = mapped_column(String(100), comment='店铺名称')
    buyer_uid: Mapped[str] = mapped_column(String(100), nullable=False, index=True, comment='买家UID（会话分组键）')
    from_uid: Mapped[str] = mapped_column(String(100), nullable=False, comment='发送者UID')
    from_role: Mapped[str] = mapped_column(String(50), nullable=False, comment='发送者角色(user/mall_cs)')
    to_uid: Mapped[Optional[str]] = mapped_column(String(100), comment='接收者UID')
    to_role: Mapped[Optional[str]] = mapped_column(String(50), comment='接收者角色')
    nickname: Mapped[Optional[str]] = mapped_column(String(100), comment='发送者昵称')
    content: Mapped[Optional[str]] = mapped_column(Text, comment='消息内容')
    msg_type: Mapped[Optional[str]] = mapped_column(String(50), comment='消息类型')
    context_type: Mapped[Optional[str]] = mapped_column(String(50), comment='上下文类型')
    direction: Mapped[str] = mapped_column(String(20), nullable=False, default='inbound',
                                            comment='inbound=买家发来, outbound=客服发出')
    reply_source: Mapped[Optional[str]] = mapped_column(String(50), comment='回复来源: ai/keyword/staff/fallback/manual')
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment='消息时间')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    __table_args__ = (
        Index('ix_chat_msg_buyer_ts', 'buyer_uid', 'timestamp'),
        Index('ix_chat_msg_shop_ts', 'shop_id', 'timestamp'),
        Index('ix_chat_msg_msg_id', 'msg_id'),
    )

    def __repr__(self):
        return f"<ChatMessageRecord(buyer_uid='{self.buyer_uid}', direction='{self.direction}', shop='{self.shop_name}')>"


class ProductKnowledge(Base):
    """产品知识表，存储LLM提取的商品详细知识"""
    __tablename__ = 'product_knowledge'
    __allow_unmapped__ = True

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(Integer, ForeignKey('shops.id', ondelete='CASCADE'), nullable=False, comment='店铺ID')
    goods_id: Mapped[int] = mapped_column(Integer, nullable=False, comment='商品ID')
    goods_name: Mapped[str] = mapped_column(String(255), nullable=False, comment='商品名称')
    price: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, comment='价格范围（文本格式）')
    price_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment='最低价（分）')
    price_max: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment='最高价（分）')
    sold_quantity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment='已售数量')
    thumb_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True, comment='商品缩略图URL')
    specifications: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment='规格信息（JSON格式）')
    extracted_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment='LLM提取的详细产品知识')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment='创建时间')
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, comment='更新时间')
    last_extracted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment='上次提取时间')

    __table_args__ = (
        UniqueConstraint('shop_id', 'goods_id', name='uix_product_knowledge_shop_goods'),
    )

    # 关联关系
    shop: Mapped['Shop'] = relationship('Shop', backref='product_knowledge')

    def __repr__(self):
        return f"<ProductKnowledge(goods_id='{self.goods_id}', goods_name='{self.goods_name}')>"


class CustomerServiceKnowledge(Base):
    """客服知识表，存储人工添加的客服话术和规则知识"""
    __tablename__ = 'customer_service_knowledge'
    __allow_unmapped__ = True

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(Integer, ForeignKey('shops.id', ondelete='CASCADE'), nullable=False, comment='店铺ID')
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment='知识标题')
    content: Mapped[str] = mapped_column(Text, nullable=False, comment='知识内容')
    tags: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment='标签（逗号分隔）')
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, comment='是否启用')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, comment='创建时间')
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now, comment='更新时间')

    # 关联关系
    shop: Mapped['Shop'] = relationship('Shop', backref='customer_service_knowledge')

    def __repr__(self):
        return f"<CustomerServiceKnowledge(title='{self.title}', enabled={self.enabled})>"

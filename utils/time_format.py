"""
微信风格时间格式化工具
- 聊天消息流：日期/时间分隔标签
- 会话列表：右侧时间显示
"""

from datetime import datetime

# 同一天内相邻消息间隔超过该阈值(秒)时插入时间标签（微信约 5 分钟）
TIME_LABEL_GAP_SECONDS = 300

_WEEKDAYS = "一二三四五六日"


def parse_dt(ts_str) -> datetime | None:
    """解析 ISO 格式时间戳，失败返回 None"""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(str(ts_str))
    except (ValueError, TypeError):
        return None


def format_day_label(dt: datetime, now: datetime | None = None) -> str:
    """消息流日期标签：今天 / 昨天 / M月d日 / yyyy年M月d日"""
    now = now or datetime.now()
    d, today = dt.date(), now.date()
    if d == today:
        return "今天"
    if (today - d).days == 1:
        return "昨天"
    if d.year == today.year:
        return f"{d.month}月{d.day}日"
    return f"{d.year}年{d.month}月{d.day}日"


def format_time_label(dt: datetime) -> str:
    """消息流时间标签（微信 12 小时制）：上午8:30 / 下午3:45"""
    period = "上午" if dt.hour < 12 else "下午"
    hour = dt.hour % 12 or 12
    return f"{period}{hour}:{dt.minute:02d}"


def needs_time_separator(prev_dt: datetime | None, dt: datetime) -> bool:
    """是否需要插入时间分隔标签：跨天 或 同一天间隔超阈值"""
    if prev_dt is None:
        return True
    if prev_dt.date() != dt.date():
        return True
    return (dt - prev_dt).total_seconds() >= TIME_LABEL_GAP_SECONDS


def format_list_time(dt: datetime, now: datetime | None = None) -> str:
    """会话列表右侧时间（微信风格）：
    今天 HH:MM / 昨天 / 一周内 星期X / 今年 M月d日 / 更早 yyyy/M/d
    """
    now = now or datetime.now()
    d, today = dt.date(), now.date()
    if d == today:
        return dt.strftime("%H:%M")
    if (today - d).days == 1:
        return "昨天"
    if (today - d).days < 7:
        return f"星期{_WEEKDAYS[d.weekday()]}"
    if d.year == today.year:
        return f"{d.month}月{d.day}日"
    return f"{d.year}/{d.month}/{d.day}"

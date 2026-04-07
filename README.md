# 🤖 拼多多智能客服系统

<div align="center">
  <img src="docs/设置.png" alt="系统配置界面" width="600">
  <p><em>拼多多智能客服系统 - 提升客服效率的智能化解决方案</em></p>
</div>

## 📖 项目简介

拼多多智能客服系统是一个专为电商平台设计的综合性客户服务管理工具。本系统通过AI技术和自动化流程，显著提高客服工作效率，实现智能回复的同时保留人工介入的灵活性，为商家提供完整的客服解决方案。

## ✨ 主要功能

### 🔐 账号管理
- 商家账号管理（支持多账号）
- 自动登录获取cookies
- 账号状态实时监控
- **批量上线/下线** - 一键批量设置所有账号状态
- **启动后自动开始** - 应用启动后自动开始所有账号的自动回复

<div align="center">
  <img src="docs/账号管理.png" alt="账号管理界面" width="500">
  <p><em>账号管理 - 管理您的拼多多商家账号</em></p>
</div>

### 💬 智能消息处理
- **智能防抖合并** - 同一买家连续发消息时等待5秒后合并处理，避免逐条回复
- **夜间延迟回复** - 23:01-07:55夜间时段延迟5分钟回复，避免打扰客户休息
- 实时消息监控与自动回复
- 集成AI (Coze API) 生成智能回复内容
- **AI超时重发** - 25秒内无响应自动取消并重发，确保回复及时性

<div align="center">
  <img src="docs/自动回复.png" alt="自动回复界面" width="500">
  <p><em>智能回复 - 自动回复客户消息</em></p>
</div>

### 👨‍💼 人工客服协作
- **人工优先回复** - 白天工作时段优先等待人工客服回复（最长30秒），超时再转AI
- 智能人机协作，确保服务质量

### 🔄 智能转接系统
- **关键词分组** - 支持多个关键词对应同一回复/转人工操作，支持优先级匹配
- 基于关键词智能识别客户需求
- 自动将复杂问题转接给人工客服
- 无缝衔接确保服务质量

<div align="center">
  <img src="docs/关键词管理.png" alt="关键词管理界面" width="500">
  <p><em>关键词管理 - 智能识别转接需求</em></p>
</div>

### 🛡️ API 限流保护
- **Coze API 限流** - 固定窗口限流（默认4小时10次），超限自动发送兜底回复
- 防止API滥用，控制成本

### 📊 系统监控
- 实时日志记录
- 系统运行状态监控
- 详细的操作记录和统计

<div align="center">
  <img src="docs/日志管理.png" alt="日志界面" width="500">
  <p><em>日志界面 - 实时监控系统运行状态</em></p>
</div>

## 🚀 快速开始

### 环境要求
- Python 3.11+
- Windows 10/11 (推荐)
- 网络连接稳定

### 安装步骤

1. **克隆项目**
   ```bash
   git clone https://github.com/JC0v0/Customer-Agent.git
   cd Customer-Agent
   ```

2. **安装依赖**
   ```bash
   ##使用uv进行环境配置
   ##安装uv
   pip install uv

   uv venv
   uv sync
   ```

3. **安装浏览器驱动**
   ```bash
   uv run playwright install chrome
   ```


## 📱 使用指南

### 启动系统
```bash
python app.py
```

### 配置流程

1. **配置商家账号**
   - 在账号管理界面配置您的拼多多商家账号
   - 系统将自动获取并保存登录凭证

2. **配置Coze API**
   - 在设置界面配置 Coze API 密钥和 Bot ID
   - 设置工作时间（默认 08:00-23:00）

3. **设置API限流（可选）**
   - 配置每个买家的请求频率限制
   - 设置兜底回复话术

4. **设置人工优先回复（可选）**
   - 启用/禁用人工客服优先回复功能
   - 配置等待超时时间（默认30秒）

5. **设置关键词规则**
   - 配置需要人工转接的关键词分组
   - 设置自动回复的话术模板

6. **启动系统**
   - 应用启动后会自动开始所有账号的自动回复
   - 或在账号管理界面手动启动指定账号
   - 系统将根据配置自动处理消息

7. **监控日志**
   - 在日志管理界面查看系统运行日志
   - 监控消息处理状态和异常情况

### 配置文件说明

系统配置文件 `config.json` 包含以下主要配置项：

```json
{
    "coze_api_base": "https://api.coze.cn",
    "coze_token": "your-coze-token",
    "coze_bot_id": "your-bot-id",
    "bot_type": "coze",
    "businessHours": {
        "start": "08:00",
        "end": "23:00"
    },
    "rate_limit": {
        "window_hours": 4,
        "max_requests": 10,
        "fallback_reply": "这个我不了解呢，帮你问下我们的技术人员"
    },
    "staff_reply_wait": {
        "enable": true,
        "wait_seconds": 30
    }
}
```

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `coze_token` | Coze API 访问令牌 | - |
| `coze_bot_id` | Coze Bot ID | - |
| `businessHours` | 工作时间配置 | 08:00-23:00 |
| `rate_limit.window_hours` | 限流窗口时长（小时） | 4 |
| `rate_limit.max_requests` | 窗口内最大请求数 | 10 |
| `staff_reply_wait.enable` | 是否启用人工优先回复 | true |
| `staff_reply_wait.wait_seconds` | 人工等待超时（秒） | 30 |


## 🔍 核心特性详解

### 消息防抖合并机制
当同一买家连续发送多条消息时，系统不会立即逐条回复，而是：
1. 收到第一条消息后启动5秒倒计时
2. 期间收到新消息会刷新倒计时并追加到缓冲区
3. 5秒内无新消息时，合并所有消息内容一次性发给AI处理
4. 避免重复打扰客户，提高回复质量

### 夜间延迟回复
- **夜间时段**: 23:01 - 次日 07:55
- 夜间收到消息后延迟5分钟再请求AI
- 期间收到新消息会重置倒计时并追加内容
- 避免夜间频繁消息通知打扰客户休息

### 人工优先回复
- **白天时段**: 优先等待人工客服回复（可配置等待时间，默认30秒）
- 人工回复后取消AI自动回复
- 超时后扣除等待时间继续AI处理
- **夜间时段**: 直接触发AI回复，不等待人工

### API 限流保护
- **固定窗口限流**: 从买家首次请求开始计时
- **默认配置**: 4小时窗口期内最多10次请求
- **超限处理**: 自动发送兜底回复，不消耗API额度
- **跨店铺共享**: 按买家ID全局计数

### 关键词分组
- 支持将多个关键词归入同一分组
- 匹配任一关键词触发相同操作
- 优先匹配最长关键词（避免"客服"误匹配"人工客服"）
- 支持同时发送回复并转人工

## 🛠️ 技术架构

- **前端界面**: PyQt6 + PyQt-Fluent-Widgets
- **后端逻辑**: Python 3.11+
- **AI集成**: Coze API
- **数据存储**: SQLite + JSON
- **浏览器自动化**: Playwright
- **异步处理**: asyncio + WebSocket
- **依赖管理**: uv

## 📁 项目结构

```
Customer-Agent/
├── Agent/                  # AI智能代理模块
│   ├── bot_factory.py          # 机器人工厂
│   ├── bot.py                  # 机器人基类
│   └── CozeAgent/              # Coze AI代理
│       ├── bot.py
│       ├── conversation_manager.py
│       └── user_session.py
├── Channel/                # 渠道接口模块
│   ├── channel.py              # 渠道基类
│   └── pinduoduo/              # 拼多多渠道
│       ├── pdd_chnnel.py
│       ├── pdd_login.py
│       ├── pdd_message.py
│       └── utils/              # 拼多多API工具
├── Message/                # 消息处理模块
│   ├── message_consumer.py     # 消息消费者（含防抖合并、夜间延迟）
│   ├── message_handler.py      # 消息处理器（限流、关键词、人工等待）
│   ├── message_queue.py        # 消息队列
│   ├── message.py              # 消息基类
│   ├── rate_limiter.py         # Coze API限流器
│   └── staff_reply_event.py    # 人工回复事件通知
├── bridge/                 # 桥接模块
│   ├── bridge.py               # 桥接器
│   ├── context.py              # 上下文管理
│   └── reply.py                # 回复处理
├── database/               # 数据库模块
│   ├── db_manager.py           # 数据库管理器
│   └── models.py               # 数据模型（关键词分组）
├── ui/                     # 用户界面模块
│   ├── main_ui.py              # 主界面
│   ├── auto_reply_ui.py        # 自动回复界面（批量操作、自动启动）
│   ├── keyword_ui.py           # 关键词分组管理界面
│   ├── log_ui.py               # 日志界面
│   ├── setting_ui.py           # 设置界面（限流、人工等待配置）
│   └── user_ui.py              # 用户管理界面
├── utils/                  # 工具函数
│   ├── logger.py               # 日志工具
│   └── performance_monitor.py  # 性能监控
├── docs/                   # 文档和截图
├── icon/                   # 图标资源
├── logs/                   # 日志文件
├── user_data/              # 用户数据（cookies等敏感信息）
├── app.py                  # 应用程序入口
├── config.py               # 配置管理
├── pyproject.toml          # 项目配置
├── uv.lock                 # 依赖锁定文件
└── README.md               # 项目说明
```

## 🤝 贡献指南

我们欢迎所有形式的贡献！如果您想参与项目开发：

1. Fork 本仓库
2. 创建您的特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交您的更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 开启一个 Pull Request

## 📄 许可证

本项目采用 MIT 许可证 - 详情请见 [LICENSE](LICENSE) 文件。

## 📞 联系我们

- **问题反馈**: [GitHub Issues](https://github.com/JC0v0/PDD-customer-bot/issues)
- **功能建议**: 欢迎通过 Issues 提出您的想法
- **技术交流**: 
<div align="center">
  <img src="icon/Customer-Agent-qr.png" alt="频道二维码" width="200">
  <p><em>频道二维码</em></p>
</div>

## 💖 支持项目

如果这个项目对您有帮助，您可以通过以下方式支持我们：

<div align="center">
  <img src="docs/赞赏码.png" alt="赞赏码" width="200">
  <p><em>您的支持是我们前进的动力</em></p>
</div>

---

<div align="center">
  <p>⭐ 如果这个项目对您有帮助，请给我们一个星标！</p>
  <p>Made with ❤️ by <a href="https://github.com/JC0v0">JC0v0</a></p>
</div>

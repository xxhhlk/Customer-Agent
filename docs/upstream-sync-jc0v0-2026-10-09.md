# JC0v0 上游 31 提交同步对照分析

> **分析日期**：2026-10-09
> **对照范围**：`9363c56` (2026-04-06，共同祖先) → `jc0v0/main` = `4343a1f` (2026-09-21)
> **本地分支**：`rewrite-on-upstream` = `e304bbc`（2026-10-01，287 个重写提交）
> **状态**：待确认移植范围，尚未执行任何移植

---

## 1. 仓库拓扑与上游关系

```
9363c56 (2026-04-06, 共同祖先: JC0v0 全面架构重构之后)
    │
    ├─ 本仓库 rewrite-on-upstream: 287 个提交 → e304bbc (2026-10-01)
    └─ JC0v0 上游新更新: 31 个提交 → 4343a1f (2026-09-21)   ← 本次对照对象
```

- **真正 upstream = GitHub `JC0v0/Customer-Agent`**（928 星，最后推送 2026-09-22）
- 本地 `origin` 是 `xxhhlk/Customer-Agent` fork 的代理，其 main 停在 2026-04-21 的旧线，**不代表 upstream 最新**
- 拉取方式（可复现）：

```powershell
git fetch https://globalbal.xxhhlk.com:12843/H94Tupd84M/github/JC0v0/Customer-Agent.git main:refs/remotes/jc0v0/main
```

- **不推荐直接 `git merge jc0v0/main`**：两边独立演化 5 个月，模拟合并有 40+ 文件冲突（30+ content/add-add + 10 modify/delete）。采用**按功能定向 backport** 策略。
- 31 提交改动规模：126 个文件，+18671 / −8844。

## 2. 总账

| 判定       | 数量  | 说明                                             |
| -------- | --- | ---------------------------------------------- |
| 🔁 可移植   | ~19 | 含 7 项 P0 缺陷修复、消息路由、登录健壮性、打包等                   |
| ❌ 不适用    | ~9  | LiteLLM 栈、CI、README、docs、Agno→自研迁移等            |
| ✅ 已覆盖/超越 | 3   | Cookie 自动登录（本地为超集）、db_manager 统一、API_VERSION 等 |

## 3. P0 缺陷：上游已修、本地仍存在（已逐一读代码核实）

### 3.1 渠道层 `remove_shop` 传参错误 → 删除店铺必抛 TypeError

- **证据**：`Channel/channel.py:48` 与 `:53` 调用 `get_shop(self.channel_name, shop_id, shop_name)`（3 参）；`database/db_manager.py:332` `get_shop(self, channel_name, shop_id)`、`:450` `delete_shop(self, channel_name, shop_id)`（均 2 参）
- **修复**：删掉两处调用里的 `shop_name` 实参即可（2 行）
- **来源**：3feffd5

### 3.2 账号停止清理：`await` 后 `del` + 清理不跳过"当前任务"

- **证据**：`Channel/pinduoduo/core/pdd_lifecycle.py:122/137/152`、`:395/406/417` 均为 await 后 `del dict[key]`；`_cleanup_reconnect_tasks`（:622）等 3 个清理方法不跳过当前正在执行的任务（自取消场景）
- **后果**：与已知 QThread 关闭隐患同源；并发清理时 KeyError / 卡死。另 `ui/auto_reply/manager.py:191` `_stopping` 永不复位——"停止全部 → 重启账号 → 再停止"时第二次 `stop_all()` 静默跳过，线程停不掉（注释写"shutdown 场景不需要重置"，但 UI 允许重启场景）
- **修复**（af240a8 移植）：`del` → `self._xxx_tasks.pop(connection_key, None)`；3 个清理方法加 `if task is asyncio.current_task(): continue`；`_stopping` 在 `_on_stop_all_finished` 中复位
- **来源**：af240a8

### 3.3 AI 会话 `session_id` 不含 `from_uid` → 同店铺买家串历史

- **证据**：`Agent/CustomerAgent/agent.py:393` `session_id = f"{context.channel_type}{context.kwargs.user_id}"`；同函数 dependencies（:395-401）中明明有 `from_uid`
- **后果**：同一客服账号下所有买家共用一份 AI 会话历史（AI 会引用别的买家上下文）
- **修复**（6ef2427 核心）：`session_id` 加入 `from_uid`，并按会话加锁
- **来源**：6ef2427

### 3.4 `mall_cs` 消息全短路 → `TRANSFER` 分支不可达、未知类型静默丢

- **证据**：`Channel/pinduoduo/pdd_message.py:184-200`——`from_user == "mall_cs"` 的消息（除 type=31）全部提前 `return`，`_process_message` 中 `:227-228` 的 `handle_transfer` 对 mall_cs 来源永远不可达；未知类型（:229-231）标 SYSTEM_STATUS 后在 `pdd_message_handler.py:117-118` 被 debug 忽略（静默丢）
- **修复**（dfe2bdb 整套）：按 (来源, 类型, 子类型) 动作表路由替代手工集合；补 type=8/20/30/31/41/56/97+sub=2；未知类型降级 CONTEXT_ONLY + 告警；白名单由动作表推导。**需与 60s cooldown、夜间防抖联调并回归 MALL_CS→staff_reply_event 行为**
- **来源**：dfe2bdb（+4781d68 的 67 例回归测试）

### 3.5 Cookie 验证接口 `json=` 与真实请求 `data=` 不一致

- **证据**：`Channel/pinduoduo/cookie_utils.py:66` `requests.post(url, json=payload)`；`Channel/pinduoduo/utils/API/get_token.py:18` `self.post(url, data=payload)`
- **后果**：同一 getToken 接口两种编码，过期检测可能误判
- **修复**：`json=payload` → `data=payload`（1 行）
- **来源**：f45f701 ①

### 3.6 关键词/兜底发送同步阻塞事件循环

- **证据**：`Message/handlers/keyword_handler.py:141,264-288`、`Message/core/enhanced_consumer.py:402,949` 发送调未包 `asyncio.to_thread`
- **修复**：4 处发送包 `to_thread`；**保持 staff 60s cooldown 判定在发送前不变**
- **来源**：f703dd2

### 3.7 打包版 Playwright 驱动定位（exe 版登录必挂）

- **证据**：`scripts/agent_customer.spec` 对 playwright 仅 `hiddenimports`，`binaries=[]`；`pdd_login.py` 无 frozen 分支——打包后 driver（node.exe/cli.js）定位指向构建机路径
- **修复**（85ee37a 移植）：spec 加 `collect_all("playwright")` + node.exe 进 binaries；`pdd_login.py` frozen 时接管 `PLAYWRIGHT_NODEJS_PATH` + monkeypatch `compute_driver_executable`
- **来源**：85ee37a

## 4. 可移植清单（按主题）

### 4.1 消息路由与协议（最高优先）

| 项        | 内容                                                       | 来源      | 建议                                                                     |
| -------- | -------------------------------------------------------- | ------- | ---------------------------------------------------------------------- |
| 动作表路由    | (来源,类型,子类型)→Action 映射；取消 mall_cs 短路；未知类型降级+告警；客服卡片写入会话历史 | dfe2bdb | 整套移植；`bridge/context.py` 加 origin/action/pdd_type 字段；保留 cooldown 与防抖不动 |
| 67 例回归测试 | unittest，解析 16 + 路由 51，锁"不静默丢弃"不变量                       | 4781d68 | 随动作表成套移植（仓库现无 tests 目录）                                                |

### 4.2 登录健壮性

| 项                | 内容                                                                     | 来源      | 建议                                       |
| ---------------- | ---------------------------------------------------------------------- | ------- | ---------------------------------------- |
| 本地浏览器登录          | chrome/msedge 复用本机浏览器，免下载 Chromium                                     | d57835d | 移植；保留本地特有 `proxy=get_playwright_proxy()` |
| 启动参数精简           | 删 `--disable-web-security` 等 4 个敏感 args（实测致部分 Edge 秒退）；exe 路径探测 + 三级回退 | 49c6d0e | 高价值；对照 `pdd_login.py:70-71`              |
| CDP-over-port 回退 | subprocess 拉起 + connect_over_cdp 绕 pipe 秒退                             | a7b3cc3 | 作为最后防线移植                                 |
| CDP 关闭杀进程树       | `taskkill /T /F` 防窗口残留                                                 | 75fab0f | 随 CDP 一起                                 |
| 锁文件清理            | 清 SingletonLock/Cookie/Socket + stderr 诊断                              | 99cbeb0 | 独立小块，登录/refresh 开头调用（防"profile 被占用"秒退）   |

### 4.3 打包 / 构建

| 项                 | 内容                                             | 来源      | 建议                                |
| ----------------- | ---------------------------------------------- | ------- | --------------------------------- |
| Playwright 驱动内置   | 见 3.7                                          | 85ee37a | **必修**                            |
| PyInstaller 路径    | 裸 `"pyinstaller"` 改 venv 路径                    | b475feb | `build_win_exe.py:51` 同样会踩 PATH 坑 |
| 安装包安全点            | 排除 config.json 防 api_key 泄漏、免管理员装 LocalAppData | b475feb | 若用 installer.nsi 分发则借鉴            |
| UTF-8 reconfigure | stdout/stderr reconfigure                      | a02c760 | 2 行防御，顺带                          |
| 版本号从 tag          | git describe                                   | 285509d | 低优先（本地已有 v1.x tag）                |

### 4.4 架构 / 性能 / 正确性

| 项                     | 内容                                                            | 来源      | 建议                                                  |
| --------------------- | ------------------------------------------------------------- | ------- | --------------------------------------------------- |
| CustomerAgent DI 单例   | 注册进容器，消除重连重建/丢状态                                              | f703dd2 | `configure_standard_services` 加注册，去掉两处 fallback new |
| resource_manager 同步清理 | weakref 回调改同步（GC 线程 create_task 必失败）                          | f703dd2 | 与 QThread 清理隐患直接相关                                  |
| GetToken 包 to_thread  | 重登场景阻塞主循环                                                     | f703dd2 | `pdd_lifecycle.py:182-183`                          |
| config 原子写            | save() 临时文件+replace；atomic_update deepcopy；`__contains__` 点分隔 | 3feffd5 | 防配置损坏                                               |
| agno 库补 busy_timeout  | sessions/contents 库未设                                         | f703dd2 | 部分移植                                                |

### 4.5 数据安全

| 项               | 内容                               | 来源      | 建议                       |
| --------------- | -------------------------------- | ------- | ------------------------ |
| 知识同步防覆盖         | 失败不再计 success、DB 只填空不覆盖、三态计数     | 77b77e2 | 当前"失败也覆盖"正静默污染商品库        |
| 图片 URL 安全校验     | 拒内网/回环/元数据 IP（防 agno 主动抓取 SSRF）  | 51fc9fb | 小件移植                     |
| 拒图降级重试          | 模型拒图时 strip 后纯文本重试一次             | 51fc9fb | 小件移植；历史图上限先实测 agno 实际携带数 |
| anti-content 补全 | `get_goods_detail` 同列表接口 headers | 3feffd5 | 防风控拒绝                    |

### 4.6 体验 / 工具

| 项          | 内容                                                           | 来源                | 建议         |
| ---------- | ------------------------------------------------------------ | ----------------- | ---------- |
| 日志页增量插入    | beginInsert/RemoveRows 替代 layoutChanged 全量；auto_scroll 勾选框落地 | b90c887 + f703dd2 | 本地复选框当前是空壳 |
| WS 抓包脚本    | CDP 抓 WSS + 16 字节头/protobuf/gzip 解码 + 11 例测试                 | 18b6597           | 低耦合可直接搬    |
| 登录失败提示     | 达上限置 ERROR                                                   | f45f701 ③         | P2         |
| 冷却/锁语义     | 锁占用时误判成功修复                                                   | f45f701 ④         | P2         |
| 多候选选择器     | pdd_login.py:81/93 单选择器硬编码 hash 类名                           | f45f701 ⑤         | P1         |
| queue 去重上限 | MAX_DEDUP_ENTRIES                                            | f703dd2           | 低优先        |
| 死代码清理      | connection_pool.py / cache.py 无引用                            | f703dd2           | 低优先        |

## 5. 不适用清单

| 提交                        | 内容                    | 原因                         |
| ------------------------- | --------------------- | -------------------------- |
| 9eea79d + 4343a1f         | LiteLLM 六家供应商接入及其打包修复 | 本地走 agno，深度集成；LiteLLM 与栈冲突 |
| 3feffd5 主体                | Agno → custom/ 自研框架迁移 | 有意路线差异（迁移量 3000+ 行）        |
| 82f687a / 956fefc         | README 同步             | 本地 README 已重写              |
| 52321df / b475feb 的 CI 部分 | GitHub Actions        | 本地无 CI 体系                  |
| f864ee4                   | fake-ip logo 安全抓取     | 本地无该机制（未来引入时再同步）           |
| 5946729                   | _refresh_cs_table 修复  | 本地无对应代码                    |
| 4b7b72a                   | 客服知识 Excel 导入导出       | SQLite 客服知识链路已弃用（本地走向量库）   |
| e423ff5                   | docs 移出版本库            | 按是否留存设计文档决定                |
| 3feffd5 部分                | preprocessor 图片转纯文本   | 本地视觉模型方向更超前                |

## 6. 已覆盖（含超越）

| 提交         | 说明              | 证据                                                             |
| ---------- | --------------- | -------------------------------------------------------------- |
| 32924f9    | db_manager 统一单例 | `database/__init__.py:13` `_DIProxy` 与 `get_db_manager` 均已走 DI |
| 44e1bff    | Cookie 自动登录优化   | `cookie_cache.py` 全文（另加失败计数 + Bark）                            |
| 44e1bff 附带 | API_VERSION 引用  | `pdd_lifecycle.py:200` 写法一致                                    |

## 7. 待决策项

| 项                                             | 取舍                                      |
| --------------------------------------------- | --------------------------------------- |
| `bridge/sender.py` 发送抽象层 + `service/` 层       | 架构解耦投资 vs 最小改动偏好；当前 `_DIProxy` 已零成本走 DI |
| `enhanced_consumer` 停止语义（gather 等待 vs cancel） | 本地 cancel 可能是有意（QThread 关闭修复）；建议实测后不动   |
| 4b7b72a 客服知识导入导出                              | 取决于 SQLite 客服知识链路去留                     |
| 图片历史还原上限                                      | 先实测 agno 每轮实际携带图片数                      |

## 附录 A：31 提交全表

| #   | SHA     | 日期    | 提交                   | 判定               |
| --- | ------- | ----- | -------------------- | ---------------- |
| 1   | 4343a1f | 09-21 | 打包版缺 litellm 数据文件修复  | ❌                |
| 2   | e423ff5 | 09-21 | docs 移出版本库           | ❌/可选             |
| 3   | af240a8 | 09-21 | 停止账号并发清理 KeyError    | 🔁 P0            |
| 4   | 51fc9fb | 09-21 | 买家图片接入多模态            | 🔁 小件            |
| 5   | 4b7b72a | 09-21 | 客服知识导入导出             | ❌/待决策            |
| 6   | 77b77e2 | 09-21 | 火山 JSON 约束 + 知识降级    | 🔁 防护部分          |
| 7   | 4781d68 | 09-21 | 消息解析路由回归测试 67 例      | 🔁 随 dfe2bdb     |
| 8   | dfe2bdb | 09-21 | 消息动作表路由              | 🔁 最高优先          |
| 9   | 18b6597 | 09-20 | WS 抓包解码脚本            | 🔁 中优先           |
| 10  | f45f701 | 08-13 | Cookie 重登五缺陷         | 🔁 ①⑤ P1         |
| 11  | f864ee4 | 08-12 | fake-ip logo 加载      | ❌                |
| 12  | 9eea79d | 08-12 | LiteLLM 六供应商         | ❌                |
| 13  | 52321df | 08-05 | 打包 UTF-8/Node24      | ❌                |
| 14  | 6ef2427 | 08-05 | 账号运行时状态隔离            | 🔁 session_id P0 |
| 15  | 99cbeb0 | 07-27 | profile 锁文件清理        | 🔁               |
| 16  | 75fab0f | 07-22 | CDP 关闭杀进程树           | 🔁 随 CDP         |
| 17  | a7b3cc3 | 07-22 | CDP-over-port 回退     | 🔁               |
| 18  | 49c6d0e | 07-22 | 本地浏览器启动健壮性           | 🔁 高价值           |
| 19  | 956fefc | 07-22 | README 安装包说明         | ❌                |
| 20  | 285509d | 07-22 | 版本号从 git tag         | 🔁 低优先           |
| 21  | a02c760 | 07-22 | 强制 UTF-8             | 🔁 低成本           |
| 22  | b475feb | 07-22 | Inno Setup + CI      | ❓ 部分             |
| 23  | 85ee37a | 07-22 | 打包内置 Playwright 驱动   | 🔁 必修            |
| 24  | d57835d | 07-22 | 本地 Chrome/Edge 登录    | 🔁 高价值           |
| 25  | b90c887 | 07-22 | 日志页滚动修复              | 🔁               |
| 26  | f703dd2 | 07-22 | 审计修复（解耦/性能/正确性）      | 🔁 多项            |
| 27  | 5946729 | 05-27 | _refresh_cs_table 修复 | ❌                |
| 28  | 44e1bff | 05-21 | Cookie 自动登录四项优化      | ✅ 已覆盖            |
| 29  | 32924f9 | 04-23 | db_manager 单例统一      | ✅ 已覆盖            |
| 30  | 82f687a | 04-23 | README 架构同步          | ❌                |
| 31  | 3feffd5 | 04-23 | v1.2.0 架构重构与模块化升级    | ❌ 主体 / 🔁 个别项    |

## 附录 B：验证说明

- 第 3 节全部 P0 结论经人工逐行读代码复核（channel.py / db_manager.py / pdd_lifecycle.py / agent.py / pdd_message.py / cookie_utils.py / get_token.py / manager.py）
- 第 4 节各项来自四组并行代码分析（对照 `git show <sha>` diff 与本地实现），移植时需按对应提交 diff 逐项复核
- 本地已新增 remote-tracking ref `jc0v0/main`（仅引用，未改动工作区与任何分支）

---

## 8. 移植决策清单（请填写）

> 填写方式：在最后一列填 `✅`（要做）/ `❌`（不做）/ `？`（待实测或再议）。
> 填完可直接回复编号范围，例如「做 A1~A6、B1、B3、C 全组、D 全组」。

### A. P0 缺陷修复（建议全做）

| #   | 项目               | 说明                                                                                 | 改动量               | 建议         | 是否要做 |
| --- | ---------------- | ---------------------------------------------------------------------------------- | ----------------- | ---------- | ---- |
| A1  | 修复删店铺 TypeError  | `channel.py:48,53` 多传 `shop_name` 实参，删掉即可                                          | 2 行               | ⭐推荐        | 1    |
| A2  | 停止清理防护           | `pdd_lifecycle.py` `del`→`pop(k,None)` + 3 个清理方法跳过当前任务；`manager.py` `_stopping` 复位 | 小                 | ⭐推荐        | 1    |
| A3  | 修复买家会话串历史        | `agent.py:393` `session_id` 补 `from_uid` + 按会话加锁                                   | 小                 | ⭐推荐        | 1    |
| A4  | Cookie 验证编码      | `cookie_utils.py:66` `json=` → `data=`                                             | 1 行               | ⭐推荐        | 1    |
| A5  | 发送不阻塞事件循环        | keyword_handler / enhanced_consumer 共 4 处包 `to_thread`（cooldown 判定位置不动）            | 小                 | ⭐推荐        | 1    |
| A6  | 打包版登录修复          | spec `collect_all(playwright)` + node.exe；`pdd_login` frozen 接管驱动路径                | 中                 | ⭐（用打包版则必做） | 1    |
| A7  | 消息动作表路由 + 67 例测试 | dfe2bdb + 4781d68 整套；修转人工不可达、未知消息静默丢                                               | 大（需与 cooldown 联调） | ⭐单独批次      | 1    |

### B. 登录健壮性

| #   | 项目                       | 说明                                                        | 改动量 | 建议        | 是否要做 |
| --- | ------------------------ | --------------------------------------------------------- | --- | --------- | ---- |
| B1  | 本地 Chrome/Edge 登录 + 参数精简 | d57835d + 49c6d0e；免下载 Chromium、删导致 Edge 秒退的 args、exe 路径探测 | 中   | 推荐        | 1    |
| B2  | CDP 回退 + 关闭杀进程树          | a7b3cc3 + 75fab0f；部分电脑 pipe 秒退的防线                         | 中   | 可选（依赖 B1） | 1    |
| B3  | 登录 profile 锁文件清理         | 99cbeb0；防"profile 被占用"秒退                                  | 小   | 推荐        | 1    |

### C. 架构 / 性能

| #   | 项目                                          | 说明                                             | 改动量 | 建议  | 是否要做 |
| --- | ------------------------------------------- | ---------------------------------------------- | --- | --- | ---- |
| C1  | CustomerAgent 注册 DI 单例                      | 防每次重连重建 Agent（丢内存态）                            | 小   | 推荐  | 1    |
| C2  | resource_manager 同步清理                       | weakref 回调 GC 线程 `create_task` 必失败             | 小   | 推荐  | 1    |
| C3  | GetToken 包 to_thread + agno 库补 busy_timeout | 重登阻塞主循环 / 数据库锁竞争                               | 小   | 可选  | 1    |
| C4  | config 原子写                                  | `save()` 临时文件+replace；`atomic_update` deepcopy | 小   | 推荐  | 1    |

### D. 数据安全

| #   | 项目               | 说明                                       | 改动量 | 建议  | 是否要做 |
| --- | ---------------- | ---------------------------------------- | --- | --- | ---- |
| D1  | 知识同步防覆盖          | 失败不计 success、DB 只填空不覆盖、三态计数（当前失败会覆盖已有知识） | 小   | 推荐  | 1    |
| D2  | 图片 URL 校验 + 拒图降级 | 拒内网/回环 IP（防 SSRF）；模型拒图时纯文本重试一次           | 小   | 推荐  | 1    |
| D3  | anti-content 补全  | `get_goods_detail` 补接口 headers（与列表接口一致）  | 小   | 推荐  | 1    |

### E. 体验 / 工具

| #   | 项目               | 说明                                      | 改动量 | 建议  | 是否要做 |
| --- | ---------------- | --------------------------------------- | --- | --- | ---- |
| E1  | 日志页增量插入 + 自动滚动落地 | b90c887 + f703dd2；当前 auto_scroll 勾选框是空壳 | 小   | 推荐  | 1    |
| E2  | WS 抓包解码脚本        | 18b6597；用于发现协议新消息类型                     | 小   | 可选  | 1    |
| E3  | 登录多候选选择器         | f45f701⑤；PDD 页面类名变化时单选择器会失效             | 小   | P1  | 1    |
| E4  | 重登失败提示 + 冷却/锁语义  | f45f701③④；锁占用时误判成功等问题                   | 小   | P2  | 1    |

### F. 打包 / 构建

| #   | 项目                                  | 说明                                  | 改动量 | 建议  | 是否要做 |
| --- | ----------------------------------- | ----------------------------------- | --- | --- | ---- |
| F1  | PyInstaller 路径修复 + UTF-8 + 版本号从 tag | b475feb + a02c760 + 285509d 三小件     | 小   | 可选  | 1    |
| F2  | 安装包安全点                              | 排除 config.json（防 api_key 泄漏）、免管理员安装 | 小   | 按需  | 1    |

### G. 低优先 / 可延后

| #   | 项目                 | 说明                                                  | 改动量 | 建议  | 是否要做 |
| --- | ------------------ | --------------------------------------------------- | --- | --- | ---- |
| G1  | queue 去重上限 + 死代码清理 | MAX_DEDUP_ENTRIES；connection_pool.py / cache.py 无引用 | 小   | 低   | 1    |
| G2  | docs 目录移出 / 忽略     | e423ff5；按是否留存设计文档决定                                 | 1 行 | 按需  | 1    |

### H. 待决策（填写时如有意见请备注）

| #   | 项目                       | 说明                                          | 改动量 | 建议  | 是否要做 |
| --- | ------------------------ | ------------------------------------------- | --- | --- | ---- |
| H1  | sender.py + service/ 抽象层 | 架构解耦投资 vs 最小改动偏好                            | 中   | ？   | 按推荐做 |
| H2  | enhanced_consumer 停止语义   | 上游改"等待"；本地 cancel 可能是有意（QThread 修复），建议实测后不动 | —   | ？   | 0    |
| H3  | 客服知识导入导出                 | 4b7b72a；取决于 SQLite 客服知识链路去留                 | 中   | ？   | 0    |
| H4  | 图片历史还原上限                 | 先实测 agno 每轮实际携带图片数再定                        | 小   | ？   | 实测一下 |

> **H 组详细信息（最新建议以此为准：H1 只做 sender.py 部分；H2 保持现状；H3 跳过；H4 先实测）**

#### H1 详细信息：`bridge/sender.py` + `service/` 抽象层（来自 f703dd2）

**上游做法**（两件独立的事）：

1. `bridge/sender.py`（74 行）：`ReplySender` 抽象基类定义 5 个方法（send_text / send_image / send_product_card / get_cs_list / transfer_to_cs）；`PinduoduoSender` 封装现有 SendMessage；`get_sender()` 工厂按渠道返回。目的：Message 层与 Agent 工具不再直接 import 拼多多模块，加渠道时只新增 Sender 实现。
2. `service/`（83 行）：AccountService / KeywordService 薄封装，只转发 db_manager 方法、不改业务逻辑。目的：UI 不再直接 import database。

**你的现状**：

- Message 层有 **6 处**直接 `from Channel.pinduoduo.utils.API.send_message import SendMessage`：`enhanced_consumer.py:402,949`、`keyword_handler.py:11,141,264`、`ai_handler.py:161`
- UI 已走 `core/service_providers.py` 的 `_DIProxy`（零成本转发 DI），不像上游直接 import database

**取舍**：

- sender.py：做 → 6 处散落发送收敛为统一入口（未来统一加 to_thread / 日志 / cooldown 方便）；不做 → 现状正常工作（仅拼多多一个渠道）
- service/：对你价值低（UI 已走 _DIProxy，再包一层是纯形式）
- **推荐**：sender.py 可抄（适配你的 SendMessage 接口），且**与 A5（发送包 to_thread）合并执行**——改 6 处调用点时一次到位；service/ 跳过

#### H2 详细信息：consumer 停止语义（来自 6ef2427，与 A3 同提交）

**上游做法**：consumer 重构为**固定 worker 池**（替代每条消息 spawn task，队列 maxsize 成为真正背压）+ `stop(drain_timeout=5.0)` 协作式停止（最多排空 5 秒）+ per-channel queue_manager。

**你的现状**：EnhancedConsumer 是**每用户分桶队列**（保序）+ **cancel 语义**停止（cancel 所有任务、semaphore 等待、清空，`enhanced_consumer.py:865-899`）——你在 e077fda 专门修过"关闭时 ~30s 卡顿"，cancel 是那次优化的成果。

**取舍**：

- drain（上游）：优雅但可能被长任务拖住 5s，与你的快速关闭目标冲突
- cancel（你）：关闭快，代价是丢正在处理的消息（可接受——停止后也发不出去）
- **建议：保持现状**。为对齐上游改回 drain 会重新引入关闭风险，收益为零；worker 池设计好但属架构替换非 bug 修复，不值得重写已验证的管道。

#### H3 详细信息：客服知识导入导出（4b7b72a）

**上游提供**（配套其 SQLite 客服知识管理页）：导出全店客服知识 xlsx（标题/内容/标签/启用）；下载导入模板；导入按表头名识别列、兼容旧「一级/二级分类」格式、公式注入防护、成功/重复/格式问题分计数 + 行号反馈；配套 342 行测试 + UI 改动 113 行。

**你的现状**（已交叉核实）：

- `database/knowledge_service.py` 有客服知识 CRUD + `batch_import_customer_service`（:278），但**全仓无任何引用方**（ui/、Message/、scripts/、Channel/ 及全仓 import 交叉验证）——是死代码
- 你的知识库 UI 走 LanceDB 向量库（KnowledgeManager，已进程隔离），与 SQLite 客服知识无关
- 转人工（`_transfer_to_human:260-294`）直接调 API，不读话术库

**结论**：**建议跳过**。若你还想恢复"人工话术库"功能，4b7b72a 的格式设计可借鉴（表头别名/模板/行号级反馈/防注入），但落点应在你的 LanceDB 链路，属新功能开发而非移植。

#### H4 详细信息：图片历史还原上限（来自 51fc9fb）

**上游做法**（其自研框架内）：图片以"信封"持久化，历史消息重建时**仅还原最近若干条图**；模型拒图时单次降级纯文本。

**你的现状**（agno 2.3.4 源码级证据）：

- 图片传入：`arun(images=[Image(url=...)])`（`agent.py:415/419/469`），URL 直传优先、失败转 base64 重试
- agno 默认 **`store_media=True` + `send_media_to_model=True`**（你的 Agent 初始化未显式设置，用默认值；`.venv/.../agno/agent/agent.py:296,298`）
- 源码 `agno/agent/agent.py:7609`：`images=None if not self.send_media_to_model else images` —— **历史消息的 images 在 True 时会随请求发送**
- 你的配置：`add_history_to_context=True` + `num_history_runs=8`

**推论**：每轮请求可能重复携带历史 8 轮内的图片（本轮 + 历史叠加），成本随历史图数量增长。**准确行为需实测**。

**实测方法**（约 10 分钟）：连续发 2 张图 + 1 条纯文字 → 看第 3 轮请求 payload 的 images 数量（开 OpenAI 客户端 DEBUG 日志或抓包）。

**备选方案（实测确认后选）**：

- a. 调小 `num_history_runs`（8→4）：简单但减半非对症
- b. 试 `send_media_to_model=False`：若只影响历史重放（:7609），可实现"本轮发图、历史不带图"——需实测区分当前轮/历史路径（当前轮走 `run_input.images`）
- c. 会话历史层裁剪（对标上游"仅保留最近若干条"）：最精确但要碰 agno session 逻辑

**建议**：先实测；确认重复携带后 → 优先试 b，其次 a，最后 c。

### 执行批次建议（确认范围后参考）

1. **批次 1**：A1~A6（小改动密集，一次提交全部 P0 修复）
2. **批次 2**：A7（动作表路由 + 测试，单独大件，含回归验证）
3. **批次 3**：B + C + D 组（登录健壮性、架构、数据安全）
4. **批次 4**：E + F + G + H 组按勾选执行

---

## 9. 执行记录（2026-10-09 全部完成）

| 批次 | 提交 | 内容 | 状态 |
|---|---|---|---|
| 1 | `3ff4252` | A1 删店铺 TypeError / A2 停止清理防护 / A3 买家会话隔离 / A4 cookie 编码 / A5+H1 sender.py 发送抽象+to_thread / A6 打包 Playwright 驱动 | ✅ 已推送 |
| 2 | `b890fc6` | A7 消息动作表路由（转人工恢复可达、未知消息不再静默丢）+ 63 例回归测试 | ✅ 已推送 |
| 3 | `94adbd4` | B1~B3 登录四级回退（本地 Chrome/Edge+CDP+锁清理）/ C1~C4（DI 单例、资源清理、db_pragma、config 原子写）/ D1~D3（知识防覆盖、图片 URL 校验+拒图降级、anti-content） | ✅ 已推送 |
| 4 | `385f118` | E1~E4（日志页滚动/WS 抓包脚本/登录多候选选择器/重登失败弹窗）/ F1~F2（构建修复+Inno 安装包）/ G1（去重上限+死代码清理） | ✅ 已推送 |
| H4 | `70a3932` | 历史图片重放实测 + 补丁（只保留最近 2 条历史图，当前轮不受影响） | ✅ 已推送 |

**验证**：pyright 改动文件 0 新增错误；回归测试 74 例全过（解析 16 + 路由 47 + WS 解码 11）。

**未执行**：
- **G2**（docs 移出版本库）：docs 内截图为 README 资源、本分析文档为工作产物，移出会破坏内容——如需调整请指明范围。
- **H2 / H3**：按决策跳过（保持现状 / 不适用）。

**建议实测项**：B 组登录改造（本机已探测到 Chrome，首次线上登录时确认一下四级回退日志）；H4 补丁（发图 → 追问场景确认 AI 仍能引用最近图片）。

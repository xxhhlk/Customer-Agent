# 素材空间发送能力调研（图片空间 / 视频空间）

> 调研日期：2026-10-11
> 目标：让客服助手在聊天窗口从拼多多「图片空间 / 视频空间」挑选素材，直接发给当前会话买家
> 状态：**接口已实测跑通**（用库里已存登录态直连，只读 GET/POST，未发消息、未改库）
> 方法：不启动浏览器、不抢 profile —— 用 `Account.cookies` 直连商家后台，拉真实页面与其前端 JS bundle，
> 从中还原接口，再用同源请求验证。工具脚本：`scripts/pdd_web_probe.py`

---

## 0. 结论速览

| # | 结论 | 证据 |
|---|---|---|
| 1 | **素材列表接口已找到并实测可用**：`POST /garner/mms/file/list`，**不需要 anti-content** | 实调返回 `success:true`，本店 226 条素材 |
| 2 | **图片通道现在就能做**：素材项的 `url` 直接喂 `send_image(uid, url)` | `send_image` 线上已跑通（聊天窗转发在用） |
| 3 | **视频通道的 `info` 结构已解出并逐字段验证**（`file_id` = 素材 `id`、`download_url` = 素材 `transcode_url`、其余映射见 §3.2），现有代码只传了 `preview`+`duration`，正是静默失败的根因 | 库里真实「客服发出」的视频消息 `media_meta.raw_info`，与素材库同名条目逐字段比对 |
| 4 | **无需浏览器抓包**：图片空间是独立微前端（`mms.pinduoduo.com/material/`，代号 galerie-go），但列表能力由 `garner` 接口提供，纯 HTTP 可调 | 前端 JS 还原 + 实调 |
| 5 | ★ **「视频空间」= `dir_id: -1`**，与图片空间同一接口、独立计容（20GB vs 10GB） | `/material/service` 页面默认参数 + 实调（5 条视频、含已知 `file_id`） |
| 6 | 素材**文件夹**、容量、删除/移动/重命名接口也一并拿到 | 见 §2 |
| 7 | ★ **图片空间业务上只取「客服专用」文件夹**（按文件夹名解析；两账号实测 `182454400` / `190003343`） | 见 §1 / §5.1 |
| 8 | ★ **素材空间按账号隔离**；缓存与在途请求必须带 `(shop_id, user_id)`，否则会跨账号串数据 | 见 §5.1「账号隔离」 |

---

## 1. 素材体系（实测修正：**两个独立库，同一接口**）

**核心结论：所谓「图片空间」和「视频空间」是同一套 `garner` 服务下的两个独立库，靠 `dir_id` 区分，容量各自独立计算。**

| 空间 | 后台入口 | 列表请求 | 实测条目 | 实测容量 |
|---|---|---|---|---|
| **图片空间** | `https://mms.pinduoduo.com/material/`（独立 SPA，代号 **galerie-go**） | `/garner/mms/file/list`，`dir_id` 省略 或 `0` | **226**（224 图 + 2 视频） | 已用 60MB / 上限 **10GB** |
| **客服/售后专用视频空间** | `https://mms.pinduoduo.com/material/service` | 同上，**`dir_id: -1`** ★ | **5**（全视频） | 已用 84MB / 上限 **20GB** |
| 回收站 | `https://mms.pinduoduo.com/material/recycle` | 同 SPA 子路由 | — | — |

> **业务约定（2026-10-11 定）：图片空间只取「客服专用」文件夹下的图片**，不展示全店素材。
> 实现按**文件夹名关键字**解析（`MaterialSpace.get_cs_dir_id()`，关键字 `CS_DIR_NAME_KEYWORD = "客服"`），
> 不写死 id —— 换店铺、改文件夹名都不受影响；解析结果按 `(shop_id, user_id)` 缓存 10 分钟。
>
> **★ 为什么必须按名字解析（实测）**：同一套代码下，两个账号的客服文件夹 id **并不相同**：
>
> | 账号 | shop_id | 客服文件夹 dir_id | 图片数 | 视频空间 |
> |---|---|---|---|---|
> | 技术图源服务 | 277169009 | **182454400** | 9 | 5 |
> | 奥维高清图源服务社 | 455871288 | **190003343** | 9 | 5 |
>
> 若写死 `182454400`，第二个账号会直接拿不到文件夹。**素材空间是按账号隔离的独立库。**

`sumSize` 实测（同店、同账号，只换 `dir_id`）：

```
dir_id = -1  →  sum_size=88,046,809 (84MB)  max_size=21,474,836,480 (20GB)   ← 客服专用视频库
dir_id = 0   →  sum_size=63,203,902 (60MB)  max_size=10,737,418,240 (10GB)   ← 图片空间
```

`dir_id` 取值语义（实测）：

| 值 | 含义 |
|---|---|
| 省略 | 全店素材（含所有文件夹）→ 226 条 |
| `0` | 根目录（不含文件夹）→ 54 条 |
| `-1` | ★ **客服专用视频库**（不在 `dirListV2` 的文件夹列表里） |
| `-2` | 空（语义未明） |
| 正整数 | 指定文件夹 id（见 `dirListV2`） |

> `/material/service` 页面代码内默认请求体即 `{page:1, page_size:10, dir_id:-1, tool_source:""}` —— 这是 `dir_id=-1` 的来源。

素材 CDN（公网直链、无签名参数）：

- `https://img.pddpic.com/mms-goods-image/<date>/<uuid>.jpeg` — 商品图
- `https://img.pddpic.com/mms-material-img/<date>/<uuid>.png` — 素材空间图
- `https://video5.pddpic.com/i1/.../<hash>.mp4` — 视频源；`.f30.mp4` = 转码后（发送用这个）
- `https://img.pddpic.com/mms-material-img/.../<hash>.mp4.pdd.000001.jpeg` — 视频封面

---

## 2. 素材接口（实测，全部 `POST https://mms.pinduoduo.com`，仅需 cookies）

### 2.1 文件列表 ★核心

```
POST /garner/mms/file/list
body: {
  page: 1,
  page_size: 10,
  file_type_desc: "video",   // "video" 只出视频；"pic" 只出图片；""/省略 = 全部
  file_name: "",             // 按文件名搜索
  check_status_list: [],     // 审核状态过滤
  order_by: "",              // 排序
  tool_source: "",            // 实测：传数字(0/1/2/3/9)一律 0 条；传 'dd_video' 命中 1 条；
                             //       传 'local'/'out_video' 只能在 dir_id=-1 下解释为"上传来源"列
  dir_id: -1                 // ★ -1=客服专用视频库；省略=全店；0=根目录；正数=文件夹 id
}
→ {"success":true,"error_code":1000000,
   "result":{"total":226,"list":[ ... ]}}
```

> 过滤灵敏度实测：`file_type_desc` 只认 `"video"`（→2 条）与 `"pic"`（→224 条）；其它值（`out_video`/`service_video`/`all`/`service`…）等同不传。
> **唯一能拿到「视频空间」的方式是 `dir_id: -1`。**

**图片项（真实响应）**

```json
{
  "id": 88540451772, "name": "IMG_6363", "extension": "jpeg", "file_type": "pic",
  "url": "https://img.pddpic.com/mms-goods-image/2026-06-23/23047245-....jpeg",
  "transcode_url": "https://img.pddpic.com/mms-goods-image/2026-06-23/23047245-....jpeg",
  "size": 254634, "check_status": 2, "signed_url": null, "is_deleted": 0,
  "tool_source": null, "out_video_url": null,
  "mall_dir_name_list": [{"dir_id":224251748,"name":"主图"}],
  "extra_info": {"video_cover_url":null,"duration":null,"height":1024,"width":1024,
                 "size":254634,"f20_url":null,"f30_url":null,"vid":null}
}
```

**视频项（真实响应）**

```json
{
  "id": 49109762417, "name": "多多视频讲解同步_...", "extension": "mp4", "file_type": "video",
  "url": "https://video5.pddpic.com/i1/porphyrios-task/2024-07-23/235c34ed....mp4",
  "transcode_url": "https://video5.pddpic.com/i1/porphyrios-task/2024-07-23/235c34ed....mp4.f30.mp4",
  "tool_source": "dd_video", "check_status": 2,
  "extra_info": {
    "video_cover_url": "https://img.pddpic.com/mms-material-img/.../235c34ed....mp4.pdd.000001.jpeg",
    "duration": 52, "width": 720, "height": 720, "size": 12516129,
    "f20_url": "...f20.mp4", "f30_url": "...f30.mp4", "audio_type": 2
  }
}
```

要点：
- `file_type` 取值为 **`"pic"` / `"video"`**（不是 image/video）；
- 发视频用 `transcode_url`（`.f30.mp4`），封面用 `extra_info.video_cover_url`，时长/宽高在 `extra_info`；
- `check_status` 2 = 已通过（`-1`/`-2` 为不可用，前端会过滤）。

### 2.2 其余接口（同一族，已从 JS 还原）

| 接口 | 作用 | body 要点 |
|---|---|---|
| `/garner/mms/file/dir_list` | 目录+文件混合列表 | `{if_query_dir, order_by, dir_id, page, page_size, file_param:{file_name,...}, file_extension}` |
| `/garner/mms/dir/dirListV2` | **文件夹清单** | `{page,page_size}` → `result:[{id,parent_dir_id,name,child_file_count,child_dir_count}]` |
| `/garner/mms/dir/createV2` / `deleteV2` / `rename` / `move` | 文件夹增删改移 | — |
| `/garner/mms/file/create` / `createVideo` | 登记素材记录 | 上传完成后调用 |
| `/garner/mms/file/delete` / `moveFile` / `rename` | 文件增删改移 | `moveFile: {old_dir_id,new_dir_id,file_id_list}` |
| `/garner/mms/file/queryFileDetail` | 文件详情 | `{file_id}`（实测 `2000001 该文件记录不存在`，语义待确认） |
| `/garner/mms/file/preCheck` / `appeal` / `sumSize` / `fileSceneList` | 预检 / 申诉 / 容量 / 场景 | — |
| `/garner/mms/file/trash/list` / `recover` / `delete` / `checkDir` | 回收站 | — |

本店实测文件夹（8 个）：`客服专用`（**id=182454400，9 图，图片空间实际取用**）、`1`（18）、`主图`（9）、`袁记云饺技术配方教程…`（15）、`奥维地图…` ×4（45/26/22/28）。

---

## 3. 发送通道

### 3.1 图片（可直接落地）

```
POST https://mms.pinduoduo.com/plateau/chat/send_message
message: { to:{role:"user",uid:<买家>}, from:{role:"mall_cs"},
           content:<图片 url>, msg_id:null, type:1, is_aut:0, manual_reply:1 }
```

即现有 `send_image()`（`Channel/pinduoduo/utils/API/send_message.py:55`），**线上已跑通**，素材项 `url` 直接可用。
（官方前端另会带 `info:{file_id,...}` 做本地占位替换，但实测非必需。）

### 3.2 视频（本仓库的硬阻塞，现已解出）

真实「客服发出」的视频消息（库内 `chat_message_records`，`media_meta.raw_info` 即原始 `info`）：

```json
{
  "download_url": "https://video5.pddpic.com/i1/2024-06-23/xxx.mp4.f30.mp4",
  "duration": 106,
  "file_id": "46393494801",
  "preview": { "url": "https://img.pddpic.com/mms-material-img/i1/2024-06-23/xxx.mp4.pdd.000001.jpeg",
               "size": {"width":1080, "height":1920} },
  "size": 35.28614807128906,
  "status": 0
}
```

其中 `content` = 同 `download_url`（`.f30.mp4`）。

**★ 映射规则已逐字段实测验证**（用 `dir_id=-1` 库里 `id=46393494801`（`奥维登录202406_无水印_Mux`）的素材项，与库中 3 条 `file_id=46393494801` 的视频消息比对）：

| 消息 `info` 字段 | 来源（素材项） | 实测结果 |
|---|---|---|
| `file_id` | 素材 `id`（字符串化） | ✅ 完全一致 |
| `download_url` | 素材 **`transcode_url`**（`.f30.mp4` 转码版） | ✅ 一致；取 `url`（原片、无 `.f30`）则 ❌ |
| `duration` | `extra_info.duration` | ✅ 一致 |
| `size` | 素材 `size` ÷ 1048576（单位 **MB**，float32） | ✅ 35.286146 vs 35.28614807128906（仅浮点精度差） |
| `preview.url` | `extra_info.video_cover_url` | ✅ 一致 |
| `preview.size.{width,height}` | `extra_info.width/height` | ✅ 一致 |
| `status` | 固定 **`0`** | ✅；注意素材 `check_status=2` ≠ 消息 `status=0`，**两者语义不同，不可混用** |

> **「素材项 id = 消息 info.file_id」至此确认为真**（§7 的 V1 已验证一半）。

**与现有代码的差异（根因）** —— `ui/chat_ui.py:356-402` 转发视频时只构造了：

```python
info = {"preview": {...}, "duration": ...}        # 缺 file_id / download_url / size / status
```

而 PDD 要求 `info` 至少含 `download_url` / `file_id` / `size` / `status`（`services/message_persistence.py:315` 已记录该报错），
这就是「`result=ok` 但不投递」的原因。

**正确构造（从素材项生成）**：

```python
item = <file/list 返回的视频项>
info = {
    "download_url": item["transcode_url"] or item["url"],
    "duration": item["extra_info"]["duration"],
    "file_id": str(item["id"]),
    "preview": {
        "url": item["extra_info"]["video_cover_url"],
        "size": {"width": item["extra_info"]["width"], "height": item["extra_info"]["height"]},
    },
    "size": round(item["extra_info"]["size"] / 1048576, 2),   # 单位 MB
    "status": 0,
}
sender.send_video(buyer_uid, info["download_url"], info=info)
```

> 转发既有视频的**最省事改法**：把库里存的 `media_meta.raw_info` **原样**作为 `info` 传回（含它自己的 `file_id`），
> 不再丢字段。这比重新拼装更稳。

---

## 4. 上传链路（本地文件 → URL，供未来「发本地图/视频」用）

从商家前端 JS 还原（`index.bundle.7d18b7a24...js` / `home.899320ee.chunk...js`）：

**小图（同步 base64/FormData）**

```
POST /plateau/file/pre_upload   {chat_type_id: 9|5|1, file_usage: 1}   # 9=客服会话, 5=conciliation, 1=默认
  → {result:{upload_signature, upload_url}}
POST <upload_url>/v3/store_image    (FormData: image=<file>, upload_sign=<sig>[, pic_operations])
  → 图片 URL
```
旧路径（仍存在）：`POST /galerie/business/get_signature {bucket_tag:"pdd_mms"}` → `POST https://file.yangkeduo.com/v3/store_image`。

**大文件 / 视频（COS 分片）**

```
POST {fileDomain}/api/galerie/cos_large_file/upload_init      # fileDomain = getOtherDomain("file") = file.yangkeduo.com
POST {fileDomain}/api/galerie/cos_large_file/upload_part      # 分片
POST {fileDomain}/api/galerie/cos_large_file/upload_complete  {upload_sign} → download_url
POST /garner/mms/file/createVideo                             # 登记到素材空间
```
另有通用文件通道 `/{general_file}`（`file_usage: 4=FILE`）。

> 结论：上传是**三段式 + 分片**，实现成本明显高于「发已有素材」。建议第一期只做**素材空间选取**，本地新上传留到第二期。

---

## 5. 实现建议

| 阶段 | 内容 | 改动 |
|---|---|---|
| 1 | `Channel/pinduoduo/utils/API/material_space.py`：`list_files(space="image"\|"video", ...)`（内部映射 `dir_id = 0 / -1`）、`list_dirs`、`sum_size`（走 `BaseRequest`，与现有 API 类同构） | 新增 1 文件 |
| 2 | `bridge/sender.py` 加 `send_material_image(uid,item)` / `send_material_video(uid,item)`；**同时修 `chat_ui.py` 的视频转发 `info` 组装**（顺带解禁视频转发按钮） | 小 |
| 3 | 素材元数据本地缓存表（`url/名称/类型/尺寸/时长/封面/file_id/审核状态`），UI 零等待；低频刷新 | 新增 DB 表 |
| 4 | 聊天输入区加「素材」入口，面板：图片/视频切换 + 分页 + 搜索 + 文件夹筛选（`ui/chat/material/`） | 新增 2~3 文件 |
| 5 | （二期）本地图片/视频上传（§4） | 中 |

**无需浏览器**：接口纯 HTTP 可调，不碰 Playwright profile，不与客服进程抢锁。

---

## 5.1 实施记录（2026-10-11 已完成 阶段 1/2/4）

### 新增

| 文件 | 说明 |
|---|---|
| `Channel/pinduoduo/utils/API/material_space.py` | `MaterialSpace(BaseRequest)`：`list_files(space, page, page_size, keyword, dir_id, order_by)` / `list_dirs()` / `sum_size(space)` / `is_sendable(item)` / `build_video_info(item)` / `build_send_payload(item)`。素材项已归一化为 `id/name/file_type/url/transcode_url/size/check_status/duration/width/height/cover_url/dir_names` |
| `ui/chat/material_panel.py` | `MaterialPopup`（Tab 切图片/视频空间 + 搜索 + 刷新 + 分页 + 4 列缩略图网格 + 审核状态标记）+ `MaterialCard` + `_MaterialLoader(QThread)`；列表结果按 `(space,page,keyword)` 缓存 180s |

### 改动

| 文件 | 改动 |
|---|---|
| `ui/chat/input_area.py` | 新增「素材」按钮（`FluentIcon.PHOTO`）与 `material_selected` 信号；`set_account(shop_id,user_id)`；面板定位做屏幕边界收敛；点外/发消息/切会话自动收起；主题切换同步刷新 |
| `ui/chat/chat_area.py` | 新增 `send_material(shop_id, user_id, item, buyer_uid)` 信号；会话加载完成后把账号同步给输入区 |
| `ui/chat_ui.py` | 新增 `_MaterialSendWorker(QThread)` + `_on_send_material` / `_on_material_send_done`；**视频转发 info 改为原样回传 `media_meta.raw_info`** |
| `bridge/sender.py` | `ReplySender` / `PinduoduoSender` 新增 `send_video(...)` |
| `ui/chat/message_bubble.py` | 解禁视频「转发消息」菜单（原文案：视频暂不支持转发） |

### 发送行为

- 图片：`send_image(uid, 素材.url)` → `type=1`
- 视频：`send_video(uid, 素材.transcode_url, info=build_video_info(item))` → `type=14`，info 含 `download_url/file_id/size/status/preview`
- 成功后按 `reply_source="manual"`、`context_type=image|video` 写入 `chat_message_records`（视频带 `media_meta.raw_info`，便于后续转发），并 `notify_new_message` 让聊天气泡立即出现
- 发送素材同样视为**人工介入**：`staff_reply_event_manager.notify_staff_reply()` 取消等待中的 AI 流程；`staff_message_cache` 写入 `[图片]` / `[视频]`
- 失败走 `InfoBar.error` 提示（项目禁 `QMessageBox`）

### 已知取舍 / 待验证

| 项 | 说明 |
|---|---|
| **图片空间范围** | **只展示「客服专用」文件夹**（按名称解析；实测两账号分别为 `182454400` / `190003343`，各 9 张）；解析失败时面板给出明确提示而非回退全店 |
| **账号隔离** | 缓存与在途请求 key 含 `(shop_id, user_id)`；修复前存在跨账号串数据竞态（见 §5.1） |
| 列表缓存 | 采用进程内内存缓存（TTL 180s，切账号即清空）+ 刷新按钮，**未新增 DB 表**（原 §5 阶段 3 的本地缓存表暂缓）；文件夹 id 另有 600s 独立缓存，刷新按钮会一并清空 |
| 加速器 | 未做；图片空间 9 条单页加载，视频空间 5 条，实测首屏可接受 |
| 真机发送验证 | 图片链路复用线上已跑通的 `send_image`；**视频链路仍需真发一条给测试买家**确认端到端（未做，避免误发真实客户） |

### 账号隔离（2026-10-11 修复）

素材空间**按账号（店铺）隔离**：同一接口换一份 cookies 就换一个库。逐层结论：

| 层 | 是否区分账号 | 说明 |
|---|---|---|
| 请求 | ✅ 天然区分 | `MaterialSpace(shop_id, user_id)` → `BaseRequest` 按 `(channel, shop_id, user_id)` 取该账号 cookies |
| 面板传入的账号 | ✅ 可靠 | 来自当前会话：`_current_shop_id` + `_current_user_id`（后者取自该会话消息的 `user_id`；实测消息表 `(shop_id, user_id)` 严格 1:1 且无空值） |
| 发送链路 | ✅ | `_MaterialSendWorker` 在创建时即捕获 `(shop_id, user_id, buyer_uid)`，中途切会话也不会发错账号/买家 |
| **列表缓存 / 在途请求** | ❌ → ✅ **已修** | 原 key 为 `(space, page, keyword)`，**不含账号** → 见下 |
| 客服文件夹 id 缓存 | ❌ → ✅ **已修** | 原 key 仅 `shop_id`，改为 `(shop_id, user_id)` |
| 缩略图缓存 | ✅ | key 为素材 CDN URL，各账号 URL 天然不同 |

**原缺陷（已复现）**：切账号与在途请求之间存在竞态 ——
① 账号 A 打开面板，A 的请求在飞行中 → ② 用户切到账号 B 的会话（`set_account(B)` 清缓存）→
③ A 的请求此刻才返回，**把 A 的列表写进缓存并 emit 结果** → ④ `_on_loaded` 只比对
`(space, page, keyword)`（此时与当前状态相同）→ **通过校验并渲染** → ⑤ 用户为 B 打开面板，
`use_cache=True` **命中 A 的缓存** → B 看到 A 的素材（最长 180s）。

**修法**：缓存与在途请求的 key 统一为 `(shop_id, user_id, space, page, keyword)`
（`MaterialPopup._cache_key()` / `_MaterialLoader.key`），`_on_loaded` 按同一 key 校验；
迟到的结果只会落进**自己账号的桶**，既不会渲染到别的账号，也不会污染缓存。
`set_account` 仍保留一次 `_clear_cache()` 作防御，但正确性已由分桶保证。
确定性复现脚本：`temp/repro_cross_account_cache.py`；修复后验证：`temp/test_account_isolation.py`（8/8 通过）。

### 复现过的坑（重要）

`_MaterialLoader` 用 `worker.finished.connect(worker.deleteLater)` 后仍从 `self._worker` 访问它 → `RuntimeError: wrapped C/C++ object ... has been deleted`。
该异常若发生在 Qt 信号槽（如 `QTabBar.currentChanged`）内，PyQt6 会直接 `qFatal` → 进程 **abort（`0xC0000409`，崩溃点 Qt6Core.dll）**。
**结论：后台线程对象必须在线程结束后把 Python 引用清空**（见 `MaterialPopup._on_worker_finished` / `_current_worker`），不要留指向已销毁 C++ 包装的引用。


---

## 6. 风险

| # | 风险 | 等级 | 说明 / 缓解 |
|---|---|---|---|
| R1 | ~~视频 `file_id` 语义未确认~~ **已消除** | — | 实测确认：素材项 `id` == 消息 `info.file_id`（`46393494801` 与 `dir_id=-1` 素材库双向对上） |
| R2 | `garner` 接口无文档，改版即失效 | 中 | 封装到单文件；失败降级为「不可用」而非报错 |
| R3 | 频控 | 低 | 实调无 anti-content、无验证码；仍建议列表结果缓存、避免高频拉取 |
| R4 | 审核状态 | 低 | `check_status != 2` 的素材在 UI 上标注不可发 |
| R5 | 素材 URL 防盗链 | 低 | CDN 公网直连，无签名参数 |
| R6 | 浏览器路线 | — | **已不需要**（原路线 B 作废） |

---

## 7. 剩余待验证

| # | 操作 | 状态 |
|---|---|---|
| V1a | 确认 `file_id = 素材项 id` | ✅ **已完成**（2026-10-11 03:33，用真实消息 `46393494801` 与 `dir_id=-1` 素材库双向比对） |
| V1b | 用 §3.2 拼装的 `info` 真发一条视频给测试买家 | ⏳ 待做（需发消息，用户配合或夜间窗口执行） |

V1a 已完成；视频链路只剩「真发一次」的端到端验证。注意：用户 `2026-10-11 03:22` 发出的视频 `file_id=47267892229`（`3c3b67a5…mp4`）**已不在**当前 `dir_id=-1` 列表中（现有 5 条），推测为审核驳回/超期清理（页面文案：驳回未申诉或上传失败的文件 15 天内删除）；同批次的 `46393494801` 仍在库。

---

## 附：本次使用的工具与产物

- `scripts/pdd_web_probe.py`（新增）：带 cookie 只读拉取商家后台页面与 JS bundle、关键词 grep。
  用法：`.venv\Scripts\python.exe scripts\pdd_web_probe.py --list | --url <页面> [--dump-js]`
- 抓取产物（临时）：`temp/web_probe/`（页面 HTML、JS bundle、各 *_report.txt）
- 只读探针（临时）：`temp/probe_material.py` / `temp/probe_material2.py`
- 第二轮实测（2026-10-11 03:2x，只读）：
  - `temp/verify_video.py` — 从库快照读最新视频/图片消息 → `temp/web_probe/verify_video.txt`
  - `temp/probe_fileid.py` — 用已知 `file_id` 打点各参数组合 → `fileid_probe.txt`
  - `temp/probe_all.py` — 拉全 226 条做归属判定 → `all_material.txt`
  - `temp/desc_probe.py` — `file_type_desc`/`tool_source` 候选值实测 → `desc_ctx.txt`
  - `temp/probe_service.py` — **`dir_id=-1` 验证** → `service_lib.txt`
  - `temp/verify_map.py` — **素材项 → 消息 `info` 逐字段比对** → `field_map.txt`
  - `temp/importmap.py` / `temp/find_importmap.py` / `temp/scan_mf.py` — 客服微前端（import map 来自
    `https://apistatic.pddpic.com/api-static/faas/chat-eva/dynamic-material-dispatch/26`，其中**不含**素材能力，
    已排除）→ `mf_report.txt`

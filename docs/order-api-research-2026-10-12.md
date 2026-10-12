# 订单信息 / 订单备注 接口调研（2026-10-12）

> 来源：商家前端 chat-merchant 页缓存 JS（`temp/web_probe/index.bundle.7d18b7a24ddb25faed7a.js`）
> + 本系统 HTTP 客户端**线上实测**（`temp/probe_order_api.py`，全部只读或零变更验证）。
> 结论：**两个功能均可直接用现有 cookie 调用，无需 anti-content**（同 garner / chat_list 一档）。

## 1. 买家订单列表（显示订单信息）

```
POST https://mms.pinduoduo.com/latitude/order/userAllOrder
body: {"uid": "<买家uid>", "pageNo": 1, "pageSize": 10, "showHistory": true}
```

- 实测：`success=True`，返回 `result.{pageNo, pageSize, total, historyTotal, historyTotalPeriodCode, orders[]}`。
- **无订单返回空列表**；`showHistory=true` 含历史订单。

### 订单对象关键字段（实测截取）

| 字段 | 说明 |
|---|---|
| `orderSn` | 订单号，如 `261011-523375767090740` |
| `orderStatusStr` | **现成的状态文案**，如「已发货，待签收」 |
| `orderStatus / payStatus / shippingStatus / groupStatus` | 数值状态码 |
| `orderAmount` | 订单金额（**分**） |
| `orderGoodsList` | `{goodsId, skuId, goodsName, goodsPrice, goodsNumber, thumbUrl, spec}` |
| `note` | **商家备注（直接带在列表里）** |
| `orderTime / payTime / shippingTime / trackingNumber` | 时间与物流 |
| `uid / mallId / id / groupOrderId` | 买家/店铺/订单内部ID |

### 同族接口（同参数风格，未逐一实测）

- `userUnshippedOrder` / `userRefundOrder` / `userReceivedOrder` / `userUnfinishedOrder` / `userNotReceivedOrder`（分状态列表，默认 `pageNo/pageSize/uid`）
- `getMallUnpaidOrderCount`（未付款角标）、`recentGrouped` / `searchGrouped`（分组/搜索）、
  `orderAddressInfo`（地址）、`mobile/get`（手机号）、`price/update`（改价）

## 2. 订单备注（读取 / 编辑）

```
读：POST https://mms.pinduoduo.com/pizza/order/note/query
    body: {"orderSn": "...", "source": 1}          → result = 备注文本（无备注时为 null）
写：POST https://mms.pinduoduo.com/pizza/order/note/update
    body: {"orderSn": "...", "source": 1, "remark": "<新备注>"}
新增（无备注时）：POST /pizza/order/note/add   同一 body
按标签写：POST /pizza/order/noteTag/update
```

### 实测要点（重要）

- **`source` 必须为 `1`**：`0 / 2 / 3 / 100` 等均返回 `2000000 入参有误`；query 对 source 宽容（0~3 都返回同一备注），update 严格。
- **写入限流**：同一订单两次写备注**间隔需 > 1 秒**，否则返回「两次备注间隔时长需大于1秒」→ 前端必须防抖/禁用按钮。
- 写入为**全量覆盖**（不是追加）；服务端无删除备注接口。
- 工作台自己的编辑逻辑（`queryAndAppendRemark`）是「追加」：
  `用户备注：{新内容}\n{旧内容}`（先 query，无备注则 add，有则 update）。
- query 读回有**短暂延迟**（写完立刻读可能读到旧值，秒级后一致）→ 写完以后端返回为准，或延迟刷新。

## 3. 验证过程摘要

1. `userAllOrder` 直调（买家 2689094372）→ 拿到真实订单：`261011-523375767090740`，¥19.90（1990分），「企业版+历史影像」，状态「已发货，待签收」，备注 `28E4AB37 - 5DFC0A40`。
2. `note/query` 直调 → 读出该备注。
3. `note/update` 受控验证：写入带标记文本 → 读回确认生效 → 恢复原值 → 读回一致（**零残留**）。
4. 全部请求均为 cookies 认证，未携带 anti-content；未触发风控。

## 4. 备注字段的归属（判断）

- `note`（列表内）与 `note/query` 返回的 `result` 一致。
- 各订单独立；测试账号 3395880824 的订单未探测（无必要）。

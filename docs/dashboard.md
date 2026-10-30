# 用量面板

一个页面看全两个网关的运营数据：**积分总数、签到状况、请求来源、各来源消耗量**。

单文件 FastAPI（HTML 内联，无前端构建、无外部 CDN），默认只绑 tailnet，
**不带鉴权** —— 访问控制交给 tailnet 本身。

```
┌─────────────────────────────────────────────────────────────┐
│ 网关用量面板      更新于 2026-10-30 08:55  [24h][7d][30d][全部] │
├───────────────┬───────────────┬─────────────────────────────┤
│ 合计          │ Trae          │ WorkBuddy                   │
│ 5,152 积分剩余 │ 4,145 积分剩余 │ 1,007 积分剩余              │
│ 已用 3,876    │ 已连接        │ 已连接                      │
│ 区间请求 1360 │ 今日已签 ✅    │ 今日已签 ✅ 连续 25 天       │
├───────────────┴───────────────┴─────────────────────────────┤
│ 请求来源                                                     │
│ 来源         IP              请求  失败 断开  输入tok  成本   │
│ DSH          192.168.1.10    19     0    19    4.8M   $0.03 │
│ Hermes       192.168.1.10     2     0     0      46    $0.00 │
├─────────────────────────────────────────────────────────────┤
│ 最近请求（时间 / 网关 / 来源 / 模型 / token / 状态）          │
└─────────────────────────────────────────────────────────────┘
```

## 数据从哪来

| 指标 | 来源 | 为什么 |
|---|---|---|
| 积分 | 网关 HTTP 接口：Trae `/credits/summary`、WorkBuddy `/api/usage_summary` | 只有接口能拿到**上游最新值**（含账号余额回写） |
| 签到 | WorkBuddy `/api/checkin/status`；Trae `/credits/summary` + `scheduler.json` | 同上 |
| 请求来源 / 消耗 | **直接读两边 `usage.jsonl`** | 接口的聚合键是 `key_id`，本项目只有一个 key（恒为 `launcher`），拿不到「谁在用」；逐请求日志才有客户端维度 |

## 请求来源字段

来源列依赖 `usage.jsonl` 里的三个字段，由两个补丁写入：

| 字段 | 含义 |
|---|---|
| `client` | UA 归一后的客户端名（`DSH` / `Hermes` / `Claude Code` / `curl` / …） |
| `client_ip` | 优先 `X-Forwarded-For`，否则连接对端地址 |
| `endpoint` | `/v1/chat/completions` \| `/v1/messages` \| `/v1/responses` |

UA 归一是一张**有序匹配表**（先具体的后宽泛的），匹配不到就退化为 UA 第一个
token，不猜：

```
deepseek-harness / dsh     -> DSH
claude-cli / claude-code   -> Claude Code
codex                      -> Codex CLI
hermes                     -> Hermes
openai-python / httpx      -> Python SDK / Python httpx
curl / postman / insomnia  -> 原样
cherry / lobechat / chatbox / nextchat -> 对应客户端名
```

历史行（补丁上线前产生的）没有这三个字段，统一归入 **`(历史数据)`**，不伪造。

## 失败统计的口径（重要）

粗看「失败」会严重高估故障率。面板把两者分开：

| 列 | 含义 |
|---|---|
| **失败** | 真实失败：上游 4xx/5xx、渠道拒绝(11128)、401、超时… |
| **主动断开** | 客户端主动放弃（`client disconnected`）—— 长上下文请求被客户端超时/重发，**与网关可用性无关** |

实测样本里 958 条「主动断开」全部来自 30 万 token 级别的超长会话被客户端切断，
网关本身没有故障。混在一起会把失败率显示成 78%，分开后是 7%。

其它归类：`upstream_401`（区域不匹配）、`channel_reject`（11128）、
`bad_request`（400）、`rate_limit`（429/6004）、`timeout`。

## 参数

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `WB_BASE` | `http://workbuddy2api:8787` | WorkBuddy 网关地址 |
| `TRAE_BASE` | `http://trae2api-hub:8791` | Trae 网关地址 |
| `WORKBUDDY2API_KEY` | 必填 | 调 WorkBuddy 接口用 |
| `TRAE_API_KEY` | 必填 | 调 Trae 接口用 |
| `WB_USAGE` | `/data/wb/usage.jsonl` | WorkBuddy 用量日志 |
| `TRAE_USAGE` | `/data/trae/usage/usage.jsonl` | Trae 用量日志 |
| `TRAE_SETTINGS` | `/data/trae/accounts/settings.json` | 用来签 `/usage` 所需的面板 token |
| `DASH_CACHE_TTL` | `20` | 上游指标缓存秒数（别让每次刷新都打上游） |
| `DASH_HTTP_TIMEOUT` | `20` | 调网关的超时秒数 |

## 接口

| 端点 | 说明 |
|---|---|
| `GET /` | 面板页面（每 30 秒自动刷新） |
| `GET /api/overview?window=24h\|7d\|30d\|all` | 面板数据（JSON，可自己接监控） |
| `GET /health` | 存活探针 |

## 面板 token 是怎么来的

Trae 的 `/usage` 是面板路由，要 `X-Panel-Token`。面板进程读
`data/trae/accounts/settings.json` 里的 `panel_session_secret`，按网关自己的格式
签一个 10 分钟有效的 token：

```
<过期秒>.<nonce>.<hmac_sha256(secret, "过期秒.nonce")>
```

只读密钥、不涉及任何账号明文，也不落盘。

## 已知限制

- **历史行没有来源**：补丁只对上线后的请求生效。
- **成本是参考值**：Trae 用 OpenRouter 刊例价估算（`cost_usd`）；WorkBuddy 记的是
  **积分不是美元**，面板里它那行成本显示 `—`，不硬凑。
- **来源靠 UA**：客户端伪造 UA 会改变归组。这是运营观察口径，不是安全边界。
- **面板只读**：不提供改配置/切账号的写操作，避免误操作生产网关。

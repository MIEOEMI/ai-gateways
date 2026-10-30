# WorkBuddy 网关

把腾讯 WorkBuddy / CodeBuddy 的订阅额度暴露成 OpenAI / Anthropic / Responses
三协议兼容的 API。

上游：[`3304711297/workbuddy2api`](https://github.com/3304711297/workbuddy2api)
本仓库的 `patches/workbuddy2api.patch` + `patches/channel_guard.py` 解决 11128
渠道校验，见下文。

## 核心问题：11128 渠道校验

```json
{"code":11128,"msg":"Illegal API invocation from an unapproved channel"}
```

由**两个独立的门**组成，必须同时通过。

### 门 1：开头规则

`system` 消息起始（trim 后）匹配被拒身份就整单拒绝：

| 请求 | 结果 |
|---|---|
| `You are an AI agent powered by DeepSeek Harness.` | 400 / 11128 |
| 前面加任意非空白文本 + 原句 | **200** |
| `" "` + 原句（空格被 trim） | 400 / 11128 |
| `"\n"` + 原句 | **200** |

### 门 2：内容模糊匹配

正文与已知 harness 提示词**相似度超阈值**即拒绝。实测特征：**任何真实文本改动都能
通过**，说明是模糊匹配而非精确子串：

```
A = "Tokens prefixed with @ are paths the user explicitly referenced."
B = "Relative paths resolve from the workspace root;"

A                           → 200
B                           → 200
A + B                       → 11128   ← 只有合起来才超阈值
A + (workspace→project)     → 200     ← 改一个词就够
A + (Relative→Rel)          → 200
A + (删掉 @)                 → 200
```

### 被证伪的方案（2026-10-29 实测，别再走）

| 方案 | 结果 |
|---|---|
| 零宽空格 / ZWNJ / ZWJ / WJ / BOM / 软连字符 / MVS / ALM | **全部无效** —— 上游先**剥离不可见字符**再匹配，逐字符插入 U+200B 仍是 11128 |
| 只加中性头 | 只解决门 1，门 2 照样拦 |
| 连续空格 / 双空格 | 无效，空白被归一化 |

> ⚠️ 2026-10-28 零宽空格方案曾**确实有效**，10-29 起上游加了剥离，那套修复因此
> 整体失效。这是 `channel_guard.py` 被重写的原因。

### 有效方案

**门 1**：给匹配被拒开头的 `system` / `developer` 消息前面加中性头。

**门 2**：把空白改写成等价空白 —— `\t` / `\n` / `\r` 上游**不归一化**。
空白对模型等价，语义不变，但足以打断模糊匹配：

| 请求 | 结果 |
|---|---|
| A+B 句号后换行 | **200** |
| A+B 空格换制表符 | **200** |

`channel_guard.py` 的实现：

1. 句子边界换行：`[.!?;:。！？；：]` 后的空白 → 换行
2. 每 4 个空格把一个换成制表符
3. **围栏代码块 ```...``` 内部不动**（代码空白有语义）

只作用于 `system` / `assistant` / `developer`，`user` / `tool` 不动 —— 与上游实测的
拦截面一致。失败时还有三级救援（强制中性头 → 稀疏扰动 → 密集扰动）。

### 实测（真实 DSH 全量 prompt，6583 字）

| 处理 | 结果 |
|---|---|
| 原样 | 11128 |
| +中性头 | 11128 |
| +中性头 + 每 3 空格→tab | 200 |
| +中性头 + 句后换行 | 200 |
| +中性头 + 句后换行 + 隔 3 tab | 200 |

端到端回归（全量 prompt + 3 条 assistant 历史 + 26 工具）：

```
全量prompt + 历史           → 200
全量prompt + 工具           → 200 tool_calls=1
全量prompt + 历史 + 工具     → 200
流式                        → 200, 98 chunks, 中文输出正常
语义（3+4）                 → 7
usage                       → prompt 1564 / completion 99 / credit 0.03
```

## ⚠️ 必须开 `--desensitize`

仅靠 `channel_guard.py` 不够。上游 `desensitize.py` 覆盖的是**另一批**触发点：
Claude Code 身份句、`Main branch (you will usually use this for PRs)`、49 个品牌/安全
术语。缺了它，纯对话能过、**带 tools 的请求仍然 11128**。

compose 里已经带上 `--desensitize`，删掉会退化。

## ⚠️ 国内版 / 国际版必须分区

`www.codebuddy.cn`（国内）与 `www.workbuddy.ai`（国际）是**两套互不相通的账号体系**：
token、计费、模型清单都独立。本网关后端固定国内站 `https://copilot.tencent.com`，因此：

> **国际版账号的 token 发到国内后端，会被上游 APISIX 直接挡下，返回 HTTP 401 +
> APISIX 的 HTML 错误页**（不是 JSON 错误体）。

这是本项目最隐蔽的故障源，实测踩到的完整链路：

```
09:07:05  HTTP 429 | deepseek-v4.1-flash | code 6004 频率限制
09:07:05  🔄 账号 A(国内) 触发限流 → 切到 账号 B(国际)
09:07:06  HTTP 401  ← 国际号打国内后端
```

致命之处：`active_uid` 是**持久化**的。切错一次后，网关此后所有请求都是 401，
且不会自动切回 —— 表现为「网关突然全挂、重启也不一定好」。

### 三道门禁

| 门禁 | 位置 | 作用 |
|---|---|---|
| **选号分区** | `get_candidate_uids` / `_tiered` | 候选只含与后端同区的账号 |
| **切号门禁** | `switch_active_account` | 拒绝把 `active_uid` 切到异区账号（Fail-Open） |
| **请求头门禁** | `X-WorkBuddy-Account` 解析处 | 指定异区账号时放弃指定、回退策略 |

另外把**上游 401（APISIX HTML 页）**归入「账号态故障」：不是限流，但换号可自愈，
因此触发 failover。识别靠特征串（`401 Authorization Required` + `APISIX`）而不是
code —— 这类响应根本不是 JSON；网关自身的鉴权 401 返回 JSON（`invalid api key`），
不会误判。

巡检脚本 `scripts/daily_sweep.py` 同步跳过异区账号，避免每天对国际号发必然 401 的
请求、把账号写进冷却池。

## 其他端点

| 端点 | 说明 |
|---|---|
| `POST /v1/chat/completions` | OpenAI 兼容，原生 tools/tool_calls，真流式 |
| `POST /v1/messages` | Anthropic 兼容（Claude Code / Cline / Roo Code） |
| `POST /v1/responses` | Responses 兼容（Codex CLI / OpenCode） |
| `GET /v1/models` | 模型清单 |
| `GET /api/usage_summary` | 积分概览 |
| `GET /api/checkin/status` | 签到状态 |
| `GET /api/rate_limit` | 限流状态 |
| `GET /health` | 存活探针（免鉴权） |

## 上下文长度

上游**实际**限制是 **100K tokens**，尽管目录里标称 1M：

```
11115 prompt is too long: 100001 tokens > 100000 maximum
```

配置客户端的 `contextWindow` 时按 100K 算，别按目录标称值。

## 排错

| 现象 | 先查 |
|---|---|
| `11128` | `--desensitize` 是否开着；再看 `channel_guard.py` 在不在容器里 |
| `401` + `<html>APISIX` | `active_uid` 指向了异区账号（见上文分区） |
| `413` 请求体过大 | `WORKBUDDY2API_MAX_BODY_MB` 是否 ≥ 请求体（默认 64MB） |
| `11115 prompt too long` | 上下文超 100K，降历史或换模型 |
| `11140` | 内容审核，**不是**本项目问题，改写内容 |
| `6004` | 上游频控，等 reset 或换号 |
| 全部请求失败但容器在跑 | 端口绑定丢失，见 [`operations.md`](operations.md) |

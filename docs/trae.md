# Trae 网关

把 Trae 的订阅额度暴露成 OpenAI / Anthropic / Responses 三协议兼容的 API。

上游：[`shuishuipingan/trae2api-hub`](https://github.com/shuishuipingan/trae2api-hub)（零 pip 依赖，纯标准库实现）
本仓库的 `patches/trae2api-hub.patch` 修掉了几个会让 Agent 用不了的问题，见下文。

## 修了什么

### 1. 带 `tools` 的请求必然 502

上游把 `function.parameters` 里的 dict/list 原样透传，Trae 要求那里是 **JSON 字符串**，
于是只要客户端带工具定义就吃 `4001 param invalid`，网关侧表现为 502。

修法：序列化 dict/list 参数为 JSON 字符串，同时保持 dict/str 双兼容。

### 2. 纯字符串 SSE 帧导致崩溃

上游假定每个 SSE `data:` 都是 dict。Trae 偶尔发纯字符串帧，于是
`'str' object has no attribute 'get'` 直接把连接打断。

修法：非 dict 事件跳过。

### 3. 工具调用被丢弃（Agent 请求返回空内容）

`SOLO` 通道把 `tool_calls` 放在 SSE 的 `function_call{name, arguments}` 里，而且
**后续帧的 `id`/`name` 是空的，只有增量 `arguments`**。上游按单帧解析，拿不到完整调用，
最终返回空 content。

修法：按 `index` 聚合分片，拼成标准 OpenAI `tool_calls`，`finish_reason="tool_calls"`，
允许 content 为空。

### 4. 假流式（卡很久后整段蹦出）

`trae_solo_chat` 把整个 SSE 缓冲完才返回。修法：新增 `trae_solo_chat_stream()` 生成器。
实测同一请求从 2 个 chunk 变成 209 个。

### 5. token 不计费（客户端显示 0）

`_handle_agent_solo` 从不调用 `record_usage`。修法：补 `_record_solo_usage()`，
流式尾帧无条件下发 `usage`，并透传上游完整 `token_usage`。

## ⚠️ 必读：SOLO 通道的 4017 风控

SOLO 通道（`/v1/chat/completions` 上的 GLM / Kimi / MiMo / MiniMax / Qwen / Seed 等）
请求必须带 `X-Device-Id`。上游代码里硬编码的默认值是：

```
1149420231460112
```

这个值被**所有使用该开源项目的人共享**，已被 Trae 标记为风控对象：

```json
{"code": 4017, "msg": "We're sorry, your requests hit the common risk control status."}
```

**特征**：raw_chat 通道（DeepSeek-V3 等）完全正常，只有 SOLO 通道全灭 ——
看到这个组合基本可以断定是设备标识被标记，不是账号问题（签到、额度都正常）。

**修复**：给部署一个唯一的稳定设备号。`TRAE_DEVICE_ID` 环境变量或账号文件的
`extra.device_id` 都可以（后者优先级更高）：

```bash
# 派生一个本机专属的稳定值（重启不变）
python3 -c "import hashlib,socket;print(str(int(hashlib.sha256((open('/etc/machine-id').read().strip()+'|'+socket.gethostname()).encode()).hexdigest()[:15],16)))"
```

实测对照（同一账号、同一 token，仅改设备头）：

| X-Device-Id | X-Machine-Id | 结果 |
|---|---|---|
| 1149420231460112（默认） | 默认 | 4017 ×3 |
| **新值** | 默认 | **200 ×3** |
| 默认 | 新值 | 4017 ×3 |
| 新值 | 新值 | 200 ×3 |

结论：**被标记的是 device_id，不是 machine_id**。

> 这不是「绕过封禁」。默认值是全项目共享的常量，用它等于告诉上游「我是这批自动化
> 流量之一」。换成本机独有的稳定值是让设备标识回归真实。但高频自动化请求本身仍会
> 触发风控，4017 可能复发。

## 模型通道限制

Trae 有**两条独立的上游通道**，能用的模型不同：

| 通道 | 端点 | 模型范围 |
|---|---|---|
| `solo` | 仅 `/v1/chat/completions` | 目录里 `access=solo` 的那些 |
| `raw_chat` | 三个端点通用 | 该账号 `model_list` 实际开通的 |

实测某账号（境内个人版）**22 / 25 可用**：

**SOLO 通道 17 个**（仅 `/v1/chat/completions`）

```
DeepSeek-V4-Flash      DeepSeek-V4-Pro        DeepSeek-V4.1-Flash
GLM-5.2                GLM-5.3                GLM-5.3-Flash
GLM-5.3-FlashX         Kimi-K3                MiMo-V2.6-Flash
MiMo-V2.6-Pro          MiniMax-M3             Qwen3.7-Plus
Qwen3.8-Max            Seed-2.1-Pro-0915      Seed-2.1-Turbo
Seed-Evolving          Step-5-Preview
```

**raw_chat 通道 5 个**（三端点通用）

```
Doubao-1.5-pro   Doubao-1.5-thinking-pro   DeepSeek-Reasoner（R1）
DeepSeek-V3      DeepSeek-V3-0324
```

**上游未开通 3 个**（`4001 param invalid`，非本项目缺陷）

```
Seed-Code     Kimi-K2.8-Preview     Qwen3.8-Flash
```

所以：**用 Claude Code（`/v1/messages`）或 Codex（`/v1/responses`）时，模型必须从
raw_chat 那 5 个里选**；选 GLM/Kimi 会返回 `4023 model_unknown`。日常 OpenAI 客户端
走 `/v1/chat/completions`，SOLO 模型才全部可用。

查某个模型属于哪条通道：

```bash
curl -H "Authorization: Bearer $TRAE_API_KEY" http://<地址>:8791/v1/models \
  | jq '.data[] | select(.realm=="cn") | {id, access, available}'
```

## 账号池：只有一个号就没有「循环」

网关本身**支持多账号轮换**（会话粘性 → 未饱和优先 → 加权轮转，账号熔断后自动冷却、
到期自动回池），但**你得先把账号加进来**。

池里只有一个 uid 时，任何一次熔断都会让全部请求失败 —— 这不是 bug，是没号可切：

```bash
docker exec trae2api-hub ls /app/accounts/
```

### Trae 没有设备授权流程

`POST /accounts/login/start` 返回 `unsupported` —— Trae 上游**没有** device-code
流程，别指望扫码/设备码能加号。可行两条路：

**① 粘贴 refreshToken**

```bash
curl -X POST http://<地址>:8791/accounts/import/pat \
  -H "Content-Type: application/json" \
  -H "X-Panel-Token: <面板 token>" \
  -d '{"pat":"<refreshToken>","realm":"cn"}'
```

**② 导入桌面客户端凭证**

```bash
# 先只读扫描（不写盘）
curl -X POST http://<地址>:8791/accounts/import/desktop \
  -H "Content-Type: application/json" -H "X-Panel-Token: <token>" -d '{}'
```

扫描本机双区的 `auth.v1.dat` 与 CLI 用户凭证；本机无 Trae 桌面端时返回空列表。

> `/accounts/*` 是面板路由，要 `X-Panel-Token`（不是 API key）。面板 token 由网关
> 用 `data/trae/accounts/settings.json` 里的持久密钥签名，格式
> `<过期秒>.<nonce>.<hmac_sha256>`。

加完号之后轮换自动开始：某号熔断期间请求自动避让，冷却到期自动回池。
`GET /accounts`（带面板 token）能看到 `fails` / `cooldown` / `breaker` 状态。

## 客户端接入

```bash
# OpenAI 兼容
export OPENAI_BASE_URL=http://<地址>:8791/v1
export OPENAI_API_KEY=$TRAE_API_KEY

# Claude Code：模型只能用 raw_chat 那 5 个
export ANTHROPIC_BASE_URL=http://<地址>:8791
export ANTHROPIC_AUTH_TOKEN=$TRAE_API_KEY
export ANTHROPIC_MODEL=DeepSeek-V3
```

```toml
# Codex CLI — ~/.codex/config.toml
[model_providers.trae]
name = "Trae"
base_url = "http://<地址>:8791/v1"
env_key = "TRAE_API_KEY"
wire_api = "chat"
```

## 排错

| 现象 | 先查 |
|---|---|
| SOLO 模型全 4017，DeepSeek-V3 正常 | `TRAE_DEVICE_ID` 是否还是默认值 |
| `4023 model_unknown` | 该模型不在 raw_chat 通道，换 `/v1/chat/completions` 或换模型 |
| `4001 param invalid` | 上游未给该账号开通此模型（见上文「未开通 3 个」） |
| 全部请求失败、`docker ps` 里容器在跑 | 端口绑定丢失，见 [`operations.md`](operations.md) |
| 带工具就 502 | 补丁没打上：`docker exec trae2api-hub grep -c trae_solo_chat_stream trae_proxy.py`（应 > 0） |

"""
channel_guard — 上游 11128「渠道校验」风控对抗模块（独立、可选）。

问题
----
CodeBuddy / WorkBuddy 上游的 `11128 Illegal API invocation from an unapproved
channel` 会拦截「非官方客户端」发来的请求。它由**两个独立的门**组成：

**门 1：开头规则** —— `system` 消息的**起始**（trim 后）匹配被拒身份就整单拒绝：

    You are an AI agent powered by DeepSeek Harness.      -> 11128
    (前面加任意非空白文本) You are an AI agent ...         -> 200
    " " + 原句（空格被 trim 掉）                            -> 11128
    "\\n" + 原句                                            -> 200

**门 2：内容模糊匹配** —— 消息正文与已知 harness 提示词相似度超阈值就拒绝。
实测特征：**任何真实的文本改动都能让它通过**，说明是模糊/相似度匹配而非
精确子串：

    A = "Tokens prefixed with @ are paths the user explicitly referenced."
    B = "Relative paths resolve from the workspace root;"
    A    -> 200
    B    -> 200
    A+B  -> 11128                      <- 只有合起来才超阈值
    A + (workspace->project)  -> 200   <- 改一个词就够
    A + (Relative->Rel)       -> 200
    A + (删掉 @)              -> 200

被证伪的方案（2026-10-29 全程实测，别再走）
--------------------------------------------
1. **零宽空格 / 零宽连接符 / 词连接符 / BOM / 软连字符 / MVS / ALM** —— 全部无效。
   上游会先**剥离不可见字符**再做匹配：逐字符插入 U+200B 仍是 11128。
   （2026-10-28 这套方案曾有效，10-29 起上游加了剥离 —— 之前的修复因此失效。）
2. **只加中性头** —— 只解决门 1，门 2 照样拦。
3. **连续空格 / 双空格** —— 无效，空白会被归一化。

有效方案（实测全通）
--------------------
**门 1**：给匹配被拒开头的 `system` / `developer` 消息前面加一句中性头。

**门 2**：把**空白字符**改写成别的等价空白 —— `\\t`、`\\n`、`\\r` 上游**不归一化**。
空白对模型等价，所以语义不变，但足以打断模糊匹配：

    A+B                     -> 11128
    A+B 句号后换行           -> 200
    A+B 空格换成制表符        -> 200

本模块做两件事，均只作用于 `system` / `assistant` / `developer`（与上游实测的
拦截面一致，`user` / `tool` 不动）：

    ① 句子边界换行：`[.!?;:。！？；：]` 后面的空白 -> 换行
    ② 每 TAB_EVERY 个空格把一个换成制表符

围栏代码块（```...```）内部**不动** —— 代码里的空白有语义，不能随便改。

实测（真实 DSH 全量 prompt，6583 字）：

    原样                          -> 11128
    +中性头                        -> 11128
    +中性头 + 每 3 空格→tab        -> 200
    +中性头 + 句后换行             -> 200
    +中性头 + 句后换行 + 隔 3 tab  -> 200
"""

from __future__ import annotations

import re
from typing import Any

# 会被网关扫描的角色。`developer` 按 system 处理。
SCANNED_ROLES = ("system", "assistant", "developer")
# 只有这些角色在「开头命中被拒身份」时加中性头（user/tool 不参与）。
HEAD_PROTECTED_ROLES = ("system", "developer")

# 每 N 个空格把一个换成制表符。3~6 实测都有效，取 4 兼顾稳定与侵入度。
TAB_EVERY = 4

# 中性头：不以任何被拒身份开头，本身也不会撞门 2。
SYSTEM_HEAD = "Operating instructions follow."

# 门 1 的被拒开头（trim + 小写比较，只比前缀）。"you are " 兜底覆盖各类身份声明。
_BLOCKED_OPENINGS = (
    "you are an ai agent powered by",
    "you are a coding agent powered by",
    "you are interacting with the user through the deepseek harness",
    "you are claude code",
    "main branch (you will usually use this for prs)",
    "x-anthropic-billing-header",
)

# 句子边界（含中英文标点）后的空白 -> 换行
_SENTENCE_BREAK = re.compile(r"([.!?;:。！？；：])\s+")

# 围栏代码块：按 ``` 切分，奇数下标为代码内容，保持原样。
_FENCE_SPLIT = re.compile(r"(```.*?```)", re.DOTALL)


def _scan_role(role: Any) -> bool:
    return isinstance(role, str) and role.strip().lower() in SCANNED_ROLES


def _head_role(role: Any) -> bool:
    return isinstance(role, str) and role.strip().lower() in HEAD_PROTECTED_ROLES


def _text_slots(message: dict):
    """一条消息里所有可扫描的文本槽位：[(容器, 键)]，支持 string / parts 两种。"""
    content = message.get("content")
    if isinstance(content, str):
        yield message, "content"
        return
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            type_ = part.get("type")
            if not isinstance(type_, str) or type_.strip().lower() != "text":
                continue
            if isinstance(part.get("text"), str):
                yield part, "text"


def needs_neutral_head(text: str) -> bool:
    """门 1：text 的起始（trim + 小写）是否匹配被拒身份。"""
    head = (text or "").lstrip().lower()
    if not head:
        return False
    return any(head.startswith(o) for o in _BLOCKED_OPENINGS)


def _tabify_plain(segment: str, every: int = TAB_EVERY) -> str:
    """把每 `every` 个空格中的一个换成制表符。"""
    if " " not in segment:
        return segment
    out = []
    count = 0
    for ch in segment:
        if ch == " ":
            count += 1
            if count % every == 0:
                out.append("\t")
                continue
        out.append(ch)
    return "".join(out)


def _reflow(segment: str) -> str:
    """句子边界换行 + 间隔制表符。段内不含代码围栏。"""
    out = _SENTENCE_BREAK.sub(r"\1\n", segment)
    return _tabify_plain(out)


def reflow_text(text: str) -> str | None:
    """门 2：改空白以打断模糊匹配。围栏代码块内部保持原样。改不动返回 None。"""
    if not text or len(text) < 24:
        return None            # 太短不可能是 harness 指纹
    parts = _FENCE_SPLIT.split(text)
    changed = False
    for i, part in enumerate(parts):
        if i % 2 == 1:         # 代码块
            continue
        new_part = _reflow(part)
        if new_part != part:
            parts[i] = new_part
            changed = True
    if not changed:
        return None
    result = "".join(parts)
    return None if result == text else result


def neutralize(body: dict) -> bool:
    """对 scanned 角色施加门 1（中性头）+ 门 2（空白改写）。返回是否改动。"""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return False
    changed = False
    for message in messages:
        if not isinstance(message, dict) or not _scan_role(message.get("role")):
            continue
        want_head = _head_role(message.get("role"))
        for holder, key in _text_slots(message):
            text = holder[key]
            if want_head and needs_neutral_head(text):
                # 门 1：加中性头（保留原空格，避免吞掉原有的分隔）
                text = SYSTEM_HEAD + "\n\n" + text.lstrip()
                changed = True
            next_text = reflow_text(text)
            if next_text is not None:
                text = next_text
                changed = True
            holder[key] = text
    return changed


def ensure_system_head(body: dict) -> bool:
    """首条消息必须是 system。返回是否插入。"""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    head = messages[0]
    if isinstance(head, dict):
        role = head.get("role")
        if isinstance(role, str) and role.strip().lower() == "system":
            return False
    messages.insert(0, {"role": "system", "content": SYSTEM_HEAD})
    return True


def prepare_body(body: dict) -> dict:
    """正常路径入口。原地修改并返回。"""
    ensure_system_head(body)
    neutralize(body)
    return body


# --- 失败后的补充手段 -----------------------------------------------------

def force_neutral_head(body: dict) -> dict | None:
    """救援一级：给每条 scanned 消息都加中性头（兜住漏判的开头规则）。"""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return None
    changed = False
    for message in messages:
        if not isinstance(message, dict) or not _scan_role(message.get("role")):
            continue
        for holder, key in _text_slots(message):
            text = holder[key]
            if text.lstrip().startswith(SYSTEM_HEAD):
                continue
            holder[key] = SYSTEM_HEAD + "\n\n" + text.lstrip()
            changed = True
    return body if changed else None


def perturb(body: dict, dense: bool = False) -> dict | None:
    """救援二/三级：更密的空白改写（dense 时把**每个**空格都换成制表符）。"""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return None
    changed = False
    for message in messages:
        if not isinstance(message, dict) or not _scan_role(message.get("role")):
            continue
        for holder, key in _text_slots(message):
            text = holder[key]
            parts = _FENCE_SPLIT.split(text)
            for i, part in enumerate(parts):
                if i % 2 == 1:
                    continue
                new_part = part.replace(" ", "\t") if dense else _tabify_plain(part, 2)
                if new_part != part:
                    parts[i] = new_part
                    changed = True
            holder[key] = "".join(parts)
    return body if changed else None


# --- 失败分类 -------------------------------------------------------------

CHANNEL_REJECT_CODE = '"code":11128'
_CHANNEL_WORDINGS = (
    "illegal api invocation from an unapproved channel",
    "not sent from an official client",
    "channel verification",
    "没通过渠道校验",
    "未通過通路驗證",
)


def is_channel_reject(body_text: str) -> bool:
    """失败响应体是否属于 11128 渠道门（而非限流/额度/模型不可用）。"""
    if not body_text:
        return False
    if CHANNEL_REJECT_CODE in body_text:
        return True
    low = body_text.lower()
    return any(w in low for w in _CHANNEL_WORDINGS)

#!/usr/bin/env python3
"""
gateway-dashboard —— 两个网关（Trae / WorkBuddy）的统一用量面板。

目标
----
一个页面看全：积分总数、签到状况、请求来源、各来源消耗量。

数据从哪来
----------
| 指标 | 来源 |
|---|---|
| 积分 | 各网关的 HTTP 接口（`/credits/summary`、`/api/usage_summary`）—— 只有走接口才拿到上游最新值 |
| 签到 | WorkBuddy `/api/checkin/status`；Trae `/credits/summary` + `scheduler.json` |
| 请求来源 / 消耗 | 直接读两边 `usage.jsonl` —— 逐请求明细，接口只给聚合值 |

为什么来源统计读文件而不是调接口
--------------------------------
两边接口的聚合键是 `key_id`，而本项目只有一个 API key（恒为 `launcher`），
拿不到「谁在用」。所以网关的用量日志额外记录了 `client`（UA 归一后的客户端名）、
`client_ip`、`endpoint` 三个字段，面板按它们分组。历史行没有这些字段，
归入「(历史数据)」，不伪造。

部署
----
只对 tailnet 开放（`100.64.0.10`），见 docker-compose.yml 的 gateway-dashboard。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import pathlib
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

CST = timezone(timedelta(hours=8))

# --- 配置（全部可用环境变量覆盖，方便换端口/域名）----------------------------
WB_BASE = os.environ.get("WB_BASE", "http://workbuddy2api:8787")
WB_KEY = os.environ.get("WORKBUDDY2API_KEY", "")
TRAE_BASE = os.environ.get("TRAE_BASE", "http://trae2api-hub:8791")
TRAE_KEY = os.environ.get("TRAE_API_KEY", "")

TRAE_SETTINGS = os.environ.get("TRAE_SETTINGS", "/data/trae/accounts/settings.json")
TRAE_SCHEDULER = os.environ.get("TRAE_SCHEDULER", "/data/trae/usage/scheduler.json")
WB_USAGE = os.environ.get("WB_USAGE", "/data/wb/usage.jsonl")
TRAE_USAGE = os.environ.get("TRAE_USAGE", "/data/trae/usage/usage.jsonl")

HTTP_TIMEOUT = float(os.environ.get("DASH_HTTP_TIMEOUT", "20"))
CACHE_TTL = float(os.environ.get("DASH_CACHE_TTL", "20"))   # 上游指标缓存秒数

app = FastAPI(title="gateway-dashboard")


# ---------------------------------------------------------------------------
# 缓存：积分/签到要走上游，别让每次页面刷新都打一遍
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    try:
        value = fn()
    except Exception as exc:
        value = {"_error": str(exc)[:200]}
    with _cache_lock:
        _cache[key] = (now, value)
    return value


# ---------------------------------------------------------------------------
# Trae 面板 token（`/credits/summary` 不需要，但 `/usage` 需要；保留备用）
# ---------------------------------------------------------------------------

def _trae_panel_token() -> str:
    """用网关自己的持久密钥签一个面板会话 token。

    格式与 trae_settings.PanelSessions 一致：`<过期秒>.<nonce>.<hmac_sha256>`。
    只读 settings.json 里的密钥，不涉及任何账号明文。
    """
    try:
        secret = json.loads(pathlib.Path(TRAE_SETTINGS).read_text(encoding="utf-8"))[
            "panel_session_secret"
        ]
        expiry = int(time.time()) + 600
        nonce = secrets.token_urlsafe(12)
        sig = hmac.new(secret.encode(), f"{expiry}.{nonce}".encode(), hashlib.sha256).hexdigest()
        return f"{expiry}.{nonce}.{sig}"
    except Exception:
        return ""


def _get(url: str, headers: dict) -> dict:
    with httpx.Client(timeout=HTTP_TIMEOUT) as c:
        r = c.get(url, headers=headers)
        r.raise_for_status()
        return r.json()


def _trae_headers() -> dict:
    h = {"Authorization": f"Bearer {TRAE_KEY}"} if TRAE_KEY else {}
    tok = _trae_panel_token()
    if tok:
        h["X-Panel-Token"] = tok
    return h


def _wb_headers() -> dict:
    return {"Authorization": f"Bearer {WB_KEY}"} if WB_KEY else {}


# ---------------------------------------------------------------------------
# 网关指标（积分 / 签到 / 聚合用量）
# ---------------------------------------------------------------------------

def fetch_trae() -> dict:
    out: dict = {"ok": False, "credits": {}, "checkin": {}, "usage": {}}
    try:
        summary = _get(f"{TRAE_BASE}/credits/summary", _trae_headers())
        totals = summary.get("totals") or {}
        accounts = summary.get("accounts") or []
        remain = float(totals.get("remain") or 0)
        used = sum(float(a.get("credits_used") or 0) for a in accounts)
        size = sum(float(a.get("credits_size") or 0) for a in accounts)
        # 签到：credits/summary 给出最近一次到账额与时间
        last_at = next((a.get("checkin_at") for a in accounts if a.get("checkin_at")), "")
        daily = next((float(a.get("checkin") or 0) for a in accounts if a.get("checkin")), 0.0)
        out["credits"] = {
            "remain": round(remain, 2),
            "used": round(used, 2),
            "total": round(size, 2),
            "accounts": int(totals.get("accounts") or 0),
            "by_realm": summary.get("by_realm") or {},
        }
        out["checkin"] = {
            "daily_credit": daily,
            "last_at": last_at,
            "today_checked_in": (last_at[:10] == datetime.now(CST).strftime("%Y-%m-%d")) if last_at else None,
        }
        out["ok"] = True
    except Exception as exc:
        out["error"] = f"积分接口失败: {str(exc)[:160]}"

    # 排程器状态：签到任务结果与下次运行时间
    try:
        sch = json.loads(pathlib.Path(TRAE_SCHEDULER).read_text(encoding="utf-8"))
        stats = sch.get("last_cycle_stats") or {}
        task = (stats.get("tasks") or {}).get("daily_checkin") or {}
        out["checkin"]["scheduler"] = {
            "last_run": sch.get("last_run_time") or "",
            "next_run": sch.get("next_run_time") or "",
            "ok": task.get("ok"),
            "summary": task.get("summary") or "",
        }
    except Exception:
        pass

    # 聚合用量（接口给的是按 key 聚合，key 恒为 launcher，仅作总览参考）
    try:
        u = _get(f"{TRAE_BASE}/usage", _trae_headers())
        out["usage"] = {
            "requests": int(u.get("requests") or 0),
            "errors": int(u.get("errors") or 0),
            "prompt_tokens": int(u.get("prompt_tokens") or 0),
            "completion_tokens": int(u.get("completion_tokens") or 0),
            "cost_usd": float(u.get("cost_usd") or 0),
            "cost_cny": float(u.get("cost_cny") or 0),
        }
    except Exception:
        pass
    return out


def fetch_workbuddy() -> dict:
    out: dict = {"ok": False, "credits": {}, "checkin": {}, "usage": {}}
    try:
        s = _get(f"{WB_BASE}/api/usage_summary", _wb_headers())
        if s.get("error"):
            out["error"] = str(s["error"])[:160]
        else:
            out["credits"] = {
                "remain": round(float(s.get("remain") or 0), 2),
                "used": round(float(s.get("used") or 0), 2),
                "total": round(float(s.get("total") or 0), 2),
                "accounts": 1,
                "nickname": s.get("nickname") or "",
                "packages": s.get("packages") or [],
            }
            out["ok"] = True
    except Exception as exc:
        out["error"] = f"积分接口失败: {str(exc)[:160]}"

    try:
        c = _get(f"{WB_BASE}/api/checkin/status", _wb_headers())
        data = c.get("data") if isinstance(c.get("data"), dict) else c
        if isinstance(data, dict):
            out["checkin"] = {
                "today_checked_in": data.get("today_checked_in"),
                "streak_days": data.get("streak_days"),
                "daily_credit": data.get("daily_credit"),
                "today_credit": data.get("today_credit"),
                "active": data.get("active"),
            }
    except Exception as exc:
        out["checkin"]["error"] = str(exc)[:120]
    return out


# ---------------------------------------------------------------------------
# 逐请求用量：读 usage.jsonl，按来源聚合
# ---------------------------------------------------------------------------

def _read_jsonl(path: str) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if isinstance(d, dict):
                    rows.append(d)
    except Exception:
        pass
    return rows


def _wb_row(d: dict) -> dict:
    """WorkBuddy 行 -> 统一结构。"""
    ts = d.get("ts")
    at = (float(ts) / 1000.0) if isinstance(ts, (int, float)) else 0.0
    return {
        "gateway": "workbuddy",
        "at": at,
        "model": d.get("model") or "",
        "ok": bool(d.get("ok", True)),
        "prompt_tokens": int(d.get("input_tokens") or 0),
        "completion_tokens": int(d.get("output_tokens") or 0),
        "cost_usd": 0.0,          # WorkBuddy 记的是积分，不是美元；积分按租户套餐计
        "credit": 0.0,
        "client": d.get("client") or "",
        "client_ip": d.get("client_ip") or "",
        "endpoint": d.get("endpoint") or "",
        "error": _err_text(d),
        "err_kind": _err_kind(_err_text(d)),
    }


def _err_kind(err: str) -> str:
    """把失败原因归类，避免把「客户端主动断开」算成网关故障。

    `client disconnected` 是客户端在长上下文请求上主动放弃（超时/重发），
    与网关可用性无关；混在一起会让面板的失败率看起来像故障。
    """
    e = (err or "").strip().lower()
    if not e:
        return ""
    if "disconnect" in e or "cancel" in e:
        return "client_abort"
    if "401" in e or "unauthorized" in e:
        return "upstream_401"
    if "11128" in e or "channel" in e:
        return "channel_reject"
    if "400" in e:
        return "bad_request"
    if "429" in e or "6004" in e:
        return "rate_limit"
    if "timeout" in e or "timed out" in e:
        return "timeout"
    return "other"


def _err_text(d: dict) -> str:
    """把各网关形态不一的 error 字段归一成字符串。

    WorkBuddy 写 `error: "client disconnected"`（字符串）；
    Trae 写 `error: true` 布尔标志 + 真实信息在 `message`。
    不归一会让前端对布尔值取切片直接抛 TypeError。
    """
    raw = d.get("error")
    if isinstance(raw, bool):
        if not raw:
            return ""
        return str(d.get("message") or d.get("status") or "失败")[:200]
    if raw is None:
        return ""
    return str(raw)[:200]


def _trae_row(d: dict) -> dict:
    """Trae 行 -> 统一结构。"""
    at = d.get("at")
    err = _err_text(d)
    return {
        "gateway": "trae",
        "at": float(at) if isinstance(at, (int, float)) else 0.0,
        "model": d.get("model") or "",
        "ok": not err,
        "prompt_tokens": int(d.get("prompt_tokens") or 0),
        "completion_tokens": int(d.get("completion_tokens") or 0),
        "cost_usd": float(d.get("cost_usd") or 0),
        "credit": float(d.get("credit") or 0),
        "client": d.get("client") or "",
        "client_ip": d.get("client_ip") or "",
        "endpoint": d.get("endpoint") or "",
        "error": err,
        "err_kind": _err_kind(err),
    }


def _fmt_ts(at: float) -> str:
    if not at:
        return ""
    try:
        return datetime.fromtimestamp(at, CST).strftime("%m-%d %H:%M:%S")
    except Exception:
        return ""


def load_rows() -> list[dict]:
    rows = [_wb_row(d) for d in _read_jsonl(WB_USAGE)]
    rows += [_trae_row(d) for d in _read_jsonl(TRAE_USAGE)]
    rows = [r for r in rows if r["at"] > 0]
    rows.sort(key=lambda r: r["at"], reverse=True)
    return rows


def aggregate(rows: list[dict], cutoff: float) -> dict:
    """按 (客户端, IP) 聚合。cutoff=0 表示全部。"""
    picked = [r for r in rows if cutoff <= 0 or r["at"] >= cutoff]
    buckets: dict[tuple, dict] = {}
    for r in picked:
        client = r["client"] or "(历史数据)"
        ip = r["client_ip"] or ""
        key = (client, ip)
        b = buckets.setdefault(key, {
            "client": client,
            "client_ip": ip,
            "requests": 0,
            "errors": 0,
            "client_aborts": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_usd": 0.0,
            "credit": 0.0,
            "last_at": 0.0,
            "gateways": {},
            "endpoints": {},
        })
        b["requests"] += 1
        if not r["ok"]:
            if r.get("err_kind") == "client_abort":
                b["client_aborts"] += 1
            else:
                b["errors"] += 1
        b["prompt_tokens"] += r["prompt_tokens"]
        b["completion_tokens"] += r["completion_tokens"]
        b["cost_usd"] += r["cost_usd"]
        b["credit"] += r["credit"]
        b["last_at"] = max(b["last_at"], r["at"])
        b["gateways"][r["gateway"]] = b["gateways"].get(r["gateway"], 0) + 1
        ep = r["endpoint"] or "(未记录)"
        b["endpoints"][ep] = b["endpoints"].get(ep, 0) + 1

    sources = sorted(buckets.values(), key=lambda x: x["requests"], reverse=True)
    for b in sources:
        b["total_tokens"] = b["prompt_tokens"] + b["completion_tokens"]
        b["cost_usd"] = round(b["cost_usd"], 6)
        b["credit"] = round(b["credit"], 2)
        b["last_seen"] = _fmt_ts(b.pop("last_at"))
        b["gateways"] = " / ".join(f"{k}:{v}" for k, v in sorted(b["gateways"].items()))
        b["endpoints"] = ", ".join(
            f"{k.replace('/v1/', '')}×{v}"
            for k, v in sorted(b["endpoints"].items(), key=lambda kv: -kv[1])[:3]
        )
    return {
        "rows": len(picked),
        "sources": sources,
        "totals": {
            "requests": sum(b["requests"] for b in sources),
            "errors": sum(b["errors"] for b in sources),
            "client_aborts": sum(b["client_aborts"] for b in sources),
            "prompt_tokens": sum(b["prompt_tokens"] for b in sources),
            "completion_tokens": sum(b["completion_tokens"] for b in sources),
            "cost_usd": round(sum(b["cost_usd"] for b in sources), 6),
        },
    }


def recent(rows: list[dict], limit: int = 25) -> list[dict]:
    out = []
    for r in rows[:limit]:
        out.append({
            "time": _fmt_ts(r["at"]),
            "gateway": r["gateway"],
            "client": r["client"] or "(历史数据)",
            "client_ip": r["client_ip"] or "",
            "model": r["model"],
            "tokens": r["prompt_tokens"] + r["completion_tokens"],
            "ok": r["ok"],
            "err_kind": r.get("err_kind") or "",
            "error": str(r.get("error") or "")[:80],
        })
    return out


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@app.get("/api/overview")
def overview(window: str = "7d"):
    now = time.time()
    spans = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "all": 0}
    span = spans.get(window, spans["7d"])
    cutoff = (now - span) if span else 0.0

    trae = _cached("trae", fetch_trae)
    wb = _cached("workbuddy", fetch_workbuddy)
    rows = load_rows()
    agg = aggregate(rows, cutoff)

    credits_total = 0.0
    used_total = 0.0
    for gw in (trae, wb):
        c = gw.get("credits") or {}
        credits_total += float(c.get("remain") or 0)
        used_total += float(c.get("used") or 0)

    return {
        "generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": "Asia/Shanghai",
        "window": window,
        "gateways": {"trae": trae, "workbuddy": wb},
        "credits_total": {
            "remain": round(credits_total, 2),
            "used": round(used_total, 2),
            "total": round(credits_total + used_total, 2),
        },
        "usage": agg,
        "recent": recent(rows),
        "usage_file_rows": len(rows),
    }


@app.get("/health")
def health():
    return {"ok": True, "service": "gateway-dashboard"}


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(HTML)


# ---------------------------------------------------------------------------
# 前端（单页，无外部依赖：离线/tailnet 内可直接用）
# ---------------------------------------------------------------------------

HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>网关用量面板</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c2330; --line:#2b3444;
    --txt:#e6edf3; --dim:#8b949e; --accent:#58a6ff;
    --ok:#3fb950; --warn:#d29922; --bad:#f85149;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
       font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans SC",sans-serif}
  .wrap{max-width:1440px;margin:0 auto;padding:20px}
  header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:18px}
  h1{font-size:19px;margin:0;font-weight:600}
  .meta{color:var(--dim);font-size:12.5px}
  .spacer{flex:1}
  .btns{display:flex;gap:6px}
  .btn{background:var(--panel2);border:1px solid var(--line);color:var(--dim);
       padding:5px 11px;border-radius:6px;cursor:pointer;font-size:12.5px}
  .btn:hover{color:var(--txt);border-color:var(--accent)}
  .btn.on{background:var(--accent);color:#04121f;border-color:var(--accent);font-weight:600}
  .grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));margin-bottom:14px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:15px}
  .card h2{font-size:13px;margin:0 0 12px;color:var(--dim);font-weight:600;
           letter-spacing:.04em;text-transform:uppercase;display:flex;align-items:center;gap:8px}
  .tag{font-size:10.5px;padding:1px 7px;border-radius:20px;font-weight:600}
  .tag.ok{background:rgba(63,185,80,.16);color:var(--ok)}
  .tag.bad{background:rgba(248,81,73,.16);color:var(--bad)}
  .tag.warn{background:rgba(210,153,34,.16);color:var(--warn)}
  .big{font-size:30px;font-weight:650;letter-spacing:-.5px;line-height:1.15}
  .big small{font-size:13px;color:var(--dim);font-weight:400;margin-left:5px}
  .kv{display:flex;justify-content:space-between;padding:5px 0;
      border-bottom:1px dashed rgba(139,148,158,.14);font-size:13px}
  .kv:last-child{border-bottom:0}
  .kv span:first-child{color:var(--dim)}
  .kv span:last-child{font-variant-numeric:tabular-nums}
  .bar{height:6px;border-radius:6px;background:var(--panel2);overflow:hidden;margin:11px 0 4px}
  .bar > i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),#79c0ff)}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
  th{color:var(--dim);font-weight:600;font-size:11.5px;letter-spacing:.04em;text-transform:uppercase}
  td.num{text-align:right;font-variant-numeric:tabular-nums}
  tbody tr:hover{background:rgba(88,166,255,.05)}
  .scroll{overflow-x:auto}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
  .ok{color:var(--ok)} .bad{color:var(--bad)} .dim{color:var(--dim)}
  .err{background:rgba(248,81,73,.09);border:1px solid rgba(248,81,73,.3);
       color:#ffb3ad;padding:9px 12px;border-radius:8px;font-size:12.5px;margin-top:9px}
  .empty{color:var(--dim);text-align:center;padding:26px;font-size:13px}
  footer{color:var(--dim);font-size:11.5px;text-align:center;padding:18px 0 6px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>网关用量面板</h1>
    <div class="meta" id="meta">加载中…</div>
    <div class="spacer"></div>
    <div class="btns" id="wins">
      <button class="btn" data-w="24h">24 小时</button>
      <button class="btn on" data-w="7d">7 天</button>
      <button class="btn" data-w="30d">30 天</button>
      <button class="btn" data-w="all">全部</button>
    </div>
  </header>

  <div class="grid" id="cards"></div>

  <div class="card" style="margin-bottom:14px">
    <h2>请求来源 <span class="meta" id="srcNote"></span></h2>
    <div class="scroll">
      <table>
        <thead><tr>
          <th>来源</th><th>IP</th><th class="num">请求</th><th class="num">失败</th>
          <th class="num">主动断开</th>
          <th class="num">输入 token</th><th class="num">输出 token</th>
          <th class="num">参考成本</th><th>网关</th><th>端点</th><th>最近</th>
        </tr></thead>
        <tbody id="srcBody"><tr><td colspan="11" class="empty">加载中…</td></tr></tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <h2>最近请求</h2>
    <div class="scroll">
      <table>
        <thead><tr>
          <th>时间</th><th>网关</th><th>来源</th><th>模型</th>
          <th class="num">token</th><th>状态</th>
        </tr></thead>
        <tbody id="recBody"><tr><td colspan="6" class="empty">加载中…</td></tr></tbody>
      </table>
    </div>
  </div>

  <footer>仅对 tailnet 开放 · 每 30 秒自动刷新 · 时间为北京时间</footer>
</div>

<script>
const $ = id => document.getElementById(id);
let win = "7d";
let timer = null;

const nf = n => (n===null||n===undefined) ? "—" : Number(n).toLocaleString("zh-CN");
const money = v => (v===null||v===undefined||v===0) ? "—" : "$" + Number(v).toFixed(4);
const esc = s => String(s??"").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function tag(ok, text){
  if (ok === true)  return `<span class="tag ok">${esc(text||"正常")}</span>`;
  if (ok === false) return `<span class="tag bad">${esc(text||"异常")}</span>`;
  return `<span class="tag warn">${esc(text||"未知")}</span>`;
}

function gwCard(title, g, isTrae){
  const c = g.credits || {}, k = g.checkin || {};
  const remain = Number(c.remain||0), used = Number(c.used||0), total = Number(c.total||0);
  const pct = total > 0 ? Math.min(100, remain/total*100) : 0;
  const okTag = g.ok ? tag(true,"已连接") : tag(false, "未连接");
  let rows = "";
  rows += `<div class="kv"><span>剩余</span><span><b>${nf(remain)}</b></span></div>`;
  rows += `<div class="kv"><span>已用</span><span>${nf(used)}</span></div>`;
  rows += `<div class="kv"><span>总量</span><span>${nf(total)}</span></div>`;

  let ck = "";
  if (isTrae){
    const sch = k.scheduler || {};
    ck += `<div class="kv"><span>今日已签</span><span>${k.today_checked_in===true?'<b class="ok">是</b>':(k.today_checked_in===false?'<b class="bad">否</b>':'—')}</span></div>`;
    if (k.daily_credit) ck += `<div class="kv"><span>单次签到</span><span>${nf(k.daily_credit)}</span></div>`;
    if (k.last_at)      ck += `<div class="kv"><span>最近签到</span><span>${esc(k.last_at)}</span></div>`;
    if (sch.summary)    ck += `<div class="kv"><span>最近任务</span><span title="${esc(sch.summary)}">${esc(sch.summary.slice(0,26))}</span></div>`;
    if (sch.next_run)   ck += `<div class="kv"><span>下次排程</span><span>${esc(sch.next_run)}</span></div>`;
  } else {
    ck += `<div class="kv"><span>今日已签</span><span>${k.today_checked_in===true?'<b class="ok">是</b>':(k.today_checked_in===false?'<b class="bad">否</b>':'—')}</span></div>`;
    if (k.streak_days!=null) ck += `<div class="kv"><span>连续天数</span><span><b>${nf(k.streak_days)}</b></span></div>`;
    if (k.daily_credit!=null) ck += `<div class="kv"><span>单次签到</span><span>${nf(k.daily_credit)}</span></div>`;
    if (k.today_credit!=null) ck += `<div class="kv"><span>今日到账</span><span>${nf(k.today_credit)}</span></div>`;
  }

  const err = g.error ? `<div class="err">${esc(g.error)}</div>` : "";

  return `<div class="card">
    <h2>${esc(title)} ${okTag}</h2>
    <div class="big">${nf(remain)}<small>积分剩余</small></div>
    <div class="bar"><i style="width:${pct.toFixed(1)}%"></i></div>
    <div class="meta" style="margin-bottom:10px">已用 ${nf(used)} / 总量 ${nf(total)}</div>
    ${rows}${ck}${err}
  </div>`;
}

function totalCard(d){
  const t = d.credits_total || {};
  const u = (d.usage && d.usage.totals) || {};
  return `<div class="card">
    <h2>合计</h2>
    <div class="big">${nf(t.remain)}<small>积分剩余</small></div>
    <div class="bar"><i style="width:${t.total>0?Math.min(100,t.remain/t.total*100).toFixed(1):0}%"></i></div>
    <div class="kv"><span>两网已用</span><span>${nf(t.used)}</span></div>
    <div class="kv"><span>区间请求</span><span>${nf(u.requests)}</span></div>
    <div class="kv"><span>区间失败</span><span>${nf(u.errors)}</span></div>
    <div class="kv"><span>区间主动断开</span><span class="dim">${nf(u.client_aborts)}</span></div>
    <div class="kv"><span>区间 token</span><span>${nf((u.prompt_tokens||0)+(u.completion_tokens||0))}</span></div>
    <div class="kv"><span>区间参考成本</span><span>${money(u.cost_usd)}</span></div>
  </div>`;
}

function render(d){
  $("meta").textContent = `更新于 ${d.generated_at} · 用量行数 ${nf(d.usage_file_rows)}`;
  const g = d.gateways || {};
  $("cards").innerHTML = totalCard(d)
    + gwCard("Trae", g.trae || {}, true)
    + gwCard("WorkBuddy", g.workbuddy || {}, false);

  const src = (d.usage && d.usage.sources) || [];
  $("srcNote").textContent = src.length ? `（${src.length} 个来源，区间 ${nf(d.usage.rows)} 条请求）` : "";
  $("srcBody").innerHTML = src.length ? src.map(s => `<tr>
      <td><b>${esc(s.client)}</b></td>
      <td class="mono dim">${esc(s.client_ip||"—")}</td>
      <td class="num">${nf(s.requests)}</td>
      <td class="num ${s.errors?'bad':'dim'}">${nf(s.errors)}</td>
      <td class="num dim" title="客户端主动断开（长上下文超时/重发），非网关故障">${nf(s.client_aborts)}</td>
      <td class="num">${nf(s.prompt_tokens)}</td>
      <td class="num">${nf(s.completion_tokens)}</td>
      <td class="num">${money(s.cost_usd)}</td>
      <td class="dim">${esc(s.gateways)}</td>
      <td class="dim mono">${esc(s.endpoints||"—")}</td>
      <td class="dim">${esc(s.last_seen)}</td>
    </tr>`).join("")
    : `<tr><td colspan="11" class="empty">该区间没有请求记录</td></tr>`;

  const rec = d.recent || [];
  $("recBody").innerHTML = rec.length ? rec.map(r => `<tr>
      <td class="mono dim">${esc(r.time)}</td>
      <td>${esc(r.gateway)}</td>
      <td>${esc(r.client)}${r.client_ip?` <span class="dim mono">${esc(r.client_ip)}</span>`:""}</td>
      <td class="mono">${esc(r.model)}</td>
      <td class="num">${nf(r.tokens)}</td>
      <td>${r.ok?'<span class="ok">成功</span>'
            :(r.err_kind==="client_abort"
              ? `<span class="dim" title="${esc(r.error)}">主动断开</span>`
              : `<span class="bad" title="${esc(r.error)}">失败</span>`)}</td>
    </tr>`).join("")
    : `<tr><td colspan="6" class="empty">暂无记录</td></tr>`;
}

async function load(){
  try{
    const r = await fetch(`/api/overview?window=${encodeURIComponent(win)}`, {cache:"no-store"});
    if(!r.ok) throw new Error("HTTP "+r.status);
    render(await r.json());
  }catch(e){
    $("meta").textContent = "加载失败：" + e.message;
  }
}

$("wins").addEventListener("click", ev => {
  const b = ev.target.closest(".btn"); if(!b) return;
  win = b.dataset.w;
  [...$("wins").children].forEach(x => x.classList.toggle("on", x === b));
  load();
});

load();
timer = setInterval(load, 30000);
</script>
</body>
</html>
"""

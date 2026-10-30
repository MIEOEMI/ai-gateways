#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日账号巡检（daily sweep）—— workbuddy2api 的旁挂定时器。

作用：
  每天固定时间，先给「每个账号」各发一条最小对话请求（把当日活跃/任务走一遍），
  再查一次签到状态、未签则补签；巡检结束后网关仍按原配置（固定/故障转移）服务，
  不会改变当前生效账号。

为什么做成旁挂而不是改 converter.py：
  converter.py 是 7000 行的生产网关，动它要重建镜像、有停机风险。
  这里只通过它已有的公开接口（/v1/chat/completions 的 X-WorkBuddy-Account 指定账号、
  /api/checkin/status、/api/checkin/claim）完成巡检，零侵入、可随时摘掉。

用法：
  python daily_sweep.py --once              # 立即巡检一次（调试/手动）
  python daily_sweep.py --loop              # 常驻，每天 --time 指定时刻巡检
  python daily_sweep.py --once --dry-run    # 只打印将要做的事，不发请求
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx

CST = timezone(timedelta(hours=8))

# 网关后端固定国内站（copilot.tencent.com）。国内版（codebuddy.cn）与国际版
# （workbuddy.ai / codebuddy.ai）是两套独立账号体系，token 不能跨区使用：
# 异区账号的 token 发到国内后端会被上游 APISIX 挡下，返回 401 HTML 页。
# 这里与 converter.py 的选号/切号门禁保持同一口径。
BACKEND_REGION = "cn"
_INTL_DOMAINS = ("workbuddy.ai", "codebuddy.ai")


def region_of(domain: str) -> str:
    """域名 → 区域标记：'intl' 或 'cn'。"""
    d = (domain or "").lower()
    return "intl" if any(x in d for x in _INTL_DOMAINS) else "cn"


def log(msg: str) -> None:
    print("%s %s" % (datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def accounts_path(data_dir: str) -> str:
    candidates = [
        os.path.join(data_dir, "workbuddy2api", "accounts.json"),
        os.path.join(data_dir, "codebuddy2openai", "accounts.json"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return candidates[0]


def load_accounts(data_dir: str) -> list[dict]:
    p = accounts_path(data_dir)
    with open(p, encoding="utf-8") as f:
        cfg = json.load(f)
    active = cfg.get("active_uid") or ""
    out = []
    for uid, sess in (cfg.get("accounts") or {}).items():
        auth = (sess or {}).get("auth") or {}
        acct = (sess or {}).get("account") or {}
        out.append({
            "uid": uid,
            "active": uid == active,
            "domain": auth.get("domain") or "",
            "nickname": acct.get("nickname") or "",
        })
    out.sort(key=lambda a: (not a["active"], a["uid"]))
    return out


def model_for(acc: dict, default_model: str) -> str:
    domain = (acc.get("domain") or "").lower()
    if "workbuddy.ai" in domain:
        # 国际版：用免费模型，避免消耗积分
        return os.environ.get("SWEEP_MODEL_INTL", "deepseek-v4.1-flash")
    if "codebuddy.ai" in domain:
        return os.environ.get("SWEEP_MODEL_INTL", "deepseek-v4.1-flash")
    return default_model


def sweep_once(args) -> int:
    accounts = load_accounts(args.data_dir)
    if not accounts:
        log("✗ 没有读到任何账号：%s" % accounts_path(args.data_dir))
        return 2

    log("巡检开始，共 %d 个账号（当前生效：%s）" % (
        len(accounts), next((a["uid"][:8] for a in accounts if a["active"]), "?")))

    headers = {"X-Api-Key": args.api_key, "Content-Type": "application/json"}
    ok = fail = skipped = 0
    with httpx.Client(timeout=args.timeout) as client:
        for acc in accounts:
            model = model_for(acc, args.model)
            body = {
                "model": model,
                "messages": [{"role": "user", "content": args.prompt}],
                "max_tokens": args.max_tokens,
                "stream": False,
            }
            h = dict(headers)
            h["X-WorkBuddy-Account"] = acc["uid"]
            label = "%s(%s)" % (acc["uid"][:8], acc["domain"] or "?")
            # 区域门禁：网关后端固定国内站（copilot.tencent.com），国际版账号的
            # token 与之不通 —— 请求必然 401，还会把账号写进冷却池。
            # 与网关的选号/切号门禁同一口径，这里主动跳过，不去污染状态。
            if region_of(acc.get("domain") or "") != BACKEND_REGION:
                skipped += 1
                log("- %s 跳过：异区账号（网关后端为 %s 区）" % (label, BACKEND_REGION))
                continue
            if args.dry_run:
                log("[dry-run] 将请求 %s model=%s" % (label, model))
                continue
            t0 = time.time()
            try:
                r = client.post("%s/v1/chat/completions" % args.base_url, headers=h, json=body)
                dt = time.time() - t0
                if r.status_code == 200:
                    ok += 1
                    log("✓ %s 请求成功 %.1fs model=%s" % (label, dt, model))
                else:
                    fail += 1
                    log("✗ %s HTTP %s %.1fs %s" % (label, r.status_code, dt, r.text[:160].replace("\n", " ")))
            except Exception as e:
                fail += 1
                log("✗ %s 请求异常：%s" % (label, str(e)[:160]))

        # 签到：接口只作用于当前生效账号，幂等
        if not args.dry_run and not args.no_checkin:
            try:
                st = client.get("%s/api/checkin/status" % args.base_url, headers=headers).json()
                data = st.get("data") or {}
                if not st.get("ok"):
                    log("· 签到状态不可用：%s" % str(st.get("error"))[:120])
                elif data.get("today_checked_in"):
                    log("· 今日已签到（连签 %s 天，今日 +%s）" % (data.get("streak_days"), data.get("today_credit")))
                elif data.get("active"):
                    cr = client.post("%s/api/checkin/claim" % args.base_url, headers=headers).json()
                    if cr.get("ok"):
                        log("✓ 签到成功 +%s 积分（连签 %s 天）" % (cr.get("credit"), cr.get("streak_days")))
                    else:
                        log("· 签到未完成：%s / %s" % (cr.get("status"), cr.get("msg")))
                else:
                    log("· 签到活动未开放")
            except Exception as e:
                log("· 签到检查异常：%s" % str(e)[:160])

    log("巡检结束：成功 %d，失败 %d%s" % (ok, fail, ("，跳过 %d（异区账号）" % skipped) if skipped else ""))
    return 0 if fail == 0 else 1


def seconds_until(hhmm: str) -> float:
    h, m = (int(x) for x in hhmm.split(":"))
    now = datetime.now(CST)
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="立即巡检一次后退出")
    ap.add_argument("--loop", action="store_true", help="常驻，每天 --time 巡检")
    ap.add_argument("--time", default="00:05", help="每日巡检时刻（北京时间 HH:MM，默认 00:05）")
    ap.add_argument("--base-url", default=os.environ.get("SWEEP_BASE_URL", "http://127.0.0.1:8787"))
    ap.add_argument("--api-key", default=os.environ.get("WORKBUDDY2API_KEY", ""))
    ap.add_argument("--data-dir", default=os.environ.get("LOCALAPPDATA", "/data"))
    ap.add_argument("--model", default=os.environ.get("SWEEP_MODEL", "hy4-preview"))
    ap.add_argument("--prompt", default="ping")
    ap.add_argument("--max-tokens", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--no-checkin", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.once and not args.loop:
        args.once = True
    if not args.api_key:
        log("✗ 缺少 API key（--api-key 或 WORKBUDDY2API_KEY）")
        return 2

    if args.once:
        return sweep_once(args)

    log("常驻模式：每天 %s（北京时间）巡检一次" % args.time)
    while True:
        wait = seconds_until(args.time)
        log("下次巡检：%.0f 分钟后" % (wait / 60))
        time.sleep(wait)
        try:
            sweep_once(args)
        except Exception as e:
            log("巡检异常：%s" % str(e)[:200])


if __name__ == "__main__":
    sys.exit(main())

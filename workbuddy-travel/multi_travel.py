# -*- coding: utf-8 -*-
"""WorkBuddy 宠物旅行多账号闭环：遍历 wb-switch 全部个人账号，逐个「先领后派」。

与 wb_checkin.py 的 multi_account_checkin 同源思路：
  凭据源 = ~/.wb-switch/accounts.json（本机 WorkBuddy 工具保存的 access_token/uid/domain），
  与客户端当前登录状态完全解耦；企业账号无旅行机制，跳过。
与 buddy_station.py 的 travel_auto 同样的安全约束：
  派遣前必须确认 daily_limit_reached 为假；旅行中/达上限一律跳过，绝不发多余写请求。
日志追加写 multi_travel.log（UTF-8），由 travel_auto.py 在 buddy_station 主账号闭环之后调用。
"""
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
import datetime

WBSWITCH_ACCOUNTS_PATH = os.path.expanduser(r"~\.wb-switch\accounts.json")
TRAVEL_DOMAIN = "www.workbuddy.cn"
STATUS_PATH = "/activity/growth/buddy/travel/status"
CLAIM_PATH = "/activity/growth/buddy/travel/claim"
DEPART_PATH = "/activity/growth/buddy/travel/depart"
CONFIG_PATH = "/activity/growth/buddy/travel/config"
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "multi_travel.log")
# 可选的 token 信封解密助手：放在本脚本同目录即可，没有则只支持明文 token
DECRYPT_HELPER_DIR = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write("[%s] %s\n" % (ts, msg))


def _unwrap_token(value):
    """兼容新旧 auth 格式：明文直接返回；{"$wbEncrypted":1,...} 信封自动解密。"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if DECRYPT_HELPER_DIR not in sys.path:
            sys.path.insert(0, DECRYPT_HELPER_DIR)
        try:
            from wb_auth_decrypt import unwrap
            return unwrap(value)
        except Exception as exc:
            log("token 解密失败：%s" % exc)
            return None
    return None


def _is_enterprise(acc):
    """企业账号没有旅行/签到机制，跳过。"""
    if acc.get("enterpriseId"):
        return True
    pr = acc.get("profile_raw") or {}
    return bool(pr.get("enterpriseId")) or pr.get("type") in ("ultimate", "enterprise")


def api(domain, token, path, method="GET", payload=None):
    """调旅行接口，返回 (http_status, json_body)。GET 不带体，POST 带 JSON。"""
    url = "https://" + domain + path
    body = json.dumps(payload if payload is not None else {}).encode("utf-8") if method == "POST" else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", "Bearer " + (token or ""))
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:
            return exc.code, {"code": -1, "msg": "HTTP " + str(exc.code)}
    except Exception as exc:  # noqa: BLE001
        return 0, {"code": -1, "msg": str(exc)}


def travel_auto_for_account(nickname, token, fallback_domain):
    """单账号旅行闭环。返回行为摘要字符串。"""
    domain = TRAVEL_DOMAIN
    st, body = api(domain, token, STATUS_PATH)
    if st in (401, 403) and fallback_domain:
        domain = fallback_domain
        st, body = api(domain, token, STATUS_PATH)
    if st != 200 or not isinstance(body, dict) or body.get("code") != 0:
        return "查询旅行状态失败（HTTP %s, code %s, %s）" % (
            st, body.get("code") if isinstance(body, dict) else "?",
            (body.get("msg") if isinstance(body, dict) else None) or "")

    status = body.get("data") or {}
    state = status.get("state")
    actions = []

    # 1) 先领：到达即领
    if state == "arrived":
        st, body = api(domain, token, CLAIM_PATH, method="POST", payload={})
        if st == 200 and isinstance(body, dict) and body.get("code") == 0:
            credit = (body.get("data") or {}).get("reward_credit")
            actions.append("领取旅行奖励 +%s 积分" % (credit if credit is not None else "?"))
        else:
            actions.append("领取失败（HTTP %s, %s）" % (
                st, (body.get("msg") if isinstance(body, dict) else None) or ""))
        st, body = api(domain, token, STATUS_PATH)
        status = (body.get("data") or {}) if (st == 200 and isinstance(body, dict)
                                              and body.get("code") == 0) else status
        state = status.get("state")

    # 2) 后派：idle 且未达上限才发写请求
    if status.get("daily_limit_reached"):
        actions.append("今日派遣已达上限，跳过")
    elif state in (None, "", "idle"):
        st, cfg = api(domain, token, CONFIG_PATH)
        locations = (cfg.get("data") or {}).get("locations") if (st == 200 and isinstance(cfg, dict)) else None
        ids = [loc.get("id") for loc in (locations or []) if isinstance(loc, dict) and loc.get("id") is not None]
        if not ids:
            actions.append("未获取到可选地点，跳过派遣")
        else:
            loc_id = random.choice(ids)
            st, body = api(domain, token, DEPART_PATH, method="POST", payload={"location_id": loc_id})
            if st == 200 and isinstance(body, dict) and body.get("code") == 0:
                loc = (body.get("data") or {}).get("location") or {}
                actions.append("已派出 → %s" % (loc.get("name") or ("地点 %s" % loc_id)))
            else:
                actions.append("派遣失败（HTTP %s, %s）" % (
                    st, (body.get("msg") if isinstance(body, dict) else None) or ""))
    else:
        actions.append("旅行中，无需派遣")

    return "state=%s | %s" % (state, "；".join(actions) if actions else "无操作")


def main():
    if not os.path.exists(WBSWITCH_ACCOUNTS_PATH):
        log("多账号旅行：未找到 wb-switch accounts.json，跳过")
        return 0
    try:
        with open(WBSWITCH_ACCOUNTS_PATH, "r", encoding="utf-8") as f:
            accounts = json.load(f)
    except Exception as exc:
        log("多账号旅行：读取 accounts.json 失败：%s" % exc)
        return 0

    now_ms = time.time() * 1000
    rc = 0
    for acc in accounts:
        nickname = acc.get("nickname") or "?"
        try:
            if _is_enterprise(acc):
                log("多账号 [%s]：企业账号无旅行机制，跳过" % nickname)
                continue
            raw = acc.get("access_token") or (acc.get("auth_raw") or {}).get("accessToken")
            token = _unwrap_token(raw)
            if not token:
                log("多账号 [%s]：缺少可用 token，跳过" % nickname)
                rc = rc or 1
                continue
            exp = acc.get("expiresAt")
            if exp and exp < now_ms:
                log("多账号 [%s]：token 已过期，等刷新后再跑（本次跳过）" % nickname)
                rc = rc or 1
                continue
            log("多账号 [%s]：%s" % (nickname, travel_auto_for_account(
                nickname, token, ("https://" + acc["domain"]).strip() if acc.get("domain") else None)))
        except Exception as exc:  # noqa: BLE001
            log("多账号 [%s]：异常 %r（继续下一个）" % (nickname, exc))
            rc = rc or 1
    return rc


if __name__ == "__main__":
    sys.exit(main())

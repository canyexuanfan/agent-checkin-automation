# -*- coding: utf-8 -*-
"""ima「每日登录福利」自动签到（无需客户端运行，纯 HTTP）

流程（安全纪律与豆包签到一致）：
  1) refresh_token 换 access token（等效客户端打开，无风险）
  2) get_activity_info 只读查询今日状态
  3) status==3(可领取) 才发 check_in；status==2 视为已签到，绝不重复请求
  4) 周日额外试领一次满签奖励 type=2（code 全接住，不中断）
失败不重试，留给 supervisor 下一触发点。

凭据：同目录 credentials.json（extract_credentials.py 从 ima 客户端提取）。
日志：ima-checkin.log，成功标记"签到成功/已签到"（供 supervisor 判定）。
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_URL = "https://ima.qq.com"
REFRESH_URL = BASE_URL + "/cgi-bin/auth_login/refresh"
ACTIVITY_INFO_URL = BASE_URL + "/cgi-bin/daily_login_activity/get_activity_info"
CHECK_IN_URL = BASE_URL + "/cgi-bin/daily_login_activity/check_in"

CST = timezone(timedelta(hours=8))
DIR = os.path.dirname(os.path.abspath(__file__))
CRED_FILE = os.path.join(DIR, "credentials.json")
LOG = os.path.join(DIR, "ima-checkin.log")

STATUS_CLAIMED = 2
STATUS_AVAILABLE = 3
CODE_OK = 0
CODE_NOT_FULL_WEEK = 1001
CODE_ALREADY_CHECKED_IN = 1002
STATUS_TEXT = {1: "已过期", 2: "已领取", 3: "可领取", 4: "未解锁"}


def log(msg):
    line = "[%s] %s" % (datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"), msg)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    try:
        print(line)
    except Exception:
        pass


def to_int32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v >= 0x80000000 else v


def bkn(token):
    """页面 JS 的 djb2 签名，作 x-ima-bkn 头。"""
    h = 5381
    for ch in token:
        h += to_int32(to_int32(h) << 5) + ord(ch)
    return to_int32(h) & 0x7FFFFFFF


def post(url, body, headers, timeout=25):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except Exception as e:
        return {"code": -1, "msg": "网络异常: %r" % e}
    if not raw.strip():
        return {"code": -1, "msg": "HTTP %s 空响应(可能限流)" % status}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"code": -1, "msg": "HTTP %s 非 JSON: %s" % (status, raw[:150])}
    if isinstance(data, dict):
        data.setdefault("http_status", status)
    return data


def auth_headers(cookie, token=None):
    h = {
        "Content-Type": "application/json",
        "from_browser_ima": "1",
        "extension_version": "5.17.2",
        "x-ima-cookie": cookie,
    }
    if token:
        h["x-ima-bkn"] = str(bkn(token))
    return h


def build_cookie(cred, token):
    return "; ".join([
        "IMA-UID=%s" % cred["ima_uid"],
        "IMA-TOKEN=%s" % token,
        "IMA-REFRESH-TOKEN=%s" % cred["refresh_token"],
        "UID-TYPE=%s" % cred["uid_type"],
        "IMA-GUID=%s" % cred["ima_guid"],
        "PLATFORM=H5",
    ])


def find_item(info, top):
    for item in info.get("infos") or []:
        if item.get("top") == top:
            return item
    return None


def week_summary(info):
    cells = []
    for item in info.get("infos") or []:
        mark = {2: "已领", 3: "可领", 4: "未解锁"}.get(item.get("status"), "?")
        cells.append("%s:%s" % (item.get("top") or "?", mark))
    return " ".join(cells)


def main():
    with open(CRED_FILE, "r", encoding="utf-8") as f:
        cred = json.load(f)

    # 1) 换 access token
    rr = post(REFRESH_URL,
              {"userId": cred["ima_uid"], "refreshToken": cred["refresh_token"], "tokenType": -1},
              auth_headers("IMA-GUID=%s; PLATFORM=H5" % cred["ima_guid"]))
    token = rr.get("token")
    if rr.get("code") != 0 or not token:
        log("FAIL refresh 失败 code=%s msg=%s（凭证可能失效，需重开 ima 后重新提取）"
            % (rr.get("code"), rr.get("msg")))
        return 1

    cookie = build_cookie(cred, token)
    # 2) 只读查询活动状态
    info = post(ACTIVITY_INFO_URL, {}, auth_headers(cookie, token))
    if info.get("code") != 0:
        log("FAIL 查询活动失败 code=%s msg=%s" % (info.get("code"), info.get("msg")))
        return 1

    today = find_item(info, "今日")
    if today is None:
        log("FAIL 活动改版：未找到今日条目 %s" % json.dumps(info, ensure_ascii=False)[:300])
        return 1
    status = today.get("status")
    log("本周进度: %s | 累计 %s 天 %s 算力"
        % (week_summary(info), info.get("checkin_days"), info.get("total_reward_points")))

    # 3) 状态机：只在可领取时发签到
    if status == STATUS_CLAIMED:
        log("已签到（今日状态=已领取，不发请求）")
    elif status == STATUS_AVAILABLE:
        resp = post(CHECK_IN_URL, {"type": 1}, auth_headers(cookie, token))
        if resp.get("code") == CODE_OK:
            got = resp.get("reward_received", resp.get("rewardReceived"))
            log("签到成功%s（今日算力 +%s）" % ("，本周满签奖励见下" if datetime.now(CST).weekday() == 6 else "",
                                            got if got is not None else "?"))
        else:
            log("FAIL check_in code=%s msg=%s（不重试）" % (resp.get("code"), resp.get("msg")))
            return 1
        info = post(ACTIVITY_INFO_URL, {}, auth_headers(cookie, token)) or info
    else:
        log("今日不可签到 status=%s(%s)" % (status, STATUS_TEXT.get(status, "未知")))

    # 4) 周日满签奖励（第 7 天）
    if datetime.now(CST).weekday() == 6:
        bonus = find_item(info, "满签奖励")
        if bonus is not None and bonus.get("status") == STATUS_AVAILABLE:
            resp = post(CHECK_IN_URL, {"type": 2}, auth_headers(cookie, token))
            if resp.get("code") == CODE_OK:
                got = resp.get("reward_received", resp.get("rewardReceived"))
                log("满签奖励领取成功 +%s 算力" % (got if got is not None else "?"))
            else:
                log("满签奖励试领 code=%s msg=%s（不重试）" % (resp.get("code"), resp.get("msg")))
        elif bonus is not None:
            log("满签奖励状态=%s（%s）" % (bonus.get("status"), STATUS_TEXT.get(bonus.get("status"), "?")))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        log("FAIL 未捕获异常: %r" % e)
        sys.exit(1)

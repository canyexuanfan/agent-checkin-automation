# -*- coding: utf-8 -*-
"""签到补跑巡检（supervisor）：检查当天各签到任务是否成功，失败的自动补跑。

背景：主任务 00:05（DuMate 00:07）运行，遇临时断网/网络抖动会失败且无人补跑。
本脚本由计划任务在 00:45 / 08:30 / 12:45 触发，作为兜底：

判定规则（每个任务）：
  1) 先扫当天日志里的成功标记（幂等脚本重跑无副作用，但能不跑就不跑）
  2) 没有成功标记 → 运行脚本，退出码 0 = 成功（wb/minimax/dumate）
  3) travel 特殊：退出码不反映网络失败，改看日志——
     当天有成功标记(已派出/旅行中/已达上限/领取旅行奖励)且无失败标记 → 成功；
     否则补跑，并只评估补跑新增的日志段（旧失败行不重复计）
状态记录：supervisor_state.json（按天） + supervisor.log

用法：python supervisor.py            # 巡检+补跑
      python supervisor.py --dry-run  # 只检查不补跑
"""
import json
import os
import re
import subprocess
import sys
import datetime

# Python 解释器（pythonw 静默运行）；按本机环境修改
PY = sys.executable
DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(DIR)  # 各签到脚本按子目录与本文件同级存放
STATE_PATH = os.path.join(DIR, "supervisor_state.json")
SLOG = os.path.join(DIR, "supervisor.log")
PASSES = 3          # 最多巡检轮数（首轮 + 2 轮补跑）
PASS_SLEEP = 180    # 轮间等待（秒），给网络恢复留时间
CHILD_TIMEOUT = 900

TRAVEL_OK_RE = re.compile(r"已派出|旅行中|已达上限|领取旅行奖励")
TRAVEL_BAD_RE = re.compile(r"查询旅行状态失败|派遣失败|领取失败|token 已过期|缺少可用 token")

TASKS = [
    {"name": "wb", "script": os.path.join(ROOT, "workbuddy-checkin", "wb_checkin.py"), "args": ["--verbose"],
     "log": os.path.join(ROOT, "workbuddy-checkin", "checkin.log"), "mode": "exit",
     "ok_re": re.compile(r"签到成功|已签到")},
    {"name": "minimax", "script": os.path.join(ROOT, "minimax-checkin", "minimax_checkin.py"), "args": [],
     "log": os.path.join(ROOT, "minimax-checkin", "minimax-checkin.log"), "mode": "exit",
     "ok_re": re.compile(r"签到成功|已签到")},
    {"name": "dumate", "script": os.path.join(ROOT, "dumate-checkin", "dumate_checkin.py"), "args": [],
     "log": os.path.join(ROOT, "dumate-checkin", "dumate-checkin.log"), "mode": "exit",
     "ok_re": re.compile(r"签到成功")},
    {"name": "travel", "script": os.path.join(ROOT, "workbuddy-travel", "multi_travel.py"), "args": [],
     "log": os.path.join(ROOT, "workbuddy-travel", "multi_travel.log"), "mode": "travel"},
    # 豆包工作：脚本内部有只读探测+每日1次硬限制，无活动投放视为已处理
    {"name": "doubao", "script": os.path.join(ROOT, "doubao-work-checkin", "dw_checkin.py"), "args": [],
     "log": os.path.join(ROOT, "doubao-work-checkin", "dw_checkin.log"), "mode": "exit",
     "ok_re": re.compile(r"签到成功|已签到"),
     "settled_re": re.compile(r"无活动投放|已尝试过签到")},
    # ima 每日登录福利：脚本内部先只读查询，状态=已领取时不发签到请求
    {"name": "ima", "script": os.path.join(ROOT, "ima-checkin", "ima_checkin.py"), "args": [],
     "log": os.path.join(ROOT, "ima-checkin", "ima-checkin.log"), "mode": "exit",
     "ok_re": re.compile(r"签到成功|已签到|领取成功")},
]


def slog(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(SLOG, "a", encoding="utf-8") as f:
        f.write("[%s] %s\n" % (ts, msg))
    try:
        print("[%s] %s" % (ts, msg))
    except Exception:
        pass


def today():
    return datetime.date.today().isoformat()


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    # 只保留最近 14 天，防止无限膨胀
    keys = sorted(st.keys())[-14:]
    trimmed = {k: st[k] for k in keys}
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False, indent=1)


def today_log_text(path):
    """返回日志中属于今天的行拼接文本。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return ""
    prefix = "[%s " % today()
    return "".join(ln for ln in lines if ln.startswith(prefix))


def log_size(path):
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


def log_tail(path, offset):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            return f.read()
    except Exception:
        return ""


def run_script(task):
    """运行签到脚本（无窗口），返回退出码。脚本自带日志与幂等。"""
    cmd = [PY, task["script"]] + task.get("args", [])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=CHILD_TIMEOUT, creationflags=0x08000000)
        return p.returncode
    except Exception as exc:
        slog("  运行异常：%r" % exc)
        return -1


def eval_task(task, extra_text=""):
    """评估任务当天是否已成功。extra_text 用于 travel 只看新增段。"""
    t = today_log_text(task["log"]) + extra_text
    if task["mode"] == "travel":
        # 按时间顺序找最后一条匹配行：最后一次结果是成功才算成功
        # （凌晨主任务失败留下的旧行不该否定之后补跑的成功）
        lines = [ln for ln in t.splitlines() if ln.strip()]
        verdict = None
        for ln in lines:
            if TRAVEL_BAD_RE.search(ln):
                verdict = False
            elif TRAVEL_OK_RE.search(ln):
                verdict = True
        return bool(verdict)
    if task.get("settled_re") and task["settled_re"].search(t):
        # 无活动投放/今日已尝试 → 视为已处理（脚本内部有每日1次硬限制，安全）
        return True
    return bool(task["ok_re"].search(t))


def process(task, state, dry=False):
    """处理单个任务。返回 True=当天已成功。"""
    name = task["name"]
    day_state = state.setdefault(today(), {})

    # 0) 本次巡检会话里已确认成功
    if day_state.get(name) == "ok":
        slog("%s：状态记录=成功，跳过" % name)
        return True

    # 1) 扫当天日志（主任务 00:05 成功的记录）
    if eval_task(task):
        slog("%s：当天日志有成功记录，跳过补跑" % name)
        day_state[name] = "ok"
        save_state(state)
        return True

    if dry:
        slog("%s：未发现成功记录（dry-run，不补跑）" % name)
        return False

    # 2) 补跑（travel 记录日志偏移，只看新增段）
    offset = log_size(task["log"]) if task["mode"] == "travel" else 0
    slog("%s：未成功，开始补跑 ..." % name)
    rc = run_script(task)
    if task["mode"] == "exit":
        ok = (rc == 0)
    else:
        ok = eval_task(task, extra_text=log_tail(task["log"], offset))
    if ok:
        slog("%s：补跑成功（exit=%s）" % (name, rc))
        day_state[name] = "ok"
        save_state(state)
        return True
    slog("%s：补跑失败（exit=%s），留给下一轮/下一触发点" % (name, rc))
    day_state[name] = "fail@%s" % datetime.datetime.now().strftime("%H:%M")
    save_state(state)
    return False


def main():
    dry = "--dry-run" in sys.argv
    slog("=== 巡检开始%s ===" % ("（dry-run）" if dry else ""))
    state = load_state()
    # 清掉非今天的记录视图（save 时会裁剪）
    pending = list(TASKS)
    for i in range(1 if dry else PASSES):
        if i > 0:
            slog("--- 第 %d 轮补跑（等待 %ds 后开始）---" % (i + 1, PASS_SLEEP))
            import time
            time.sleep(PASS_SLEEP)
        still = []
        for task in pending:
            if not process(task, state, dry):
                still.append(task)
        pending = still
        if not pending:
            break
    for task in pending:
        slog("警告：%s 当天仍未成功，请人工检查日志 %s" % (task["name"], task["log"]))
    slog("=== 巡检结束：成功 %d/%d ===" % (len(TASKS) - len(pending), len(TASKS)))
    return 0 if not pending else 1


if __name__ == "__main__":
    sys.exit(main())

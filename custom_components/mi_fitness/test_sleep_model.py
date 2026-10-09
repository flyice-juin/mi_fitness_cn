# -*- coding: utf-8 -*-
"""本地沙箱验证:_upsert_sleep_record/_rebuild_sleep_views 用真实形状的假数据跑通。
不经 HA,直接从 coordinator.py 抽取这两个方法 + 依赖的模块级助手。"""
import ast
import time
from datetime import datetime, timedelta

SRC = open("coordinator.py", encoding="utf-8").read()
tree = ast.parse(SRC)
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
methods = {n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
mod = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

ns = {
    "time": time,
    "datetime": datetime,
    "timedelta": timedelta,
    "_sleep_quality_score": None,
    "_stages_to_list": None,
}
for name in ("_sleep_quality_score", "_stages_to_list"):
    code = ast.get_source_segment(SRC, mod[name])
    exec(compile(code, "coordinator", "exec"), ns)


class Sandbox:
    def __init__(self):
        self._sleep_records = {}
        self._sleep_stages = {}
        self._sleep_history = {}
        self._sensor_data = {}

    def _build_sleep_history_list(self):
        return sorted(self._sleep_history.values(), key=lambda x: x["date"], reverse=True)[:14]


for name in ("_upsert_sleep_record", "_rebuild_sleep_views"):
    code = ast.get_source_segment(SRC, methods[name])
    exec(compile(code, "coordinator", "exec"), ns)
    setattr(Sandbox, name, ns[name])

c = Sandbox()

# 昨晚主觉:10/7 22:09 睡 → 10/8 05:57 醒,464 分钟,带分段
night_bed = 1791382140
night_wake = 1791410220
night_stages = [
    {"start": night_bed, "end": night_bed + 1740, "state": 3, "label": "deep"},
    {"start": night_bed + 1740, "end": night_wake, "state": 2, "label": "light"},
]
c._upsert_sleep_record(night_bed, night_wake, 464, 109, 355, 0, 1, 0, night_stages,
                       {"avg_hr": 74, "max_hr": 88, "min_hr": 63})

# 日汇总重发(同 bedtime,时长更长、带小米评分 86)——应吸收评分不丢分段
c._upsert_sleep_record(night_bed, night_wake, 468, 109, 355, 0, 1, 86, [],
                       {"avg_hr": 0, "max_hr": 0, "min_hr": 0})

# 中午小睡:10/8 12:30 → 13:00,30 分钟
nap_bed = 1791448200
nap_wake = 1791450000
nap_stages = [{"start": nap_bed, "end": nap_wake, "state": 2, "label": "light"}]
c._upsert_sleep_record(nap_bed, nap_wake, 30, 0, 30, 0, 0, 0, nap_stages, {})

c._rebuild_sleep_views()

sd = c._sensor_data
hist = c._sleep_history
ok = True


def chk(label, cond, extra=""):
    global ok
    print(("✓" if cond else "✗"), label, extra)
    if not cond:
        ok = False


chk("历史键=按醒来日(只有 10/8)", list(hist.keys()) == ["2026-10-08"], list(hist.keys()))
chk("总时长=主觉+小睡", sd["sleep_duration"] == 468 + 30, sd["sleep_duration"])
chk("深睡合计", sd["sleep_deep"] == 109, sd["sleep_deep"])
chk("夜醒合计", sd["sleep_awake_count"] == 1, sd["sleep_awake_count"])
chk("stages 合并(夜+小睡)", len(sd["sleep_stages"]) == 3, len(sd["sleep_stages"]))
chk("stages 按时间排序", sd["sleep_stages"][0]["start"] < sd["sleep_stages"][-1]["start"])
chk("小米评分吸收", sd["sleep_quality"] == 86, sd["sleep_quality"])
chk("就寝=最早段", sd["sleep_bedtime"] == night_bed, sd["sleep_bedtime"])
chk("醒来=最晚段", sd["sleep_wakeup"] == nap_wake, sd["sleep_wakeup"])
chk("历史条目带分段三元组", len(hist["2026-10-08"]["stages"]) == 3)
chk("效率=468+30/(22:09→13:00)", 0 < sd["sleep_efficiency"] < 100, sd["sleep_efficiency"])

print("\n" + ("ALL PASS" if ok else "FAILED"))
raise SystemExit(0 if ok else 1)

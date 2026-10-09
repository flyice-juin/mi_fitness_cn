# -*- coding: utf-8 -*-
"""本地沙箱验证:运动记录解析/入库/视图重建用「真实响应形状」跑通。
形状取自 get_sport_records_by_time 实测(信封 key/category + value JSON 字符串,
心率字段名 avg_hrm/max_hrm、duration 秒、distance 米)。
不经 HA,直接从 coordinator.py 抽取方法 + 依赖的模块级助手。"""
import ast
import json
import time
from datetime import datetime
from typing import Any

SRC = open("coordinator.py", encoding="utf-8").read()
tree = ast.parse(SRC)
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
methods = {n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
mod = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

ns = {"time": time, "datetime": datetime, "json": json, "_LOGGER": None, "Any": Any}
for name in ("_day_start_ts", "_parse_value"):
    code = ast.get_source_segment(SRC, mod[name])
    exec(compile(code, "coordinator", "exec"), ns)

# 模块级常量(赋值语句,不在 FunctionDef 里):_SPORT_TYPES + _SPORT_TYPE_KEYS
for node in tree.body:
    if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in (
        "_SPORT_TYPES", "_SPORT_TYPE_KEYS",
    ):
        exec(compile(ast.Module(body=[node], type_ignores=[]), "coordinator", "exec"), ns)


class _FakeLogger:
    def warning(self, *a, **k):
        print("  [warn-log]", a[0] % a[1:] if len(a) > 1 else a[0])
    def info(self, *a, **k):
        print("  [info-log]", a[0] % a[1:] if len(a) > 1 else a[0])


ns["_LOGGER"] = _FakeLogger()


class Sandbox:
    def __init__(self):
        self._workouts = {}
        self._sensor_data = {}
        self._exercise_shape_logged = set()

    def _log_exercise_shape_once(self, tag, obj):
        code = ast.get_source_segment(SRC, methods["_log_exercise_shape_once"])
        local = dict(ns)
        exec(compile(code, "coordinator", "exec"), local)
        local["_log_exercise_shape_once"](self, tag, obj)


for name in ("_extract_workouts", "_workout_from_value", "_upsert_workout",
             "_rebuild_workout_views", "_log_exercise_shape_once"):
    code = ast.get_source_segment(SRC, methods[name])
    local = dict(ns)
    exec(compile(code, "coordinator", "exec"), local)
    setattr(Sandbox, name, local[name])

c = Sandbox()
ok = True


def chk(label, cond, extra=""):
    global ok
    print(("✓" if cond else "✗"), label, extra)
    if not cond:
        ok = False


# ── 形状1(实测真实):信封列表,value JSON 字符串,心率字段 avg_hrm ─────────
now = int(time.time())
t1_start = now - 7200
t1_end = t1_start + 1200
shape1 = [{
    "sid": "2285146073",
    "key": "outdoor_riding",
    "category": "outdoor_riding",
    "time": t1_start,
    "zone_offset": 28800,
    "update_time": t1_end,
    "value": json.dumps({
        "sport_type": 6, "start_time": t1_start, "end_time": t1_end,
        "duration": 1200, "distance": 5367, "calories": 144, "total_cal": 144,
        "avg_hrm": 123, "max_hrm": 143, "min_hrm": 82, "avg_speed": 16.14,
        "train_load": 15,
    }),
}]
recs = c._extract_workouts(shape1)
chk("真实形状抽出1条", len(recs) == 1, len(recs))
chk("信封 key 映射=户外骑行", recs[0]["type"] == "户外骑行", recs[0].get("type"))
chk("时长=20min", recs[0]["duration_min"] == 20.0, recs[0]["duration_min"])
chk("距离=5367m", recs[0]["distance_m"] == 5367, recs[0]["distance_m"])
chk("avg_hrm 别名→avg_hr=123", recs[0]["avg_hr"] == 123, recs[0]["avg_hr"])
chk("max_hrm 别名→max_hr=143", recs[0]["max_hr"] == 143, recs[0]["max_hr"])
chk("avg_speed=16.1 km/h", recs[0]["avg_speed"] == 16.1, recs[0]["avg_speed"])
chk("type_code=6", recs[0]["type_code"] == 6, recs[0]["type_code"])
for r in recs:
    c._upsert_workout(r)

# ── 形状2(实测真实):户外步行,key= outdoor_walking / category=walking ─────
t2_start = now - 86400
shape2 = [{
    "sid": "2285146073",
    "key": "outdoor_walking",
    "category": "walking",
    "time": t2_start,
    "zone_offset": 28800,
    "value": json.dumps({
        "sport_type": 2, "start_time": t2_start, "end_time": t2_start + 2700,
        "duration": 2700, "distance": 3300, "calories": 136,
        "avg_hrm": 96, "max_hrm": 118, "avg_speed": 4.4,
    }),
}]
recs2 = c._extract_workouts(shape2)
chk("步行抽出1条", len(recs2) == 1, len(recs2))
chk("key 优先于 category=户外健走", recs2[0]["type"] == "户外健走", recs2[0]["type"])
chk("步行时长=45min", recs2[0]["duration_min"] == 45.0, recs2[0]["duration_min"])
for r in recs2:
    c._upsert_workout(r)

# ── 形状3:未收录的 key 原样保留,字段照常解析 ─────────────────────────────
shape3 = [{
    "key": "underwater_basket_weaving",
    "time": now - 86400 * 3,
    "value": json.dumps({"start_time": now - 86400 * 3, "duration": 1800,
                         "distance": 0, "calories": 200}),
}]
recs3 = c._extract_workouts(shape3)
chk("未收录 key 原样保留", recs3 and recs3[0]["type"] == "underwater_basket_weaving",
    recs3[0]["type"] if recs3 else "none")
chk("无距离有心率无关字段仍入库(卡路里在场)", recs3[0]["calories"] == 200)
for r in recs3:
    c._upsert_workout(r)

# ── 形状4:不可识别条目 → None + 只记一次日志,不炸 ────────────────────────
shape4 = [{"key": "outdoor_riding", "value": '{"foo": "bar"}'},
          {"key": "outdoor_riding", "value": '{"foo": "baz"}'}]
recs4 = c._extract_workouts(shape4)
chk("不可识别→0条", recs4 == [], recs4)
chk("有日志且未超上限", 0 < len(c._exercise_shape_logged) <= 5, len(c._exercise_shape_logged))

# ── 合并:同 start 重发,字段更多者胜、互相补齐 ────────────────────────────
refetch = dict(recs[0])
refetch.pop("avg_hr")
refetch["calories"] = 150          # 更新后的卡路里
changed = c._upsert_workout(refetch)
merged = c._workouts[t1_start]
chk("重发更新被吸收", merged["calories"] == 150, merged["calories"])
chk("旧记录字段被保留(avg_hr)", merged["avg_hr"] == 123, merged["avg_hr"])

# ── 视图重建 ──────────────────────────────────────────────────────────────
c._rebuild_workout_views()
sd = c._sensor_data
chk("最新运动=最近开始的那条", sd["workout_latest_start"] == t1_start, sd["workout_latest_start"])
chk("最新运动类型=户外骑行", sd["workout_latest_type"] == "户外骑行", sd["workout_latest_type"])
chk("最新运动均速入视图", sd["workout_latest_avg_speed"] == 16.1, sd["workout_latest_avg_speed"])
chk("今日运动次数=1", sd["workout_count_today"] == 1, sd["workout_count_today"])
chk("30天历史=3条", len(sd["workout_history_30d"]) == 3, len(sd["workout_history_30d"]))
chk("历史按新→旧排序", sd["workout_history_30d"][0]["start"] >= sd["workout_history_30d"][1]["start"])
chk("历史条目带 avg_hr/avg_speed", "avg_hr" in sd["workout_history_30d"][0]
    and "avg_speed" in sd["workout_history_30d"][0])

# 30 天外的被修剪
c._workouts[now - 31 * 86400] = {"start": now - 31 * 86400, "end": 0, "duration_min": 0,
                                 "distance_m": 0, "calories": 0, "avg_hr": 0, "max_hr": 0,
                                 "avg_speed": 0, "type_code": 1, "type": "旧运动"}
c._rebuild_workout_views()
chk("30天外修剪", len(c._workouts) == 3, len(c._workouts))

# 空库 → 视图清空不炸
c._workouts = {}
c._rebuild_workout_views()
chk("空库清空 latest 视图", "workout_latest_type" not in c._sensor_data)

print("\n" + ("ALL PASS" if ok else "FAILED"))
raise SystemExit(0 if ok else 1)

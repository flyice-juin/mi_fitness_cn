# -*- coding: utf-8 -*-
"""运动记录探针 round 5(在 HA 容器内运行)。

思路:agg by_time 不带 key 会返回该 tag 的全部 key(daily_report 6 个 key 就是
这么发现的)。换一组运动向 tag 逐个不带 key 查,看有没有藏运动记录的 tag;
顺带试一次 watch.iot.mi.com 设备域接口。
"""
import importlib.util
import json
import time

CONFIG = "/config"
API_PATH = f"{CONFIG}/custom_components/mi_fitness/api.py"

spec = importlib.util.spec_from_file_location("mi_api", API_PATH)
mi_api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mi_api)

storage = json.load(open(f"{CONFIG}/.storage/core.config_entries", encoding="utf-8"))
entry = next(e for e in storage["data"]["entries"] if e["domain"] == "mi_fitness")
d = entry["data"]
client = mi_api.MiFitnessClient(
    user_id=d["user_id"], c_user_id=d["c_user_id"], ssecurity=d["ssecurity"],
    service_token=d["service_token"], region=d.get("region", "cn"),
    phone_id=d.get("phone_id", ""),
)

now_s = int(time.time())
d30 = now_s - 30 * 86400

TAGS = (
    "sport_report", "workout", "exercise", "training", "sport",
    "exercise_report", "training_report", "workout_report", "sport_brief",
    "daily_sport", "exercise_brief",
)

for tag in TAGS:
    try:
        r = client._get("/app/v1/data/get_aggregated_fitness_data_by_time", {
            "relative_uid": 0, "tag": tag,
            "start_time": d30, "end_time": now_s, "limit": 20,
        })
        s = json.dumps(r, ensure_ascii=False)
        mark = "  <<< 有数据!" if '"data_list": [{' in s or '"data_list":[{"' in s else ""
        print(f"tag={tag:<16} {len(s):>5} chars{mark}")
        if mark or (r.get("code") not in (0, -8)):
            print("   ", s[:800])
    except Exception as exc:
        print(f"tag={tag:<16} FAILED {str(exc)[:120]}")
    time.sleep(0.3)

# watch.iot.mi.com 设备域:同一套 cookie 直接 GET(不带 RC4 签名)
import requests
host = "https://watch.iot.mi.com"
for path in ("/cgi-op/api/v1/miwear/health/weeklyList",):
    try:
        resp = requests.get(
            host + path,
            headers={"User-Agent": "Android-12-3.53.1-vivo-V2284A"},
            cookies={
                "userId": d["user_id"], "cUserId": d["c_user_id"],
                "serviceToken": d["service_token"], "locale": "zh_CN",
            },
            timeout=15,
        )
        print(f"\nwatch.iot {path} → HTTP {resp.status_code}: {resp.text[:300]}")
    except Exception as exc:
        print(f"\nwatch.iot {path} FAILED: {str(exc)[:150]}")

print("\nPROBE DONE")

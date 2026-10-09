"""DataUpdateCoordinator for Mi Fitness."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import MiFitnessApiError, MiFitnessAuthError, MiFitnessClient
from .auth import try_silent_token_refresh
from .const import (
    CONF_PASS_TOKEN,
    CONF_SERVICE_TOKEN,
    CONF_SSECURITY,
    CONF_USER_ID,
    DEFAULT_START_WATERMARK,
    DOMAIN,
    KEY_CALORIES,
    KEY_HEART_RATE,
    KEY_INTENSITY,
    KEY_RESTING_HEART_RATE,
    KEY_SLEEP,
    KEY_SPO2,
    KEY_STEPS,
    KEY_TRAINING_LOAD,
    KEY_VITALITY,
    KEY_VALID_STAND,
    KEY_WEIGHT,
    SCAN_INTERVAL_MINUTES,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=SCAN_INTERVAL_MINUTES)

# Sleep stage state codes from the API
_SLEEP_STATE_NAMES = {2: "light", 3: "deep", 4: "rem", 5: "awake"}

# 运动类型码 → 名称(回退用)。信封 key 字符串优先(上方 _SPORT_TYPE_KEYS),
# 码位仅在信封缺失时兜底;已实测确认 6=户外骑行,其余仍为推测。
_SPORT_TYPES = {
    1:  "户外跑步",
    2:  "步行",
    3:  "骑行",
    4:  "室内跑步",
    5:  "户外步行",
    6:  "户外骑行",
    7:  "力量训练",
    8:  "徒步",
    9:  "瑜伽",
    10: "椭圆机",
    11: "划船机",
    12: "跳绳",
    13: "健身操",
    14: "舞蹈",
    15: "篮球",
    16: "足球",
    17: "羽毛球",
    18: "乒乓球",
    19: "网球",
    20: "登山",
}

# 运动记录信封的 key/category 字符串 → 中文名(get_sport_records_by_time 实测
# 返回英文名,如 outdoor_riding/walking);未收录的 key 原样保留,便于按真实
# 数据补表。
_SPORT_TYPE_KEYS = {
    "running":            "户外跑步",
    "outdoor_running":    "户外跑步",
    "indoor_running":     "室内跑步",
    "treadmill":          "跑步机",
    "walking":            "健走",
    "outdoor_walking":    "户外健走",
    "indoor_walking":     "室内健走",
    "hiking":             "徒步",
    "mountain_climbing":  "登山",
    "climbing":           "登山",
    "riding":             "户外骑行",
    "outdoor_riding":     "户外骑行",
    "indoor_riding":      "室内骑行",
    "outdoor_cycling":    "户外骑行",
    "indoor_cycling":     "室内骑行",
    "spinning":           "动感单车",
    "swimming":           "游泳",
    "open_water_swimming": "公开水域游泳",
    "jump_rope":          "跳绳",
    "strength":           "力量训练",
    "free_training":      "自由训练",
    "stretching":         "拉伸",
    "yoga":               "瑜伽",
    "pilates":            "普拉提",
    "gymnastics":         "健身操",
    "dance":              "舞蹈",
    "boxing":             "拳击",
    "basketball":         "篮球",
    "football":           "足球",
    "soccer":             "足球",
    "badminton":          "羽毛球",
    "table_tennis":       "乒乓球",
    "tennis":             "网球",
    "golf":               "高尔夫",
    "skiing":             "滑雪",
    "ice_skating":        "滑冰",
    "rowing":             "划船机",
    "elliptical":         "椭圆机",
    "stepper":            "踏步机",
    "indoor_fitness":     "室内健身",
    "cross_training":     "交叉训练",
}


def _day_start_ts() -> int:
    """Unix timestamp of 00:00 local time today."""
    now = datetime.now()
    return int(datetime(now.year, now.month, now.day).timestamp())


def _local_day_num() -> int:
    """Calendar day number in LOCAL time (so daily sensors reset at local
    midnight, not UTC midnight)."""
    return datetime.now().toordinal()


def _parse_value(item: dict) -> Any:
    """Parse 'value' field from a data_list item (JSON string or raw)."""
    raw = item.get("value", "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _sleep_quality_score(
    duration: int,
    deep: int,
    rem: int,
    awake_count: int,
) -> int:
    """
    Calculate sleep quality score 0-100.

    Weights:
      30 % — duration proximity to 8h optimum
      35 % — deep sleep ratio (target ≥20 %)
      20 % — REM ratio (target ≥20 %)
      15 % — continuity (awake interruptions penalty)
    """
    if not duration or duration < 60:
        return 0

    # Duration score: best at 480 min (8h), -1 pt per 5 min away
    dur_score = max(0, 100 - abs(duration - 480) // 5)

    # Deep score: 20 % = 100 pts, linear
    deep_ratio = deep / duration if duration else 0
    deep_score = min(100, int(deep_ratio * 500))

    # REM score
    rem_ratio = rem / duration if duration else 0
    rem_score = min(100, int(rem_ratio * 500))

    # Continuity: lose 8 pts per awakening, min 40 pts
    continuity = max(40, 100 - (awake_count or 0) * 8)

    total = (
        dur_score  * 0.30
        + deep_score * 0.35
        + rem_score  * 0.20
        + continuity * 0.15
    )
    return round(total)


def _sleep_chronotype_6(history: list[dict]) -> str:
    """
    Classify into one of Xiaomi's 6 sleep animals using multi-night history.

    Based on actual Mi Fitness app descriptions:
      sheep      — early bed, sleeps VERY LITTLE (short duration, gets tired)
      penguin    — early bed, sleeps ok but EASILY AWAKENED (high awake count)
      brown_bear — healthy/normal schedule (22-23h), plenty of restful sleep
      koala      — stays up late, sleeps SOUNDLY once asleep (low awake, good deep)
      night_owl  — stays up late, sleeps RESTLESSLY (high awake count)
      shark      — very late and/or sleeps LITTLE (severely deprived)

    Uses averages over available history (ideally ≥7 nights).
    """
    records = [r for r in history if r.get("bedtime") and r.get("duration", 0) >= 120]
    if not records:
        return "brown_bear"

    bed_hours = []
    for r in records:
        bed_dt = datetime.fromtimestamp(r["bedtime"])
        h = bed_dt.hour + bed_dt.minute / 60
        if h < 7:            # normalise midnight crossings: 00:30 → 24.5
            h += 24
        bed_hours.append(h)

    avg_bed      = sum(bed_hours) / len(bed_hours)
    avg_awake    = sum(r.get("awake_count", 0) for r in records) / len(records)
    avg_duration = sum(r.get("duration", 0) for r in records) / len(records)

    peaceful   = avg_awake < 1.5    # ≤1 awakening/night on average
    enough     = avg_duration >= 390  # ≥6.5h
    very_short = avg_duration < 300   # <5h — "sleeps very little"

    # Very late (after 01:30) or critically short sleep → Shark
    if avg_bed >= 25.5 or very_short:
        return "shark"

    # Late (23:00 – 01:30)
    if avg_bed >= 23.0:
        if not peaceful:
            return "night_owl"  # late + restless
        return "koala"          # late but sleeps soundly

    # Normal schedule (22:00 – 23:00)
    if avg_bed >= 22.0:
        if peaceful:
            return "brown_bear"
        return "night_owl"      # normal time but fragmented sleep

    # Early (<22:00)
    if not peaceful:
        return "penguin"        # early but easily awakened
    return "sheep"              # early + short/little sleep


def _stages_to_list(items: list[dict]) -> list[dict]:
    """
    Convert raw API sleep items into clean stage dicts.
    Each item: {start, end, state, label}
    """
    out = []
    for seg in items:
        state = seg.get("state", 0)
        out.append({
            "start": seg.get("start_time", 0),
            "end":   seg.get("end_time",   0),
            "state": state,
            "label": _SLEEP_STATE_NAMES.get(state, "unknown"),
        })
    return out


class MiFitnessCoordinator(DataUpdateCoordinator):
    """Polls Mi Fitness API and aggregates sensor data."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: MiFitnessClient,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self._client = client
        self._entry = entry
        self._entry_id = entry.entry_id
        self._store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}_{entry.entry_id}")
        self._watermark: int = 0
        self._daily_report_wm: int = 0   # watermark of aggregated daily_report feed
        self._day_wm: int = 0       # watermark saved at start of each new day
        self._day_num: int = 0      # calendar day when _day_wm was recorded
        self._sensor_data: dict[str, Any] = {}
        self._sleep_history: dict[str, dict] = {}
        # 睡眠分段 timeline 按 bedtime 键存(bedtime → [{start,end,state,label}])。
        # 原始流里带分段的 sleep 项窗口很短(只有最近一两晚),错过水位就再也
        # 拿不到,所以拿到就落库存起来;日汇总合成的记录没有分段,从这里回填。
        self._sleep_stages: dict[int, list] = {}
        # 全天多段模型的段库(bedtime → 段记录):夜间主觉+白天小睡各自一条
        self._sleep_records: dict[int, dict] = {}
        self._steps_history: dict[str, dict] = {}
        # Per-day step records keyed by timestamp: {date_str: {ts: {...}}}.
        # The server re-sends the same interval with a new watermark as data
        # accumulates, so we DEDUPE by ts (replace) instead of summing.
        self._step_buckets: dict[str, dict] = {}
        # 运动记录库(按运动开始时间戳键):get_sport_records_by_time
        # 拉取,同键取更完整的记录(字段非空数多者胜)
        self._workouts: dict[int, dict] = {}
        # 运动记录首次 30 天回填是否已完成(避免每次轮询都拉长窗口)
        self._exercise_backfill_done: bool = False
        # 未知响应形状的只记录一次的哨兵(避免日志刷屏)
        self._exercise_shape_logged: set[str] = set()
        self._unknown_feed_keys_logged: set[str] = set()
        self._initial_load_done: bool = False
        self._fetch_errors: int = 0   # consecutive failed polls → backoff

    async def async_load_stored_state(self):
        """Load persisted state. Called once before the first refresh.

        Deliberately NOT named ``_async_setup`` — that is a DataUpdateCoordinator
        hook (HA 2024.8+) auto-invoked by ``async_config_entry_first_refresh``,
        so reusing the name would run this twice.
        """
        stored = await self._store.async_load()
        if stored:
            self._watermark     = stored.get("watermark", 0)
            self._daily_report_wm = stored.get("daily_report_wm", 0)
            self._day_wm        = stored.get("day_wm", 0)
            self._day_num       = stored.get("day_num", 0)
            self._sleep_history = stored.get("sleep_history", {})
            self._sleep_stages = {
                int(k): v for k, v in (stored.get("sleep_stages") or {}).items()
            }
            self._sleep_records = {
                int(k): v for k, v in (stored.get("sleep_records") or {}).items()
            }
            self._steps_history = stored.get("steps_history", {})
            self._step_buckets  = stored.get("step_buckets", {})
            self._sensor_data   = stored.get("sensor_data", {})
            self._workouts = {
                int(k): v for k, v in (stored.get("workouts") or {}).items()
            }
            self._exercise_backfill_done = stored.get("exercise_backfill_done", False)
            # 重启后从 0 全量重走当前数据窗口(通常只有几页):原始流是短窗口
            # 馈送,带分段 timeline 的 sleep 项错过水位就永久丢失;全量重走
            # 让分段等短窗口数据自愈。幂等性:步数按 ts 去重替换、心率按 ts
            # 单调、睡眠按 bedtime 判重 —— 重放不会重复计数。
            if self._watermark:
                self._watermark = 0
                # Clear today's step totals — they'll be rebuilt from re-fetched records.
                # (Avoids double-counting since we accumulate into _steps_history.)
                today_str = datetime.now().strftime("%Y-%m-%d")
                self._steps_history.pop(today_str, None)
                for _k in ("steps_today", "distance_today", "step_calories_today", "steps_hourly"):
                    self._sensor_data.pop(_k, None)
            elif self._watermark == 0:
                # First-ever install: skip ancient history, start ~30 days ago
                self._watermark = DEFAULT_START_WATERMARK

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch new data from API and merge into sensor state."""

        # Reset daily metrics when calendar day changes.
        # Without this, yesterday's vitality/steps/hr values show as today's
        # until the watch syncs for the first time each day.
        today_day = _local_day_num()
        if self._initial_load_done and today_day != self._day_num and self._day_num != 0:
            _LOGGER.debug("Mi Fitness: new day — clearing daily sensor accumulators")
            for _k in (
                "steps_today", "distance_today", "step_calories_today", "steps_hourly",
                "calories_today",
                "heart_rate_avg_today", "heart_rate_max_today", "heart_rate_min_today",
                "stand_hours_today", "stand_hours_list",
                "workout_count_today", "workout_today_list",
                "training_load_today", "training_load_level", "training_load_wtl_sum",
                "training_load_ts",
            ):
                self._sensor_data.pop(_k, None)

        # 活动时长是「今日」累计但没有进午夜重置名单的日期戳机制:数据日期
        # 不是今天就清掉——跨天后 HA 重启过的场景(存储里的 day_num 已是新
        # 一天,上面的午夜重置块不会再触发),昨天的残值会一直挂到出现一次
        # 超过它的运动才被顶掉(实测:刚起床什么都没干却显示昨天 43 分钟)
        if "activity_duration_today" in self._sensor_data:
            today_str = datetime.now().strftime("%Y-%m-%d")
            if self._sensor_data.get("activity_duration_date") != today_str:
                self._sensor_data.pop("activity_duration_today", None)

        # First refresh: fetch only 1 page to verify connectivity fast
        # (avoids CancelledError timeout during async_config_entry_first_refresh)
        # Subsequent polls: 30 pages to catch up through historical data quickly
        first_refresh = not self._initial_load_done
        max_pages = 1 if first_refresh else 30
        self._initial_load_done = True

        try:
            items, new_wm, errored = await self.hass.async_add_executor_job(
                self._client.fetch_all_since,
                self._watermark,
                max_pages,
            )
        except MiFitnessAuthError as exc:
            # ── Try silent serviceToken refresh using stored passToken ──────────
            # passToken-based auth skips 2FA (token was issued after completed 2FA).
            # If it works, we update credentials and retry on the next poll —
            # no user interaction required.
            pass_token = self._entry.data.get(CONF_PASS_TOKEN, "")
            user_id    = self._entry.data.get(CONF_USER_ID, "")
            if pass_token and user_id:
                _LOGGER.warning(
                    "serviceToken expired — attempting silent refresh with passToken"
                )
                creds = await self.hass.async_add_executor_job(
                    try_silent_token_refresh, pass_token, user_id
                )
                if creds.get("service_token"):
                    new_data = {**self._entry.data, CONF_SERVICE_TOKEN: creds["service_token"]}
                    if creds.get("ssecurity"):
                        new_data[CONF_SSECURITY] = creds["ssecurity"]
                        self._client.ssecurity = creds["ssecurity"]
                    # Do NOT update passToken — server rotates it on each exchange,
                    # and the new passToken gives different (wrong) ssecurity.
                    self._client.update_service_token(creds["service_token"])
                    self.hass.config_entries.async_update_entry(self._entry, data=new_data)
                    _LOGGER.warning("Silent refresh succeeded — resuming on next poll")
                    raise UpdateFailed(
                        "Credentials refreshed silently — data updates on next poll"
                    ) from exc
                _LOGGER.warning(
                    "Silent token refresh failed — passToken may have expired; "
                    "triggering user re-auth"
                )
            # Silent refresh unavailable or failed — ask the user
            self._entry.async_start_reauth(self.hass)
            raise UpdateFailed(f"Auth error (re-auth triggered): {exc}") from exc
        except MiFitnessApiError as exc:
            raise UpdateFailed(str(exc)) from exc

        # ── CN 账号:心率/睡眠的当天汇总在 daily_report 聚合接口里 ────────────
        # 原始水位流只有 steps/intensity/calories 的条目;heart_rate 日汇总、
        # sleep(评分/深浅/REM/分段就寝起床/平均血氧)只在聚合流里。
        # 注意:聚合流的水位参数是过滤语义,传上次的水位会把条目全滤空,
        # 因此每次都从 0 拉全量(每天每指标一条,数据量极小)。
        dr_items: list[dict] = []
        try:
            dr_items = await self.hass.async_add_executor_job(
                self._client.fetch_daily_reports_since, 0
            )
        except MiFitnessApiError as exc:
            _LOGGER.warning("Mi Fitness: daily_report fetch failed: %s", exc)

        # Dynamic interval: catching up → 30 s, up to date → 15 min,
        # repeated failures → exponential backoff (don't hammer a rate-limit).
        if items:
            self._merge_items(items)
            self._fetch_errors = 0
            latest_ts = max(
                (item.get("time") or item.get("update_time") or 0) for item in items
            )
            # 首轮只拉了 1 页,无论数据多旧都算「未追平」→ 30s 快速追赶。
            # (原条件 latest_ts < now-2d 对短窗口账号永不成立:实测 CN 账号
            # 原始流窗口只有 2~3 天,重启后全量回放要按 15 分钟/页爬一小时)
            catching_up = first_refresh or latest_ts < (time.time() - 2 * 86400)
            self.update_interval = timedelta(seconds=30) if catching_up else SCAN_INTERVAL
            if catching_up:
                _LOGGER.debug(
                    "Mi Fitness: catching up — latest_item=%s",
                    datetime.fromtimestamp(latest_ts).strftime("%Y-%m-%d"),
                )
        elif errored:
            # No data AND a page failed → likely rate-limit / outage. Back off
            # exponentially (60s, 120s, … capped at 10 min) but keep retrying.
            # Crucially, do NOT mistake this for "caught up" and never stall.
            self._fetch_errors += 1
            backoff = min(30 * (2 ** self._fetch_errors), 600)
            self.update_interval = timedelta(seconds=backoff)
            _LOGGER.warning(
                "Mi Fitness: fetch failed x%d — backing off to %ds",
                self._fetch_errors, backoff,
            )
        else:
            # Genuinely caught up (no items, no error).
            self._fetch_errors = 0
            if self.update_interval != SCAN_INTERVAL:
                self.update_interval = SCAN_INTERVAL
                _LOGGER.debug(
                    "Mi Fitness: caught up, switching to %s min interval",
                    SCAN_INTERVAL_MINUTES,
                )

        if dr_items:
            self._merge_daily_reports(dr_items)

        # ── 运动记录(get_sport_records_by_time,独立接口,失败不阻塞主流程)──
        workouts_dirty = await self._poll_exercises()

        # 持久化放在合并之后:重启恢复时才有完整的传感器数据
        if items or dr_items or workouts_dirty or (new_wm and new_wm != self._watermark):
            if new_wm and new_wm != self._watermark:
                self._watermark = new_wm
                today = _local_day_num()
                if today != self._day_num:
                    # New day — snapshot watermark so restart can roll back to here
                    self._day_wm  = new_wm
                    self._day_num = today
            await self._store.async_save({
                "watermark":       new_wm or self._watermark,
                "daily_report_wm": self._daily_report_wm,
                "day_wm":          self._day_wm,
                "day_num":         self._day_num,
                "sleep_history":   self._sleep_history,
                "sleep_stages":    self._sleep_stages,
                "sleep_records":   self._sleep_records,
                "steps_history":   self._steps_history,
                "step_buckets":    self._step_buckets,
                "sensor_data":     self._sensor_data,
                "workouts":        self._workouts,
                "exercise_backfill_done": self._exercise_backfill_done,
            })
        return dict(self._sensor_data)

    def _rebuild_steps_history_14d(self) -> None:
        """Refresh the 14-day steps history list from merged day records."""
        self._sensor_data["steps_history_14d"] = sorted(
            self._steps_history.values(), key=lambda x: x["date"], reverse=True
        )[:14]

    # ── 运动记录(get_sport_records_by_time)─────────────────────────────────

    async def _poll_exercises(self) -> bool:
        """拉取运动记录并入库,返回是否有变化。

        数据源 POST /app/v1/data/get_sport_records_by_time(已实测 CN 账号
        返回真实记录),独立接口,失败不阻塞主流程。字段解析全部容错:抓到
        无法识别的形状时 WARNING 记原始 JSON(进程内最多 5 条,防刷屏)。
        """
        now = int(time.time())
        if self._exercise_backfill_done:
            # 稳态:只看最近 3 天窗口,足以接住新运动,数据量小
            start = now - 3 * 86400
        else:
            # 首次:回填 30 天历史
            start = now - 30 * 86400
        try:
            items = await self.hass.async_add_executor_job(
                self._client.get_sport_records, start, now
            )
        except MiFitnessApiError as exc:
            _LOGGER.debug("Mi Fitness: sport_records pull failed (non-fatal): %s", exc)
            return False

        records = self._extract_workouts(items)
        if records and not self._workouts:
            _LOGGER.info(
                "Mi Fitness: 运动记录开始入库(%d 条,最近一条 %s)",
                len(records),
                records[0].get("type"),
            )
        dirty = False
        for rec in records:
            if self._upsert_workout(rec):
                dirty = True
        # 视图每次都重建:跨午夜后今日计数立即归零,不依赖新运动到来
        self._rebuild_workout_views()
        # 空响应也算回填完成(账号无运动记录),
        # 避免每 15 分钟都发 30 天窗口的请求
        self._exercise_backfill_done = True
        return dirty

    def _extract_workouts(self, payload: Any) -> list[dict]:
        """从运动记录响应中容错抽取记录。

        真实形状(get_sport_records_by_time,已实测):
          [信封, ...]  信封 = {key:"outdoor_riding", category, time(秒),
                            zone_offset, value:"<JSON 字符串>"}
        兼容历史猜测形状({result|data: {data_list: [...]}} 等)。
        每条记录归一化为:
          {start, end, duration_min, distance_m, calories, avg_hr, max_hr,
           avg_speed, type_code, type, name}
        """
        if payload is None:
            return []
        items: Any = None
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            found_container = False
            for k in ("data_list", "list", "records", "data", "result"):
                v = payload.get(k)
                if isinstance(v, list):
                    items = v
                    found_container = True
                    break
            if items is None and isinstance(payload.get("data"), dict):
                return self._extract_workouts(payload["data"])
            if items is None and isinstance(payload.get("result"), dict):
                return self._extract_workouts(payload["result"])
            if found_container and not items:
                # 合法容器(可能只是空列表,如账号近期无运动记录),不算未识别
                return []
        if items is None:
            self._log_exercise_shape_once("container", payload)
            return []

        out: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            val = item.get("value", item)
            if isinstance(val, str):
                val = _parse_value(item)
            if not isinstance(val, dict):
                val = item
            rec = self._workout_from_value(val, item)
            if rec:
                out.append(rec)
        return out

    def _workout_from_value(self, val: dict, item: dict) -> dict | None:
        """单条运动 value → 归一化记录;识别不出返回 None。"""
        def pick(d: dict, keys: tuple[str, ...]) -> Any:
            for k in keys:
                v = d.get(k)
                if v not in (None, 0, "", []):
                    return v
            return None

        start = pick(val, ("start_time", "startTime", "begin_time", "start"))
        if start is None:
            start = item.get("time")
        try:
            start = int(start)
        except (TypeError, ValueError):
            start = None
        end = pick(val, ("end_time", "endTime", "stop_time"))
        try:
            end = int(end) if end is not None else None
        except (TypeError, ValueError):
            end = None
        # 毫秒 → 秒
        if start and start > 10**12:
            start //= 1000
        if end and end > 10**12:
            end //= 1000

        distance = pick(val, ("distance", "distance_meter", "total_distance"))
        calories = pick(val, ("calories", "calorie", "total_calories"))
        duration = pick(val, ("duration", "duration_time", "total_duration"))
        type_code = pick(val, ("type", "sport_type", "exercise_type", "activity_type"))
        name = pick(val, ("name", "sport_name", "title"))
        avg_hr = pick(val, ("avg_hr", "average_hr", "avg_heart_rate", "avg_hrm"))
        max_hr = pick(val, ("max_hr", "max_heart_rate", "max_hrm"))
        avg_speed = pick(val, ("avg_speed", "average_speed"))

        # 必须至少有起点时间 + 一个可识别的运动量字段,否则视为非运动条目
        if not start or all(
            x in (None, 0) for x in (distance, calories, duration, end, type_code)
        ):
            self._log_exercise_shape_once("item", val)
            return None

        # 时长归一化为分钟:end-start 优先,其次原始 duration(≥300 视为秒)
        duration_min = 0
        if end and end > start:
            duration_min = round((end - start) / 60, 1)
        elif duration:
            duration_min = round(duration / 60, 1) if duration >= 300 else round(float(duration), 1)
        span_sec = (end - start) if (end and end > start) else 0
        # 防噪声:EXERCISE 若按逐分钟下发(类似 intensity 桶),没有距离/卡路里
        # 且时长<10 分钟的条目不是一次真实运动
        if not (distance or calories or duration_min >= 10 or span_sec >= 600):
            self._log_exercise_shape_once("item", val)
            return None
        try:
            distance = round(float(distance), 1) if distance is not None else 0.0
        except (TypeError, ValueError):
            distance = 0.0
        try:
            calories = round(float(calories), 1) if calories is not None else 0.0
        except (TypeError, ValueError):
            calories = 0.0

        tname = None
        try:
            type_code = int(type_code) if type_code is not None else None
        except (TypeError, ValueError):
            type_code = None
        # 类型名优先级:信封 key/category(实测权威,如 outdoor_riding)
        # → value 内 name 字段 → 码表 → 原样保留未知 key
        env_key = item.get("key") or item.get("category")
        if isinstance(env_key, str) and env_key:
            tname = _SPORT_TYPE_KEYS.get(env_key, env_key)
        elif isinstance(name, str) and name:
            tname = name
        elif type_code is not None:
            tname = _SPORT_TYPES.get(type_code, f"运动 {type_code}")

        try:
            avg_speed = round(float(avg_speed), 1) if avg_speed is not None else 0.0
        except (TypeError, ValueError):
            avg_speed = 0.0

        return {
            "start": start,
            "end": end or 0,
            "duration_min": duration_min,
            "distance_m": distance,
            "calories": calories,
            "avg_hr": avg_hr or 0,
            "max_hr": max_hr or 0,
            "avg_speed": avg_speed,
            "type_code": type_code,
            "type": tname or "运动",
        }

    def _upsert_workout(self, rec: dict) -> bool:
        """按 start 时间戳入库/合并运动记录,返回是否有变化。

        服务器会分批重发同一次运动(越发越全):单调递增字段(距离/卡路里/
        时长/结束时间/最高心率)取大,其余元数据以新值为准。
        """
        key = rec["start"]
        prev = self._workouts.get(key)
        if prev is None:
            self._workouts[key] = rec
            return True
        merged = dict(prev)
        changed = False
        for k, v in rec.items():
            if v in (None, 0, 0.0, ""):
                continue
            if k in ("distance_m", "calories", "duration_min", "max_hr", "end"):
                if v > (merged.get(k) or 0):
                    merged[k] = v
                    changed = True
            elif merged.get(k) != v:
                merged[k] = v
                changed = True
        merged["start"] = key
        self._workouts[key] = merged
        return changed

    def _rebuild_workout_views(self) -> None:
        """运动库 → 传感器视图:最新运动 + 今日统计 + 30 天历史列表。"""
        # 修剪 30 天外
        cutoff = int(time.time()) - 30 * 86400
        self._workouts = {k: v for k, v in self._workouts.items() if k >= cutoff}

        workouts = sorted(self._workouts.values(), key=lambda x: x["start"], reverse=True)
        history = []
        for w in workouts:
            history.append({
                "date":     datetime.fromtimestamp(w["start"]).strftime("%Y-%m-%d %H:%M"),
                "type":     w.get("type"),
                "type_code": w.get("type_code"),
                "start":    datetime.fromtimestamp(w["start"]).isoformat(),
                "duration_min": w.get("duration_min", 0),
                "distance_km": round((w.get("distance_m") or 0) / 1000, 2),
                "calories": w.get("calories", 0),
                "avg_hr":   w.get("avg_hr", 0),
                "avg_speed": w.get("avg_speed", 0),
            })
        self._sensor_data["workout_history_30d"] = history[:60]

        today = _day_start_ts()
        today_list = [
            {
                "type": w.get("type"),
                "start": datetime.fromtimestamp(w["start"]).strftime("%H:%M"),
                "duration_min": w.get("duration_min", 0),
                "distance_km": round((w.get("distance_m") or 0) / 1000, 2),
                "calories": w.get("calories", 0),
            }
            for w in workouts if w["start"] >= today
        ]
        self._sensor_data["workout_count_today"] = len(today_list)
        self._sensor_data["workout_today_list"] = today_list

        if workouts:
            w = workouts[0]
            self._sensor_data.update({
                "workout_latest_type":     w.get("type") or "运动",
                "workout_latest_type_code": w.get("type_code"),
                "workout_latest_start":    w["start"],
                "workout_latest_end":      w.get("end") or 0,
                "workout_latest_duration": w.get("duration_min", 0),
                "workout_latest_distance": w.get("distance_m", 0),
                "workout_latest_calories": w.get("calories", 0),
                "workout_latest_avg_hr":   w.get("avg_hr", 0),
                "workout_latest_max_hr":   w.get("max_hr", 0),
                "workout_latest_avg_speed": w.get("avg_speed", 0),
            })
        else:
            for k in (
                "workout_latest_type", "workout_latest_type_code", "workout_latest_start",
                "workout_latest_end", "workout_latest_duration", "workout_latest_distance",
                "workout_latest_calories", "workout_latest_avg_hr", "workout_latest_max_hr",
                "workout_latest_avg_speed",
            ):
                self._sensor_data.pop(k, None)

    def _log_exercise_shape_once(self, tag: str, obj: Any) -> None:
        """记录未识别的 EXERCISE 响应形状(进程内最多 5 条,防刷屏)。"""
        if len(self._exercise_shape_logged) >= 5:
            return
        try:
            raw = json.dumps(obj, ensure_ascii=False, default=str)[:600]
        except Exception:
            raw = str(obj)[:600]
        self._exercise_shape_logged.add(raw)
        _LOGGER.warning(
            "Mi Fitness: 运动记录未识别的%s形状(请反馈此日志以完善解析): %s",
            tag, raw,
        )

    def _merge_daily_reports(self, items: list[dict]) -> None:
        """
        Merge aggregated daily_report items (CN accounts).

        heart_rate: avg/max/min/latest + resting heart rate per day.
        sleep:      Xiaomi's own score, deep/light/rem minutes, per-segment
                    bedtime/wake_up_time, average SpO₂ — synthesised into a
                    raw-style sleep item and fed through _process_sleep so the
                    history/chronotype machinery keeps working.
        """
        now       = time.time()
        today     = _day_start_ts()
        tomorrow  = today + 86400

        # 睡眠日汇总:每条候选都是一段睡眠(夜间主觉/白天小睡各自一条),全部入库
        # 到全天多段模型,末尾 _rebuild_sleep_views 按醒来日聚合
        sleep_candidates: dict[int, dict] = {}
        val_by_bed: dict[int, list] = {}

        for item in sorted(items, key=lambda i: i.get("time") or 0):
            key = item.get("key", "")
            ts  = item.get("time") or 0
            val = _parse_value(item)
            if not isinstance(val, dict):
                continue

            # ── 心率日汇总 ────────────────────────────────────────────────
            if key == "heart_rate":
                latest = val.get("latest_hr") or {}
                bpm    = latest.get("bpm") or 0
                lts    = latest.get("time") or 0
                if bpm and lts > self._sensor_data.get("heart_rate_ts", 0):
                    self._sensor_data["heart_rate"]      = bpm
                    self._sensor_data["heart_rate_ts"]   = lts
                if today <= ts < tomorrow:
                    if val.get("avg_hr"):
                        self._sensor_data["heart_rate_avg_today"] = val["avg_hr"]
                    if val.get("max_hr"):
                        self._sensor_data["heart_rate_max_today"] = val["max_hr"]
                    if val.get("min_hr"):
                        self._sensor_data["heart_rate_min_today"] = val["min_hr"]
                rhr = val.get("avg_rhr") or 0
                if rhr and ts > self._sensor_data.get("resting_hr_ts", 0):
                    self._sensor_data["resting_heart_rate"] = rhr
                    self._sensor_data["resting_hr_ts"]      = ts

            # ── 步数/距离/卡路里日汇总(原始流分页拿不到时的权威兜底)────────
            elif key == "steps":
                date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                if today <= ts < tomorrow:
                    # 与原始流桶合计取大:两者都是当日累计口径,daily_report 聚合
                    # 有延迟,直接覆盖会把原始流已拿到的新鲜数据压回旧值
                    # (实测:走 2km 后传感器停在聚合延迟的 1215m 就是这个原因)
                    for k_src, k_dst in (
                        ("steps",    "steps_today"),
                        ("distance", "distance_today"),
                        ("calories", "step_calories_today"),
                    ):
                        v_new = val.get(k_src)
                        if v_new is not None:
                            v_old = self._sensor_data.get(k_dst) or 0
                            self._sensor_data[k_dst] = max(v_old, v_new)
                    # 今日历史条目与传感器同口径取大;往日以聚合值为准(允许校正)
                    prev_rec = self._steps_history.get(date_str) or {}
                    self._steps_history[date_str] = {
                        "date": date_str,
                        "steps":    max(prev_rec.get("steps") or 0,    val.get("steps") or 0),
                        "distance": max(prev_rec.get("distance") or 0, val.get("distance") or 0),
                        "calories": max(prev_rec.get("calories") or 0, val.get("calories") or 0),
                    }
                else:
                    self._steps_history[date_str] = {
                        "date":     date_str,
                        "steps":    val.get("steps", 0),
                        "distance": val.get("distance", 0),
                        "calories": val.get("calories", 0),
                    }
                self._rebuild_steps_history_14d()

            # ── 活动卡路里日汇总 ──────────────────────────────────────────
            elif key == "calories":
                if today <= ts < tomorrow and val.get("calories") is not None:
                    self._sensor_data["calories_today"] = val["calories"]

            # ── 活动强度日汇总(今日中高强度活动分钟数,锻炼/骑行的核心读数)──
            elif key == "intensity":
                if today <= ts < tomorrow and val.get("duration") is not None:
                    prev = self._sensor_data.get("activity_duration_today") or 0
                    self._sensor_data["activity_duration_today"] = max(prev, val["duration"])
                    # 记数据所属日期:跨天后靠它清掉昨日残值(见 _async_update_data 头部)
                    self._sensor_data["activity_duration_date"] = (
                        datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                    )

            # ── 睡眠日汇总(含平均血氧):候选按 bedtime 去重后逐条入库 ────────
            elif key == "sleep":
                segs = [
                    s for s in (val.get("segment_details") or [])
                    if s.get("bedtime") and (s.get("wake_up_time") or 0) > s["bedtime"]
                ]
                if not segs:
                    continue
                bedtime = min(s["bedtime"] for s in segs)
                wakeup  = max(s["wake_up_time"] for s in segs)
                duration = int(val.get("total_duration") or ((wakeup - bedtime) / 60))
                cand = {
                    "wake_up_time":   wakeup,
                    "duration":       duration,
                    "deep":           val.get("sleep_deep_duration", 0),
                    "light":          val.get("sleep_light_duration", 0),
                    "rem":            val.get("sleep_rem_duration", 0),
                    "avg_hr":         val.get("avg_hr", 0),
                    "max_hr":         val.get("max_hr", 0),
                    "min_hr":         val.get("min_hr", 0),
                    "awake_count":    val.get("awake_count", 0),
                    "score":          val.get("sleep_score", 0),
                    "avg_spo2":       val.get("avg_spo2", 0),
                    "ts":             ts,
                }
                prev = sleep_candidates.get(bedtime)
                if not prev or duration > prev["duration"]:
                    sleep_candidates[bedtime] = cand
                val_by_bed[bedtime] = segs

        for bedtime in sorted(sleep_candidates):
            c = sleep_candidates[bedtime]
            # 分段 timeline 补齐:真实分段只覆盖有 stage 数据的段(夜间主觉);
            # 小睡这类没有 stage 的段,用整段浅睡块补上,图表才画得出小睡
            staged = list(self._sleep_stages.get(bedtime, []))
            covered_end = max((x["end"] for x in staged), default=0)
            for seg in sorted(
                (s for s in (val_by_bed.get(bedtime) or [])),
                key=lambda x: x["bedtime"],
            ):
                if seg["wake_up_time"] > covered_end + 60:
                    staged.append({
                        "start": seg["bedtime"],
                        "end": seg["wake_up_time"],
                        "state": 2,
                        "label": "light",
                    })
                    covered_end = seg["wake_up_time"]
            self._sleep_stages[bedtime] = staged
            self._upsert_sleep_record(
                bedtime,
                c["wake_up_time"],
                c["duration"],
                c["deep"], c["light"], c["rem"],
                c["awake_count"],
                c["score"],
                staged,
                {"avg_hr": c["avg_hr"], "max_hr": c["max_hr"], "min_hr": c["min_hr"]},
            )
            if c["avg_spo2"] and c["ts"] > self._sensor_data.get("spo2_ts", 0):
                self._sensor_data["spo2"]    = c["avg_spo2"]
                self._sensor_data["spo2_ts"] = c["ts"]
        if sleep_candidates:
            self._rebuild_sleep_views()

    def _merge_items(self, items: list[dict]) -> None:
        """Merge new data_list items into aggregated sensor state."""
        now       = time.time()
        today     = _day_start_ts()
        tomorrow  = today + 86400

        # Cutoffs: ignore stale data for sensor VALUES.
        # The watermark still advances so we never re-fetch these items.
        # "recent" sensors (HR, sleep, vitality): 14 days
        # "infrequent" sensors (weight, SpO2): 90 days
        recent_cutoff = now - 14 * 86400
        slow_cutoff   = now - 90 * 86400

        touched_step_dates: set[str] = set()
        calories_today: list[dict] = []
        hr_readings: list[dict] = []
        stand_hours_today_set: set[int] = set()
        # 中高强度活动的逐分钟桶(intensity 原始项 = 每分钟一条):今日活动
        # 时长的兜底口径(daily_report 的 duration 缺席时用它数分钟)
        intensity_minutes: set[int] = set()

        for item in items:
            key = item.get("key", "")
            ts  = item.get("time") or item.get("update_time") or 0
            val = _parse_value(item)

            # ── Steps ─────────────────────────────────────────────────────
            if key == KEY_STEPS:
                if ts >= recent_cutoff and isinstance(val, dict):
                    date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                    hour     = datetime.fromtimestamp(ts).hour
                    _LOGGER.debug(
                        "Mi Fitness STEP: date=%s h=%02d steps=%s dist=%s",
                        date_str, hour, val.get("steps"), val.get("distance"),
                    )
                    # Dedupe by timestamp: the server re-sends the same interval
                    # with a new watermark as data accumulates. REPLACE (not add),
                    # otherwise a day inflates by the number of re-sends.
                    day = self._step_buckets.setdefault(date_str, {})
                    day[str(ts)] = {
                        "steps":    val.get("steps", 0),
                        "distance": val.get("distance", 0),
                        "calories": val.get("calories", 0),
                        "hour":     hour,
                    }
                    touched_step_dates.add(date_str)

            # ── Heart rate ────────────────────────────────────────────────
            elif key == KEY_HEART_RATE:
                if ts < recent_cutoff:
                    continue
                prev = self._sensor_data.get("heart_rate_ts", 0)
                if ts > prev and isinstance(val, dict) and val.get("bpm"):
                    self._sensor_data["heart_rate"]      = val["bpm"]
                    self._sensor_data["heart_rate_ts"]   = ts
                    self._sensor_data["heart_rate_type"] = val.get("type", 0)
                if today <= ts < tomorrow:
                    hr_readings.append(val)

            # ── Resting heart rate ────────────────────────────────────────
            elif key == KEY_RESTING_HEART_RATE:
                if ts < recent_cutoff:
                    continue
                prev = self._sensor_data.get("resting_hr_ts", 0)
                if ts > prev and isinstance(val, dict) and val.get("bpm"):
                    self._sensor_data["resting_heart_rate"] = val["bpm"]
                    self._sensor_data["resting_hr_ts"]      = ts

            # ── Sleep(全天多段模型:入库段记录+重建视图)──────────────────
            elif key == KEY_SLEEP:
                if ts >= recent_cutoff:
                    self._process_sleep(val, ts)

            # ── Calories ──────────────────────────────────────────────────
            elif key == KEY_CALORIES:
                if today <= ts < tomorrow:
                    calories_today.append(val)

            # ── Weight ────────────────────────────────────────────────────
            elif key == KEY_WEIGHT:
                if ts < slow_cutoff:
                    continue
                prev = self._sensor_data.get("weight_ts", 0)
                if ts > prev and isinstance(val, dict) and val.get("weight"):
                    self._sensor_data["weight"]    = val["weight"]
                    self._sensor_data["weight_ts"] = ts

            # ── SpO2 ──────────────────────────────────────────────────────
            elif key == KEY_SPO2:
                if ts < slow_cutoff:
                    continue
                prev = self._sensor_data.get("spo2_ts", 0)
                if ts > prev and isinstance(val, dict):
                    spo2_val = val.get("spo2") or val.get("value") or val.get("blood_oxygen")
                    if spo2_val:
                        self._sensor_data["spo2"]    = spo2_val
                        self._sensor_data["spo2_ts"] = ts

            # ── Vitality(已无实体消费,仅维持日清理键;数据源本身不丢弃)────
            # 低/中/高强度活力 + 建议活动实体已按用户要求移除(更新极少、无
            # 自动化价值);中高强度活动的可用读数见「今日活动时长」。
            elif key == KEY_VITALITY:
                continue

            # ── Valid stand ───────────────────────────────────────────────
            elif key == KEY_VALID_STAND:
                if today <= ts < tomorrow:
                    stand_hours_today_set.add(datetime.fromtimestamp(ts).hour)

            # ── Intensity 逐分钟桶(该分钟有中高强度活动)──────────────────
            elif key == KEY_INTENSITY:
                if today <= ts < tomorrow:
                    intensity_minutes.add(ts // 60)

            # ── Training load(训练负荷:锻炼在云端留下的量化痕迹)──────────
            # value={current_day_train_load, current_train_load_level,
            #        date_time, wtl_sum}。当日项服务器原地更新(time 恒为
            # 08:00),判重必须用 update_time —— 与活力恒 0 同一个坑。
            elif key == KEY_TRAINING_LOAD:
                ut = item.get("update_time") or ts
                prev_ut = self._sensor_data.get("training_load_ts", 0)
                if ut > prev_ut and isinstance(val, dict):
                    self._sensor_data["training_load_today"] = val.get(
                        "current_day_train_load", 0
                    )
                    self._sensor_data["training_load_level"] = val.get(
                        "current_train_load_level", 0
                    )
                    self._sensor_data["training_load_wtl_sum"] = val.get("wtl_sum", 0)
                    self._sensor_data["training_load_ts"] = ut

            # ── 未接的 key:发现日志(每个 key 每进程只记一次)───────────────
            # 原始流偶尔出现新 key(如 training_load),记录下来便于后续接入
            elif key:
                if key not in self._unknown_feed_keys_logged:
                    self._unknown_feed_keys_logged.add(key)
                    _LOGGER.info(
                        "Mi Fitness: 原始流出现未接的数据 key=%s(time=%s)", key, ts
                    )

        # Steps: rebuild day totals from deduped buckets (sum of unique-ts values)
        if touched_step_dates:
            # prune buckets older than 14 days
            cutoff_str = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
            self._step_buckets = {
                d: b for d, b in self._step_buckets.items() if d >= cutoff_str
            }
            # derive per-day totals from the deduped buckets; keep dates that
            # exist ONLY as daily_report entries (raw pagination can't reach them)
            daily_only = {
                d: v for d, v in self._steps_history.items()
                if d not in self._step_buckets
            }
            self._steps_history = {}
            for date_str, buckets in self._step_buckets.items():
                self._steps_history[date_str] = {
                    "date":     date_str,
                    "steps":    sum(b["steps"]    for b in buckets.values()),
                    "distance": sum(b["distance"] for b in buckets.values()),
                    "calories": sum(b["calories"] for b in buckets.values()),
                }
            self._steps_history.update(daily_only)
            today_str = datetime.fromtimestamp(today).strftime("%Y-%m-%d")
            tb = self._step_buckets.get(today_str)
            if tb:
                self._sensor_data["steps_today"]         = sum(b["steps"]    for b in tb.values())
                self._sensor_data["distance_today"]      = sum(b["distance"] for b in tb.values())
                self._sensor_data["step_calories_today"] = sum(b["calories"] for b in tb.values())
                hourly: dict[int, dict] = {}
                for b in tb.values():
                    he = hourly.setdefault(b["hour"], {"steps": 0, "distance": 0})
                    he["steps"]    += b["steps"]
                    he["distance"] += b["distance"]
                self._sensor_data["steps_hourly"] = [
                    {"hour": h, "steps": v["steps"], "distance": v["distance"]}
                    for h, v in sorted(hourly.items())
                ]
            self._rebuild_steps_history_14d()

        if calories_today:
            self._sensor_data["calories_today"] = sum(
                v.get("calories", 0) for v in calories_today if isinstance(v, dict)
            )

        if hr_readings:
            bpms = [v["bpm"] for v in hr_readings if isinstance(v, dict) and v.get("bpm")]
            if bpms:
                self._sensor_data["heart_rate_avg_today"] = round(sum(bpms) / len(bpms))
                self._sensor_data["heart_rate_max_today"] = max(bpms)
                self._sensor_data["heart_rate_min_today"] = min(bpms)

        if stand_hours_today_set:
            prev_set = set(self._sensor_data.get("stand_hours_list", []))
            merged   = sorted(prev_set | stand_hours_today_set)
            self._sensor_data["stand_hours_list"]  = merged
            self._sensor_data["stand_hours_today"] = len(merged)

        # 活动时长兜底口径:逐分钟桶数与日汇总取大(两者都只增不减,再大者胜)
        if intensity_minutes:
            cur = self._sensor_data.get("activity_duration_today") or 0
            self._sensor_data["activity_duration_today"] = max(cur, len(intensity_minutes))

        # Rebuild history + compute chronotype from full history (not single night)
        history_list = self._build_sleep_history_list()
        self._sensor_data["sleep_history_14d"] = history_list
        if history_list:
            self._sensor_data["sleep_chronotype"] = _sleep_chronotype_6(history_list)

    # ── Sleep processing(全天多段模型)─────────────────────────────────────────

    def _upsert_sleep_record(
        self,
        bedtime: int,
        wakeup: int,
        duration: int,
        deep: int,
        light: int,
        rem: int,
        awake_count: int,
        score: int,
        stages: list[dict],
        hr: dict,
    ) -> None:
        """入库一段睡眠(bedtime 键)。小米会把同一次就寝的数据分批重发(越发越全),
        同 bedtime 取更长的 duration、补非零的评分与分段。"""
        prev = self._sleep_records.get(bedtime)
        stages_keep = stages or (prev.get("stages") if prev else [])
        if prev and duration < prev["duration"]:
            # 旧记录更全:只吸收新的分段/评分
            if stages:
                prev["stages"] = stages
            if score and not prev.get("score"):
                prev["score"] = score
            return
        rec = {
            "bedtime": bedtime,
            "wakeup": wakeup,
            "duration": duration,
            "deep": deep,
            "light": light,
            "rem": rem,
            "awake_count": awake_count,
            "score": score or (prev.get("score", 0) if prev else 0),
            "stages": stages_keep,
            "ts": int(time.time()),
            **({k: v for k, v in hr.items() if v}),
        }
        if prev:
            for k in ("avg_hr", "max_hr", "min_hr"):
                if not rec.get(k) and prev.get(k):
                    rec[k] = prev[k]
        self._sleep_records[bedtime] = rec

    def _rebuild_sleep_views(self) -> None:
        """按「醒来日期」把全天多段聚合成视图:
        昨晚(10/7 睡→10/8 醒)与午睡(10/8)都归入 10/8 —— 小米 App 全天睡眠同构。
        最新醒来日的合计写入传感器;_sleep_history 按醒来日供 UI 逐夜翻页。"""
        groups: dict[str, list[dict]] = {}
        for rec in self._sleep_records.values():
            if not rec.get("wakeup"):
                continue
            wd = datetime.fromtimestamp(rec["wakeup"]).strftime("%Y-%m-%d")
            groups.setdefault(wd, []).append(rec)

        self._sleep_history = {}
        for wd, segs in groups.items():
            segs.sort(key=lambda x: x["bedtime"])
            duration = sum(x["duration"] for x in segs)
            deep = sum(x["deep"] for x in segs)
            light = sum(x["light"] for x in segs)
            rem = sum(x["rem"] for x in segs)
            awake = sum(x["awake_count"] for x in segs)
            score = next((x["score"] for x in segs if x.get("score")), 0)
            bedtime = segs[0]["bedtime"]
            wakeup = segs[-1]["wakeup"]
            stages = [seg for x in segs for seg in (x.get("stages") or [])]
            stages.sort(key=lambda x: x["start"])
            time_in_bed = (wakeup - bedtime) / 60 if wakeup > bedtime else duration
            hr = {
                "avg_hr": next((x["avg_hr"] for x in segs if x.get("avg_hr")), 0),
                "max_hr": max((x["max_hr"] for x in segs if x.get("max_hr")), default=0),
                "min_hr": min((x["min_hr"] for x in segs if x.get("min_hr")), default=0),
            }
            entry = {
                "date": wd,
                "bedtime": bedtime,
                "wakeup": wakeup,
                "duration": duration,
                "deep": deep,
                "light": light,
                "rem": rem,
                "awake_count": awake,
                "quality": score or _sleep_quality_score(duration, deep, rem, awake),
                "efficiency": round(duration / time_in_bed * 100) if time_in_bed > 0 else 0,
                **hr,
            }
            if stages:
                entry["stages"] = [[x["start"], x["end"], x["state"]] for x in stages]
            self._sleep_history[wd] = entry

        # 修剪 14 天外
        cutoff = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
        self._sleep_history = {k: v for k, v in self._sleep_history.items() if k >= cutoff}
        stage_cutoff = int(time.time()) - 14 * 86400
        self._sleep_records = {k: v for k, v in self._sleep_records.items() if k >= stage_cutoff}
        self._sleep_stages = {k: v for k, v in self._sleep_stages.items() if k >= stage_cutoff}

        # 最新醒来日的合计 → 传感器(分段 timeline 用 dict,含 label)
        if groups:
            latest = max(groups)
            segs = sorted(groups[latest], key=lambda x: x["bedtime"])
            entry = self._sleep_history.get(latest) or {}
            stages = [seg for x in segs for seg in (x.get("stages") or [])]
            stages.sort(key=lambda x: x["start"])
            self._sensor_data.update({
                "sleep_bedtime": entry.get("bedtime", segs[0]["bedtime"]),
                "sleep_wakeup": entry.get("wakeup", segs[-1]["wakeup"]),
                "sleep_duration": entry.get("duration", sum(x["duration"] for x in segs)),
                "sleep_deep": entry.get("deep", 0),
                "sleep_light": entry.get("light", 0),
                "sleep_rem": entry.get("rem", 0),
                "sleep_awake_count": entry.get("awake_count", 0),
                "sleep_quality": entry.get("quality", 0),
                "sleep_efficiency": entry.get("efficiency", 0),
                "sleep_avg_hr": entry.get("avg_hr", 0),
                "sleep_max_hr": entry.get("max_hr", 0),
                "sleep_min_hr": entry.get("min_hr", 0),
                "sleep_stages": stages,
                "sleep_ts": segs[-1].get("ts", int(time.time())),
            })

        self._sensor_data["sleep_history_14d"] = self._build_sleep_history_list()

    def _build_sleep_history_list(self) -> list[dict]:
        """Return sleep history sorted newest-first (max 14 entries)."""
        return sorted(
            self._sleep_history.values(),
            key=lambda x: x["date"],
            reverse=True,
        )[:14]

    def _process_sleep(self, val: Any, ts: int) -> None:
        """原始流 sleep 项(带分段 timeline):入库一段并重建视图。"""
        if not isinstance(val, dict):
            return
        bedtime = val.get("bedtime", 0)
        if not bedtime:
            return
        stages_raw = val.get("items") or []
        if stages_raw:
            self._sleep_stages[bedtime] = _stages_to_list(stages_raw)
        self._upsert_sleep_record(
            bedtime,
            val.get("wake_up_time", 0),
            val.get("duration", 0),
            val.get("sleep_deep_duration", 0),
            val.get("sleep_light_duration", 0),
            val.get("sleep_rem_duration", val.get("rem_duration", 0)),
            val.get("awake_count", 0),
            0,  # 原始项无小米评分,等日汇总合成条目补
            self._sleep_stages.get(bedtime, []),
            {"avg_hr": val.get("avg_hr", 0), "max_hr": val.get("max_hr", 0), "min_hr": val.get("min_hr", 0)},
        )
        self._rebuild_sleep_views()

"""Sensor entities for Mi Fitness integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfLength,
    UnitOfMass,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_USER_ID,
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DOMAIN,
)
from .coordinator import MiFitnessCoordinator


@dataclass(frozen=True, kw_only=True)
class MiFitnessSensorDescription(SensorEntityDescription):
    """Descriptor for a Mi Fitness sensor."""
    data_key: str = ""
    icon: str = "mdi:heart-pulse"


SENSOR_TYPES: tuple[MiFitnessSensorDescription, ...] = (
    MiFitnessSensorDescription(
        key="steps_today",
        name="今日步数",
        data_key="steps_today",
        native_unit_of_measurement="steps",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:walk",
    ),
    MiFitnessSensorDescription(
        key="distance_today",
        name="今日距离",
        data_key="distance_today",
        native_unit_of_measurement=UnitOfLength.METERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:map-marker-distance",
    ),
    MiFitnessSensorDescription(
        key="calories_today",
        name="今日活动卡路里",
        data_key="calories_today",
        native_unit_of_measurement="kcal",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:fire",
    ),
    MiFitnessSensorDescription(
        key="heart_rate",
        name="当前心率",
        data_key="heart_rate",
        native_unit_of_measurement="bpm",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=None,
        icon="mdi:heart-pulse",
    ),
    MiFitnessSensorDescription(
        key="resting_heart_rate",
        name="静息心率",
        data_key="resting_heart_rate",
        native_unit_of_measurement="bpm",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=None,
        icon="mdi:heart",
    ),
    MiFitnessSensorDescription(
        key="heart_rate_avg_today",
        name="今日平均心率",
        data_key="heart_rate_avg_today",
        native_unit_of_measurement="bpm",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=None,
        icon="mdi:heart-pulse",
    ),
    MiFitnessSensorDescription(
        key="sleep_duration",
        name="睡眠时长",
        data_key="sleep_duration",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:sleep",
    ),
    MiFitnessSensorDescription(
        key="sleep_deep",
        name="深睡时长",
        data_key="sleep_deep",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:weather-night",
    ),
    MiFitnessSensorDescription(
        key="sleep_light",
        name="浅睡时长",
        data_key="sleep_light",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:sleep",
    ),
    MiFitnessSensorDescription(
        key="sleep_rem",
        name="快速眼动时长",
        data_key="sleep_rem",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:brain",
    ),
    MiFitnessSensorDescription(
        key="sleep_avg_hr",
        name="睡眠平均心率",
        data_key="sleep_avg_hr",
        native_unit_of_measurement="bpm",
        state_class=SensorStateClass.MEASUREMENT,
        device_class=None,
        icon="mdi:heart-pulse",
    ),
    MiFitnessSensorDescription(
        key="weight",
        name="体重",
        data_key="weight",
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:scale-bathroom",
    ),
    MiFitnessSensorDescription(
        key="spo2",
        name="血氧饱和度",
        data_key="spo2",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:water-percent",
    ),
    MiFitnessSensorDescription(
        key="stand_hours_today",
        name="今日静止/久坐时长",
        data_key="stand_hours_today",
        native_unit_of_measurement="h",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:sofa",
    ),
    MiFitnessSensorDescription(
        key="vitality_high",
        name="高强度活力",
        data_key="vitality_high",
        native_unit_of_measurement="min",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:lightning-bolt",
    ),
    MiFitnessSensorDescription(
        key="vitality_medium",
        name="中强度活力",
        data_key="vitality_medium",
        native_unit_of_measurement="min",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:run",
    ),
    MiFitnessSensorDescription(
        key="vitality_low",
        name="低强度活力",
        data_key="vitality_low",
        native_unit_of_measurement="min",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:walk",
    ),
    MiFitnessSensorDescription(
        key="sleep_quality",
        name="睡眠评分",
        data_key="sleep_quality",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:sleep",
    ),
    MiFitnessSensorDescription(
        key="sleep_chronotype",
        name="睡眠体型",
        data_key="sleep_chronotype",
        icon="mdi:paw",
    ),
    MiFitnessSensorDescription(
        key="sleep_efficiency",
        name="睡眠效率",
        data_key="sleep_efficiency",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:percent",
    ),
    MiFitnessSensorDescription(
        key="sleep_awake_count",
        name="夜醒次数",
        data_key="sleep_awake_count",
        native_unit_of_measurement="times",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:eye-outline",
    ),
    MiFitnessSensorDescription(
        key="sleep_max_hr",
        name="睡眠最高心率",
        data_key="sleep_max_hr",
        native_unit_of_measurement="bpm",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:heart-plus",
    ),
    MiFitnessSensorDescription(
        key="sleep_min_hr",
        name="睡眠最低心率",
        data_key="sleep_min_hr",
        native_unit_of_measurement="bpm",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:heart-minus",
    ),
    MiFitnessSensorDescription(
        key="sleep_bedtime",
        name="就寝时间",
        data_key="sleep_bedtime",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:bed-clock",
    ),
    MiFitnessSensorDescription(
        key="sleep_wakeup",
        name="起床时间",
        data_key="sleep_wakeup",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:alarm",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Mi Fitness sensors from a config entry."""
    coordinator: MiFitnessCoordinator = hass.data[DOMAIN][entry.entry_id]
    user_id = entry.data[CONF_USER_ID]

    async_add_entities(
        MiFitnessSensor(coordinator, description, user_id, entry.entry_id)
        for description in SENSOR_TYPES
    )


class MiFitnessSensor(CoordinatorEntity[MiFitnessCoordinator], SensorEntity):
    """A single Mi Fitness sensor."""

    entity_description: MiFitnessSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MiFitnessCoordinator,
        description: MiFitnessSensorDescription,
        user_id: str,
        entry_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._user_id = user_id
        self._attr_unique_id = f"{DOMAIN}_{user_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, user_id)},
            name=f"小米运动健康 ({user_id})",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
        )

    @property
    def native_value(self) -> Any:
        """Return sensor value from coordinator data."""
        if self.coordinator.data is None:
            return None
        val = self.coordinator.data.get(self.entity_description.data_key)
        # TIMESTAMP device_class requires aware datetime
        if self.entity_description.device_class == SensorDeviceClass.TIMESTAMP and val is not None:
            if isinstance(val, (int, float)):
                return datetime.fromtimestamp(val, tz=timezone.utc)
        return val

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes for sensors that have them."""
        data = self.coordinator.data or {}
        key = self.entity_description.key
        attrs: dict[str, Any] = {}

        if key == "heart_rate":
            ts = data.get("heart_rate_ts")
            if ts:
                attrs["last_measured"] = datetime.fromtimestamp(ts).isoformat()
            attrs["heart_rate_max_today"] = data.get("heart_rate_max_today")
            attrs["heart_rate_min_today"] = data.get("heart_rate_min_today")

        elif key == "sleep_duration":
            bedtime = data.get("sleep_bedtime")
            wakeup  = data.get("sleep_wakeup")
            if bedtime:
                attrs["bedtime"] = datetime.fromtimestamp(bedtime).isoformat()
            if wakeup:
                attrs["wake_up_time"] = datetime.fromtimestamp(wakeup).isoformat()
            attrs["awake_count"]   = data.get("sleep_awake_count")
            attrs["quality"]       = data.get("sleep_quality")
            attrs["efficiency"]    = data.get("sleep_efficiency")
            attrs["chronotype"]    = data.get("sleep_chronotype")
            # 分段条 UI 的单实体数据源:各阶段分钟数直接挂属性
            attrs["deep_minutes"]  = data.get("sleep_deep")
            attrs["light_minutes"] = data.get("sleep_light")
            attrs["rem_minutes"]   = data.get("sleep_rem")
            stages = data.get("sleep_stages")
            if stages:
                attrs["stages"] = stages
            # 历史夜分段(压缩 [start,end,state] 三元组),按日期
            stages_hist = {
                h["date"]: h["stages"]
                for h in (data.get("sleep_history_14d") or [])
                if h.get("stages")
            }
            if stages_hist:
                attrs["stages_history"] = stages_hist
            history = data.get("sleep_history_14d")
            if history:
                attrs["history_14d"] = history

        elif key == "sleep_chronotype":
            _ANIMAL_EMOJI = {
                "sheep":      "🐑",
                "penguin":    "🐧",
                "brown_bear": "🐻",
                "koala":      "🐨",
                "night_owl":  "🦉",
                "shark":      "🦈",
            }
            ct = data.get("sleep_chronotype")
            if ct:
                attrs["emoji"] = _ANIMAL_EMOJI.get(ct, "")
            bedtime = data.get("sleep_bedtime")
            wakeup  = data.get("sleep_wakeup")
            if bedtime:
                attrs["bedtime"] = datetime.fromtimestamp(bedtime).isoformat()
            if wakeup:
                attrs["wake_up_time"] = datetime.fromtimestamp(wakeup).isoformat()

        elif key == "steps_today":
            attrs["distance_m"]    = data.get("distance_today")
            attrs["step_calories"] = data.get("step_calories_today")
            hourly = data.get("steps_hourly")
            if hourly:
                attrs["hourly"] = hourly
            history = data.get("steps_history_14d")
            if history:
                attrs["history_14d"] = history

        elif key == "stand_hours_today":
            hours_list = data.get("stand_hours_list")
            if hours_list:
                attrs["hours_list"] = hours_list

        elif key == "weight":
            ts = data.get("weight_ts")
            if ts:
                attrs["last_measured"] = datetime.fromtimestamp(ts).isoformat()

        return {k: v for k, v in attrs.items() if v is not None}

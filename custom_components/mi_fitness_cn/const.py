"""Constants for Mi Fitness CN integration (小米运动健康 CN 分支)。

CN 分支独立域名说明:上游 batareya16/MiFitnessHass 已占用 mi_fitness 域名,
本分支作为独立 HACS 项目发布,域名/目录/存储键一律 mi_fitness_cn,两者不可
混装(HA 按 domain 识别集成,同名会互相覆盖)。
"""

DOMAIN = "mi_fitness_cn"

CONF_USER_ID       = "user_id"
CONF_C_USER_ID     = "c_user_id"
CONF_SSECURITY     = "ssecurity"
CONF_SERVICE_TOKEN = "service_token"
CONF_REGION        = "region"
CONF_PHONE_ID      = "phone_id"

CONF_PASS_TOKEN    = "pass_token"    # passToken — used for silent serviceToken refresh

REGIONS = ["us", "de", "sg", "cn", "ru", "i2"]
DEFAULT_REGION = "cn"

SCAN_INTERVAL_MINUTES = 15

# Approximate watermark corresponding to ~30 days before 2026-05-30.
# Used as the default starting point on first install so users don't scan
# from absolute zero. Xiaomi watermarks are global sequential IDs (~78B/day).
# Update this value with each major release.
DEFAULT_START_WATERMARK = 157_500_000_000_000

STORAGE_KEY = "mi_fitness_cn_watermarks"
STORAGE_VERSION = 1

# Sensor keys in the API response
KEY_STEPS              = "steps"
KEY_HEART_RATE         = "heart_rate"
KEY_RESTING_HEART_RATE = "resting_heart_rate"
KEY_SLEEP              = "sleep"
KEY_CALORIES           = "calories"
KEY_WEIGHT             = "weight"
KEY_VITALITY           = "vitality"
KEY_INTENSITY          = "intensity"
KEY_VALID_STAND        = "valid_stand"
KEY_SPO2               = "spo2"
KEY_TRAINING_LOAD      = "training_load"

# Device info
DEVICE_MANUFACTURER = "Xiaomi"
DEVICE_MODEL        = "小米运动健康 账号"

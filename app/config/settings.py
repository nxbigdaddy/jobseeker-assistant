"""应用配置模块。

集中定义应用元信息、路径与默认配置常量。
默认常量对照系统设计文档 §9.2（风控策略）与 §14（调度设计），
可被数据库 settings 表按 key 覆盖（见 get_setting_override）。
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------- 应用信息
APP_NAME = "个人求职助手"
APP_VERSION = "0.1.0"

# ---------------------------------------------------------------- 路径
# settings.py 位于 <项目根>/app/config/ 下，parents[2] 即项目根目录（<workspace>\HR）
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 运行时数据目录（不入版本库）
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "app.db"                     # SQLite 主库
BROWSER_DIR = DATA_DIR / "browser"                # Playwright user_data_dir（每站点子目录）
RESUMES_DIR = DATA_DIR / "resumes"                # 简历文件副本
LOG_DIR = DATA_DIR / "logs"                       # 应用日志

# 模块导入时自动创建运行时目录；只读目录等场景下容错，不崩溃
for _dir in (DATA_DIR, BROWSER_DIR, RESUMES_DIR, LOG_DIR):
    try:
        _dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # 目录不可写时静默跳过，运行期再提示

# ---------------------------------------------------------------- 默认配置
# 调度（§14）：每日增量采集时间
DAILY_CRAWL_TIME = "08:00"

# 投递风控（§9.2）：
APPLY_DAILY_LIMIT = 20        # 每日每站投递上限
APPLY_INTERVAL_MIN = 30       # 两次投递随机间隔下限（秒）
APPLY_INTERVAL_MAX = 90       # 两次投递随机间隔上限（秒）
APPLY_TIME_START = "09:00"    # 投递时间窗起点
APPLY_TIME_END = "21:00"      # 投递时间窗终点
BOSS_DAILY_LIMIT = 10         # Boss 直聘每日沟通上限（更保守）

# 数据保留（§14/§15）：岗位数据保留天数，过期自动清理
JOB_RETENTION_DAYS = 90

# LLM 调用（§11.1）：
DEFAULT_LLM_TEMPERATURE = 0.7   # 默认温度
LLM_TIMEOUT_SECONDS = 30        # 请求超时（秒）
LLM_RETRIES = 2                 # 失败重试次数


def get_setting_override(key: str, default: object) -> object:
    """从 settings 表读取覆盖值；数据库不可用或未配置时返回默认值。

    延迟导入 app.db 相关模块（可能尚未就绪或损坏），
    任何异常都回退到传入的默认值，保证配置加载不崩溃。
    约定：repository.get_setting(conn, key, default=None)。
    """
    try:
        from app.db.database import get_connection  # 延迟导入
        from app.db.repository import get_setting

        with get_connection() as conn:
            value = get_setting(conn, key)
    except Exception:  # noqa: BLE001 —— DB 不可用/无该 key 均回退默认值
        return default
    return default if value is None else value

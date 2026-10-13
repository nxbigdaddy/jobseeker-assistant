"""个人求职助手 —— 程序入口。

启动流程：
1. 配置日志（控制台 + data/logs/app.log，UTF-8）；
2. 初始化 SQLite（失败仅记日志，不阻塞启动）；
3. 启动 PySide6 主窗口。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中，app 包可被导入
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# settings 导入时即创建 data/ 等运行时目录
from app.config.settings import APP_NAME, APP_VERSION, LOG_DIR  # noqa: E402

_logger = logging.getLogger(__name__)


def setup_logging() -> None:
    """配置日志：控制台输出 + data/logs/app.log（Windows 下显式 UTF-8 编码）。"""
    log_file = LOG_DIR / "app.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


def init_database() -> None:
    """初始化 SQLite 数据库；失败仅记录日志，不崩溃。"""
    try:
        from app.db.database import init_db

        init_db()
        _logger.info("数据库初始化完成")
    except Exception as exc:  # noqa: BLE001
        _logger.exception("数据库初始化失败：%s", exc)


def main() -> int:
    """应用主流程：初始化数据库并启动 GUI。"""
    setup_logging()
    _logger.info("%s v%s 启动中……", APP_NAME, APP_VERSION)
    init_database()

    # GUI 依赖（PySide6 / 主窗口）放在函数内导入并容错
    try:
        from PySide6.QtWidgets import QApplication

        from app.ui.main_window import MainWindow
    except ImportError as exc:
        print(
            "无法加载 GUI 模块，请确认已安装依赖并创建主窗口模块：\n"
            f"  详情：{exc}\n"
            "  安装依赖：pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("JobSeekerAssistant")

    window = MainWindow()
    window.show()
    _logger.info("GUI 主窗口已显示")

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

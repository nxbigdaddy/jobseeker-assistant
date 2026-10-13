"""数据库层：连接管理、数据模型与仓储函数。

对外主要提供：
- ``Database`` / ``get_connection`` / ``init_db``（app.db.database）
- 数据模型（app.db.models）
- 纯函数 CRUD 仓储（app.db.repository）
"""

from .database import Database, get_connection, init_db
from .models import (
    AISettings,
    Application,
    CrawlRun,
    Job,
    PromptTemplate,
    Resume,
    SearchTask,
)

__all__ = [
    "Database",
    "get_connection",
    "init_db",
    "Job",
    "Application",
    "Resume",
    "SearchTask",
    "CrawlRun",
    "AISettings",
    "PromptTemplate",
]

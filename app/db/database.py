"""数据库连接与初始化（SQLite）。

本模块提供：
- ``get_connection()``：返回配置好 row_factory / WAL / 外键约束的连接；
- ``init_db()``：创建 8 张表、执行 schema 版本迁移（user_version），
  并在建表后写入默认数据（调用 repository.seed_defaults）；
- ``Database``：上下文管理器，``with Database() as db`` 正常退出自动 commit、
  异常退出自动 rollback，并自动关闭连接。

数据库路径优先从 ``app.config.settings.DB_PATH`` 获取；若该模块尚未创建
（导入失败），回退到项目根目录下 ``data/app.db``。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

try:
    from app.config.settings import DB_PATH
except ImportError:  # app/config/settings.py 由其他模块负责创建，缺失时回退
    DB_PATH = str(Path(__file__).resolve().parents[2] / "data" / "app.db")

from app.db.repository import seed_defaults

# Schema 版本迁移列表：下标 0 对应 v1，后续新版本依次在末尾追加。
# 版本号通过 PRAGMA user_version 记录，init_db 只执行缺失的迁移。
MIGRATIONS: list[str] = [
    # ---- v1：初始建表（8 张表）----
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        source        TEXT    NOT NULL,
        job_id        TEXT    NOT NULL,
        title         TEXT    NOT NULL DEFAULT '',
        company       TEXT    NOT NULL DEFAULT '',
        company_size  TEXT    NOT NULL DEFAULT '',
        industry      TEXT    NOT NULL DEFAULT '',
        salary_min    INTEGER,
        salary_max    INTEGER,
        salary_raw    TEXT    NOT NULL DEFAULT '',
        city          TEXT    NOT NULL DEFAULT '',
        district      TEXT    NOT NULL DEFAULT '',
        experience    TEXT    NOT NULL DEFAULT '',
        education     TEXT    NOT NULL DEFAULT '',
        tags          TEXT    NOT NULL DEFAULT '[]',
        description   TEXT    NOT NULL DEFAULT '',
        publish_date  TEXT    NOT NULL DEFAULT '',
        job_url       TEXT    NOT NULL DEFAULT '',
        crawl_time    TEXT    NOT NULL DEFAULT '',
        status        TEXT    NOT NULL DEFAULT 'active',
        is_favorite   INTEGER NOT NULL DEFAULT 0,
        match_score   REAL,
        UNIQUE (source, job_id)
    );
    CREATE INDEX IF NOT EXISTS idx_jobs_publish_date ON jobs (publish_date);
    CREATE INDEX IF NOT EXISTS idx_jobs_source        ON jobs (source);
    CREATE INDEX IF NOT EXISTS idx_jobs_city          ON jobs (city);

    CREATE TABLE IF NOT EXISTS applications (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        job_ref_id     INTEGER NOT NULL REFERENCES jobs (id),
        source         TEXT    NOT NULL DEFAULT '',
        resume_version TEXT    NOT NULL DEFAULT '',
        applied_at     TEXT    NOT NULL DEFAULT '',
        status         TEXT    NOT NULL DEFAULT 'submitted',
        note           TEXT    NOT NULL DEFAULT '',
        UNIQUE (job_ref_id)
    );

    CREATE TABLE IF NOT EXISTS resumes (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT NOT NULL DEFAULT '',
        target_title TEXT NOT NULL DEFAULT '',
        file_path    TEXT NOT NULL DEFAULT '',
        content      TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL DEFAULT '',
        updated_at   TEXT NOT NULL DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS ai_settings (
        id          INTEGER PRIMARY KEY,
        base_url    TEXT NOT NULL DEFAULT '',
        api_key     TEXT NOT NULL DEFAULT '',
        model       TEXT NOT NULL DEFAULT '',
        temperature REAL NOT NULL DEFAULT 0.7,
        enabled     INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS prompts (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        feature TEXT NOT NULL UNIQUE,
        name    TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL DEFAULT '',
        enabled INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS search_tasks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL DEFAULT '',
        sites       TEXT NOT NULL DEFAULT '[]',
        keywords    TEXT NOT NULL DEFAULT '[]',
        cities      TEXT NOT NULL DEFAULT '[]',
        salary_min  INTEGER,
        salary_max  INTEGER,
        enabled     INTEGER NOT NULL DEFAULT 1,
        last_run_at TEXT
    );

    CREATE TABLE IF NOT EXISTS crawl_runs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id     INTEGER,
        started_at  TEXT NOT NULL DEFAULT '',
        finished_at TEXT,
        fetched     INTEGER NOT NULL DEFAULT 0,
        inserted    INTEGER NOT NULL DEFAULT 0,
        status      TEXT NOT NULL DEFAULT 'running',
        error       TEXT
    );

    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL DEFAULT ''
    );
    """,
    # ---- v2：applications.job_ref_id 改为可空 ----
    # 设计文档 §9.3 允许"job_ref 未找到时 note='job_ref 未找到' 仍插入"，
    # v1 的 NOT NULL 约束与之矛盾。SQLite 无法直接改列约束，采用标准重建流程：
    # 新建表 → 拷贝数据 → 删旧表 → 重命名；唯一约束改为唯一索引（SQLite 中
    # UNIQUE 索引允许多个 NULL，保留"同一岗位不重复投递"语义）。
    """
    CREATE TABLE applications_v2 (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        job_ref_id     INTEGER REFERENCES jobs (id),
        source         TEXT    NOT NULL DEFAULT '',
        resume_version TEXT    NOT NULL DEFAULT '',
        applied_at     TEXT    NOT NULL DEFAULT '',
        status         TEXT    NOT NULL DEFAULT 'submitted',
        note           TEXT    NOT NULL DEFAULT ''
    );
    INSERT INTO applications_v2 (id, job_ref_id, source, resume_version, applied_at, status, note)
        SELECT id, job_ref_id, source, resume_version, applied_at, status, note FROM applications;
    DROP TABLE applications;
    ALTER TABLE applications_v2 RENAME TO applications;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_applications_job_ref ON applications (job_ref_id);
    """,
]


def get_connection() -> sqlite3.Connection:
    """创建并返回 SQLite 连接。

    - row_factory = sqlite3.Row（支持按列名访问）；
    - 开启 WAL 模式（journal_mode=WAL）；
    - 开启外键约束（foreign_keys=ON）。
    """
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """初始化数据库：创建 8 张表、执行 schema 版本迁移，并写入默认数据。

    通过 ``PRAGMA user_version`` 记录当前 schema 版本，仅执行缺失的迁移
    （当前最新版本为 v2）。建表完成后调用 ``seed_defaults`` 写入默认
    提示词与默认设置。
    """
    conn = get_connection()
    try:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        for version, ddl in enumerate(MIGRATIONS, start=1):
            if version > current:
                conn.executescript(ddl)
                conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
        seed_defaults(conn)
        conn.commit()
    finally:
        conn.close()


class Database:
    """数据库上下文管理器。

    用法::

        with Database() as db:
            upsert_job(db, job)
            # 正常退出自动 commit；抛出异常自动 rollback；连接自动关闭。
    """

    def __init__(self) -> None:
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        self._conn = get_connection()
        return self._conn

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._conn is None:
            return
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        self._conn.close()
        self._conn = None

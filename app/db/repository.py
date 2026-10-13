"""SQLite 仓储层：纯函数 CRUD。

所有函数接收 ``sqlite3.Connection`` 作为第一个参数，不管理连接生命周期
与事务提交/回滚，由调用方负责（例如 ``app.db.database.Database`` 上下文
管理器，或手动 ``conn.commit()`` / ``conn.close()``）。

默认数据（提示词、设置）与设计文档 §11.4 / §12.1 保持一致。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from app.db.models import (
    AISettings,
    Application,
    CrawlRun,
    Job,
    PromptTemplate,
    Resume,
    SearchTask,
)

# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------

# upsert_job 涉及的列（除 id 外的全部字段，按表结构顺序）
_JOB_COLUMNS = (
    "source", "job_id", "title", "company", "company_size", "industry",
    "salary_min", "salary_max", "salary_raw", "city", "district",
    "experience", "education", "tags", "description", "publish_date",
    "job_url", "crawl_time", "status", "is_favorite", "match_score",
)


def upsert_job(conn: sqlite3.Connection, job: Job) -> int:
    """按 (source, job_id) 幂等写入岗位。

    已存在则更新除 id 外的全部字段，否则插入；返回该岗位的主键 id。
    """
    data = job.as_dict()
    columns = ", ".join(_JOB_COLUMNS)
    placeholders = ", ".join("?" for _ in _JOB_COLUMNS)
    assignments = ", ".join(f"{col}=excluded.{col}" for col in _JOB_COLUMNS)
    conn.execute(
        f"INSERT INTO jobs ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(source, job_id) DO UPDATE SET {assignments}",
        tuple(data[col] for col in _JOB_COLUMNS),
    )
    row = conn.execute(
        "SELECT id FROM jobs WHERE source=? AND job_id=?",
        (job.source, job.job_id),
    ).fetchone()
    return row["id"]


def get_job(conn: sqlite3.Connection, job_id_pk: int) -> Job | None:
    """按主键获取岗位，不存在时返回 None。"""
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id_pk,)).fetchone()
    return Job.from_row(row) if row is not None else None


def get_job_by_source_id(
    conn: sqlite3.Connection, source: str, job_id: str
) -> Job | None:
    """按 (source, job_id) 唯一键获取岗位，不存在时返回 None。

    供投递记录解析 job_ref_id 使用（设计文档 §9.3）。
    """
    row = conn.execute(
        "SELECT * FROM jobs WHERE source=? AND job_id=? LIMIT 1", (source, job_id)
    ).fetchone()
    return Job.from_row(row) if row is not None else None


def list_jobs(
    conn: sqlite3.Connection,
    source: str | None = None,
    city: str | None = None,
    keyword: str | None = None,
    only_active: bool = True,
    limit: int = 500,
) -> list[Job]:
    """按条件查询岗位列表。

    - keyword 对 title / company / description 做模糊匹配；
    - only_active=True 时只返回 status='active' 的岗位。
    """
    sql = ["SELECT * FROM jobs WHERE 1=1"]
    params: list = []
    if only_active:
        sql.append("AND status='active'")
    if source:
        sql.append("AND source=?")
        params.append(source)
    if city:
        sql.append("AND city=?")
        params.append(city)
    if keyword:
        sql.append("AND (title LIKE ? OR company LIKE ? OR description LIKE ?)")
        kw = f"%{keyword}%"
        params.extend((kw, kw, kw))
    sql.append("ORDER BY id DESC LIMIT ?")
    params.append(limit)
    rows = conn.execute(" ".join(sql), params).fetchall()
    return [Job.from_row(r) for r in rows]


def mark_offline(
    conn: sqlite3.Connection, source: str, seen_job_ids: list[str]
) -> int:
    """把该 source 下不在 seen_job_ids 列表中的 active 岗位置为 offline。

    返回受影响（被标记）的行数。seen_job_ids 为空时将该 source 下所有
    active 岗位标记为 offline。
    """
    if seen_job_ids:
        placeholders = ", ".join("?" for _ in seen_job_ids)
        cur = conn.execute(
            f"UPDATE jobs SET status='offline' "
            f"WHERE source=? AND status='active' AND job_id NOT IN ({placeholders})",
            [source, *seen_job_ids],
        )
    else:
        cur = conn.execute(
            "UPDATE jobs SET status='offline' "
            "WHERE source=? AND status='active'",
            (source,),
        )
    return cur.rowcount


def count_jobs(conn: sqlite3.Connection) -> int:
    """返回岗位总数。"""
    row = conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()
    return row["c"]


# ---------------------------------------------------------------------------
# applications
# ---------------------------------------------------------------------------


def insert_application(conn: sqlite3.Connection, app: Application) -> int:
    """插入一条投递记录，返回新记录 id。"""
    cur = conn.execute(
        "INSERT INTO applications "
        "(job_ref_id, source, resume_version, applied_at, status, note) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (app.job_ref_id, app.source, app.resume_version,
         app.applied_at, app.status, app.note),
    )
    return cur.lastrowid


def list_applications(
    conn: sqlite3.Connection, limit: int = 200
) -> list[Application]:
    """按投递时间倒序返回投递记录列表。"""
    rows = conn.execute(
        "SELECT * FROM applications ORDER BY applied_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [Application.from_row(r) for r in rows]


def update_application_status(
    conn: sqlite3.Connection, pk: int, status: str
) -> None:
    """更新投递记录状态。"""
    conn.execute("UPDATE applications SET status=? WHERE id=?", (status, pk))


def get_application_by_job(
    conn: sqlite3.Connection, job_ref_id: int
) -> Application | None:
    """按岗位主键查询投递记录（同一岗位至多一条），不存在时返回 None。"""
    row = conn.execute(
        "SELECT * FROM applications WHERE job_ref_id=? LIMIT 1", (job_ref_id,)
    ).fetchone()
    return Application.from_row(row) if row is not None else None


# ---------------------------------------------------------------------------
# resumes
# ---------------------------------------------------------------------------


def insert_resume(conn: sqlite3.Connection, r: Resume) -> int:
    """插入一份简历版本，返回新记录 id。"""
    cur = conn.execute(
        "INSERT INTO resumes "
        "(name, target_title, file_path, content, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (r.name, r.target_title, r.file_path, r.content,
         r.created_at, r.updated_at),
    )
    return cur.lastrowid


def list_resumes(conn: sqlite3.Connection) -> list[Resume]:
    """返回全部简历版本（新→旧）。"""
    rows = conn.execute("SELECT * FROM resumes ORDER BY id DESC").fetchall()
    return [Resume.from_row(r) for r in rows]


def get_resume(conn: sqlite3.Connection, pk: int) -> Resume | None:
    """按主键获取简历版本，不存在时返回 None。"""
    row = conn.execute("SELECT * FROM resumes WHERE id=?", (pk,)).fetchone()
    return Resume.from_row(row) if row is not None else None


def delete_resume(conn: sqlite3.Connection, pk: int) -> None:
    """按主键删除简历版本。"""
    conn.execute("DELETE FROM resumes WHERE id=?", (pk,))


# ---------------------------------------------------------------------------
# search_tasks
# ---------------------------------------------------------------------------


def list_search_tasks(
    conn: sqlite3.Connection, enabled_only: bool = True
) -> list[SearchTask]:
    """返回搜索任务列表；enabled_only=True 时只返回启用中的任务。"""
    sql = "SELECT * FROM search_tasks"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY id"
    rows = conn.execute(sql).fetchall()
    return [SearchTask.from_row(r) for r in rows]


def insert_search_task(conn: sqlite3.Connection, t: SearchTask) -> int:
    """插入一个搜索任务，返回新记录 id。"""
    data = t.as_dict()
    cur = conn.execute(
        "INSERT INTO search_tasks "
        "(name, sites, keywords, cities, salary_min, salary_max, enabled, last_run_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (data["name"], data["sites"], data["keywords"], data["cities"],
         data["salary_min"], data["salary_max"], data["enabled"],
         data["last_run_at"]),
    )
    return cur.lastrowid


def update_search_task(conn: sqlite3.Connection, t: SearchTask) -> None:
    """按主键更新搜索任务的全部字段。"""
    data = t.as_dict()
    conn.execute(
        "UPDATE search_tasks SET name=?, sites=?, keywords=?, cities=?, "
        "salary_min=?, salary_max=?, enabled=?, last_run_at=? WHERE id=?",
        (data["name"], data["sites"], data["keywords"], data["cities"],
         data["salary_min"], data["salary_max"], data["enabled"],
         data["last_run_at"], t.id),
    )


def delete_search_task(conn: sqlite3.Connection, pk: int) -> None:
    """按主键删除搜索任务。"""
    conn.execute("DELETE FROM search_tasks WHERE id=?", (pk,))


def set_task_last_run(conn: sqlite3.Connection, pk: int, ts: str) -> None:
    """记录任务最近一次运行时间。"""
    conn.execute("UPDATE search_tasks SET last_run_at=? WHERE id=?", (ts, pk))


# ---------------------------------------------------------------------------
# crawl_runs
# ---------------------------------------------------------------------------


def insert_crawl_run(conn: sqlite3.Connection, r: CrawlRun) -> int:
    """插入一条采集运行记录，返回新记录 id。"""
    cur = conn.execute(
        "INSERT INTO crawl_runs "
        "(task_id, started_at, finished_at, fetched, inserted, status, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (r.task_id, r.started_at, r.finished_at,
         r.fetched, r.inserted, r.status, r.error),
    )
    return cur.lastrowid


def finish_crawl_run(
    conn: sqlite3.Connection,
    pk: int,
    fetched: int,
    inserted: int,
    status: str,
    error: str | None = None,
) -> None:
    """结束一次采集运行：写入结束时间、统计与状态。"""
    conn.execute(
        "UPDATE crawl_runs SET finished_at=?, fetched=?, inserted=?, "
        "status=?, error=? WHERE id=?",
        (datetime.now().isoformat(timespec="seconds"),
         fetched, inserted, status, error, pk),
    )


def list_crawl_runs(
    conn: sqlite3.Connection, limit: int = 50
) -> list[CrawlRun]:
    """按时间倒序返回采集运行记录。"""
    rows = conn.execute(
        "SELECT * FROM crawl_runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [CrawlRun.from_row(r) for r in rows]


def delete_old_crawl_runs(
    conn: sqlite3.Connection, keep_days: int = 90
) -> int:
    """删除 keep_days 天以前的采集运行记录，返回删除行数。"""
    cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat(
        timespec="seconds"
    )
    cur = conn.execute("DELETE FROM crawl_runs WHERE started_at < ?", (cutoff,))
    return cur.rowcount


# ---------------------------------------------------------------------------
# ai_settings（单行配置，id=1）
# ---------------------------------------------------------------------------


def get_ai_settings(conn: sqlite3.Connection) -> AISettings:
    """读取 AI 配置（单行 id=1）；无记录时插入默认行并返回默认配置。"""
    row = conn.execute("SELECT * FROM ai_settings WHERE id=1").fetchone()
    if row is not None:
        return AISettings.from_row(row)
    default = AISettings()
    conn.execute(
        "INSERT INTO ai_settings "
        "(id, base_url, api_key, model, temperature, enabled) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (default.id, default.base_url, default.api_key,
         default.model, default.temperature, default.enabled),
    )
    return default


def save_ai_settings(conn: sqlite3.Connection, s: AISettings) -> None:
    """保存 AI 配置（固定写入单行 id=1）。"""
    conn.execute(
        "INSERT INTO ai_settings "
        "(id, base_url, api_key, model, temperature, enabled) "
        "VALUES (1, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "base_url=excluded.base_url, api_key=YOUR_SECRET, "
        "model=excluded.model, temperature=excluded.temperature, "
        "enabled=excluded.enabled",
        (s.base_url, s.api_key, s.model, s.temperature, s.enabled),
    )


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------


def list_prompts(conn: sqlite3.Connection) -> list[PromptTemplate]:
    """返回全部提示词模板。"""
    rows = conn.execute("SELECT * FROM prompts ORDER BY id").fetchall()
    return [PromptTemplate.from_row(r) for r in rows]


def get_prompt(
    conn: sqlite3.Connection, feature: str
) -> PromptTemplate | None:
    """按 feature 获取提示词模板，不存在时返回 None。"""
    row = conn.execute(
        "SELECT * FROM prompts WHERE feature=? LIMIT 1", (feature,)
    ).fetchone()
    return PromptTemplate.from_row(row) if row is not None else None


def save_prompt(conn: sqlite3.Connection, p: PromptTemplate) -> None:
    """按 feature 幂等保存提示词（已存在则更新名称/内容/启用状态）。"""
    conn.execute(
        "INSERT INTO prompts (feature, name, content, enabled) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(feature) DO UPDATE SET "
        "name=excluded.name, content=excluded.content, enabled=excluded.enabled",
        (p.feature, p.name, p.content, p.enabled),
    )


# ---------------------------------------------------------------------------
# settings（通用 key-value）
# ---------------------------------------------------------------------------


def get_setting(
    conn: sqlite3.Connection, key: str, default=None
):
    """读取通用设置；key 不存在时返回 default。"""
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?", (key,)
    ).fetchone()
    return row["value"] if row is not None else default


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    """写入通用设置，已存在则覆盖（value 统一转为字符串存储）。"""
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


# ---------------------------------------------------------------------------
# 默认数据（seed）
# ---------------------------------------------------------------------------

# 默认 AI 提示词模板：feature → 名称/内容（参照设计文档 §11.4 输出格式约定）
DEFAULT_PROMPTS: list[dict] = [
    {
        "feature": "resume_optimize",
        "name": "简历针对性优化",
        "content": """你是资深 HR 和简历顾问，擅长针对目标岗位优化简历。
请根据以下岗位 JD 和我的简历，给出针对性优化建议。

岗位 JD：
{jd}

我的简历：
{resume}

输出格式约定（严格按此结构输出 Markdown）：
## 一、亮点改写
逐条改写简历中与 JD 匹配的经历，突出量化成果与岗位关键词。
## 二、关键词补充
根据 JD 高频词，列出应补充到简历中的技能与关键词。
## 三、删除建议
列出与目标岗位无关、建议删减或弱化的内容。
## 四、可直接使用的优化要点摘要""",
    },
    {
        "feature": "match_score",
        "name": "岗位匹配度评分",
        "content": """你是招聘专家，负责评估候选人与岗位的匹配程度。
请根据以下简历和岗位 JD，给出匹配度评分。

我的简历：
{resume}

岗位 JD：
{jd}

输出格式约定（严格输出 JSON，不要包含其他任何内容）：
{"score": 0-100 的整数, "reasons": ["匹配理由1", "匹配理由2"], "gaps": ["短板1", "短板2"]}""",
    },
    {
        "feature": "apply_strategy",
        "name": "投递策略建议",
        "content": """你是招聘平台风控分析师，熟悉各平台的投递规则与账号风控特征。
请结合平台特征、用户活跃时段与近期投递情况，给出保守的投递策略建议。

平台：{platform}
城市：{city}
近期投递/岗位情况（可空）：{jd}

输出格式约定（Markdown）：
## 每日投递上限
## 推荐投递时段
## 投递节奏建议
## 风险提示""",
    },
    {
        "feature": "generate_greeting",
        "name": "招呼语/求职信生成",
        "content": """你是求职沟通顾问，擅长撰写打招呼语与求职信。
请根据岗位 JD 和我的简历摘要，生成合适的打招呼语。

平台：{platform}
岗位 JD：
{jd}

我的简历摘要：
{resume}

输出格式约定：
- Boss 直聘等聊天式平台：输出 1~3 条口语化、真诚的打招呼语，每条不超过 50 字；
- 其他平台：输出一封正式的求职信（150 字以内），突出 2~3 个与岗位最匹配的亮点。""",
    },
    {
        "feature": "follow_up",
        "name": "投递跟进话术",
        "content": """你是求职沟通顾问，擅长撰写投递后的跟进话术。
请根据岗位信息和已投递天数，输出合适的跟进消息建议。

平台：{platform}
岗位信息：
{jd}

已投递天数：{days} 天

输出格式约定（Markdown）：
## 跟进话术（已投递 {days} 天）
给出 2~3 条可直接发送的跟进消息（礼貌、简洁、口语化）。
## 注意事项
提醒跟进频率上限、建议发送时段等。""",
    },
    {
        "feature": "interview_predict",
        "name": "面试问题预测",
        "content": """你是资深面试教练，擅长预测面试问题并给出回答要点。
请根据岗位 JD 和我的简历，预测面试中可能出现的高频问题。

岗位 JD：
{jd}

我的简历：
{resume}

输出格式约定（Markdown）：
## 高频面试题
按出现概率列出 5~8 个问题。
## 回答要点
结合我的简历经历，为每题给出 3 个左右回答要点。
## 加分技巧""",
    },
    {
        "feature": "weekly_report",
        "name": "周度投递复盘报告",
        "content": """你是求职教练，负责生成每周求职复盘报告。
请根据我过去一周的投递、查看、面试记录，输出复盘总结与改进建议。

本周投递记录（投递/查看/面试/结果）：
{jd}

平台分布：{platform}
城市：{city}

输出格式约定（Markdown）：
## 本周总结
## 数据回顾（投递/查看/面试数量与转化率）
## 做得好的地方
## 待改进项
## 下周计划""",
    },
    {
        "feature": "recommend_jobs",
        "name": "岗位智能推荐",
        "content": """你是职业顾问，负责从候选岗位中为用户推荐最匹配的岗位。
请根据我的简历和候选岗位列表，从中挑选出最值得投递的岗位并排序。

我的简历：
{resume}

候选岗位列表（JSON 数组，每条含 title/company/city/salary_raw/description）：
{jobs}

输出格式约定（严格输出 JSON，不要包含其他任何内容）：
{"ranked": [完整保留我输入中匹配岗位的原始对象，按推荐度从高到低排列，最多 {top_n} 条]}""",
    },
    {
        "feature": "salary_analysis",
        "name": "薪资行情分析",
        "content": """你是薪酬分析师，负责解读岗位薪资行情并给出个人定位建议。
请根据关键词与城市的薪资统计数据，输出分析结论。

岗位关键词：{keyword}
城市：{city}

本地已采集岗位的薪资统计（样本数、均值、分位、区间分布）：
{stats}

输出格式约定（Markdown）：
## 薪资概况
## 分布解读
## 与我的匹配定位建议（结合我的期望与经验）
## 谈判提示""",
    },
]

# 默认通用设置（settings 表，key → value 字符串）
DEFAULT_SETTINGS: dict[str, str] = {
    "apply_daily_limit": "20",     # 每日每站投递上限
    "apply_interval_min": "30",    # 两次投递随机间隔下限（秒）
    "apply_interval_max": "90",    # 两次投递随机间隔上限（秒）
    "apply_time_start": "09:00",   # 投递时间窗开始
    "apply_time_end": "21:00",     # 投递时间窗结束
    "boss_daily_limit": "10",      # Boss 每日沟通上限
    "job_retention_days": "90",    # 岗位数据保留天数
    "daily_crawl_time": "08:00",   # 每日定时采集时间
}


def seed_defaults(conn: sqlite3.Connection) -> None:
    """写入默认数据。

    - prompts 表为空时插入默认提示词模板（7 条，参照 §11.4）；
    - 补齐缺失的默认 settings（已存在的 key 不覆盖，避免重置用户修改）。
    """
    count = conn.execute("SELECT COUNT(*) AS c FROM prompts").fetchone()["c"]
    if count == 0:
        for p in DEFAULT_PROMPTS:
            conn.execute(
                "INSERT INTO prompts (feature, name, content, enabled) "
                "VALUES (?, ?, ?, 1)",
                (p["feature"], p["name"], p["content"]),
            )
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )

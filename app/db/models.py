"""数据模型：与 SQLite 表对应的 dataclass 定义。

每个模型提供：
- ``from_row(row)``：从 sqlite3.Row 构造模型实例，row 为 None 时返回字段默认值；
- ``as_dict()``：转换为 json 可序列化的 dict，其中 tags / sites / keywords / cities
  等列表字段以 JSON 字符串形式存储。

字段与设计文档 §8.4（jobs）、§9.3（applications）、§12.1（其余表）保持一致。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, fields
from datetime import datetime

# 在数据库中按 JSON 字符串存储的列表字段
_LIST_JSON_FIELDS = frozenset({"tags", "sites", "keywords", "cities"})


def _now() -> str:
    """返回当前时间的 ISO 字符串（精确到秒）。"""
    return datetime.now().isoformat(timespec="seconds")


def _load_str_list(raw):
    """把数据库中的 JSON 字符串解析为 list；空值/非法值返回空列表。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except (ValueError, TypeError):
        return []


class _ModelBase:
    """dataclass 基类：提供统一的 from_row / as_dict 通用实现。"""

    @classmethod
    def from_row(cls, row: sqlite3.Row | None = None):
        """从 sqlite3.Row 构造模型；row 为 None 时返回字段默认值的实例。"""
        if row is None:
            return cls()
        keys = set(row.keys())
        kwargs = {}
        for f in fields(cls):
            if f.name not in keys:
                continue
            value = row[f.name]
            if f.name in _LIST_JSON_FIELDS:
                value = _load_str_list(value)
            kwargs[f.name] = value
        return cls(**kwargs)

    def as_dict(self) -> dict:
        """转换为 json 可序列化 dict；列表字段以 JSON 字符串存储。"""
        result = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in _LIST_JSON_FIELDS:
                value = json.dumps(value, ensure_ascii=False)
            result[f.name] = value
        return result


@dataclass
class Job(_ModelBase):
    """岗位（对应 jobs 表，字段见设计文档 §8.4）。"""

    id: int | None = None
    source: str = ""
    job_id: str = ""
    title: str = ""
    company: str = ""
    company_size: str = ""
    industry: str = ""
    salary_min: int | None = None
    salary_max: int | None = None
    salary_raw: str = ""
    city: str = ""
    district: str = ""
    experience: str = ""
    education: str = ""
    tags: list = field(default_factory=list)
    description: str = ""
    publish_date: str = ""
    job_url: str = ""
    crawl_time: str = ""
    status: str = "active"
    is_favorite: int = 0
    match_score: float | None = None


@dataclass
class Application(_ModelBase):
    """投递记录（对应 applications 表，字段见设计文档 §9.3）。"""

    id: int | None = None
    job_ref_id: int | None = None
    source: str = ""
    resume_version: str = ""
    applied_at: str = field(default_factory=_now)
    status: str = "submitted"
    note: str = ""


@dataclass
class Resume(_ModelBase):
    """简历版本（对应 resumes 表，见设计文档 §10 / §12.1）。"""

    id: int | None = None
    name: str = ""
    target_title: str = ""
    file_path: str = ""
    content: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


@dataclass
class SearchTask(_ModelBase):
    """搜索任务（对应 search_tasks 表，见设计文档 §8.1 / §12.1）。"""

    id: int | None = None
    name: str = ""
    sites: list = field(default_factory=list)
    keywords: list = field(default_factory=list)
    cities: list = field(default_factory=list)
    salary_min: int | None = None
    salary_max: int | None = None
    enabled: int = 1
    last_run_at: str | None = None


@dataclass
class CrawlRun(_ModelBase):
    """采集运行记录（对应 crawl_runs 表，见设计文档 §8.3 / §12.1）。"""

    id: int | None = None
    task_id: int | None = None
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None
    fetched: int = 0
    inserted: int = 0
    status: str = "running"
    error: str | None = None


@dataclass
class AISettings(_ModelBase):
    """AI 大模型配置（单行，id=1，见设计文档 §11.1 / §12.1）。"""

    id: int = 1
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.7
    enabled: int = 0


@dataclass
class PromptTemplate(_ModelBase):
    """AI 功能提示词模板（对应 prompts 表，见设计文档 §11.2 / §12.1）。"""

    id: int | None = None
    feature: str = ""
    name: str = ""
    content: str = ""
    enabled: int = 1

# -*- coding: utf-8 -*-
"""应用级共享状态（进程内单例）。

职责：
- ``selected_jobs``：岗位列表页勾选、投递中心待投递队列的来源
  （元素为 :class:`JobDict`，本质是 dict，含 job 主键 id、title、company、
  source、job_url、job_id 等字段）；
- ``current_resume_name``：投递时使用的简历版本名（默认 ''，由简历库页选中时设置）。
"""

from __future__ import annotations


class JobDict(dict):
    """兼具 dict 与属性访问的岗位数据容器。

    投递队列中保存的岗位既要以 dict 方式读取（GUI 填表 / JSON 序列化），
    也要支持属性访问（ApplyService 与站点采集器内部使用
    ``getattr(job, "job_url")`` 等读取字段）。
    """

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


class AppState:
    """应用级共享状态（单例）。"""

    _instance: "AppState | None" = None

    def __init__(self) -> None:
        #: 待投递队列：JobDict 列表（含 id/title/company/source/job_url/job_id/description 等）
        self.selected_jobs: list[JobDict] = []
        #: 投递用简历版本名，默认 ''（简历库页选中简历时写入）
        self.current_resume_name: str = ""

    @classmethod
    def get(cls) -> "AppState":
        """获取全局单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------ 岗位队列
    def add_selected(self, job_dict: dict) -> None:
        """加入待投递队列（去重：优先按 job 主键 id，其次按 source+job_id）。"""
        item = JobDict(job_dict or {})
        for existing in self.selected_jobs:
            if existing.get("id") is not None and existing.get("id") == item.get("id"):
                return
            if (item.get("job_id") and existing.get("source") == item.get("source")
                    and existing.get("job_id") == item.get("job_id")):
                return
        self.selected_jobs.append(item)

    def clear_selected(self) -> None:
        """清空待投递队列。"""
        self.selected_jobs.clear()

    def get_selected(self) -> list:
        """返回待投递队列副本（JobDict 列表）。"""
        return list(self.selected_jobs)

    # ------------------------------------------------------------ 简历
    def set_resume(self, name: str) -> None:
        """设置当前投递用简历版本名（空串清空）。"""
        self.current_resume_name = name or ""

    def get_resume(self) -> str:
        """返回当前投递用简历版本名（可能为 ''）。"""
        return self.current_resume_name

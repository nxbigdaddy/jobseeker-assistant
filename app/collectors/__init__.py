"""站点采集适配器包：统一导出基类与公共符号。

其他模块（服务层、GUI）统一从本包导入：

    from app.collectors import Collector, RawJob, SITES, normalize_salary
"""

from .base import Collector, RawJob, SITES, normalize_salary

__all__ = ["Collector", "RawJob", "SITES", "normalize_salary"]

"""服务注册中心 —— 把已实现的站点采集器注册到各服务。

对应设计文档 §6：站点适配器统一接口；新增站点 = 新增适配器类 + 在此注册。
模块在应用启动时调用一次 register_all()；未实现的采集器自动跳过（try/except），
不影响其他站点功能。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register_all() -> dict[str, str]:
    """注册所有已实现的站点采集器，返回 {站点: 状态}。

    状态：'ok'（已注册）/ 'missing'（采集器未实现，跳过）。
    """
    from app.services.auth_service import AuthService
    from app.services.crawl_service import CrawlService
    from app.services.apply_service import ApplyService

    # 站点标识 -> 采集器模块路径（按序注册，全部 try/except）
    COLLECTOR_MODULES = [
        ("51job", "app.collectors.wuyou51", "Wuyou51Collector"),
        ("zhaopin", "app.collectors.zhaopin", "ZhaopinCollector"),
        ("lagou", "app.collectors.lagou", "LagouCollector"),
        ("liepin", "app.collectors.liepin", "LiepinCollector"),
        ("zhipin", "app.collectors.zhipin", "ZhipinCollector"),
    ]

    result: dict[str, str] = {}
    for site, module_name, cls_name in COLLECTOR_MODULES:
        try:
            module = __import__(module_name, fromlist=[cls_name])
            collector_cls = getattr(module, cls_name)
            AuthService.register(site, collector_cls)
            CrawlService.register(site, collector_cls)
            ApplyService.register(site, collector_cls)
            result[site] = "ok"
            logger.info("采集器已注册：%s -> %s.%s", site, module_name, cls_name)
        except Exception as exc:  # noqa: BLE001 —— 采集器未实现/导入失败则跳过
            result[site] = "missing"
            logger.warning("采集器注册跳过：%s（%s: %s）", site, type(exc).__name__, exc)
    return result

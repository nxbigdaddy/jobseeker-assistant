# -*- coding: utf-8 -*-
"""P3 调度验证：SchedulerService 启动/停止/立即触发。"""
import sys
sys.path.insert(0, '.')

from app.scheduler.scheduler import SchedulerService

svc = SchedulerService.get()
status_log = []
svc.set_status_callback(lambda t: status_log.append(t))

print('1. start:', svc.start())
print('2. running:', svc.is_running())
print('3. trigger_now:', svc.trigger_now())
import time
time.sleep(3)
print('4. status callbacks:', status_log[:3])
svc.stop()
print('5. running after stop:', svc.is_running())
print('P3 SCHEDULER OK')

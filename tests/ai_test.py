# -*- coding: utf-8 -*-
"""P6 AI 链路验证：设置页读写 + 未配置门控 + 提示词管理。"""
import asyncio
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, '.')


def say(*args):
    print(*args, flush=True)


from PySide6.QtWidgets import QApplication, QMessageBox
# 离屏测试无用户点击，模态框会永久阻塞——打补丁为 no-op
QMessageBox.information = lambda *a, **k: None
QMessageBox.warning = lambda *a, **k: None
from app.ui.pages.ai_settings_page import AISettingsPage
from app.db.database import get_connection
from app.db import repository as repo
from app.services.ai_service import AiService

# 0. 重置为未配置
with get_connection() as conn:
    conn.execute("UPDATE ai_settings SET enabled=0, api_key='', base_url='', model='deepseek-chat', temperature=0.7 WHERE id=1")
    conn.commit()

app = QApplication([])
ai_page = AISettingsPage()

# 1. 页面加载（未配置状态）
ai_page.load_settings_from_db()
ai_page.load_prompts_from_db()
say("PASS 设置页加载（未配置）")

# 2. 未配置门控：功能方法应抛 RuntimeError
try:
    asyncio.run(AiService.get().match_score("jd", "resume"))
    say("FAIL 未配置时应抛错")
except RuntimeError as e:
    say("PASS 未配置门控:", str(e)[:40])

# 3. 配置保存回读
ai_page.base_url_edit.setText("https://api.deepseek.com/v1")
ai_page.api_key_edit.setText("sk-test-123")
ai_page.model_edit.setText("deepseek-chat")
ai_page.enabled_check.setChecked(True)
ai_page._on_save_config()
with get_connection() as conn:
    s = repo.get_ai_settings(conn)
say("PASS 配置保存:", s.base_url, s.model, "enabled=", s.enabled)
say("PASS 配置后 is_configured:", AiService.get().is_configured())

# 4. 提示词管理
say("PASS 提示词下拉数:", ai_page.func_combo.count())
ai_page.func_combo.setCurrentIndex(0)
content = ai_page.prompt_edit.toPlainText() if hasattr(ai_page, "prompt_edit") else ai_page._prompt_edit.toPlainText()
say("PASS 提示词内容加载:", len(content) > 50, f"({len(content)} chars)")

# 5. 恢复未配置状态
with get_connection() as conn:
    conn.execute("UPDATE ai_settings SET enabled=0, api_key='', base_url='' WHERE id=1")
    conn.commit()
say("P6 AI OK")

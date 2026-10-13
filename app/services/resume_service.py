"""简历库服务 —— 多简历版本管理 + 文本解析。

对应设计文档 §10：
- 简历版本列表 / 详情 / 删除（读写 SQLite resumes 表）；
- 上传 PDF / Word / 纯文本，文件副本归档到 data/resumes/；
- 提取文本供 AI 功能（简历优化、匹配评分等）使用。
"""

from __future__ import annotations

import logging
import shutil
import uuid
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# 支持的上传扩展名
SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".txt", ".md")


class ResumeService:
    """简历库服务（单例）。"""

    _instance: "ResumeService | None" = None

    @classmethod
    def get(cls) -> "ResumeService":
        """单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------ 查询
    def list_resumes(self) -> list:
        """返回全部简历版本（模型对象列表，空表返回 []）。"""
        from app.db.database import get_connection
        from app.db import repository as repo

        try:
            with get_connection() as conn:
                return repo.list_resumes(conn)
        except Exception:  # noqa: BLE001
            logger.exception("读取简历列表失败")
            return []

    def get_resume(self, pk: int):
        """按主键取单个简历，不存在返回 None。"""
        from app.db.database import get_connection
        from app.db import repository as repo

        try:
            with get_connection() as conn:
                return repo.get_resume(conn, pk)
        except Exception:  # noqa: BLE001
            logger.exception("读取简历失败：%s", pk)
            return None

    # ------------------------------------------------------------ 增删改
    def add_resume(self, name: str, target_title: str, file_path: str) -> dict | None:
        """新增简历版本。

        - 把文件副本复制到 data/resumes/（保留扩展名，UUID 命名防冲突）；
        - 提取文本内容（AI 使用）；
        - 写入 resumes 表。
        返回新记录 dict；失败返回 None。
        """
        src = Path(file_path)
        if not src.is_file():
            logger.error("简历文件不存在：%s", file_path)
            return None
        ext = src.suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            logger.error("不支持的简历格式：%s（支持 %s）", ext, ", ".join(SUPPORTED_EXTENSIONS))
            return None

        from app.config.settings import RESUMES_DIR
        from app.db.database import get_connection
        from app.db import repository as repo
        from app.db.models import Resume

        try:
            dest = RESUMES_DIR / f"{uuid.uuid4().hex}{ext}"
            shutil.copy2(src, dest)
            content = self.extract_text(dest) or ""
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            resume = Resume(
                name=name or src.stem,
                target_title=target_title or "",
                file_path=str(dest),
                content=content,
                created_at=now,
                updated_at=now,
            )
            with get_connection() as conn:
                pk = repo.insert_resume(conn, resume)
            logger.info("简历已入库：%s（id=%s）", resume.name, pk)
            return {"id": pk, "name": resume.name, "target_title": resume.target_title,
                    "file_path": str(dest), "created_at": now}
        except Exception:  # noqa: BLE001
            logger.exception("新增简历失败：%s", file_path)
            return None

    def update_resume(self, pk: int, name: str | None = None, target_title: str | None = None) -> bool:
        """更新简历版本名称/目标岗位（内容解析不变）。"""
        from app.db.database import get_connection
        from app.db import repository as repo

        resume = self.get_resume(pk)
        if resume is None:
            return False
        try:
            with get_connection() as conn:
                if name:
                    conn.execute("UPDATE resumes SET name=?, updated_at=? WHERE id=?", (name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), pk))
                if target_title is not None:
                    conn.execute("UPDATE resumes SET target_title=?, updated_at=? WHERE id=?", (target_title, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), pk))
                conn.commit()
            return True
        except Exception:  # noqa: BLE001
            logger.exception("更新简历失败：%s", pk)
            return False

    def delete_resume(self, pk: int) -> bool:
        """删除简历版本（连同文件副本）。"""
        from app.db.database import get_connection
        from app.db import repository as repo

        resume = self.get_resume(pk)
        if resume is None:
            return False
        try:
            with get_connection() as conn:
                repo.delete_resume(conn, pk)
            # 删除文件副本（失败仅警告，不影响记录删除）
            path = Path(resume.file_path)
            if path.is_file():
                try:
                    path.unlink()
                except OSError:
                    logger.warning("简历文件删除失败：%s", path)
            logger.info("简历已删除：%s（id=%s）", resume.name, pk)
            return True
        except Exception:  # noqa: BLE001
            logger.exception("删除简历失败：%s", pk)
            return False

    # ------------------------------------------------------------ 文本提取
    def extract_text(self, file_path: str | Path) -> str:
        """提取简历文本：PDF（pypdf）/ Word（python-docx）/ 纯文本。

        解析库缺失或解析失败时返回空字符串，不抛异常。
        """
        path = Path(file_path)
        ext = path.suffix.lower()
        try:
            if ext == ".pdf":
                from pypdf import PdfReader

                reader = PdfReader(str(path))
                return "\n".join((page.extract_text() or "") for page in reader.pages)
            if ext == ".docx":
                import docx

                doc = docx.Document(str(path))
                return "\n".join(p.text for p in doc.paragraphs)
            if ext in (".txt", ".md"):
                return path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            logger.exception("简历文本解析失败：%s", path)
        return ""

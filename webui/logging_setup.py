"""运行日志：webui logger 落盘（滚动切割），供技术人员排查维护。

日志文件：webui/assets/webui.log（2MB × 3 个滚动备份）。
敏感约束：全项目不得在此 logger 记录 API Key、合作码。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE = Path(__file__).with_name("assets") / "webui.log"

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def get_logger(name: str = "webui") -> logging.Logger:
    """获取项目 logger（首次调用时配置文件 handler，幂等）。"""
    logger = logging.getLogger(name)
    if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(_FORMAT))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

        # uvicorn 的错误日志也进同一文件，方便统一排查
        for uv_name in ("uvicorn", "uvicorn.error"):
            uv_logger = logging.getLogger(uv_name)
            if not any(isinstance(h, RotatingFileHandler) for h in uv_logger.handlers):
                uv_logger.addHandler(handler)
    return logger

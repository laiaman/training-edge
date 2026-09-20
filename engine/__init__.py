# TrainingEdge — FIT parsing + training metrics computation

"""TrainingEdge 核心包。

导入时自动完成：
1. 加载项目根目录 .env（仅填充未定义的环境变量，不覆盖已有值）
2. 配置日志系统：
   - 控制台输出（INFO 级别以上）
   - 滚动日志文件（TRAININGEDGE_LOG_FILE 或 state/training_edge.log，5MB 上限，保留 3 个备份）
   - 日志级别可通过环境变量 TRAININGEDGE_LOG_LEVEL 配置
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv() -> None:
    """加载项目根目录 .env（轻量实现，不引入额外依赖）。

    已存在的环境变量优先（例如 Docker/launchd 注入的值不会被覆盖）。
    """
    env_file = _PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and value:
                os.environ.setdefault(key, value)
    except OSError:
        pass


_load_dotenv()


def _setup_logging() -> None:
    """配置全局日志。"""
    log_level_name = os.environ.get("TRAININGEDGE_LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    # 日志格式: 时间 | 级别 | 模块 | 消息
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 根日志器
    root_logger = logging.getLogger("training_edge")
    root_logger.setLevel(log_level)

    # 避免重复添加 handler（模块被多次导入时）
    if root_logger.handlers:
        return

    # 控制台 handler
    console = logging.StreamHandler()
    console.setLevel(log_level)
    console.setFormatter(fmt)
    root_logger.addHandler(console)

    # 滚动文件 handler（优先 TRAININGEDGE_LOG_FILE，默认 state/training_edge.log）
    log_file_env = os.environ.get("TRAININGEDGE_LOG_FILE")
    if log_file_env:
        log_file = Path(log_file_env).expanduser()
        if not log_file.is_absolute():
            log_file = _PROJECT_ROOT / log_file
    else:
        log_file = _PROJECT_ROOT / "state" / "training_edge.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        str(log_file),
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(fmt)
    root_logger.addHandler(file_handler)


_setup_logging()

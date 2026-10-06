"""应用配置。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("DQDB_PATH", BASE_DIR / "data" / "dqplatform.db"))
CREDENTIAL_DIR = Path(os.environ.get("DQCRED_DIR", BASE_DIR / "data" / "credentials"))

# 分页上限：一次最多能读多少条样本，但无论怎么翻页，读到的永远是结果快照里的行
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50

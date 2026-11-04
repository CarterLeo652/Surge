"""插件改名时通过 SQLite backup 保留旧版数据（包含 WAL）。"""

import os
import sqlite3
from pathlib import Path


def database_path(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "dickgrowerbot.sqlite3"
    legacy = directory.parent / "astrbot_plugin_pinjj" / "pinjj.sqlite3"
    if not target.exists() and legacy.exists():
        temporary = directory / "dickgrowerbot.sqlite3.migrating"
        try:
            with sqlite3.connect(f"{legacy.as_uri()}?mode=ro", uri=True) as source:
                with sqlite3.connect(temporary) as destination:
                    source.backup(destination)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return target

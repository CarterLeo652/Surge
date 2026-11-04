"""创建可上传到 AstrBot 的源码 ZIP，不携带数据库、缓存和开发环境。"""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
out = root / "dist" / "astrbot_plugin_dickgrowerbot-v0.5.0.zip"
out.parent.mkdir(exist_ok=True)
include = [
    "__init__.py",
    "main.py",
    "game.py",
    "maintenance.py",
    "background.py",
    "settings.py",
    "presentation.py",
    "localization.py",
    "data_path.py",
    "locales/zh.yml",
    "UPSTREAM_SOURCE.json",
    "metadata.yaml",
    "logo.png",
    "_conf_schema.json",
    "requirements.txt",
    "README.md",
    "VERIFICATION.md",
    "LICENSE",
    "pyproject.toml",
    ".gitignore",
    "tools/package.py",
    "tests/__init__.py",
    "tests/test_game.py",
    "tests/test_adapter.py",
    "tests/test_upstream.py",
    "tests/test_maintenance.py",
    "tests/test_background.py",
]
with ZipFile(out, "w", compression=ZIP_DEFLATED) as bundle:
    for name in include:
        bundle.write(root / name, f"astrbot_plugin_dickgrowerbot/{name}")
print(out)

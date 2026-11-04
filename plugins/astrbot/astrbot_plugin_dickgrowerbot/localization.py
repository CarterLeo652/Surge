"""直接读取原项目中文语言文件；不改写原句和标点。"""

import re
from pathlib import Path

import yaml

MESSAGES = yaml.safe_load((Path(__file__).parent / "locales" / "zh.yml").read_text())


def tr(key, **values):
    value = MESSAGES
    for part in key.split("."):
        value = value[part]
    if not isinstance(value, str):
        raise TypeError(key)
    return re.sub(r"%\{([^}]+)\}", lambda match: str(values[match[1]]), value)


INVALID_DATA = tr("inline.callback.errors.invalid_data")

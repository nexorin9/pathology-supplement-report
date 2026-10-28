"""测试夹具入口。

把项目根目录放进 ``sys.path``，使 ``import patho_supplement`` 在
``pytest`` 与 ``python -m pytest`` 两种调用方式下都能解析到本项目源码
（源码真源在 ``src/patho_supplement/``，根目录另有同名软链，免安装即可运行）。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"

for path in (str(PROJECT_ROOT), str(SRC_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

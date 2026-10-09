"""
pipeline/ — 题目交付清单里指定的包结构。

这里的 6 个模块是**薄封装**: 真正的实现在 src/(已经过回归与 GT 对账),
本包只负责"题目点名的入口"——模块名、CLI 参数、输出路径(output/)。
这样既满足题目结构, 又不把逻辑复制成两份(避免两份实现漂移)。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
OUT_DIR = ROOT / "output"
# 注意顺序: 仓库根目录必须优先, 否则 `import pipeline` 会被 src/pipeline.py 抢先解析成模块,
# 而不是本包(包装层就失效了)。src/ 追加在末尾, 供 extractor/critic/report_config 等模块用。
for _p in (str(ROOT), str(SRC)):
    if _p not in sys.path:
        sys.path.append(_p)

__all__ = ["ROOT", "SRC", "OUT_DIR"]

"""
app/ — 平台壳(静态报告页 + 一键回归 + 计划中的 FastAPI 服务)

设计原则与仓库一致: 纯本地、零服务依赖、可离线打开; 不重写 src/ 的任何逻辑,
只读 src/ 的产物并把它渲染成人能看懂的一页。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "output"
PROCESSED = ROOT / "data" / "processed"
REPORTS_DIR = ROOT / "data" / "pdfs"

__all__ = ["ROOT", "OUT_DIR", "PROCESSED", "REPORTS_DIR"]

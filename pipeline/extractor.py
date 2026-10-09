"""
pipeline/extractor.py — Extractor Agent(DeepSeek 抽取)

实现: src/extractor.py(页码由代码盖戳; prompt 的商品/单位/守恒式来自报告配置)
用法: python pipeline/extractor.py data/processed/barrick.pages.jsonl
产出: data/processed/extracted.jsonl
"""
import sys
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))    # 让 `import pipeline` 可用
import pipeline._bootstrap                                        # noqa: F401,E402

from pipeline import ROOT                                        # noqa: E402
import extractor as _impl                                        # noqa: E402

call_llm = _impl.call_llm
parse_records = _impl.parse_records
use_config = _impl.use_config
usage_summary = _impl.usage_summary


def system_prompt() -> str:
    return _impl.SYSTEM_PROMPT


def extract_pages(jsonl_path) -> int:
    """调用 src/extractor.py 抽取(子进程执行, 避免包名/模块名互相遮蔽)。"""
    return subprocess.run([sys.executable, str(ROOT / "src" / "extractor.py"),
                           str(jsonl_path)], cwd=str(ROOT)).returncode


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("用法: python pipeline/extractor.py <pages.jsonl>")
    sys.exit(extract_pages(sys.argv[1]))

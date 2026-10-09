"""
pipeline/critic.py — CriticMaster Agent(GLM 审计评分)

实现: src/critic.py(评分 1-10; 物理规则与单位随报告配置切换)
用法: python pipeline/critic.py data/processed/barrick.pages.jsonl data/processed/extracted.jsonl
产出: data/processed/critiques.jsonl(每行: page/score/issues/verdict)
"""
import sys
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline._bootstrap                                        # noqa: F401,E402

from pipeline import ROOT                                        # noqa: E402
import critic as _impl                                           # noqa: E402

call_glm = _impl.call_glm
parse_critique = _impl.parse_critique
use_config = _impl.use_config
usage_summary = _impl.usage_summary


def critique_pages(pages_jsonl, extracted_jsonl) -> int:
    """调用 src/critic.py 审计(子进程执行, 避免包名/模块名互相遮蔽)。"""
    return subprocess.run([sys.executable, str(ROOT / "src" / "critic.py"),
                           str(pages_jsonl), str(extracted_jsonl)],
                          cwd=str(ROOT)).returncode


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("用法: python pipeline/critic.py <pages.jsonl> <extracted.jsonl>")
    sys.exit(critique_pages(sys.argv[1], sys.argv[2]))

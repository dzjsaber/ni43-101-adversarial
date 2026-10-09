"""
pipeline/replay_evolution.py — 失败史复跑(题目第 4 项)

实现: src/replay_evolution.py —— 读 data/evolution.jsonl 的失败史, 把炼化出的规则
作为附加规则重跑一遍, 输出"改进前 vs 改进后"的评分与确定性违规对比。
用法: python pipeline/replay_evolution.py [--pages 17,192]
产出: data/processed/replay_ab.json(并同步一份 output/replay_ab.json)
"""
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline._bootstrap                                        # noqa: F401,E402

from pipeline import ROOT, OUT_DIR                               # noqa: E402
import report_config                                             # noqa: E402
import replay_evolution as _impl                                 # noqa: E402


def _sync() -> None:
    src = ROOT / "data" / "processed" / "replay_ab.json"
    if src.exists():
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, OUT_DIR / "replay_ab.json")


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    # 子进程执行: src/replay_evolution.py 内部 `import pipeline` 需要解析到 src/pipeline.py,
    # 若在本进程内委托, 会被同名的 pipeline/ 包抢走。
    code = subprocess.run([sys.executable, str(ROOT / "src" / "replay_evolution.py")] + argv,
                          cwd=str(ROOT)).returncode
    _sync()
    return code


if __name__ == "__main__":
    report_config.setup_stdio()
    sys.exit(main())

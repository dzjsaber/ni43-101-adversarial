"""
pipeline/_bootstrap.py — 统一路径引导(被同目录的入口模块 import)

要解决的问题: 直接运行 `python pipeline/extractor.py` 时, sys.path[0] 是 pipeline/ 目录本身,
于是脚本里的 `import extractor` 会解析成**同名的包装文件自己**(同名遮蔽), 委托给 src 的实现
就会失败 —— replay_evolution 甚至因此无限递归。

做法: 把脚本自身目录从 sys.path 移除, 再把 仓库根目录 与 src/ 加进来。
仓库根必须优先于 src/, 否则 `import pipeline` 会被 src/pipeline.py 抢走。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = ROOT / "src"

sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != HERE]
for _p in (str(SRC), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)          # 逆序插入 => 最终 ROOT 在前, SRC 在后

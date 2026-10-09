"""
pipeline/evolution_log.py — 进化日志(题目第 4 项: 失败/降级 case 自动 append)

权威文件: data/evolution.jsonl(只 append, 不改写历史)
本模块提供: 追加 / 读取 / 导出到题目要求的 output/evolution.jsonl
用法:
  python pipeline/evolution_log.py --tail 10      # 看最近 10 条
  python pipeline/evolution_log.py --export       # 导出 output/evolution.jsonl 快照
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline._bootstrap                                        # noqa: F401,E402

from pipeline import ROOT, OUT_DIR                               # noqa: E402
import report_config                                             # noqa: E402

LOG = ROOT / "data" / "evolution.jsonl"


def append(entry: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load() -> list:
    if not LOG.exists():
        return []
    return [json.loads(l) for l in LOG.read_text(encoding="utf-8").splitlines() if l.strip()]


def export(path: Path = None) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = path or (OUT_DIR / "evolution.jsonl")
    shutil.copyfile(LOG, path)
    return path


if __name__ == "__main__":
    report_config.setup_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--tail", type=int, default=0)
    ap.add_argument("--export", action="store_true")
    a = ap.parse_args()
    events = load()
    print(f"{LOG.name}: {len(events)} 条事件(append-only)")
    for e in (events[-a.tail:] if a.tail else []):
        print(f"  {e.get('ts')} page={e.get('page')} kind={e.get('kind')}")
    if a.export:
        print("已导出 ->", export())
    sys.exit(0)

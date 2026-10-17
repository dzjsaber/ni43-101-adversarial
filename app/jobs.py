"""
app/jobs.py — 任务执行器: 单 worker 线程 + SQLite 页级状态

为什么是线程而不是 celery/redis: 耗时几乎全在 LLM 网络等待上, 单机单 worker 足够;
引入消息队列只会增加部署面。串行执行也让"每页状态落库"足够简单。

断点续跑的关键在数据模型而不是控制流:
  pages 表按页记 pending/running/done/abstain/failed;
  worker 只跑 pending/failed 的页, 已完成页直接跳过。
"""
import json
import os
import queue
import shutil
import sys
import threading
import traceback
from pathlib import Path

from app import db

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))          # 让 import pipeline 解析到 src/pipeline.py

import evaluate                                                    # noqa: E402
import guards                                                      # noqa: E402
import preprocess                                                  # noqa: E402
import report_config                                               # noqa: E402
import usage_log                                                   # noqa: E402
import pipeline as core_pipeline                                    # noqa: E402

JOB_DIR = ROOT / "output" / "jobs"
MIN_SCORE = 10
MAX_PAGES = 12

_Q = queue.Queue()
_ACTIVE = set()
_LOCK = threading.Lock()
_WORKER = None


def job_dir(job_id: str) -> Path:
    return JOB_DIR / job_id


def enqueue(job_id: str) -> bool:
    with _LOCK:
        if job_id in _ACTIVE:
            return False
        _ACTIVE.add(job_id)
    _Q.put(job_id)
    return True


def start_worker() -> None:
    global _WORKER
    if _WORKER and _WORKER.is_alive():
        return
    _WORKER = threading.Thread(target=_loop, name="ni43-worker", daemon=True)
    _WORKER.start()


def _loop() -> None:
    while True:
        job_id = _Q.get()
        try:
            run_job(job_id)
        except Exception:                       # 兜底: 单个 job 崩了不能带走 worker
            traceback.print_exc()
        finally:
            with _LOCK:
                _ACTIVE.discard(job_id)
            _Q.task_done()


def _locate_pages(job_id: str, jdir: Path = None) -> list:
    """PDF -> 候选页。复用 src/preprocess.py, 但把产物收进 job 目录, 不污染全局产物。"""
    jdir = jdir or job_dir(job_id)
    pdf = jdir / f"{job_id}.pdf"
    preprocess.main(pdf)                       # 输出 data/processed/<job_id>.pages.jsonl
    produced = ROOT / "data" / "processed" / f"{job_id}.pages.jsonl"
    target = jdir / "pages.jsonl"
    if produced.exists():
        os.replace(produced, target)           # 移动而不是删除, 失败可手工找回
    if not target.exists():
        raise RuntimeError("PDF 预处理未产出 pages.jsonl")
    return [json.loads(l) for l in target.read_text(encoding="utf-8").splitlines() if l.strip()]


def _usage_for_page(entries: list, page: int) -> dict:
    calls = [e for e in entries if e.get("page") == page]
    return {"calls": len(calls),
            "tokens": sum(int(e.get("total_tokens") or 0) for e in calls),
            "seconds": round(sum(float(e.get("seconds") or 0) for e in calls), 2)}


def _refresh_usage(job_id: str) -> dict:
    usage_path = job_dir(job_id) / "usage.jsonl"
    entries = usage_log.load(usage_path) if usage_path.exists() else []
    total = usage_log.summarize(entries)["total"] if entries else {
        "calls": 0, "seconds": 0, "total_tokens": 0}
    db.update_job(job_id, usage_calls=total["calls"], usage_tokens=total["total_tokens"],
                  usage_seconds=round(float(total["seconds"]), 2))
    return {"entries": entries, "total": total}


def _finalize(job_id: str, pages: list) -> None:
    """交付前分类: 聚合行/摘要组/跨页重复必须由代码判定, 这和 CLI 主链路保持同一口径。"""
    rows = db.get_results(job_id)
    if not rows:
        return
    stamped = guards.classify_records([r["record"] for r in rows], pages)
    for row, rec in zip(rows, stamped):
        db.update_result_meta(job_id, row["page_no"], row["seq"],
                              rec.get("record_class"),
                              rec.get("duplicate_of_page"))


def run_job(job_id: str) -> None:
    job = db.get_job(job_id)
    if not job:
        return
    jdir = job_dir(job_id)
    jdir.mkdir(parents=True, exist_ok=True)
    db.update_job(job_id, status="running", started_at=db.now(), error=None)
    db.add_event(job_id, "job_start")

    try:
        pages_path = jdir / "pages.jsonl"
        if not pages_path.exists():
            db.add_event(job_id, "locating", detail={"pdf": f"{job_id}.pdf"})
            pages = _locate_pages(job_id, jdir)
        else:
            pages = [json.loads(l) for l in
                     pages_path.read_text(encoding="utf-8").splitlines() if l.strip()]

        eligible = sorted([p for p in pages if p["score"] >= MIN_SCORE],
                          key=lambda p: (-p["score"], p["page"]))[:int(job.get("max_pages") or MAX_PAGES)]
        db.insert_pages(job_id, [{"page": p["page"], "score": p["score"]} for p in eligible])
        db.update_job(job_id, total_pages=len(eligible))

        cfg = report_config.load(jdir / f"{job_id}.pdf")
        core_pipeline.use_config(cfg)
        ds_key = report_config.api_key("DEEPSEEK_API_KEY")
        zp_key = report_config.api_key("ZHIPU_API_KEY")

        usage_path = jdir / "usage.jsonl"
        # 只在首次运行时创建空文件; 续跑必须保留历史调用明细, 否则成本统计会丢页
        if not usage_path.exists():
            usage_path.write_text("", encoding="utf-8")
        os.environ["NI43_USAGE_LOG"] = str(usage_path)

        rows = {r["page_no"]: r for r in db.get_pages(job_id)}
        for page in eligible:
            pg = page["page"]
            row = rows.get(pg) or {}
            if row.get("status") in ("done", "abstain"):
                db.add_event(job_id, "page_skipped", pg, {"reason": "已完成, 不重复调用"})
                continue
            db.update_page(job_id, pg, status="running", started_at=db.now(),
                           error=None, verdict=None)
            try:
                res = core_pipeline.run_page(page, ds_key, zp_key)
                recs = res.get("recs") or []
                for r in recs:
                    r["source_page"] = pg
                    r["pipeline_verdict"] = res["verdict"]
                    r["pipeline_rounds"] = res["rounds"]
                    r["critic_score"] = res["score"]
                db.add_results(job_id, pg, recs)
                u = _usage_for_page(usage_log.load(usage_path), pg)
                status = "done" if str(res["verdict"]).startswith("ACCEPT") else "abstain"
                db.update_page(job_id, pg, status=status, verdict=res["verdict"],
                               rounds=res["rounds"], critic_score=res["score"],
                               n_records=len(recs), finished_at=db.now(),
                               usage_calls=u["calls"], usage_tokens=u["tokens"],
                               usage_seconds=u["seconds"])
                db.add_event(job_id, "page_done", pg,
                             {"verdict": res["verdict"], "rounds": res["rounds"],
                              "score": res["score"], "records": len(recs),
                              "issues": (res.get("issues") or [])[:5]})
            except Exception as e:             # 页级失败不拖垮整个 job, 留给 resume
                db.update_page(job_id, pg, status="failed", error=str(e),
                               finished_at=db.now())
                db.add_event(job_id, "page_failed", pg, {"error": str(e)})

        _refresh_usage(job_id)
        _finalize(job_id, pages)
        counts = db.count_pages(job_id)
        failed = counts.get("failed", 0)
        done = counts.get("done", 0) + counts.get("abstain", 0)
        db.update_job(job_id, status="done" if done else "failed",
                      done_pages=counts.get("done", 0),
                      abstain_pages=counts.get("abstain", 0),
                      failed_pages=failed, finished_at=db.now())
        db.add_event(job_id, "job_done", detail={"pages": counts})
    except Exception as e:
        db.update_job(job_id, status="failed", error=str(e), finished_at=db.now())
        db.add_event(job_id, "job_failed", detail={"error": str(e)})
        raise
    finally:
        os.environ.pop("NI43_USAGE_LOG", None)


def recover_on_start() -> list:
    """服务启动时: 把上次未跑完的 job 重新入队(worker 会跳过已完成页)。"""
    db.mark_orphaned()
    resumed = []
    for j in db.list_jobs():
        if j["status"] in ("queued", "interrupted"):
            if enqueue(j["id"]):
                resumed.append(j["id"])
    return resumed


def save_upload(job_id: str, filename: str, stream) -> Path:
    jdir = job_dir(job_id)
    jdir.mkdir(parents=True, exist_ok=True)
    dest = jdir / f"{job_id}.pdf"
    with dest.open("wb") as f:
        shutil.copyfileobj(stream, f, length=1024 * 1024)
    return dest


def gt_diff(job: dict, records: list) -> dict:
    """结果页的 GT 对账: 直接复用 evaluate 的匹配/字段判定, 不另写一套。"""
    gt_path = job.get("gt_path")
    if not gt_path or not Path(gt_path).exists():
        return {"available": False}
    cfg = report_config.load(job_dir(job["id"]) / f"{job['id']}.pdf")
    gt_rows, skipped, _ = evaluate.build_gt_rows(evaluate.load_gt(Path(gt_path)))
    pred_rows = evaluate.as_pred_rows(records, cfg)
    details, matched, used = evaluate.diff_pairs(gt_rows, pred_rows, cfg)
    field_total = sum(1 for d in details for _ in d["fields"].values())
    field_correct = sum(1 for d in details for c in d["fields"].values() if c["pass"])
    return {"available": True, "gt_records": len(gt_rows), "skipped": skipped,
            "matched": len(matched), "field_total": field_total,
            "field_correct": field_correct,
            "field_accuracy": round(field_correct / field_total, 4) if field_total else None,
            "perfect": sum(1 for d in details if d["all_pass"]),
            "extra": len(pred_rows) - len(used), "details": details}

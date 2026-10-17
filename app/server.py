"""
app/server.py — FastAPI 应用(单机、只监听 127.0.0.1)

范围刻意的窄: 上传 / 建 job / 查进度 / 查结果 / 续跑。
不做鉴权、不做并发、不做 SSE —— 单机自用, 轮询足够。
"""
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import db, jobs

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
MAX_BYTES = 50 * 1024 * 1024
PDF_MAGIC = b"%PDF-"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    resumed = jobs.recover_on_start()
    jobs.start_worker()
    if resumed:
        print(f"[jobs] 续跑 {len(resumed)} 个未完成任务: {resumed}")
    yield


app = FastAPI(title="NI 43-101 对抗式抽取平台", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", media_type="text/html; charset=utf-8")


@app.get("/api/health")
def health():
    return {"status": "ok",
            "worker": bool(jobs._WORKER and jobs._WORKER.is_alive()),
            "keys": {"DEEPSEEK_API_KEY": bool(os.environ.get("DEEPSEEK_API_KEY")),
                     "ZHIPU_API_KEY": bool(os.environ.get("ZHIPU_API_KEY"))},
            "db": db.DB_PATH.exists()}


@app.post("/api/jobs")
async def create_job(file: UploadFile = File(...),
                     gt: UploadFile = File(None),
                     max_pages: int = Form(12)):
    if max_pages < 1 or max_pages > 50:
        raise HTTPException(400, "max_pages 必须在 1..50")
    job_id = uuid.uuid4().hex[:12]
    jdir = jobs.job_dir(job_id)
    jdir.mkdir(parents=True, exist_ok=True)
    dest = jdir / f"{job_id}.pdf"
    total = 0
    too_large = False
    with dest.open("wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                too_large = True
                break
            out.write(chunk)
    if too_large:
        dest.unlink(missing_ok=True)
        raise HTTPException(413, "PDF 超过 50MB 上限")
    with dest.open("rb") as f:
        magic = f.read(5)
    if magic != PDF_MAGIC:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "只接受 PDF 文件(缺少 %PDF- 文件头)")

    gt_path = None
    if gt is not None:
        raw = await gt.read()
        try:
            obj = json.loads(raw.decode("utf-8"))
            if not isinstance(obj, (dict, list)):
                raise ValueError
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            dest.unlink(missing_ok=True)
            raise HTTPException(400, "GT 必须是 UTF-8 JSON(数组或含 records 的对象)")
        gt_path = jdir / "gt.json"
        gt_path.write_bytes(raw)

    db.create_job(job_id, os.path.basename(file.filename or "input.pdf"),
                  gt_path=gt_path, max_pages=max_pages)
    jobs.enqueue(job_id)
    return {"id": job_id, "status": "queued"}


@app.get("/api/jobs")
def list_jobs():
    return {"jobs": db.list_jobs()}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    pages = db.get_pages(job_id)
    counts = db.count_pages(job_id)
    return {"job": job, "pages": pages, "counts": counts}


@app.get("/api/jobs/{job_id}/results")
def get_results(job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    rows = db.get_results(job_id)
    records = []
    for r in rows:
        rec = r["record"]
        rec["record_class"] = r.get("record_class")
        if r.get("duplicate_of_page"):
            rec["duplicate_of_page"] = r["duplicate_of_page"]
        records.append(rec)
    pages = db.get_pages(job_id)
    abstain = [p for p in pages if p["status"] == "abstain"]
    return {"job": job, "pages": pages, "records": records,
            "abstain": abstain, "events": db.get_events(job_id),
            "gt": jobs.gt_diff(job, records)}


@app.post("/api/jobs/{job_id}/resume")
def resume(job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    if job["status"] == "running":
        raise HTTPException(409, "任务正在运行")
    if job["status"] == "done" and not job.get("failed_pages"):
        raise HTTPException(400, "任务已完成, 无需续跑")
    db.update_job(job_id, status="queued", error=None, finished_at=None)
    db.add_event(job_id, "job_resume")
    jobs.enqueue(job_id)
    return {"id": job_id, "status": "queued"}


@app.exception_handler(Exception)
async def unhandled(_request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": str(exc)})

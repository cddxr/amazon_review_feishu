from pathlib import Path
import json

from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel

from review_incremental_only_new import run_once

app = FastAPI()
import shutil
from pathlib import Path

@app.post("/reset-data")
def reset_data():
    folder = Path("output")

    if folder.exists():
        shutil.rmtree(folder)

    folder.mkdir(exist_ok=True)

    return {"status": "ok", "message": "所有历史数据已清空"}

class ReviewRequest(BaseModel):
    asin: str
    mode: str = "max"
    translate_mode: str = "full"


def process_review_job(req: ReviewRequest):
    run_once(
        asin=req.asin,
        mode=req.mode,
        translate_mode=req.translate_mode,
        output_dir="output",
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/run-review-check")
def run_review_check(req: ReviewRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(process_review_job, req)
    return {
        "status": "accepted",
        "message": "任务已接收，后台处理中"
    }


@app.get("/latest-result")
def latest_result(asin: str):
    asin = asin.strip().upper()
    file_path = Path("output") / f"reviews_{asin}_last_result.json"

    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"未找到 {asin} 的最新结果")

    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取结果失败: {e}")


@app.get("/latest-result-flat")
def latest_result_flat(asin: str):
    asin = asin.strip().upper()
    file_path = Path("output") / f"reviews_{asin}_last_result.json"

    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"未找到 {asin} 的最新结果")

    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取结果失败: {e}")

    # 👇关键：没有新评论就直接返回空
    if data.get("new_count", 0) == 0:
        return {}

    new_reviews = data.get("new_reviews", [])
    first_review = new_reviews[0] if new_reviews else {}

    return {
        "asin": data.get("asin"),
        "scraped_at": data.get("scraped_at"),
        "current_total": data.get("current_total", 0),
        "new_count": data.get("new_count", 0),

        "title": first_review.get("Title", ""),
        "content": first_review.get("Text", ""),
        "title_zh": first_review.get("Title_zh", ""),
        "content_zh": first_review.get("Text_zh", "")
    }
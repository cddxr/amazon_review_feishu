from pathlib import Path
import json
import shutil

from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel

from review_incremental_only_new import run_once

app = FastAPI()


# ================= 请求体 =================
class ReviewRequest(BaseModel):
    asin: str
    mode: str = "max"
    translate_mode: str = "full"


# ================= 后台任务 =================
def process_review_job(req: ReviewRequest):
    run_once(
        asin=req.asin,
        mode=req.mode,
        translate_mode=req.translate_mode,
        output_dir="output",
    )


# ================= 健康检查 =================
@app.get("/health")
def health():
    return {"status": "ok"}


# ================= 触发抓取（异步） =================
@app.post("/run-review-check")
def run_review_check(req: ReviewRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(process_review_job, req)
    return {
        "status": "accepted",
        "message": "任务已接收，后台处理中"
    }


# ================= 扁平接口（单条） =================
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

    # 没有新增 → 返回空（防止飞书写空数据）
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


# ================= ⭐循环接口（核心🔥） =================
@app.get("/latest-result-loop")
def latest_result_loop(asin: str):
    asin = asin.strip().upper()
    file_path = Path("output") / f"reviews_{asin}_last_result.json"

    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"未找到 {asin} 的最新结果")

    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取失败: {e}")

    new_reviews = data.get("new_reviews", [])

    # 没有新增 → 返回空列表（飞书不会执行循环）
    if not new_reviews:
        return {"list": []}

    result_list = []

    for r in new_reviews:
        result_list.append({
            "asin": data.get("asin"),
            "scraped_at": data.get("scraped_at"),
            "current_total": data.get("current_total", 0),
            "new_count": data.get("new_count", 0),

            "title": r.get("Title", ""),
            "content": r.get("Text", ""),
            "title_zh": r.get("Title_zh", ""),
            "content_zh": r.get("Text_zh", "")
        })

    return {
        "list": result_list
    }


# ================= 清空数据（可选） =================
@app.post("/reset-data")
def reset_data():
    folder = Path("output")

    if folder.exists():
        shutil.rmtree(folder)

    folder.mkdir(exist_ok=True)

    return {"status": "ok", "message": "所有历史数据已清空"}
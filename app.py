from pathlib import Path
import json

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from review_incremental_only_new import run_once

app = FastAPI()


class ReviewRequest(BaseModel):
    asin: str
    mode: str = "max"
    translate_mode: str = "full"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/run-review-check")
def run_review_check(req: ReviewRequest):
    """
    真正执行抓取任务的慢接口
    可用于你自己手动触发，或者后面接定时服务
    """
    result = run_once(
        asin=req.asin,
        mode=req.mode,
        translate_mode=req.translate_mode,
        output_dir="output",
    )
    return {
        "status": "success",
        "message": "抓取完成",
        "result": result,
    }


@app.get("/latest-result")
def latest_result(asin: str):
    """
    给飞书读取的快接口
    只读取本地已经保存好的 last_result json
    """
    asin = asin.strip().upper()
    file_path = Path("output") / f"reviews_{asin}_last_result.json"

    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"未找到 {asin} 的最新结果")

    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取结果失败: {e}")
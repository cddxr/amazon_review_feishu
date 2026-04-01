from fastapi import FastAPI
from pydantic import BaseModel
from review_incremental_only_new import run_once

app = FastAPI()


class ReviewRequest(BaseModel):
    asin: str
    mode: str = "max"
    translate_mode: str = "none"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/run-review-check")
def run_review_check(req: ReviewRequest):
    result = run_once(
        asin=req.asin,
        mode=req.mode,
        translate_mode=req.translate_mode,
        output_dir="output",
    )
    return result
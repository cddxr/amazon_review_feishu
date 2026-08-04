# Amazon Most Recent 评论抓取

脚本使用 Selenium 打开 Amazon 的 `portal/customer-reviews/{ASIN}` Most recent
评论区，点击 `Show 10 more reviews` 加载更多评论，并输出 CSV、JSON 和 XLSX。
评论的英文标题和正文会通过 GoogleTranslator 翻译为简体中文，分别保存到
`title_zh` 和 `body_zh` 字段。

## 配置 ASIN

打开 `most_recent_review.py`，修改顶部的字典：

```python
ASIN_MAP = {
    "B0D75PSYZV": "商品备注名 1",
    "B0DGV8M1HH": "商品备注名 2",
}
```

字典左边是 10 位 ASIN，右边是写入 Excel 的商品备注名。程序会按字典顺序逐个
抓取。

## 安装和运行

```powershell
cd E:\code_data\vscode\amazon\amazon_review_feishu\most_recent_review
py -m pip install -r requirements.txt
py most_recent_review.py
```

ChromeDriver 默认由 Selenium Manager 自动匹配。如果需要指定本地驱动：

```powershell
$env:CHROMEDRIVER_PATH = "C:\path\to\chromedriver.exe"
py most_recent_review.py
```

浏览器登录状态保存在 `.chrome-profile`，生成的 Excel 位于 `output` 目录。
程序打开每个商品的页面后会等待确认；若需要登录，请先在浏览器中完成登录，再回到
终端按 Enter。

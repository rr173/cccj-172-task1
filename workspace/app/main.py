"""FastAPI 入口：数据质量规则版本化执行与回溯裁决平台。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from . import db
from .routers import credentials, rules, workbench

app = FastAPI(
    title="数据质量规则版本化执行与回溯裁决平台",
    version="1.0.0",
    description=(
        "固定快照发布字段、约束与跨表规则；执行器记录失败样本定位与规则版本；"
        "规则升级可回放历史快照但只形成修订版；豁免绑定精确样本与规则摘要；"
        "分页只读固定结果快照；签发可下载质量凭据。"
    ),
)

app.include_router(rules.router)
app.include_router(workbench.router)
app.include_router(credentials.router)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> str:
    return """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>DQ Adjudication Platform</title></head>
<body style="font-family:system-ui;max-width:900px;margin:2rem auto;line-height:1.6">
<h1>数据质量规则版本化执行与回溯裁决平台</h1>
<ul>
<li><a href="/docs">API 文档 / Swagger UI</a></li>
<li><a href="/redoc">ReDoc</a></li>
</ul>
<h2>核心不变量</h2>
<ol>
<li>数据快照不可变；数据修复派生新快照。</li>
<li>发布判断执行即冻结；规则升级回放历史快照只产生 <code>replay</code> 修订版，不改写当时判断。</li>
<li>每条失败记录精确样本定位（表 + 稳定行键 + 列）与规则版本/摘要。</li>
<li>豁免绑定精确样本 × 规则摘要；规则升级后摘要改变，旧豁免不覆盖新规则。</li>
<li>分页只读取固定结果快照，行序由 seq 固定，与后续豁免/回放隔离。</li>
<li>质量凭据带封条 SHA-256，可下载、可离线验真。</li>
</ol>
</body></html>"""


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}

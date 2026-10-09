"""FastAPI 入口。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import settings
from .routers import api
from .services.tasks import manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("mediahub")

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    db.init_db()
    await manager.start()
    log.info("MediaHub 启动完成 | 数据目录 %s | 音频格式 %s",
             settings.data_dir, settings.audio_format)
    yield
    await manager.stop()
    log.info("MediaHub 已停止")


app = FastAPI(
    title="MediaHub",
    description="跨平台（小红书 / 抖音 / YouTube）视频转音频播放器",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
)

app.include_router(api.router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index():
    html = STATIC_DIR / "index.html"
    if not html.exists():
        return JSONResponse({"detail": "前端资源缺失"}, status_code=500)
    return FileResponse(html)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return JSONResponse({}, status_code=204)

"""login-agent 的 HTTP 入口。

给主服务（media-hub）用的极简接口：
    GET    /health                      健康检查
    GET    /platforms                   支持的平台
    POST   /session/{platform}          创建扫码会话，返回二维码(data URL)
    GET    /session/{platform}/{sid}    轮询状态，成功后带 cookie
    DELETE /session/{platform}/{sid}    主动取消
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from .browser import pool
from .platforms import PLATFORMS
from .sessions import manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("login-agent")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("login-agent 启动，预加载 chromium…")
    try:
        await pool.start()
    except Exception as exc:  # noqa: BLE001
        # 预加载失败不致命：真正扫码时会再试一次，并返回明确错误
        log.warning("chromium 预加载失败（首次扫码时会重试）：%s", exc)
    yield
    await manager.shutdown()
    await pool.stop()
    log.info("login-agent 已退出")


app = FastAPI(title="MediaHub Login Agent", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "browser": pool.ready,
        "platforms": list(PLATFORMS.keys()),
    }


@app.get("/platforms")
async def list_platforms() -> list[dict]:
    return [
        {"key": p.key, "label": p.label, "hint": p.hint, "ttl": p.ttl}
        for p in PLATFORMS.values()
    ]


@app.post("/session/{platform}")
async def create_session(platform: str) -> dict:
    spec = PLATFORMS.get(platform)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"不支持的平台：{platform}")
    try:
        return await manager.create(spec)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("创建扫码会话失败")
        raise HTTPException(status_code=500, detail=f"创建扫码会话失败：{exc}") from exc


@app.get("/session/{platform}/{session_id}")
async def poll_session(platform: str, session_id: str) -> dict:
    data = await manager.poll(platform, session_id)
    if data is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    return data


@app.delete("/session/{platform}/{session_id}")
async def drop_session(platform: str, session_id: str) -> dict:
    ok = await manager.drop(platform, session_id)
    return {"ok": ok}

"""本地冒烟测试：不依赖外部网络，验证应用能起来、各接口能正常响应。"""
import os
import sys
import tempfile

tmp = tempfile.mkdtemp(prefix="mediahub-smoke-")
os.environ["DATA_DIR"] = tmp
os.environ["SEARCH_CACHE_TTL"] = "0"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -> " + str(extra)) if extra and not cond else ""))


with TestClient(app) as client:
    print("== 基础接口 ==")
    r = client.get("/api/health")
    check("GET /api/health", r.status_code == 200 and r.json().get("ok") is True, r.text[:200])
    print("        tools:", r.json().get("tools"))

    r = client.get("/api/platforms")
    plats = {p["name"]: p for p in r.json().get("items", [])}
    check("GET /api/platforms", r.status_code == 200 and len(plats) == 3, r.text[:200])
    for name in ("xhs", "douyin", "youtube"):
        p = plats.get(name, {})
        print(f"        {name:8s} available={p.get('available')} reason={p.get('reason')}")

    r = client.get("/api/stats")
    check("GET /api/stats", r.status_code == 200 and "tracks" in r.json(), r.text[:200])

    print("== 静态资源 ==")
    r = client.get("/")
    check("GET / (index.html)", r.status_code == 200 and "MediaHub" in r.text, r.status_code)
    for asset in ("/static/style.css", "/static/app.js"):
        r = client.get(asset)
        check("GET " + asset, r.status_code == 200 and len(r.text) > 500, r.status_code)

    print("== 收藏 / 歌单 / 历史 ==")
    r = client.get("/api/favorites")
    check("GET /api/favorites", r.status_code == 200 and r.json()["items"] == [], r.text[:200])

    r = client.post("/api/playlists", json={"name": "测试歌单"})
    check("POST /api/playlists", r.status_code == 200, r.text[:200])
    pid = r.json().get("id")

    r = client.get("/api/playlists")
    check("GET /api/playlists", r.status_code == 200 and any(p["name"] == "测试歌单" for p in r.json()["items"]))

    r = client.get("/api/history")
    check("GET /api/history", r.status_code == 200)

    r = client.delete(f"/api/playlists/{pid}")
    check("DELETE /api/playlists/{id}", r.status_code == 200)

    print("== 曲库写入 + 流式播放（伪造一条 ready 曲目）==")
    from app import db
    from app.config import settings

    audio_dir = settings.cache_dir / "youtube"
    audio_dir.mkdir(parents=True, exist_ok=True)
    payload = bytes(range(256)) * 400  # 102400 字节假音频
    fake = audio_dir / "smoke-test.mp3"
    fake.write_bytes(payload)

    tid = db.upsert_track({
        "platform": "youtube", "source_id": "smoke123",
        "title": "冒烟测试音频", "author": "tester", "duration": 42,
        "source_url": "https://youtube.com/watch?v=smoke123",
        "status": "ready", "audio_path": "youtube/smoke-test.mp3",
    })
    check("upsert_track 返回 id", bool(tid), tid)

    r = client.get(f"/api/track/{tid}")
    check("GET /api/track/{id}", r.status_code == 200 and r.json()["title"] == "冒烟测试音频", r.text[:200])

    r = client.get(f"/api/stream/{tid}")
    check("GET /api/stream 完整请求", r.status_code == 200 and len(r.content) == len(payload),
          f"{r.status_code} {len(r.content)}")

    r = client.get(f"/api/stream/{tid}", headers={"Range": "bytes=100-199"})
    check("GET /api/stream Range 206", r.status_code == 206 and len(r.content) == 100,
          f"{r.status_code} {len(r.content)}")
    check("Content-Range 正确", r.headers.get("content-range") == f"bytes 100-199/{len(payload)}",
          r.headers.get("content-range"))

    r = client.get(f"/api/stream/{tid}", headers={"Range": "bytes=999999-"})
    check("Range 越界返回 416", r.status_code == 416, r.status_code)

    r = client.get(f"/api/download/{tid}")
    check("GET /api/download/{id}", r.status_code == 200 and len(r.content) == len(payload))

    print("== 收藏流程 ==")
    r = client.post(f"/api/favorites/{tid}")
    check("POST /api/favorites/{id}", r.status_code == 200)
    r = client.get("/api/favorites")
    check("收藏出现在列表", len(r.json()["items"]) == 1 and r.json()["items"][0]["favorited"] is True)
    r = client.get("/api/tracks?status=ready")
    check("GET /api/tracks?status=ready", r.status_code == 200 and len(r.json()["items"]) >= 1)
    r = client.delete(f"/api/favorites/{tid}")
    check("DELETE /api/favorites/{id}", r.status_code == 200)

    print("== 错误处理 ==")
    r = client.get("/api/track/doesnotexist")
    check("不存在的曲目 -> 404", r.status_code == 404, r.status_code)
    r = client.post("/api/extract", json={"platform": "nope", "source_id": "1"})
    check("未知平台 -> 400", r.status_code == 400, r.status_code)
    r = client.post("/api/parse", json={"url": "https://example.com/foo"})
    check("无法识别的链接 -> 400", r.status_code == 400, r.status_code)
    r = client.get("/api/proxy?url=http://127.0.0.1:8000/")
    check("图片代理拒绝内网 -> 400", r.status_code == 400, r.status_code)
    r = client.get("/api/search?keyword=&limit=20")
    check("空关键词 -> 422", r.status_code == 422, r.status_code)

    print("== 链接识别（不触网，仅验证路由匹配）==")
    for url, expect in [
        ("https://www.xiaohongshu.com/explore/63db8819000000001a01ead1", "xhs"),
        ("https://www.douyin.com/video/7380308675841297704", "douyin"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
    ]:
        from app.routers.api import _URL_RULES
        hit = next((p for p, pat in _URL_RULES if pat.search(url)), None)
        check(f"链接识别 {expect}", hit == expect, hit)

    client.delete(f"/api/track/{tid}")

print()
print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：" + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)

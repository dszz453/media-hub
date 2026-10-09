"""请求 / 响应模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Platform = Literal["xhs", "douyin", "youtube", "all"]


class SearchRequest(BaseModel):
    keyword: str = Field(..., min_length=1, max_length=120)
    platform: Platform = "all"
    limit: int = Field(20, ge=1, le=50)


class SearchItem(BaseModel):
    """搜索结果里的单条。前端拿到它就能直接渲染 + 一键转音频。"""

    id: str
    platform: str
    source_id: str
    title: str = ""
    author: str = ""
    cover: str = ""
    duration: int = 0
    source_url: str = ""
    status: str = "idle"
    audio_path: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)
    favorited: bool = False


class SearchResponse(BaseModel):
    keyword: str
    platform: str
    items: list[SearchItem] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)
    cached: bool = False


class ExtractRequest(BaseModel):
    """把一条搜索结果（或一个直链）送进转音频队列。"""

    platform: str
    source_id: str = ""
    title: str = ""
    author: str = ""
    cover: str = ""
    duration: int = 0
    source_url: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)


class JobStatus(BaseModel):
    track_id: str
    status: str
    progress: float = 0
    error: str = ""
    title: str = ""
    audio_path: str = ""


class PlaylistCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)


class PlaylistItemRequest(BaseModel):
    track_id: str
    playlist_id: int


class UrlParseRequest(BaseModel):
    url: str = Field(..., min_length=4)

"""Provider 注册表。Provider 持有网络客户端，做成单例复用连接。"""

from __future__ import annotations

from .base import MediaItem, Provider, ProviderError, Resolved
from .douyin import DouyinProvider
from .xhs import XhsProvider
from .youtube import YouTubeProvider

_INSTANCES: list[Provider] = [XhsProvider(), DouyinProvider(), YouTubeProvider()]
_REGISTRY: dict[str, Provider] = {p.name: p for p in _INSTANCES}

__all__ = [
    "MediaItem",
    "Provider",
    "ProviderError",
    "Resolved",
    "get_provider",
    "all_providers",
    "provider_status",
]


def get_provider(name: str) -> Provider | None:
    return _REGISTRY.get((name or "").lower())


def all_providers() -> list[Provider]:
    return list(_INSTANCES)


def provider_status() -> list[dict[str, object]]:
    """给设置页用：每个平台当前是否可用、原因是什么。"""
    out: list[dict[str, object]] = []
    for provider in _INSTANCES:
        try:
            ok, reason = provider.available()
        except Exception as exc:  # noqa: BLE001
            ok, reason = False, str(exc)
        out.append({"name": provider.name, "label": provider.label,
                    "available": ok, "reason": reason})
    return out

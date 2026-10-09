"""采集器入口：按平台分发。"""

from .base import BaseCollector, CollectorError
from .douyin import DouyinCollector, collect_douyin
from .xiaohongshu import XiaohongshuCollector, collect_xiaohongshu

__all__ = [
    "BaseCollector",
    "CollectorError",
    "DouyinCollector",
    "XiaohongshuCollector",
    "collect_douyin",
    "collect_xiaohongshu",
]

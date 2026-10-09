"""统一的数据模型与配置加载。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# 抖音和小红书的页面会把「1.2万」这类文本塞进 DOM，这里统一归一化成整数
_COUNT_RE = re.compile(r"([\d.]+)\s*([万亿kwKW]?)")


def parse_count(raw: Any) -> int:
    """把 '1.2万' / '3.5w' / '1,234' / 1234 统一成整数。解析不了就返回 0。"""
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        return int(raw)
    text = str(raw).strip().replace(",", "")
    if not text:
        return 0
    match = _COUNT_RE.search(text)
    if not match:
        return 0
    try:
        number = float(match.group(1))
    except ValueError:
        return 0
    unit = match.group(2).lower()
    multiplier = {"万": 10_000, "w": 10_000, "k": 1_000, "kw": 1_000, "亿": 100_000_000}.get(unit, 1)
    return int(number * multiplier)


def parse_duration_seconds(raw: Any) -> int:
    """把 '1:23' / '83' / 83 统一成秒数。"""
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        return int(raw)
    text = str(raw).strip()
    if not text:
        return 0
    if ":" in text:
        parts = text.split(":")
        try:
            numbers = [int(p) for p in parts]
        except ValueError:
            return 0
        seconds = 0
        for number in numbers:
            seconds = seconds * 60 + number
        return seconds
    try:
        return int(float(text))
    except ValueError:
        return 0


class Platform:
    DOUYIN = "抖音"
    XIAOHONGSHU = "小红书"


class ContentType:
    VIDEO = "视频"
    IMAGE_TEXT = "图文"


@dataclass
class Work:
    """一条作品（视频或图文）。"""

    platform: str
    content_type: str
    url: str
    title: str = ""
    text_content: str = ""
    cover_url: str = ""
    publish_time: str = ""
    duration_seconds: int = 0
    like_count: int = 0
    comment_count: int = 0
    collect_count: int = 0
    share_count: int = 0
    audio_url: str = ""

    # 以下字段由 LLM 分析阶段填充
    summary: str = ""
    topic: str = ""
    analysis_failed: bool = False

    def to_record(self) -> dict[str, Any]:
        """转成飞书多维表格的一条记录。"""
        fields: dict[str, Any] = {
            "平台": self.platform,
            "内容类型": self.content_type,
            "作品链接": {"text": self.title or "查看作品", "link": self.url},
            "标题": self.title,
            "文案": self.text_content,
            "文案概括": self.summary,
            "选题": self.topic,
            "点赞数": self.like_count,
            "评论数": self.comment_count,
            "收藏数": self.collect_count,
        }
        if self.share_count:
            fields["分享数"] = self.share_count
        if self.publish_time:
            fields["发布时间"] = self.publish_time
        if self.duration_seconds:
            fields["视频时长"] = self.duration_seconds
        return {"fields": fields}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PlatformNotSupported(ValueError):
    pass


def detect_platform(url: str) -> str:
    """根据链接判断平台。判断不出来就抛异常，不要静默走错分支。"""
    if not url or "://" not in url:
        raise PlatformNotSupported(f"不是有效的链接：{url!r}")
    lowered = url.lower()
    if "douyin.com" in lowered or "iesdouyin.com" in lowered:
        return Platform.DOUYIN
    if "xiaohongshu.com" in lowered or "xhslink.com" in lowered:
        return Platform.XIAOHONGSHU
    raise PlatformNotSupported(
        f"目前只支持抖音和小红书，收到的是：{url}"
    )


def normalize_profile_url(url: str) -> str:
    """把分享短链展开成标准主页地址，剪掉追踪参数。"""
    url = url.strip()
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
            break
    url = url.split("?")[0].split("#")[0]
    if not url.startswith("www."):
        url = "www." + url
    return f"https://{url}"


@dataclass
class Settings:
    zhipu_api_key: str = ""
    tencent_secret_id: str = ""
    tencent_secret_key: str = ""
    tencent_asr_engine_model_id: str = ""
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_app_token: str = ""
    feishu_table_id: str = ""
    scroll_max_rounds: int = 40
    scroll_interval_ms: int = 1500
    collect_batch_size: int = 100
    request_timeout_ms: int = 45_000
    asr_enabled: bool = True

    @classmethod
    def load(cls) -> "Settings":
        project_root = Path(__file__).resolve().parent.parent
        secrets_dir = project_root / ".secrets"

        def read(name: str, default: str = "") -> str:
            # GitHub Actions 注入的环境变量优先，本地 secrets 文件作为兜底
            value = os.environ.get(name)
            if value:
                return value
            local_file = secrets_dir / name
            if local_file.is_file():
                return local_file.read_text(encoding="utf-8").strip()
            return default

        def read_int(name: str, default: int) -> int:
            try:
                return int(read(name) or default)
            except ValueError:
                return default

        return cls(
            zhipu_api_key=read("ZHIPU_API_KEY"),
            tencent_secret_id=read("TENCENT_SECRET_ID"),
            tencent_secret_key=read("TENCENT_SECRET_KEY"),
            tencent_asr_engine_model_id=read("TENCENT_ASR_ENGINE_MODEL_ID"),
            feishu_app_id=read("FEISHU_APP_ID"),
            feishu_app_secret=read("FEISHU_APP_SECRET"),
            feishu_app_token=read("FEISHU_APP_TOKEN"),
            feishu_table_id=read("FEISHU_TABLE_ID"),
            scroll_max_rounds=read_int("SCROLL_MAX_ROUNDS", 40),
            scroll_interval_ms=read_int("SCROLL_INTERVAL_MS", 1500),
            collect_batch_size=read_int("COLLECT_BATCH_SIZE", 100),
            request_timeout_ms=read_int("REQUEST_TIMEOUT_MS", 45_000),
            asr_enabled=read("ASR_ENABLED", "true").lower() != "false",
        )

    def require(self, *names: str) -> None:
        missing = [name for name in names if not getattr(self, name, None)]
        if missing:
            raise RuntimeError(
                f"缺少必需配置：{', '.join(missing)}。"
                "请在 GitHub 仓库 Settings → Secrets and variables → Actions 中添加。"
            )

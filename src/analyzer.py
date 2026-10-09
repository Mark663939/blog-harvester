"""文案概括与选题识别。

用智谱 GLM-4-Flash（永久免费，30 QPS），一次调用产出概括 + 选题，
比分三次调用省一半请求，也让概括和选题基于同一份理解，不会前后矛盾。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from .models import Settings, Work

logger = logging.getLogger(__name__)

_API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
_MODEL = "glm-4-flash"

# 选题分类清单。可以按自己的业务改，改完飞书表格的「选题」单选字段也要同步。
DEFAULT_TOPICS = [
    "产品测评",
    "好物推荐",
    "避坑指南",
    "教程干货",
    "使用测评",
    "剧情段子",
    "生活日常",
    "穿搭美妆",
    "美食探店",
    "旅行攻略",
    "健身运动",
    "职场干货",
    "情感话题",
    "热点解读",
    "知识科普",
    "广告推广",
    "其他",
]

_SYSTEM_PROMPT = """你是一名资深的内容策略分析师，服务对象是做竞品内容分析的运营人员。

你的任务：读懂一条社交媒体作品的文案，判断它在讲什么、属于哪一类选题。

判断标准（务必遵守）：
1. 选题要回答「这条内容在解决什么问题、讲什么品类、用什么形式讲」，
   而不是简单复述标题。像「分享日常穿搭」这种毫无信息量的词是无效输出。
2. 如果提供的候选选题里有合适的，必须从候选里选。
3. 只有当候选里确实没有任何一个能涵盖时，才允许输出「其他」，
   并在 topic_detail 里说明它到底是什么内容。
4. 概括控制在 100 字以内，说清楚这条内容讲了什么、对观众有什么用，
   不要写「这段视频讲了...」这种废话开头。
5. 语音转写可能有错别字和口语化重复，理解语义即可，不要因为个别错字判断失败。

只输出 JSON，不要任何解释文字、不要 markdown 代码块。"""

_USER_TEMPLATE = """候选选题分类（优先从中选择）：
{topics}

---
作品标题：{title}
内容类型：{content_type}
平台：{platform}
文案内容：
{content}
---

严格按这个 JSON 格式输出：
{{
  "summary": "100字以内的内容概括",
  "topic": "从候选中选一个，或填'其他'",
  "topic_detail": "如果 topic 是'其他'，说明具体内容；否则填与 topic 相同的值"
}}"""


class GLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "GLMClient":
        self._client = httpx.AsyncClient(timeout=60.0)
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def analyze(self, work: Work, topics: list[str]) -> tuple[str, str]:
        """返回 (概括, 选题)。失败时返回空字符串并标记 work。"""
        if not self._client:
            raise RuntimeError("GLMClient 必须在 async with 块内使用")

        content = (work.text_content or work.title or "").strip()
        if not content:
            work.analysis_failed = True
            return "", ""

        payload = {
            "model": _MODEL,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _USER_TEMPLATE.format(
                        topics="\n".join(f"- {t}" for t in topics),
                        title=work.title or "（无标题）",
                        content_type=work.content_type,
                        platform=work.platform,
                        content=content[:4000],
                    ),
                },
            ],
            "temperature": 0.3,
            "max_tokens": 500,
        }

        try:
            response = await self._client.post(
                _API_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.settings.zhipu_api_key}",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            data = response.json()
            raw = data["choices"][0]["message"]["content"]
            parsed = _parse_json(raw)
            summary = (parsed.get("summary") or "").strip()
            topic = (parsed.get("topic") or "其他").strip()
            detail = (parsed.get("topic_detail") or "").strip()

            if topic == "其他" and detail and detail != "其他":
                topic = detail[:40]

            return summary[:300], topic[:40]

        except Exception as exc:  # noqa: BLE001
            logger.warning("GLM 分析失败 %s：%s", work.url, exc)
            work.analysis_failed = True
            return "", ""


def _parse_json(raw: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON。模型有时会包 markdown 或加解释，容错一下。"""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 退而求其次：抓第一个 {...} 块
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return {}
    return {}


async def analyze_works(
    works: list[Work],
    settings: Settings,
    topics: list[str] | None = None,
    concurrency: int = 3,
) -> list[Work]:
    """并发分析所有作品。GLM-4-Flash 有 30 QPS，3 并发足够且安全。"""
    settings.require("zhipu_api_key")
    topics = topics or DEFAULT_TOPICS
    semaphore = asyncio.Semaphore(concurrency)

    async with GLMClient(settings) as client:

        async def analyze_one(work: Work) -> None:
            async with semaphore:
                summary, topic = await client.analyze(work, topics)
                work.summary = summary
                work.topic = topic
                # 分析失败时至少让「选题」有值，避免飞书单选字段留空
                if not topic:
                    work.topic = "其他"

        await asyncio.gather(*(analyze_one(w) for w in works))

    return works

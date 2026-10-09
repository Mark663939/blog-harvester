"""采集 + 详情补全：拿到作品列表后，逐条打开详情页补齐文案和视频地址。"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page

from .collectors.base import launch_context
from .models import (
    ContentType,
    Platform,
    Settings,
    Work,
    parse_duration_seconds,
)

logger = logging.getLogger(__name__)


class DetailFetcher:
    """逐条打开作品详情页，提取正文文案、发布时间、互动数、视频地址。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def enrich(self, context: BrowserContext, works: list[Work]) -> list[Work]:
        results: list[Work] = []
        for index, work in enumerate(works, start=1):
            logger.info("[%d/%d] 补全详情：%s", index, len(works), work.url)
            try:
                enriched = await self._enrich_one(context, work)
                results.append(enriched)
            except Exception as exc:  # noqa: BLE001
                # 单条失败不能拖垮整批，保留已有信息继续
                logger.warning("详情采集失败 %s：%s", work.url, exc)
                results.append(work)
            # 控制节奏，避免请求过密
            await asyncio.sleep(0.8)
        return results

    async def _enrich_one(self, context: BrowserContext, work: Work) -> Work:
        page = await context.new_page()
        try:
            await page.goto(
                work.url,
                wait_until="domcontentloaded",
                timeout=self.settings.request_timeout_ms,
            )
            await asyncio.sleep(1.8)
            await self._close_overlay(page)

            data = await page.evaluate(
                """() => {
                    const pick = (selectors) => {
                        for (const s of selectors) {
                            const el = document.querySelector(s);
                            if (el) {
                                const t = (el.innerText || el.textContent || '').trim();
                                if (t) return t;
                            }
                        }
                        return '';
                    };
                    return {
                        title: pick(['[class*="title"]', 'h1', '[data-e2e="video-title"]']),
                        desc: pick([
                            '[class*="desc"]',
                            '[class*="content"]',
                            '[data-e2e="video-desc"]',
                            'p'
                        ]),
                        publishTime: pick(['[class*="time"]', '[class*="date"]']),
                        audioSrc: (() => {
                            const v = document.querySelector('video');
                            if (v && v.src) return v.src;
                            const s = document.querySelector('source');
                            return s ? (s.src || '') : '';
                        })()
                    };
                }"""
            )

            # 抖音详情页的数字带 aria-label，单独抓
            stats = await page.evaluate(
                """() => {
                    const parse = (el) => {
                        if (!el) return 0;
                        const m = (el.innerText || '').match(/([\\d.]+)\\s*([万亿kwKW]?)/);
                        if (!m) return 0;
                        let n = parseFloat(m[1]);
                        const u = m[2].toLowerCase();
                        if (u === '万' || u === 'w') n *= 10000;
                        if (u === 'k') n *= 1000;
                        return Math.round(n);
                    };
                    const q = (sel) => document.querySelector(sel);
                    return {
                        like: parse(q('[data-e2e="like-count"]')) || parse(q('[class*="digg"]')),
                        comment: parse(q('[data-e2e="comment-count"]')) || parse(q('[class*="comment"]')),
                        collect: parse(q('[class*="collect"]')),
                        duration: (() => {
                            const v = document.querySelector('video');
                            return v && v.duration ? Math.round(v.duration) : 0;
                        })()
                    };
                }"""
            )

            work.title = work.title or (data.get("title") or "")[:200]
            desc = (data.get("desc") or "").strip()
            if desc:
                work.text_content = desc
            if data.get("publishTime"):
                work.publish_time = data["publishTime"][:30]

            if stats.get("like"):
                work.like_count = stats["like"]
            if stats.get("comment"):
                work.comment_count = stats["comment"]
            if stats.get("collect"):
                work.collect_count = stats["collect"]
            if stats.get("duration"):
                work.duration_seconds = stats["duration"]
            if data.get("audioSrc"):
                work.audio_url = data["audioSrc"]

            return work

        finally:
            await page.close()

    async def _close_overlay(self, page: Page) -> None:
        """关掉详情页可能出现的关注弹窗，避免遮挡正文。"""
        for selector in ["text=关注", "[class*='follow-btn']", "text=立即登录"]:
            try:
                element = page.locator(selector).first
                if await element.count() > 0 and await element.is_visible():
                    await element.click(timeout=1200)
                    await asyncio.sleep(0.3)
                    break
            except Exception:
                continue


def save_works(works: list[Work], output_dir: str = "out") -> str:
    """把采集结果落盘，方便排查和断点续跑。"""
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    target = path / "works.json"
    target.write_text(
        json.dumps([w.to_dict() for w in works], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(target)

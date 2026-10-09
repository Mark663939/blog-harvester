"""小红书主页采集。"""

from __future__ import annotations

import asyncio
from typing import Any

from playwright.async_api import BrowserContext, Page

from ..models import ContentType, Platform, Work, parse_count
from .base import BaseCollector, CollectorError, launch_context

# 小红书笔记链接特征
_NOTE_PATH = "/explore/"


class XiaohongshuCollector(BaseCollector):
    """采集小红书用户主页的全部笔记。

    小红书首页和详情页都有登录墙，但只要 storage_state 有效就能拿到列表。
    笔记卡片在 .note-item 类容器里，video 标记用 play-icon 判断。
    """

    def link_pattern(self) -> str:
        return "xiaohongshu.com"

    async def collect(self, context: BrowserContext, profile_url: str) -> list[Work]:
        page = await context.new_page()
        try:
            await page.goto(
                profile_url,
                wait_until="domcontentloaded",
                timeout=self.settings.request_timeout_ms,
            )
            await asyncio.sleep(3.0)
            await self._dismiss_popups(page)
            await self.before_scroll(page)

            if await self._is_login_wall(page):
                raise CollectorError(
                    "小红书主页被登录墙拦截，没有拿到笔记列表。",
                    hint=(
                        "登录态已失效。请在本地运行 "
                        "`python scripts/login.py --platform xiaohongshu` "
                        "重新扫码登录，然后提交新的 storage_state。"
                    ),
                )

            raw_cards = await self._scroll_and_collect(page)
            works = [self._build_work(card) for card in raw_cards]
            works = [w for w in works if w.url]
            return self._dedupe(works)

        except CollectorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CollectorError(
                f"小红书主页采集失败：{exc}",
                hint="可能触发了滑块验证，稍后重试；或重新执行 login.py 更新登录态。",
            ) from exc
        finally:
            await page.close()

    async def before_scroll(self, page: Page) -> None:
        # 小红书需要点「笔记」tab 才会加载全部作品
        for selector in ["text=笔记", ".fe-tab-item:nth-child(2)"]:
            try:
                element = page.locator(selector).first
                if await element.count() > 0 and await element.is_visible():
                    await element.click(timeout=2000)
                    await asyncio.sleep(1.8)
                    break
            except Exception:
                continue

    async def _is_login_wall(self, page: Page) -> bool:
        try:
            body = (await page.inner_text("body", timeout=5000))[:2000]
        except Exception:
            return False
        markers = ["登录后查看", "请先登录", "扫码登录", "立即登录", "打开小红书"]
        return any(marker in body for marker in markers)

    async def _extract_cards(self, page: Page) -> list[dict[str, Any]]:
        return await page.evaluate(
            """() => {
                const results = [];
                const seen = new Set();

                const parseCount = (text) => {
                    if (!text) return 0;
                    const m = text.match(/([\\d.]+)\\s*([万亿kwKW]?)/);
                    if (!m) return 0;
                    let n = parseFloat(m[1]);
                    const unit = m[2].toLowerCase();
                    if (unit === '万' || unit === 'w') n *= 10000;
                    else if (unit === 'k' || unit === 'kw') n *= 1000;
                    return Math.round(n);
                };

                const links = document.querySelectorAll('a[href*="/explore/"], a[href*="/discovery/item/"]');

                links.forEach(a => {
                    const href = a.href.split('?')[0];
                    if (!href || seen.has(href)) return;
                    seen.add(href);

                    // 往上找带互动数的卡片容器
                    let container = a;
                    for (let i = 0; i < 5 && container.parentElement; i++) {
                        container = container.parentElement;
                        if (container.querySelector('[class*="count"], .like-wrapper, .like-active')) break;
                    }

                    const text = (container.innerText || '').trim();
                    const likeEl = container.querySelector('[class*="count"], .count, .like-wrapper');
                    const like = likeEl ? parseCount(likeEl.innerText) : 0;

                    const img = container.querySelector('img');
                    const cover = img
                        ? (img.getAttribute('src') || img.getAttribute('data-src') || '')
                        : '';

                    // 有播放角标的是视频
                    const hasPlay = !!container.querySelector(
                        '[class*="play-icon"], .play-icon, [class*="video-icon"], svg[class*="play"]'
                    );

                    // 标题在小红书是「标题\\n正文...」的第一行
                    const lines = text.split('\\n').map(s => s.trim()).filter(Boolean);
                    let title = '';
                    for (const line of lines) {
                        const clean = line.replace(/[\\d.]+\\s*[万亿kwKW]?$/, '').trim();
                        if (clean && !/^[\\d.]+\\s*[万亿kwKW]?$/.test(clean)) {
                            title = clean;
                            break;
                        }
                    }

                    results.push({
                        href: href,
                        title: title,
                        like: like,
                        cover: cover,
                        isVideo: hasPlay
                    });
                });
                return results;
            }"""
        )

    def _build_work(self, card: dict[str, Any]) -> Work:
        return Work(
            platform=Platform.XIAOHONGSHU,
            content_type=ContentType.VIDEO if card.get("isVideo") else ContentType.IMAGE_TEXT,
            url=card.get("href", ""),
            title=(card.get("title") or "")[:200],
            cover_url=card.get("cover", ""),
            like_count=parse_count(card.get("like", 0)),
        )

    def _dedupe(self, works: list[Work]) -> list[Work]:
        seen: set[str] = set()
        unique: list[Work] = []
        for work in works:
            if work.url in seen:
                continue
            seen.add(work.url)
            unique.append(work)
        return unique


async def collect_xiaohongshu(profile_url: str, settings) -> list[Work]:
    playwright, context = await launch_context()
    try:
        collector = XiaohongshuCollector(settings)
        return await collector.collect(context, profile_url)
    finally:
        await context.close()
        await playwright.stop()

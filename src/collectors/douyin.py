"""抖音主页采集。"""

from __future__ import annotations

import asyncio
from typing import Any

from playwright.async_api import BrowserContext, Page

from ..models import ContentType, Platform, Work, parse_count, parse_duration_seconds
from .base import BaseCollector, CollectorError, launch_context

# 抖音作品链接特征
_VIDEO_PATH = "/video/"
_NOTE_PATH = "/note/"


class DouyinCollector(BaseCollector):
    """采集抖音用户主页的全部作品。

    抖音的主页分「作品」和「喜欢」两个 tab，作品列表在 /video/ 与 /note/ 路径下。
    页面是无限滚动 + 虚拟列表，滚动时旧的卡片会被回收，所以要增量抓取。
    """

    def link_pattern(self) -> str:
        return "douyin.com"

    async def collect(self, context: BrowserContext, profile_url: str) -> list[Work]:
        page = await context.new_page()
        try:
            await page.goto(
                profile_url,
                wait_until="domcontentloaded",
                timeout=self.settings.request_timeout_ms,
            )
            await asyncio.sleep(2.5)
            await self._dismiss_popups(page)
            await self.before_scroll(page)

            if await self._is_login_wall(page):
                raise CollectorError(
                    "抖音主页被登录墙拦截，没有拿到作品列表。",
                    hint=(
                        "登录态已失效。请在本地运行 "
                        "`python scripts/login.py --platform douyin` "
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
                f"抖音主页采集失败：{exc}",
                hint="可能触发了滑块验证，稍后重试；或重新执行 login.py 更新登录态。",
            ) from exc
        finally:
            await page.close()

    async def before_scroll(self, page: Page) -> None:
        # 点进「作品」tab，避免停在「喜欢」上
        for selector in ["text=作品", "[data-e2e='user-post-list']"]:
            try:
                element = page.locator(selector).first
                if await element.count() > 0 and await element.is_visible():
                    await element.click(timeout=2000)
                    await asyncio.sleep(1.5)
                    break
            except Exception:
                continue

    async def _is_login_wall(self, page: Page) -> bool:
        """判断是否被登录墙拦住了。"""
        try:
            body = (await page.inner_text("body", timeout=5000))[:2000]
        except Exception:
            return False
        markers = ["登录后查看", "请先登录", "扫码登录", "登录抖音"]
        return any(marker in body for marker in markers)

    async def _extract_cards(self, page: Page) -> list[dict[str, Any]]:
        """提取作品卡片。

        抖音的 DOM 结构变动频繁，这里不依赖具体 class 名，
        而是遍历所有指向作品页的 <a>，再从最近的祖先容器里读互动数。
        """
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
                    else if (unit === '亿') n *= 100000000;
                    return Math.round(n);
                };

                document.querySelectorAll('a[href*="/video/"], a[href*="/note/"]').forEach(a => {
                    const href = a.href.split('?')[0];
                    if (!href || seen.has(href)) return;
                    seen.add(href);

                    // 往上找到包含互动数据的容器
                    let container = a;
                    for (let i = 0; i < 6 && container.parentElement; i++) {
                        container = container.parentElement;
                        if (container.querySelector('[data-e2e*="count"], [class*="count"]')) break;
                    }

                    const text = (container.innerText || '').trim();
                    const numbers = text.match(/[\\d.]+\\s*[万亿kwKW]?/g) || [];

                    // 封面用 img 的 src 或 data-e2e
                    const img = container.querySelector('img');
                    const cover = img
                        ? (img.getAttribute('src') || img.getAttribute('data-src') || '')
                        : '';

                    results.push({
                        href: href,
                        rawText: text.slice(0, 500),
                        numbers: numbers,
                        cover: cover,
                        isVideo: href.includes('/video/')
                    });
                });
                return results;
            }"""
        )

    def _build_work(self, card: dict[str, Any]) -> Work:
        numbers = card.get("numbers", [])
        raw_text = card.get("rawText", "")

        # 抖音卡片上的数字顺序通常是：点赞、评论、收藏
        like = parse_count(numbers[0]) if len(numbers) > 0 else 0
        comment = parse_count(numbers[1]) if len(numbers) > 1 else 0
        collect = parse_count(numbers[2]) if len(numbers) > 2 else 0

        # 标题通常在文案首行，去掉互动数字行
        title = self._extract_title(raw_text)

        return Work(
            platform=Platform.DOUYIN,
            content_type=ContentType.VIDEO if card.get("isVideo") else ContentType.IMAGE_TEXT,
            url=card.get("href", ""),
            title=title,
            cover_url=card.get("cover", ""),
            like_count=like,
            comment_count=comment,
            collect_count=collect,
        )

    def _extract_title(self, raw_text: str) -> str:
        """从卡片文本里剥出标题。

        抖音的 aria-label 常是 '点赞 1.2万 评论 300 收藏 500' 这类，
        纯数字和单位开头的行都跳过，剩下的第一行当标题。
        """
        if not raw_text:
            return ""
        for line in raw_text.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.replace(" ", "").isdigit():
                continue
            if any(line.startswith(prefix) for prefix in ("点赞", "评论", "收藏", "分享")):
                continue
            return line[:200]
        return ""

    def _dedupe(self, works: list[Work]) -> list[Work]:
        seen: set[str] = set()
        unique: list[Work] = []
        for work in works:
            if work.url in seen:
                continue
            seen.add(work.url)
            unique.append(work)
        return unique


async def collect_douyin(profile_url: str, settings) -> list[Work]:
    playwright, context = await launch_context()
    try:
        collector = DouyinCollector(settings)
        return await collector.collect(context, profile_url)
    finally:
        await context.close()
        await playwright.stop()

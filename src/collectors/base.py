"""采集器基类：封装浏览器生命周期、滚动加载、反爬节奏控制。"""

from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, BrowserContext, Page

from ..models import Settings, Work

# 真人操作的常见 UA，减少被一眼识别的概率
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
]


class CollectorError(RuntimeError):
    """采集失败，消息里要带上让用户知道下一步怎么做的信息。"""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


class BaseCollector(ABC):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @abstractmethod
    async def collect(self, context: BrowserContext, profile_url: str) -> list[Work]:
        """在给定登录态下采集主页全部作品。"""

    # --- 子类可覆写 ---

    async def before_scroll(self, page: Page) -> None:
        """首屏加载完成后的钩子：关弹窗、点进作品列表等。"""

    def item_selector(self) -> str:
        """作品卡片的 CSS 选择器。"""

    def link_pattern(self) -> str:
        """从 href 中筛出作品链接的判断依据。"""

    # --- 通用逻辑 ---

    async def _random_delay(self, base_ms: int) -> None:
        """在基准间隔上加随机抖动，避免固定频率被识别。"""
        jitter = random.uniform(0.7, 1.5)
        await asyncio.sleep(base_ms * jitter / 1000)

    async def _dismiss_popups(self, page: Page) -> None:
        """尽量关掉登录引导、隐私政策、青少年模式等遮挡弹窗。"""
        candidates = [
            "text=知道了",
            "text=我知道了",
            "text=同意",
            "text=允许",
            "text=暂不",
            "text=取消",
            "text=关闭",
            "[class*='close']",
            "[class*='Close']",
            "[aria-label='关闭']",
        ]
        for selector in candidates:
            try:
                element = page.locator(selector).first
                if await element.count() > 0 and await element.is_visible():
                    await element.click(timeout=1500)
                    await asyncio.sleep(0.4)
            except Exception:
                # 弹窗没出现是常态，忽略
                continue

    async def _scroll_and_collect(
        self,
        page: Page,
        scroll_container: str | None = None,
    ) -> list[dict[str, Any]]:
        """滚动到底部收集作品卡片，滚动不再增长就停止。"""
        raw_items: list[dict[str, Any]] = []
        seen_hrefs: set[str] = set()
        stagnant_rounds = 0

        for round_index in range(self.settings.scroll_max_rounds):
            await self._random_delay(self.settings.scroll_interval_ms)

            try:
                if scroll_container:
                    await page.evaluate(
                        """(selector) => {
                            const el = document.querySelector(selector);
                            if (el) el.scrollTop = el.scrollHeight;
                        }""",
                        scroll_container,
                    )
                else:
                    await page.mouse.wheel(0, 2400)
            except Exception:
                await page.keyboard.press("End")

            await asyncio.sleep(0.8)

            new_hrefs = await self._extract_hrefs(page)
            fresh = [href for href in new_hrefs if href not in seen_hrefs]

            if not fresh:
                stagnant_rounds += 1
                # 连续两轮没有新内容，认为已经到底
                if stagnant_rounds >= 2:
                    break
                continue

            stagnant_rounds = 0
            seen_hrefs.update(fresh)

            cards = await self._extract_cards(page)
            for card in cards:
                href = card.get("href", "")
                if href and href in seen_hrefs and card.get("_counted"):
                    continue
                card["_counted"] = True
                raw_items.append(card)

        return raw_items

    async def _extract_hrefs(self, page: Page) -> list[str]:
        """提取当前页面上所有作品链接，用于判断是否还有新内容。"""
        pattern = self.link_pattern()
        try:
            hrefs = await page.evaluate(
                """(pattern) => {
                    const out = [];
                    document.querySelectorAll('a[href]').forEach(a => {
                        if (a.href && a.href.includes(pattern)) out.push(a.href);
                    });
                    return out;
                }""",
                pattern,
            )
            return list(dict.fromkeys(hrefs))
        except Exception:
            return []

    async def _extract_cards(self, page: Page) -> list[dict[str, Any]]:
        """逐个作品卡片提取字段。子类覆写以适配不同 DOM。"""
        raise NotImplementedError


async def launch_context(headless: bool = True) -> tuple[Any, BrowserContext]:
    """启动浏览器并载入登录态，返回 (playwright, context)。"""
    project_root = Path(__file__).resolve().parent.parent.parent
    state_files = [
        project_root / ".secrets" / "state_douyin.json",
        project_root / ".secrets" / "state_xiaohongshu.json",
    ]
    existing = next((p for p in state_files if p.is_file()), None)

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )
    context = await browser.new_context(
        user_agent=random.choice(_USER_AGENTS),
        viewport={"width": 1440, "height": 900},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        storage_state=str(existing) if existing else None,
    )

    # 抹掉最容易被检测的自动化标记
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    return playwright, context

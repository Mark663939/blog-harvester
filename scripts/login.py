"""登录态更新脚本：在你自己的电脑上跑一次，把 cookie 导出给 GitHub Actions 用。

为什么必须手动做：
    抖音和小红书的主页数据只在登录状态下可见，而 cookie 有效期只有 2-4 周。
    这一步没法自动化（自动化就等于让程序自己去登录，平台会立刻识别并封禁）。

用法：
    python scripts/login.py --platform douyin
    python scripts/login.py --platform xiaohongshu

跑完后：
    1. 会在 .secrets/ 下生成 state_<platform>.json
    2. 打开仓库 Settings → Secrets and variables → Actions
    3. 新建 secret，名字填 STATE_DOUYIN 或 STATE_XIAOHONGSHU
    4. 把 json 文件内容整个粘进去
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

PLATFORMS = {
    "douyin": {
        "name": "抖音",
        "start": "https://www.douyin.com/user/self",
        "check": "https://www.douyin.com/user/self",
    },
    "xiaohongshu": {
        "name": "小红书",
        "start": "https://www.xiaohongshu.com/user/profile/self",
        "check": "https://www.xiaohongshu.com/explore",
    },
}


async def login(platform: str) -> int:
    if platform not in PLATFORMS:
        print(f"不支持的平台：{platform}，可选：{', '.join(PLATFORMS)}")
        return 2

    config = PLATFORMS[platform]
    secrets_dir = Path(__file__).resolve().parent.parent / ".secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    state_file = secrets_dir / f"state_{platform}.json"

    print(f"\n即将打开浏览器，请登录{config['name']}账号")
    print("登录成功后回到这里按回车继续（不要关闭窗口）\n")

    async with async_playwright() as playwright:
        # 用有头模式，且故意保留一些自动化特征，减少登录被风控的概率
        browser = await playwright.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 900},
        )
        page = await context.new_page()

        await page.goto(config["start"], wait_until="domcontentloaded")
        # 留出扫码时间
        for remaining in range(180, 0, -30):
            print(f"  等待登录… 剩余约 {remaining} 秒", end="\r")
            await asyncio.sleep(30)
        print()

        await page.goto(config["check"], wait_until="domcontentloaded")
        await asyncio.sleep(3)

        body = ""
        try:
            body = await page.inner_text("body", timeout=5000)
        except Exception:
            pass

        failed_markers = ["扫码登录", "请先登录", "登录后查看", "立即登录"]
        if any(marker in body[:1500] for marker in failed_markers):
            print("\n看起来还没登录成功。请重试：")
            print(f"  python scripts/login.py --platform {platform}")
            await context.close()
            await browser.close()
            return 1

        await context.storage_state(path=str(state_file))
        await context.close()
        await browser.close()

    print(f"\n登录态已保存到：{state_file}")
    print("\n下一步：")
    print("  1. 打开仓库 Settings → Secrets and variables → Actions")
    secret_name = "STATE_DOUYIN" if platform == "douyin" else "STATE_XIAOHONGSHU"
    print(f"  2. New repository secret，名字填：{secret_name}")
    print("  3. 用记事本打开上面的 json，全选复制，粘进 secret 的值里")
    print("\n注意：json 里含有你的登录凭证，不要发到任何公开渠道。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="更新抖音/小红书登录态")
    parser.add_argument(
        "--platform", required=True, choices=list(PLATFORMS), help="要登录的平台"
    )
    args = parser.parse_args()
    return asyncio.run(login(args.platform))


if __name__ == "__main__":
    sys.exit(main())

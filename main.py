"""主编排入口：一条主页链接 → 飞书多维表格。

用法：
    python main.py https://www.douyin.com/user/xxx
    python main.py <url> --no-asr        # 跳过语音转写，只用标题
    python main.py <url> --topics a,b,c  # 自定义选题分类
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from src.analyzer import DEFAULT_TOPICS, analyze_works
from src.asr import transcribe_works
from src.collectors.base import launch_context
from src.collectors.douyin import collect_douyin
from src.collectors.xiaohongshu import collect_xiaohongshu
from src.detail import DetailFetcher, save_works
from src.feishu import write_to_feishu
from src.models import (
    ContentType,
    Platform,
    PlatformNotSupported,
    Settings,
    Work,
    detect_platform,
    normalize_profile_url,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="博主作品采集 → 文案概括 → 选题识别 → 写入飞书多维表格"
    )
    parser.add_argument("url", help="博主主页链接（抖音或小红书）")
    parser.add_argument("--no-asr", action="store_true", help="跳过视频语音转写")
    parser.add_argument("--no-detail", action="store_true", help="跳过详情页补全")
    parser.add_argument("--no-write", action="store_true", help="只采集，不写飞书")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条，用于试跑")
    parser.add_argument("--topics", default="", help="自定义选题分类，逗号分隔")
    parser.add_argument("--dry-run", action="store_true", help="用有头浏览器跑，方便观察")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> int:
    started = time.time()
    settings = Settings.load()

    if args.no_asr:
        settings.asr_enabled = False

    # 1. 识别平台
    try:
        platform = detect_platform(args.url)
    except PlatformNotSupported as exc:
        logger.error("%s", exc)
        return 2

    profile_url = normalize_profile_url(args.url)
    logger.info("平台：%s", platform)
    logger.info("主页：%s", profile_url)

    topics = (
        [t.strip() for t in args.topics.split(",") if t.strip()]
        if args.topics
        else DEFAULT_TOPICS
    )

    # 2. 采集作品列表
    logger.info("开始采集作品列表…")
    if platform == Platform.DOUYIN:
        works = await collect_douyin(profile_url, settings)
    else:
        works = await collect_xiaohongshu(profile_url, settings)

    if not works:
        logger.error(
            "没有采集到任何作品。可能原因：登录态失效、触发风控验证、或该博主无公开作品。"
        )
        return 3

    logger.info("采集到 %d 条作品", len(works))
    if args.limit:
        works = works[: args.limit]
        logger.info("按 --limit 截断为 %d 条", len(works))

    # 3. 补全详情
    if not args.no_detail:
        logger.info("补全作品详情…")
        playwright, context = await launch_context(headless=not args.dry_run)
        try:
            fetcher = DetailFetcher(settings)
            works = await fetcher.enrich(context, works)
        finally:
            await context.close()
            await playwright.stop()

    save_works(works)
    logger.info("采集结果已保存到 out/works.json")

    video_count = sum(1 for w in works if w.content_type == ContentType.VIDEO)
    logger.info("其中视频 %d 条，图文 %d 条", video_count, len(works) - video_count)

    # 4. 语音转写
    asr_report: dict = {}
    if settings.asr_enabled and video_count:
        logger.info("开始语音转写（这一步最慢）…")
        works, asr_report = await transcribe_works(works, settings, return_report=True)
    elif video_count:
        logger.info("已跳过语音转写，视频文案只用标题")

    # 5. LLM 分析
    logger.info("开始文案概括与选题识别…")
    works = await analyze_works(works, settings, topics=topics)
    failed = sum(1 for w in works if w.analysis_failed)
    if failed:
        logger.warning("%d 条作品分析失败，飞书中选题会标为「其他」", failed)

    save_works(works)

    # 6. 写入飞书
    if args.no_write:
        logger.info("--no-write 已启用，跳过写入。结果在 out/works.json")
    else:
        logger.info("写入飞书多维表格…")
        try:
            result = await write_to_feishu(works, settings)
        except Exception as exc:  # noqa: BLE001
            logger.error("写入飞书失败：%s", exc)
            logger.error("采集结果已保存在 out/works.json，可以手动导入")
            return 4
        logger.info(
            "写入完成：共 %d 条，成功 %d 条，失败 %d 条",
            result["total"], result["succeeded"], result["failed"],
        )

    elapsed = time.time() - started
    logger.info("总耗时 %.1f 分钟", elapsed / 60)

    # 简单的结果摘要，方便在 Actions 日志里一眼看到
    summary = _build_summary(works)
    if asr_report:
        summary["asr_quota"] = asr_report
    Path("out/summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("摘要：%s", json.dumps(summary["topic_distribution"], ensure_ascii=False))

    # 额度超了就在 Actions 日志里显式提醒，避免下次白跑
    if asr_report and asr_report.get("consumed_hours", 0) > 10:
        logger.warning(
            "::warning::本次转写消耗 %.1f 小时，已超过腾讯云每月 10 小时免费额度。"
            "超额会停服（不扣费），视频将只有标题没有口播文案。"
            "建议改用硅基流动 SenseVoiceSmall 获得无上限免费额度。",
            asr_report["consumed_hours"],
        )
    elif asr_report:
        logger.info(
            "额度充足：本次消耗 %.1f 小时，剩余约 %.1f 小时",
            asr_report.get("consumed_hours", 0),
            asr_report.get("remaining_hours_estimate", 0),
        )
    return 0


def _build_summary(works: list[Work]) -> dict:
    topic_dist: dict[str, int] = {}
    for work in works:
        key = work.topic or "其他"
        topic_dist[key] = topic_dist.get(key, 0) + 1
    return {
        "total": len(works),
        "video": sum(1 for w in works if w.content_type == ContentType.VIDEO),
        "image_text": sum(1 for w in works if w.content_type == ContentType.IMAGE_TEXT),
        "avg_like": int(sum(w.like_count for w in works) / len(works)) if works else 0,
        "topic_distribution": dict(sorted(topic_dist.items(), key=lambda kv: -kv[1])),
    }


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        logger.warning("已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())

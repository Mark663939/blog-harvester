"""语音转写：调用腾讯云录音文件识别，走每月 10 小时免费额度。

选腾讯云而不是火山引擎的原因：
- 每月 10 小时免费额度是长期政策，不是一次性试用
- 录音文件识别支持 5 小时长音频，一次提交够处理一个博主的全部视频
- SDK 稳定，失败时错误信息明确
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
from pathlib import Path

import httpx

from .models import ContentType, Settings, Work

logger = logging.getLogger(__name__)

# 录音文件识别的引擎类型
_ENGINE_MODEL_TYPE = "16k_zh"
_ENGINE_MODEL_TYPE_VOICE = "16k_zh_voice"
_CHANNEL_NUM = 1
_RES_TEXT_NORMAL = 0
_RES_TEXT_FIRST = 1


class ASRUnavailable(RuntimeError):
    pass


class TencentASR:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def transcribe(self, audio_path: str) -> str:
        """转写单个音频文件，返回文字稿。"""
        try:
            from tencentcloud.asr.v20190614 import models, asr_client
        except ImportError as exc:  # noqa: BLE001
            raise ASRUnavailable(
                "缺少腾讯云 SDK，请先执行 pip install -r requirements.txt"
            ) from exc

        with open(audio_path, "rb") as audio_file:
            audio_base64 = _to_base64(audio_file.read())

        model_id = self.settings.tencent_asr_engine_model_id
        engine_model_type = (
            _ENGINE_MODEL_TYPE_VOICE if model_id == "16k_zh_voice" else _ENGINE_MODEL_TYPE
        )

        def _call() -> str:
            from tencentcloud.common import credential
            from tencentcloud.common.profile import client_profile, http_profile

            cred = credential.Credential(
                self.settings.tencent_secret_id, self.settings.tencent_secret_key
            )
            http_profile_obj = http_profile.HttpProfile()
            client_profile_obj = client_profile.ClientProfile()
            client = asr_client.AsrClient(cred, "ap-shanghai", client_profile_obj)

            request = models.CreateRecTaskRequest()
            request.EngineModelType = engine_model_type
            request.ChannelNum = _CHANNEL_NUM
            request.ResTextFormat = _RES_TEXT_NORMAL
            request.SourceType = 0
            request.Data = audio_base64
            response = client.CreateRecTask(request)
            task_id = response.Data.TaskId
            return self._poll(client, models, task_id)

        return await asyncio.to_thread(_call)

    def _poll(self, client, models, task_id: str, timeout: int = 900) -> str:
        """轮询任务结果。录音文件识别是异步任务。"""
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            request = models.DescribeTaskStatusRequest()
            request.TaskId = task_id
            response = client.DescribeTaskStatus(request)
            status = response.Data.Status

            if status == 3:  # 成功
                return self._extract_text(response)
            if status in (0, 1):  # 排队中 / 执行中
                time.sleep(5)
                continue
            # 失败状态直接抛出，避免空转到超时
            raise RuntimeError(
                f"语音识别失败，状态码 {status}，错误信息：{response.Data.ErrorMsg or '未知'}"
            )
        raise TimeoutError("语音识别超时（超过 15 分钟）")

    def _extract_text(self, response) -> str:
        detail = response.Data
        if getattr(detail, "Result", None) and detail.Result:
            try:
                # Result 是 JSON 字符串，取每句的 Text 拼起来
                import json

                sentences = json.loads(detail.Result)
                return "".join(s.get("Text", "") for s in sentences).strip()
            except (json.JSONDecodeError, TypeError):
                return str(detail.Result)
        if getattr(detail, "ResultText", None):
            return detail.ResultText.strip()
        return ""


def _to_base64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("utf-8")


async def download_audio(work: Work, output_dir: str = "out/audio") -> str | None:
    """下载作品视频的音轨。

    优先用页面里拿到的 video 源，退化到 yt-dlp。
    """
    if not work.audio_url:
        return None

    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    suffix = ".m4a" if ".m4a" in work.audio_url else ".mp4"
    target = path / f"{abs(hash(work.url))}{suffix}"

    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            response = await client.get(work.audio_url)
            response.raise_for_status()
            target.write_bytes(response.content)
        # 音轨体积可能仍然偏大，超过 8MB 就压缩
        return await _compress_if_needed(target)
    except Exception as exc:  # noqa: BLE001
        logger.warning("下载音轨失败 %s：%s", work.url, exc)
        return None


async def _compress_if_needed(path: Path, limit_mb: int = 8) -> str:
    """超过体积限制就用 ffmpeg 压成 16k 单声道，ASR 只需要这个规格。"""
    size_mb = path.stat().st_size / 1024 / 1024
    if size_mb <= limit_mb:
        return str(path)

    compressed = path.with_suffix(".16k.mp3")
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(path),
        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
        str(compressed),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode == 0 and compressed.is_file():
        path.unlink(missing_ok=True)
        return str(compressed)
    logger.warning("音频压缩失败，沿用原文件：%s", stderr.decode(errors="ignore")[:200])
    return str(path)


def _merge_title_and_transcript(title: str, transcript: str) -> str:
    """标题和口播稿拼成完整文案，重复的开头去掉一份。"""
    title = (title or "").strip()
    transcript = (transcript or "").strip()
    if not title:
        return transcript[:2000]
    if not transcript:
        return title
    if transcript.startswith(title[:20]):
        return transcript[:2000]
    return f"{title}\n{transcript}"[:2000]


async def transcribe_works(
    works: list[Work],
    settings: Settings,
    asr: TencentASR | None = None,
    return_report: bool = False,
) -> list[Work] | tuple[list[Work], dict]:
    """给所有视频作品补上口播文字稿。

    某一条失败不影响其他条，失败的作品只保留标题，LLM 阶段会照常分析。

    return_report=True 时额外返回额度消耗报告，供 main.py 写入摘要文件。
    """
    if not settings.asr_enabled:
        logger.info("ASR 已关闭，视频文案只用标题")
        return (works, {}) if return_report else works

    settings.require(
        "tencent_secret_id", "tencent_secret_key", "tencent_asr_engine_model_id"
    )
    asr = asr or TencentASR(settings)

    video_works = [w for w in works if w.content_type == ContentType.VIDEO]
    if not video_works:
        return (works, {}) if return_report else works

    # 统计腾讯云免费额度消耗：每月 10 小时
    consumed_seconds = 0.0
    succeeded = 0
    failed = 0
    skipped = 0

    for index, work in enumerate(video_works, start=1):
        logger.info(
            "[%d/%d] 转写：%s（已知时长 %s）",
            index, len(video_works), work.url,
            f"{work.duration_seconds}s" if work.duration_seconds else "未知",
        )
        try:
            audio_path = await download_audio(work)
            if not audio_path:
                skipped += 1
                continue

            # 实际音频大小换算时长，比页面显示的时长更准
            audio_seconds = _estimate_seconds(audio_path, work.duration_seconds)
            transcript = await asr.transcribe(audio_path)

            if transcript:
                work.text_content = _merge_title_and_transcript(work.title, transcript)
                consumed_seconds += audio_seconds
                succeeded += 1
            else:
                # 转写成功但内容为空，不计入额度（腾讯云按识别成功的时长计费）
                skipped += 1

            Path(audio_path).unlink(missing_ok=True)

        except ASRUnavailable as exc:
            logger.warning("ASR 不可用，跳过所有视频转写：%s", exc)
            skipped += len(video_works) - index + 1
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning("转写失败 %s：%s", work.url, exc)
            failed += 1
            continue

    # 腾讯云录音文件识别的每月免费额度
    free_quota_hours = 10.0
    used_hours = consumed_seconds / 3600
    remaining_hours = max(0.0, free_quota_hours - used_hours)

    report = {
        "engine": "tencent-asr",
        "free_quota_hours_per_month": free_quota_hours,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "consumed_minutes": round(consumed_seconds / 60, 1),
        "consumed_hours": round(used_hours, 2),
        "remaining_hours_estimate": round(remaining_hours, 1),
    }
    _log_quota_report(
        consumed_seconds, succeeded, failed, skipped,
        free_quota_hours, used_hours, remaining_hours,
    )
    return (works, report) if return_report else works


def _estimate_seconds(audio_path: str, fallback: int = 0) -> float:
    """估算音频时长。

    腾讯云录音文件识别按「识别成功的音频时长」计费，
    所以这里尽量给准，方便你判断额度还剩多少。
    用 ffprobe 精确读取，读不到就退回页面上的时长。
    """
    try:
        process = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
            capture_output=True, text=True, timeout=30,
        )
        value = process.stdout.strip()
        if value:
            return float(value)
    except Exception:
        pass
    return float(fallback or 0.0)


def _log_quota_report(
    consumed_seconds: float,
    succeeded: int,
    failed: int,
    skipped: int,
    free_quota_hours: float,
    used_hours: float,
    remaining_hours: float,
) -> None:
    """输出额度消耗报告。"""
    logger.info("=" * 46)
    logger.info(
        "语音转写额度报告（腾讯云免费额度：每月 %g 小时）", free_quota_hours
    )
    logger.info("  转写成功：%d 条", succeeded)
    if failed:
        logger.info("  转写失败：%d 条", failed)
    if skipped:
        logger.info("  跳过：%d 条", skipped)
    logger.info(
        "  本次消耗音频时长：%.1f 分钟（%.2f 小时）",
        consumed_seconds / 60, used_hours,
    )
    logger.info("  本月免费额度估算剩余：约 %.1f 小时", remaining_hours)

    if used_hours > free_quota_hours:
        logger.warning(
            "  已超过免费额度。腾讯云新用户默认关闭后付费，超额会停服而不是扣费，"
            "此时转写会失败（视频只保留标题）。"
        )
    else:
        # 折算成还能采多少个同类博主，方便判断值不值得换方案
        per_video_minutes = (consumed_seconds / 60 / succeeded) if succeeded else 0
        if per_video_minutes > 0:
            remaining_videos = int(remaining_hours * 60 / per_video_minutes)
            logger.info(
                "  按本次用量估算，剩余额度还够转写约 %d 条同类视频", remaining_videos
            )
    logger.info("=" * 46)

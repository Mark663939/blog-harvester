"""飞书多维表格写入。

用开放平台 bitable API 直写，完全免费。
- tenant_access_token 缓存 2 小时，不用反复换
- batch_create 单次最多 500 条，这里按 100 条一批，稳一点
- 写失败的记录会落盘重试文件，不会静默丢数据
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from .models import Settings, Work

logger = logging.getLogger(__name__)

_API_BASE = "https://open.feishu.cn/open-apis"
_TOKEN_TTL = 6600  # 2 小时的 token 存 1.8 小时，留安全余量


class FeishuWriter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None
        self._token: str = ""
        self._token_expired_at: float = 0.0

    async def __aenter__(self) -> "FeishuWriter":
        self._client = httpx.AsyncClient(timeout=60.0)
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get_token(self) -> str:
        if self._token and time.time() < self._token_expired_at:
            return self._token

        if not self._client:
            raise RuntimeError("FeishuWriter 必须在 async with 块内使用")

        response = await self._client.post(
            f"{_API_BASE}/auth/v3/tenant_access_token/internal",
            json={
                "app_id": self.settings.feishu_app_id,
                "app_secret": self.settings.feishu_app_secret,
            },
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取飞书 token 失败：{data.get('msg')}")

        self._token = data["tenant_access_token"]
        self._token_expired_at = time.time() + _TOKEN_TTL
        return self._token

    async def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {await self._get_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    async def write_works(self, works: list[Work]) -> dict[str, Any]:
        """批量写入。返回成功/失败统计。"""
        if not works:
            return {"total": 0, "succeeded": 0, "failed": 0}

        batch_size = max(1, min(self.settings.collect_batch_size, 500))
        succeeded = 0
        failed_records: list[dict[str, Any]] = []

        for start in range(0, len(works), batch_size):
            batch = works[start:start + batch_size]
            records = [w.to_record() for w in batch]
            try:
                await self._create_batch(records)
                succeeded += len(batch)
                logger.info(
                    "已写入 %d/%d 条", start + len(batch), len(works)
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("写入失败（第 %d 条起共 %d 条）：%s", start, len(batch), exc)
                # 逐条重试，定位到具体是哪些记录有问题
                for work, record in zip(batch, records):
                    try:
                        await self._create_batch([record])
                        succeeded += 1
                    except Exception as single_exc:  # noqa: BLE001
                        logger.error("单条写入失败 %s：%s", work.url, single_exc)
                        failed_records.append(record)

            await asyncio.sleep(0.3)  # 官方上限 50 次/秒，这里远低于上限

        if failed_records:
            self._save_failures(failed_records)

        return {
            "total": len(works),
            "succeeded": succeeded,
            "failed": len(failed_records),
        }

    async def _create_batch(self, records: list[dict[str, Any]], retry: int = 2) -> None:
        """调用 batch_create，带指数退避重试。"""
        if not self._client:
            raise RuntimeError("FeishuWriter 必须在 async with 块内使用")

        url = (
            f"{_API_BASE}/bitable/v1/apps/{self.settings.feishu_app_token}"
            f"/tables/{self.settings.feishu_table_id}/records/batch_create"
        )
        payload = {"records": records}

        last_error: Exception | None = None
        for attempt in range(retry + 1):
            try:
                response = await self._client.post(
                    url, json=payload, headers=await self._headers()
                )
                response.raise_for_status()
                data = response.json()
                if data.get("code") != 0:
                    raise RuntimeError(f"飞书返回错误 {data.get('code')}：{data.get('msg')}")
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                # token 过期刷新一次
                if "99991663" in str(exc) or "token" in str(exc).lower():
                    self._token_expired_at = 0.0
                if attempt < retry:
                    await asyncio.sleep(2 ** attempt)
        raise last_error or RuntimeError("写入失败")

    def _save_failures(self, records: list[dict[str, Any]]) -> None:
        path = Path("out")
        path.mkdir(parents=True, exist_ok=True)
        target = path / "failed_records.json"
        target.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.warning(
            "%d 条记录写入失败，已保存到 %s，可手动导入", len(records), target
        )

    async def ensure_table_fields(self) -> list[str]:
        """读取表格字段，方便排查「字段名写错导致整批失败」的问题。"""
        if not self._client:
            raise RuntimeError("FeishuWriter 必须在 async with 块内使用")

        url = (
            f"{_API_BASE}/bitable/v1/apps/{self.settings.feishu_app_token}"
            f"/tables/{self.settings.feishu_table_id}/fields"
        )
        response = await self._client.get(url, headers=await self._headers())
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f"读取字段失败：{data.get('msg')}")
        return [item["field_name"] for item in data.get("data", {}).get("items", [])]


async def write_to_feishu(works: list[Work], settings: Settings) -> dict[str, Any]:
    settings.require(
        "feishu_app_id", "feishu_app_secret", "feishu_app_token", "feishu_table_id"
    )
    async with FeishuWriter(settings) as writer:
        return await writer.write_works(works)

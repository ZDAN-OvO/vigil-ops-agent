"""定时 AIOps 任务服务

每隔指定时间自动执行 AIOps 诊断，保存报告并发送 Webhook 回调
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path

import aiofiles
import httpx
from loguru import logger

from app.config import config, BASE_DIR


class ScheduledAIOpsService:
    """定时 AIOps 任务服务"""

    def __init__(self):
        self._task: asyncio.Task | None = None

    async def start(self):
        """启动定时任务"""
        if self._task is not None:
            logger.warning("定时 AIOps 任务已在运行中")
            return
        self._task = asyncio.create_task(self._run_loop())
        logger.info(f"✅ 定时 AIOps 任务已启动，间隔 {config.scheduled_aiops_interval_seconds} 秒")

    async def stop(self):
        """停止定时任务"""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("🛑 定时 AIOps 任务已停止")

    async def _run_loop(self):
        """主循环：定时执行诊断"""
        while True:
            try:
                await self._execute_diagnosis()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"定时 AIOps 执行异常: {e}")

            await asyncio.sleep(config.scheduled_aiops_interval_seconds)

    async def _execute_diagnosis(self):
        """执行一次诊断，消费异步生成器获取结果"""
        from app.services.aiops_service import aiops_service

        session_id = config.scheduled_aiops_session_id
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        logger.info(f"🔄 开始定时 AIOps 诊断 [{timestamp}]，session_id={session_id}")

        report = None
        async for event in aiops_service.diagnose(session_id=session_id):
            event_type = event.get("type")
            if event_type == "complete":
                report = event.get("response") or event.get("diagnosis")
            elif event_type == "error":
                logger.error(f"诊断过程出错: {event.get('message')}")

        if report is not None:
            await self._save_report(report, timestamp)
            await self._send_webhook(report, timestamp)
            logger.info(f"✅ 定时 AIOps 诊断完成 [{timestamp}]")
        else:
            logger.warning(f"⚠️ 定时 AIOps 诊断未返回报告 [{timestamp}]")

    async def _save_report(self, report, timestamp: str):
        """保存诊断报告到本地文件"""
        # timestamp 格式: YYYYMMDD_HHMMSS，提取日期部分作为子目录
        date_dir = f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
        reports_dir = BASE_DIR / "logs" / "aiops_reports" / date_dir
        reports_dir.mkdir(parents=True, exist_ok=True)

        file_path = reports_dir / f"diagnosis_{timestamp}.json"
        data = {
            "timestamp": timestamp,
            "session_id": config.scheduled_aiops_session_id,
            "report": report,
        }

        async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(data, ensure_ascii=False, indent=2))

        logger.info(f"📄 诊断报告已保存: {file_path}")

    async def _send_webhook(self, report, timestamp: str):
        """通过 Webhook 发送诊断结果"""
        webhook_url = config.scheduled_aiops_webhook_url
        if not webhook_url:
            return

        payload = {
            "timestamp": timestamp,
            "session_id": config.scheduled_aiops_session_id,
            "report": report,
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(webhook_url, json=payload)
                if resp.status_code < 300:
                    logger.info(f"📤 Webhook 发送成功 [{resp.status_code}]")
                else:
                    logger.warning(f"📤 Webhook 返回异常: {resp.status_code}")
        except Exception as e:
            logger.error(f"📤 Webhook 发送失败: {e}")


scheduled_aiops_service = ScheduledAIOpsService()

"""原生 Telegram 后台任务及生命周期，不保存机器人 Token。"""

import asyncio
import re
from html import unescape

from astrbot.api import logger
from telegram.error import BadRequest, Forbidden, RetryAfter

from .maintenance import MAX_DELETE_AGE


class BackgroundTasks:
    def __init__(self, plugin):
        self.plugin = plugin
        self.store = plugin.maintenance
        self.observed = {}
        self.locks = {}
        self.tasks = []
        self.semaphore = asyncio.Semaphore(4)

    def observe(self, client):
        self.observed[str(client.id)] = client

    def lock(self, scope):
        return self.locks.setdefault(tuple(scope), asyncio.Lock())

    def clients(self):
        manager = getattr(self.plugin.context, "platform_manager", None)
        if manager is None:
            return dict(self.observed)
        clients = {}
        for platform in manager.platform_insts:
            try:
                if platform.meta().name == "telegram":
                    client = platform.get_client()
                    clients[str(client.id)] = client
            except (AttributeError, RuntimeError):
                # Telegram 适配器可能晚于插件初始化。
                continue
        return clients

    def start(self):
        if not self.tasks:
            self.tasks = [
                asyncio.create_task(self._loop(self.run_daily_once, 30)),
                asyncio.create_task(self._loop(self.run_queue_once, 5)),
            ]

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    async def _loop(self, action, interval):
        while True:
            try:
                await action()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "DickGrowerBot后台任务暂时失败（%s），将在下一轮继续。", type(exc).__name__
                )
            await asyncio.sleep(interval)

    @staticmethod
    async def can_delete_commands(client, chat):
        try:
            member = await asyncio.wait_for(
                client.get_chat_member(int(chat), client.id), timeout=15
            )
            return member.status == "creator" or (
                member.status == "administrator" and bool(member.can_delete_messages)
            )
        except Exception:
            return False

    async def record(self, client, scope, message, body, kind, command=None):
        try:
            now = self.plugin.game.clock()
            created = message.date.timestamp() if message.date else now
            command_created = command.date.timestamp() if command and command.date else now
            await asyncio.to_thread(
                self.store.track_message,
                scope,
                message.message_id,
                created,
                command.message_id if command else None,
                command_created,
            )
            settings = await asyncio.to_thread(self.store.cleanup, scope)
            allowed = settings[kind] > 0 and await self.can_delete_commands(client, scope[1])
            visible_length = len(unescape(re.sub(r"<[^>]*>", "", body)))
            await asyncio.to_thread(
                self.store.schedule_deletion,
                scope,
                message.message_id,
                kind,
                visible_length,
                allowed,
            )
        except Exception as exc:
            logger.warning("DickGrowerBot暂时无法登记消息清理任务（%s）。", type(exc).__name__)

    async def run_daily_once(self):
        for bot in self.clients():
            rooms = await asyncio.to_thread(self.store.rooms, bot)
            for room in rooms:
                await asyncio.to_thread(self.store.run_shrink, (bot, room["chat"]))
        await asyncio.to_thread(self.store.prune)

    async def run_queue_once(self):
        await asyncio.to_thread(self.store.prune)
        calls = []
        for bot, client in self.clients().items():
            jobs = await asyncio.to_thread(self.store.claim, bot)
            calls.extend(self.process(client, job) for job in jobs)
        results = await asyncio.gather(*calls, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.warning(
                    "DickGrowerBot队列任务将在租约到期后恢复（%s）。", type(result).__name__
                )

    async def process(self, client, job):
        scope = (job["bot"], job["chat"])
        async with self.semaphore, self.lock(scope):
            if not await asyncio.to_thread(self.store.is_claimed, job):
                return
            try:
                await self._delete(client, job, scope)
            except asyncio.CancelledError:
                raise
            except RetryAfter as exc:
                delay = exc.retry_after
                seconds = delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay)
                await asyncio.to_thread(self.store.finish, job, delay=seconds + 1)
            except Forbidden:
                await asyncio.to_thread(self.store.finish, job, state="failed")
            except BadRequest as exc:
                missing = "message to delete not found" in str(exc).lower()
                await asyncio.to_thread(
                    self.store.finish, job, state="done" if missing else "failed"
                )
            except Exception as exc:
                logger.warning("DickGrowerBot消息队列处理暂时失败（%s）。", type(exc).__name__)
                if job["attempts"] >= 2:
                    await asyncio.to_thread(self.store.finish, job, state="failed")
                else:
                    await asyncio.to_thread(self.store.finish, job, delay=60 * 2 ** job["attempts"])

    async def _delete(self, client, job, scope):
        if self.plugin.game.clock() >= job["created"] + MAX_DELETE_AGE + 3600:
            await asyncio.to_thread(self.store.finish, job, state="expired")
            return
        if job["target"] == "command" and not await self.can_delete_commands(client, scope[1]):
            await asyncio.to_thread(self.store.finish, job, state="cancelled")
            return
        await asyncio.wait_for(
            client.delete_message(int(job["chat"]), int(job["message"])), timeout=30
        )
        await asyncio.to_thread(self.store.finish, job)

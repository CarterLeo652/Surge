"""AstrBot v4.29.4 的 Telegram 入口及按钮交互。"""

import asyncio
import re

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.platform.button_interaction import encode_button_callback
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, ReplyParameters
from telegram.error import BadRequest

from . import presentation as text
from .background import BackgroundTasks
from .data_path import database_path
from .game import Game, GameError
from .localization import INVALID_DATA, tr
from .maintenance import CLEANUP_DELAYS, CLEANUP_GROUPS, Maintenance
from .settings import Settings


class DickGrowerBotPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.settings = Settings.from_mapping(config)
        self.game = Game(
            database_path(StarTools.get_data_dir("astrbot_plugin_dickgrowerbot")), self.settings
        )
        self.maintenance = Maintenance(self.game)
        self.background = BackgroundTasks(self)

    async def initialize(self):
        self.background.start()

    async def terminate(self):
        await self.background.stop()

    @staticmethod
    def environment(event):
        if event.get_platform_name() != "telegram":
            raise GameError(tr("errors.not_group_chat"))
        update = event.message_obj.raw_message
        user, chat = update.effective_user, update.effective_chat
        if user is None or user.is_bot:
            raise GameError(tr("errors.group_account"))
        if chat is None:
            raise GameError(INVALID_DATA)
        return update, user, chat, (str(event.client.id), str(chat.id))

    @staticmethod
    def group_only(chat):
        if chat.type not in {"group", "supergroup"}:
            raise GameError(tr("errors.not_group_chat"))

    def group_lists(self):
        enabled = {str(value).strip() for value in self.config.get("enabled_groups", []) or []}
        disabled = {str(value).strip() for value in self.config.get("disabled_groups", []) or []}
        return enabled - {""}, disabled - {""}

    def group_enabled(self, chat_id):
        if not self.config.get("enabled", True):
            return False
        enabled, disabled = self.group_lists()
        chat_id = str(chat_id)
        return (not enabled or chat_id in enabled) and chat_id not in disabled

    @staticmethod
    def amount(value, default=None):
        if not value and default is not None:
            return default
        if not re.fullmatch(r"[0-9]{1,10}", str(value)) or not 0 <= int(value) <= 65535:
            raise GameError(tr("commands.pvp.errors.no_args"))
        return int(value)

    @staticmethod
    def button(label, action, data):
        # 使用 AstrBot 原生持久化回调路由，插件重载后本版按钮仍能正确结算。
        return InlineKeyboardButton(
            label, callback_data=encode_button_callback(f"dick:{action}", data)
        )

    def top_buttons(self, scope, result):
        row = []
        if result["page"]:
            row.append(self.button("⬅️", "top", {"scope": list(scope), "page": result["page"] - 1}))
        if result["more"]:
            row.append(self.button("➡️", "top", {"scope": list(scope), "page": result["page"] + 1}))
        rows = [row] if row else []
        return rows

    async def send(self, event, body, rows=None, *, kind="event"):
        update = event.message_obj.raw_message
        message = update.effective_message
        kwargs = {}
        if message:
            kwargs["reply_parameters"] = ReplyParameters(
                message_id=message.message_id, allow_sending_without_reply=True
            )
            if message.is_topic_message:
                kwargs["message_thread_id"] = message.message_thread_id
        sent = await event.client.send_message(
            chat_id=update.effective_chat.id,
            text=body,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows) if rows else None,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            **kwargs,
        )
        if update.effective_chat.type in {"group", "supergroup"}:
            scope = (str(event.client.id), str(update.effective_chat.id))
            async with self.background.lock(scope):
                await self.background.record(event.client, scope, sent, body, kind, message)
        return sent

    async def edit(self, event, body, rows=None, *, kind="report"):
        query = event.message_obj.raw_message.callback_query
        if query is None or query.message is None:
            raise GameError(INVALID_DATA)
        scope = (str(event.client.id), str(query.message.chat.id))
        async with self.background.lock(scope):
            try:
                await event.client.edit_message_text(
                    chat_id=query.message.chat.id,
                    message_id=query.message.message_id,
                    text=body,
                    parse_mode="HTML",
                    # PTB 22.8 会丢掉空数组；显式保留空键盘以移除应战按钮。
                    reply_markup=InlineKeyboardMarkup(
                        rows or [], api_kwargs={"inline_keyboard": []} if not rows else None
                    ),
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except BadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    raise
            if query.message.chat.type in {"group", "supergroup"}:
                await self.background.record(
                    event.client,
                    scope,
                    query.message,
                    body,
                    kind,
                    query.message.reply_to_message,
                )

    async def notify(self, event, message):
        query = event.message_obj.raw_message.callback_query
        if query is not None:
            await query.answer(text=re.sub(r"<[^>]*>", "", message)[:190], show_alert=True)
        else:
            await self.send(event, message, kind="notice")

    async def execute(self, event, action, *, private=False):
        if not self.config.get("enabled", True):
            return
        if event.get_platform_name() == "telegram":
            update = event.message_obj.raw_message
            chat = update.effective_chat
            if chat and chat.type in {"group", "supergroup"} and not self.group_enabled(chat.id):
                if update.callback_query is not None:
                    event.stop_event()
                    await update.callback_query.answer(
                        text="本群未启用 DickGrowerBot。", show_alert=True
                    )
                return
        event.stop_event()
        try:
            env = self.environment(event)
            self.background.observe(event.client)
            if not private:
                self.group_only(env[2])
            await action(*env)
        except GameError as exc:
            try:
                await self.notify(event, str(exc))
            except Exception:
                logger.warning("DickGrowerBot暂时无法发送业务提示，请检查 Telegram 连接。")
        except Exception as exc:
            # 不展开含 Telegram 客户端的局部变量，避免日志暴露 Token。
            logger.error(
                "DickGrowerBot处理失败（%s）；账本事务或请求去重会保护已保存的数据。",
                type(exc).__name__,
            )
            try:
                await self.notify(
                    event,
                    tr("inline.callback.errors.no_data"),
                )
            except Exception:
                logger.warning("DickGrowerBot暂时无法发送错误提示，请检查 Telegram 连接。")

    @staticmethod
    def callback_data(event, scope):
        interaction = event.get_button_interaction()
        data = interaction.data if interaction else None
        if not isinstance(data, dict) or data.get("scope") != list(scope):
            raise GameError(INVALID_DATA)
        return data

    @filter.platform_adapter_type(filter.PlatformAdapterType.TELEGRAM)
    @filter.command("dick_grow")
    async def grow(self, event: AstrMessageEvent):
        """让你的丁丁变大！"""

        async def action(update, user, chat, scope):
            uid = str(user.id)
            # 同一玩家的签到回复串行，避免并发请求重复展示同一份累计提醒。
            async with self.background.lock((*scope, uid, "grow")):
                kind = "event"
                try:
                    result = await asyncio.to_thread(
                        self.game.grow,
                        scope,
                        uid,
                        user.full_name,
                        str(update.effective_message.message_id),
                    )
                    body = text.grow_text(result, self.settings, self.game.clock())
                except GameError as exc:
                    if str(exc) != tr("commands.grow.tomorrow"):
                        raise
                    snapshot = await asyncio.to_thread(self.game.stats, scope, uid)
                    body = str(exc) + text.time_left(snapshot["next"], self.game.clock())
                    kind = "notice"
                loss = await asyncio.to_thread(self.maintenance.pending_loss, scope, uid)
                if loss:
                    body += f"\n\n太久没签到了，你的鸡鸡累计缩水了 {loss} 厘米。"
                await self.send(event, body, kind=kind)
                if loss:
                    await asyncio.to_thread(self.maintenance.acknowledge_loss, scope, uid, loss)

        await self.execute(event, action)

    @filter.platform_adapter_type(filter.PlatformAdapterType.TELEGRAM)
    @filter.command("dick_pvp")
    async def pvp(self, event: AstrMessageEvent, bet: str = ""):
        """斗鸡！"""

        async def action(update, user, chat, scope):
            result = await asyncio.to_thread(
                self.game.challenge,
                scope,
                str(user.id),
                user.full_name,
                self.amount(bet),
                str(update.effective_message.message_id),
            )
            if result.get("message_id"):
                # Telegram 重投同一条消息时，不再生成第二条无法绑定的挑战消息。
                return
            rows = [[self.button("斗鸡！", "pk", {"scope": list(scope), "id": result["id"]})]]
            try:
                message = await self.send(
                    event, text.challenge_text(result, self.settings), rows, kind="application"
                )
                await asyncio.to_thread(
                    self.game.bind_challenge, scope, result["id"], message.message_id
                )
            except Exception:
                await asyncio.to_thread(self.game.cancel_unsent, scope, result["id"])
                raise

        await self.execute(event, action)

    @filter.platform_adapter_type(filter.PlatformAdapterType.TELEGRAM)
    @filter.button_action("dick:pk")
    async def pk_clicked(self, event: AstrMessageEvent):
        async def action(update, user, chat, scope):
            data = self.callback_data(event, scope)
            if not isinstance(data.get("id"), str):
                raise GameError(INVALID_DATA)
            try:
                result = await asyncio.to_thread(
                    self.game.accept,
                    scope,
                    data["id"],
                    str(user.id),
                    user.full_name,
                    update.callback_query.message.message_id,
                )
            except GameError as exc:
                if str(exc) == tr("commands.pvp.errors.not_enough.initiator"):
                    await self.edit(event, str(exc), kind="notice")
                    return
                raise
            await self.edit(event, text.battle_text(result), kind="event")

        await self.execute(event, action)

    @filter.platform_adapter_type(filter.PlatformAdapterType.TELEGRAM)
    @filter.command("dick_of_day")
    async def dick_of_day(self, event: AstrMessageEvent):
        """选举今日丁丁"""

        async def action(update, user, chat, scope):
            result = await asyncio.to_thread(self.game.dick_of_day, scope)
            await self.send(
                event,
                text.dod_text(result, self.settings, self.game.clock()),
                kind="event" if result["status"] == "chosen" else "notice",
            )

        await self.execute(event, action)

    @filter.platform_adapter_type(filter.PlatformAdapterType.TELEGRAM)
    @filter.command("dick_loan")
    async def loan(self, event: AstrMessageEvent):
        """负数？申请贷款！"""

        async def action(update, user, chat, scope):
            result = await asyncio.to_thread(
                self.game.loan_request,
                scope,
                str(user.id),
                str(update.effective_message.message_id),
            )
            if result.get("message_id"):
                return
            rows = None
            if result["status"] == "confirm":
                rows = [
                    [
                        self.button(
                            tr("commands.loan.confirmation.buttons.agree"),
                            "loan",
                            {"scope": list(scope), "id": result["id"], "agree": True},
                        ),
                        self.button(
                            tr("commands.loan.confirmation.buttons.disagree"),
                            "loan",
                            {"scope": list(scope), "id": result["id"], "agree": False},
                        ),
                    ]
                ]
            message = await self.send(
                event,
                text.loan_text(result),
                rows,
                kind="application" if result["status"] == "confirm" else "notice",
            )
            if result["status"] == "confirm":
                await asyncio.to_thread(
                    self.game.bind_loan, scope, result["id"], message.message_id
                )

        await self.execute(event, action)

    @filter.platform_adapter_type(filter.PlatformAdapterType.TELEGRAM)
    @filter.button_action("dick:loan")
    async def loan_clicked(self, event: AstrMessageEvent):
        async def action(update, user, chat, scope):
            data = self.callback_data(event, scope)
            if not isinstance(data.get("id"), str) or type(data.get("agree")) is not bool:
                raise GameError(INVALID_DATA)
            result = await asyncio.to_thread(
                self.game.loan_answer,
                scope,
                data["id"],
                str(user.id),
                update.callback_query.message.message_id,
                data["agree"],
            )
            if result["status"] == "refused":
                try:
                    await event.client.delete_message(
                        chat_id=chat.id, message_id=update.callback_query.message.message_id
                    )
                    await asyncio.to_thread(
                        self.maintenance.cancel_deletion,
                        scope,
                        update.callback_query.message.message_id,
                    )
                    return
                except Exception:
                    pass
            await self.edit(event, text.loan_text(result), kind="event")

        await self.execute(event, action)

    @filter.platform_adapter_type(filter.PlatformAdapterType.TELEGRAM)
    @filter.command("dick_stats")
    async def stats(self, event: AstrMessageEvent):
        """统计"""

        async def action(update, user, chat, scope):
            if chat.type == "private":
                enabled, disabled = self.group_lists()
                result = await asyncio.to_thread(
                    self.game.personal_stats, scope[0], str(user.id), enabled, disabled
                )
                await self.send(event, tr("commands.stats.personal", **result), kind="report")
            else:
                result = await asyncio.to_thread(self.game.stats, scope, str(user.id))
                await self.send(event, text.stats_text(result, self.settings), kind="report")

        await self.execute(event, action, private=True)

    @filter.platform_adapter_type(filter.PlatformAdapterType.TELEGRAM)
    @filter.button_action("dick:stats")
    async def stats_clicked(self, event: AstrMessageEvent):
        async def action(update, user, chat, scope):
            self.callback_data(event, scope)
            result = await asyncio.to_thread(self.game.stats, scope, str(user.id))
            await self.edit(
                event,
                text.stats_text(result, self.settings),
            )

        await self.execute(event, action)

    @filter.platform_adapter_type(filter.PlatformAdapterType.TELEGRAM)
    @filter.command("dick_top")
    async def top(self, event: AstrMessageEvent, page: str = "1"):
        """获取聊天中最大的丁丁"""

        async def action(update, user, chat, scope):
            result = await asyncio.to_thread(self.game.top, scope, self.amount(page) - 1)
            await self.send(
                event,
                text.top_text(result, user.id),
                self.top_buttons(scope, result),
                kind="report",
            )

        await self.execute(event, action)

    @filter.platform_adapter_type(filter.PlatformAdapterType.TELEGRAM)
    @filter.button_action("dick:top")
    async def top_clicked(self, event: AstrMessageEvent):
        async def action(update, user, chat, scope):
            data = self.callback_data(event, scope)
            result = await asyncio.to_thread(self.game.top, scope, data.get("page", 0))
            await self.edit(event, text.top_text(result, user.id), self.top_buttons(scope, result))

        await self.execute(event, action)

    def cleanup_button(self, label, scope, owner, action, **values):
        return self.button(
            label,
            "cleanup",
            {"scope": list(scope), "owner": str(owner), "action": action, **values},
        )

    async def cleanup_screen(self, scope, owner, pick=None):
        if pick is not None:
            if pick not in CLEANUP_GROUPS:
                raise GameError(INVALID_DATA)
            buttons = [
                self.cleanup_button(
                    text.cleanup_delay(minutes), scope, owner, "set", kind=pick, minutes=minutes
                )
                for minutes in CLEANUP_DELAYS
            ]
            rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
            for key, action, values in [
                ("keep", "set", {"kind": pick, "minutes": 0}),
                ("follow", "follow", {"kind": pick}),
                ("back", "back", {}),
            ]:
                rows.append(
                    [
                        self.cleanup_button(
                            tr(f"commands.cleanup.buttons.{key}"), scope, owner, action, **values
                        )
                    ]
                )
            rows.append([self.cleanup_button("✖️ 关闭", scope, owner, "close")])
            return tr(
                "commands.cleanup.pick.prompt", group=tr(f"commands.cleanup.groups.{pick}")
            ), rows
        settings = await asyncio.to_thread(self.maintenance.cleanup, scope)
        rows = [
            [
                self.cleanup_button(
                    tr(
                        "commands.cleanup.buttons.group",
                        group=tr(f"commands.cleanup.groups.{kind}"),
                        delay=text.cleanup_delay(minutes),
                    ),
                    scope,
                    owner,
                    "pick",
                    kind=kind,
                )
            ]
            for kind, minutes in settings.items()
        ]
        if any(settings.values()):
            rows.append(
                [self.cleanup_button(tr("commands.cleanup.buttons.reset"), scope, owner, "reset")]
            )
        rows.append([self.cleanup_button("✖️ 关闭", scope, owner, "close")])
        return text.cleanup_text(settings), rows

    async def close_cleanup(self, event, scope):
        message = event.message_obj.raw_message.callback_query.message
        async with self.background.lock(scope):
            try:
                await event.client.delete_message(
                    chat_id=message.chat.id, message_id=message.message_id
                )
            except BadRequest as exc:
                if "message to delete not found" not in str(exc).lower():
                    # 超过可删除时限的菜单仍可编辑，移除键盘结束交互。
                    try:
                        await event.client.edit_message_text(
                            chat_id=message.chat.id,
                            message_id=message.message_id,
                            text="设置菜单已关闭。",
                            reply_markup=InlineKeyboardMarkup(
                                [], api_kwargs={"inline_keyboard": []}
                            ),
                        )
                    except BadRequest as edit_exc:
                        if "message is not modified" not in str(edit_exc).lower():
                            raise
            await asyncio.to_thread(self.maintenance.cancel_deletion, scope, message.message_id)

    @staticmethod
    async def require_group_admin(client, chat, user):
        member = await asyncio.wait_for(client.get_chat_member(chat.id, user.id), timeout=15)
        if member.status not in {"creator", "administrator"}:
            raise GameError(tr("commands.cleanup.errors.admins_only"))

    @filter.platform_adapter_type(filter.PlatformAdapterType.TELEGRAM)
    @filter.command("dick_cleanup")
    async def cleanup(self, event: AstrMessageEvent):
        """设置我的消息在本群留多久"""

        async def action(update, user, chat, scope):
            await self.require_group_admin(event.client, chat, user)
            body, rows = await self.cleanup_screen(scope, user.id)
            await self.send(event, body, rows, kind="application")

        await self.execute(event, action)

    @filter.platform_adapter_type(filter.PlatformAdapterType.TELEGRAM)
    @filter.button_action("dick:cleanup")
    async def cleanup_clicked(self, event: AstrMessageEvent):
        async def action(update, user, chat, scope):
            data = self.callback_data(event, scope)
            await self.require_group_admin(event.client, chat, user)
            if data.get("owner") != str(user.id):
                raise GameError(tr("inline.callback.errors.another_user"))
            operation = data.get("action")
            if operation == "close":
                await self.close_cleanup(event, scope)
                return
            kind = data.get("kind")
            if operation in ("pick", "set", "confirm", "follow") and kind not in CLEANUP_GROUPS:
                raise GameError(INVALID_DATA)
            if operation == "pick":
                body, rows = await self.cleanup_screen(scope, user.id, pick=kind)
            else:
                if operation in ("set", "confirm"):
                    minutes = data.get("minutes")
                    if type(minutes) is not int or minutes not in (0, *CLEANUP_DELAYS):
                        raise GameError(INVALID_DATA)
                    if (
                        operation == "set"
                        and minutes
                        and not await self.background.can_delete_commands(event.client, chat.id)
                    ):
                        body = tr(
                            "commands.cleanup.warning.no_rights",
                            group=tr(f"commands.cleanup.groups.{kind}"),
                        )
                        rows = [
                            [
                                self.cleanup_button(
                                    tr("commands.cleanup.buttons.confirm"),
                                    scope,
                                    user.id,
                                    "confirm",
                                    kind=kind,
                                    minutes=minutes,
                                )
                            ],
                            [
                                self.cleanup_button(
                                    tr("commands.cleanup.buttons.back"), scope, user.id, "back"
                                )
                            ],
                            [self.cleanup_button("✖️ 关闭", scope, user.id, "close")],
                        ]
                        await self.edit(event, body, rows, kind="application")
                        return
                    await asyncio.to_thread(self.maintenance.set_cleanup, scope, kind, minutes)
                elif operation == "follow":
                    await asyncio.to_thread(self.maintenance.set_cleanup, scope, kind)
                elif operation == "reset":
                    await asyncio.to_thread(self.maintenance.set_cleanup, scope)
                elif operation != "back":
                    raise GameError(INVALID_DATA)
                body, rows = await self.cleanup_screen(scope, user.id)
            await self.edit(event, body, rows, kind="application")

        await self.execute(event, action)

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register

from .account_store import AccountNotFoundError, AccountSelectionError, AccountStore, AccountStoreError, StoredAccount
from .auto_play import (
    AutoPlayManager,
    calculate_auto_play_interval,
    estimate_play_seconds_from_report_count,
    format_auto_play_interval,
)
from .batch_utils import run_blocking, run_blocking_batch
from .credential_utils import CredentialParseError, parse_credential
from .qqyy import (
    QQMusicAuth,
    QQMusicClient,
    QQMusicClientError,
    fetch_qr_code,
    poll_qr_status,
    refresh_credential,
    report_play_time,
    save_data_url_image,
)


REPORT_TYPE_MAP = {
    "年报": 4,
    "月报": 3,
    "周报": 2,
    "日报": 1,
}
DEFAULT_DENY_MESSAGE = "当前用户或群组未被允许使用 QQ 音乐听歌报告插件"
BATCH_PLAY_ACCOUNT_DELAY_SECONDS = 3


def build_auto_play_start_text(alias: str, interval_text: str) -> str:
    return (
        f'账号 "{alias}" 自动刷时长首刷成功，后台任务已启动；'
        f"当前预计上报间隔：{interval_text}；"
        "之后会按当天剩余时间自适应调整，并在每次上报后延迟检测进度，"
        "连续 3 次时长不变会先放慢，连续 6 次不变才会停止"
    )


def build_user_key(platform_name: str, sender_id: str) -> str:
    return f"{platform_name}:{sender_id}"


def is_group_admin(raw_message: dict[str, Any]) -> bool:
    sender = raw_message.get("sender")
    if not isinstance(sender, dict):
        return False
    role = sender.get("role")
    return role in {"admin", "owner"}


def _normalize_sender_id(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def extract_reply_sender_id(raw_message: dict[str, Any]) -> str | None:
    reply = raw_message.get("reply")
    if isinstance(reply, dict):
        sender = reply.get("sender")
        if isinstance(sender, dict):
            for key in ("user_id", "id"):
                sender_user_id = _normalize_sender_id(sender.get(key))
                if sender_user_id:
                    return sender_user_id

        for key in ("user_id", "sender_id", "from_user_id"):
            reply_sender_id = _normalize_sender_id(reply.get(key))
            if reply_sender_id:
                return reply_sender_id

        if isinstance(sender, dict):
            for key in ("sender_id", "from_user_id"):
                sender_reply_id = _normalize_sender_id(sender.get(key))
                if sender_reply_id:
                    return sender_reply_id

    message = raw_message.get("message")
    if isinstance(message, list):
        for segment in message:
            if not isinstance(segment, dict) or segment.get("type") != "reply":
                continue
            data = segment.get("data")
            if not isinstance(data, dict):
                continue
            for key in ("user_id", "sender_id", "from_user_id"):
                reply_sender_id = _normalize_sender_id(data.get(key))
                if reply_sender_id:
                    return reply_sender_id

    return None


def resolve_target_user_key(platform_name: str, sender_id: str, raw_message: dict[str, Any]) -> str:
    if is_group_admin(raw_message):
        reply_sender_id = extract_reply_sender_id(raw_message)
        if reply_sender_id:
            return build_user_key(platform_name, reply_sender_id)
    return build_user_key(platform_name, sender_id)


def build_account_info_text(info: dict[str, Any]) -> str:
    def get_text(key: str, default: str = "未知") -> str:
        value = info.get(key, default)
        return str(value) if value is not None and str(value) else default

    return "\n".join(
        [
            "QQ 音乐账号信息",
            f"昵称：{get_text('nick')}",
            f"等级：{get_text('level')}",
            f"成长值：{get_text('value')}",
            f"距离下一级：{get_text('need')}",
            f"好友排名：{get_text('rank')}",
            f"已播放时长：{get_text('play_time')}",
            f"当前账号别名：{get_text('alias')}",
        ]
    )


@register("qqyy", "27chcn", "QQ 音乐听歌报告插件", "5.8.0")
class QQYYPlugin(Star):
    @filter.command_group("qqyy")
    def qqyy(self):
        pass

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or AstrBotConfig()
        base_dir = Path(__file__).resolve().parent
        self.store = AccountStore(base_dir / "data" / "qqyy_accounts.json")
        self.temp_dir = base_dir / "tmp"
        self.auto_play = AutoPlayManager(logger)

    @staticmethod
    def _get_sender_context(event: AstrMessageEvent) -> tuple[str, str, dict[str, Any], str]:
        platform_name = str(event.get_platform_name())
        sender_id = str(event.get_sender_id())
        message_obj = getattr(event, "message_obj", None)
        raw_message = getattr(message_obj, "raw_message", {})
        if not isinstance(raw_message, dict):
            raw_message = {}
        user_key = build_user_key(platform_name, sender_id)
        return platform_name, sender_id, raw_message, user_key

    def _get_access_control_config(self) -> dict[str, Any]:
        config = self.config.get("access_control", {})
        return config if isinstance(config, dict) else {}

    @staticmethod
    def _normalize_id_list(value: Any) -> set[str]:
        if not isinstance(value, list):
            return set()
        return {str(item).strip() for item in value if str(item).strip()}

    def _is_event_allowed(self, event: AstrMessageEvent) -> bool:
        access_control = self._get_access_control_config()
        if not access_control.get("enabled", False):
            return True

        allowed_user_ids = self._normalize_id_list(access_control.get("allowed_user_ids", []))
        allowed_group_ids = self._normalize_id_list(access_control.get("allowed_group_ids", []))
        if not allowed_user_ids and not allowed_group_ids:
            return False

        sender_id = str(event.get_sender_id())
        group_id = getattr(event, "get_group_id", lambda: None)()
        if group_id is None:
            return sender_id in allowed_user_ids

        normalized_group_id = str(group_id)
        return sender_id in allowed_user_ids or normalized_group_id in allowed_group_ids

    def _access_denied_result(self, event: AstrMessageEvent) -> MessageEventResult:
        access_control = self._get_access_control_config()
        message = str(access_control.get("deny_message") or DEFAULT_DENY_MESSAGE)
        return event.plain_result(message)

    def _resolve_query_account(self, event: AstrMessageEvent, alias: str = ""):
        platform_name, sender_id, raw_message, _ = self._get_sender_context(event)
        target_user_key = resolve_target_user_key(platform_name, sender_id, raw_message)
        return self.store.resolve_account(target_user_key, alias or None)

    @staticmethod
    def _make_client(account: StoredAccount) -> QQMusicClient:
        return QQMusicClient(QQMusicAuth(uin=account.uin, qqmusic_key=account.qqmusic_key))

    @qqyy.command("登录", desc="扫码登录 QQ 音乐，用法：/qqyy 登录 [别名]")
    async def qr_login(self, event: AstrMessageEvent, alias: str = ""):
        if not self._is_event_allowed(event):
            yield self._access_denied_result(event)
            return
        _, _, _, user_key = self._get_sender_context(event)

        if not alias:
            alias = "大号"
            existing = self.store.list_accounts(user_key)
            if any(a.alias == alias for a in existing):
                yield event.plain_result("账号 \"大号\" 已存在，请指定别名，如：/qqyy 登录 小号")
                return

        try:
            base64_image, identifier = await run_blocking(fetch_qr_code)
        except Exception as exc:
            logger.warning("获取二维码失败: %s", exc)
            yield event.plain_result("获取二维码失败，请稍后重试")
            return

        qr_path = self.temp_dir / f"qr_{identifier}.png"
        qr_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            save_data_url_image(f"data:image/png;base64,{base64_image}", qr_path)
        except Exception as exc:
            logger.warning("保存二维码图片失败: %s", exc)
            yield event.plain_result("保存二维码图片失败，请稍后重试")
            return

        yield event.image_result(str(qr_path))

        try:
            result = await run_blocking(poll_qr_status, identifier)
        except Exception as exc:
            logger.warning("检测二维码状态失败: %s", exc)
            result = None
        finally:
            qr_path.unlink(missing_ok=True)

        if result is None:
            yield event.plain_result("二维码已过期或未被扫码，请重新登录")
            return

        credential_raw = result.get("credential")
        if not credential_raw:
            yield event.plain_result("登录失败，未获取到凭证")
            return

        try:
            parsed_credential = parse_credential(credential_raw, require_uin=True)
        except CredentialParseError:
            yield event.plain_result("登录失败，凭证中缺少必要字段")
            return

        self.store.upsert_account(
            user_key,
            alias,
            parsed_credential.uin or "",
            parsed_credential.musickey,
            parsed_credential.credential_str,
        )
        yield event.plain_result(f"扫码登录成功，已绑定账号 \"{alias}\"")

    @staticmethod
    def _parse_refreshed_credential(resp: dict[str, Any]) -> tuple[str, str] | None:
        data = resp.get("data", {})
        credential_raw = data.get("credential") if isinstance(data, dict) else None
        credential_raw = credential_raw or resp.get("credential")
        if not credential_raw:
            return None

        try:
            parsed_credential = parse_credential(credential_raw, require_uin=False)
        except CredentialParseError:
            return None
        return parsed_credential.musickey, parsed_credential.credential_str

    def _refresh_account_key(self, user_key: str, account: StoredAccount) -> tuple[bool, str]:
        if not account.credential:
            return False, "没有缓存的凭证，无法刷新，请重新扫码登录"

        try:
            resp = refresh_credential(account.credential)
        except Exception as exc:
            logger.warning("刷新密钥失败: %s", exc)
            return False, "刷新密钥失败，请稍后重试"

        parsed = self._parse_refreshed_credential(resp)
        if parsed is None:
            return False, "刷新失败，新凭证格式异常或缺少 musickey"

        musickey, credential_str = parsed
        try:
            self.store.update_account_key(user_key, account.alias, musickey, credential_str)
        except AccountStoreError as exc:
            return False, str(exc)
        return True, "密钥刷新成功"

    @qqyy.command("刷新", desc="刷新 QQ 音乐登录密钥，用法：/qqyy 刷新 [别名]")
    async def refresh_key(self, event: AstrMessageEvent, alias: str = "") -> MessageEventResult:
        if not self._is_event_allowed(event):
            return self._access_denied_result(event)
        try:
            account = self._resolve_query_account(event, alias)
        except (AccountNotFoundError, AccountSelectionError) as exc:
            return event.plain_result(str(exc))

        _, _, _, user_key = self._get_sender_context(event)
        ok, message = await run_blocking(self._refresh_account_key, user_key, account)
        if not ok:
            return event.plain_result(f'账号 "{account.alias}" {message}')
        return event.plain_result(f'账号 "{account.alias}" {message}')

    @qqyy.command("全部刷新", desc="刷新所有绑定账号的 QQ 音乐登录密钥，用法：/qqyy 全部刷新")
    async def refresh_all_keys(self, event: AstrMessageEvent) -> MessageEventResult:
        if not self._is_event_allowed(event):
            return self._access_denied_result(event)
        _, _, _, user_key = self._get_sender_context(event)
        accounts = self.store.list_accounts(user_key)
        if not accounts:
            return event.plain_result("你还没有绑定 QQ 音乐账号，请先使用 /qqyy 登录 或 /qqyy 绑定")

        def _refresh(account: StoredAccount) -> tuple[str, bool, str]:
            ok, message = self._refresh_account_key(user_key, account)
            return account.alias, ok, message

        done = await run_blocking_batch(accounts, _refresh, return_exceptions=True)
        results: dict[str, tuple[bool, str]] = {}
        for account, item in zip(accounts, done):
            if isinstance(item, BaseException):
                logger.warning("批量刷新账号失败: %s", item)
                results[account.alias] = (False, "刷新失败，请稍后重试")
                continue
            alias, ok, message = item
            results[alias] = (ok, message)
        lines: list[str] = []
        success_count = 0
        for account in accounts:
            ok, message = results.get(account.alias, (False, "刷新失败"))
            if ok:
                success_count += 1
            status = "成功" if ok else "失败"
            lines.append(f"- {account.alias}：{status}，{message}")

        return event.plain_result(f"全部刷新完成：成功 {success_count}/{len(accounts)}\n" + "\n".join(lines))

    @qqyy.command("刷时长", desc="刷 QQ 音乐听歌时长，用法：/qqyy 刷时长 [别名]")
    async def play_time(self, event: AstrMessageEvent, alias: str = "") -> MessageEventResult:
        if not self._is_event_allowed(event):
            return self._access_denied_result(event)
        try:
            account = self._resolve_query_account(event, alias)
        except (AccountNotFoundError, AccountSelectionError) as exc:
            return event.plain_result(str(exc))

        try:
            await self.auto_play.report_once(account)
        except Exception as exc:
            logger.warning("刷时长失败: %s", exc)
            return event.plain_result("刷时长失败，请稍后重试")

        return event.plain_result(f"账号 \"{account.alias}\" 刷时长成功（10 分钟）")

    @qqyy.command("自动刷时长", desc="后台自动刷时长直到累计 24 小时，用法：/qqyy 自动刷时长 [别名]")
    async def auto_play_time(self, event: AstrMessageEvent, alias: str = "") -> MessageEventResult:
        if not self._is_event_allowed(event):
            return self._access_denied_result(event)
        try:
            account = self._resolve_query_account(event, alias)
        except (AccountNotFoundError, AccountSelectionError) as exc:
            return event.plain_result(str(exc))

        _, _, _, user_key = self._get_sender_context(event)
        if self.auto_play.is_running(user_key, account.alias):
            return event.plain_result(f'账号 "{account.alias}" 自动刷时长已在运行中')

        try:
            await self.auto_play.report_once(account)
        except Exception as exc:
            logger.warning("自动刷时长首刷失败: %s", exc)
            return event.plain_result(f'账号 "{account.alias}" 自动刷时长首刷失败，请稍后重试')

        interval_text = format_auto_play_interval(calculate_auto_play_interval(estimate_play_seconds_from_report_count(1)))
        try:
            client = self._make_client(account)
            current_play_seconds = await run_blocking(client.get_account_play_time_seconds)
            interval_text = format_auto_play_interval(calculate_auto_play_interval(current_play_seconds))
        except Exception as exc:
            logger.warning("自动刷时长启动时查询上报间隔失败: %s", exc)

        if not self.auto_play.start(
            user_key,
            account,
            event,
            initial_count=1,
            initial_delay=True,
        ):
            return event.plain_result(f'账号 "{account.alias}" 自动刷时长已在运行中')

        return event.plain_result(build_auto_play_start_text(account.alias, interval_text))

    @staticmethod
    def _pending_report_paths(event: AstrMessageEvent) -> list[str]:
        pending = getattr(event, "_qqyy_pending_report_paths", None)
        if isinstance(pending, list):
            return pending
        pending = []
        setattr(event, "_qqyy_pending_report_paths", pending)
        return pending

    @qqyy.command("绑定", desc="绑定 QQ 音乐账号，用法：/qqyy 绑定 <别名> <uin> <qqmusic_key>")
    async def bind_account(
        self,
        event: AstrMessageEvent,
        alias: str,
        uin: str,
        qqmusic_key: str,
    ) -> MessageEventResult:
        if not self._is_event_allowed(event):
            return self._access_denied_result(event)
        _, _, _, user_key = self._get_sender_context(event)
        self.store.upsert_account(user_key, alias, uin, qqmusic_key)
        return event.plain_result(f"已绑定账号 {alias}")

    @qqyy.command("切换", desc="切换默认账号，用法：/qqyy 切换 <别名>")
    async def switch_account(self, event: AstrMessageEvent, alias: str) -> MessageEventResult:
        if not self._is_event_allowed(event):
            return self._access_denied_result(event)
        _, _, _, user_key = self._get_sender_context(event)
        try:
            self.store.set_default_account(user_key, alias)
        except AccountNotFoundError as exc:
            return event.plain_result(str(exc))
        return event.plain_result(f"已切换默认账号为 {alias}")

    @qqyy.command("删除", desc="删除已绑定账号，用法：/qqyy 删除 <别名>")
    async def delete_account(self, event: AstrMessageEvent, alias: str) -> MessageEventResult:
        if not self._is_event_allowed(event):
            return self._access_denied_result(event)
        _, _, _, user_key = self._get_sender_context(event)
        try:
            self.store.delete_account(user_key, alias)
        except AccountNotFoundError as exc:
            return event.plain_result(str(exc))
        return event.plain_result(f"已删除账号 {alias}")

    @qqyy.command("账户列表", desc="查看当前绑定的全部账号，用法：/qqyy 账户列表")
    async def list_accounts(self, event: AstrMessageEvent) -> MessageEventResult:
        if not self._is_event_allowed(event):
            return self._access_denied_result(event)
        _, _, _, user_key = self._get_sender_context(event)
        accounts = self.store.list_accounts(user_key)
        if not accounts:
            return event.plain_result("你还没有绑定 QQ 音乐账号，请先使用 /qqyy 绑定 <别名> <uin> <qqmusic_key>")

        default_alias = self.store.get_default_alias(user_key)
        lines = ["你的 QQ 音乐账户列表"]
        for account in accounts:
            default_mark = "（默认）" if account.alias == default_alias else ""
            lines.append(f"- {account.alias}{default_mark} uin: {account.uin}")
        return event.plain_result("\n".join(lines))

    @qqyy.command("信息", desc="查询账号信息，用法：/qqyy 信息 [别名]")
    async def account_info(self, event: AstrMessageEvent, alias: str = "") -> MessageEventResult:
        if not self._is_event_allowed(event):
            return self._access_denied_result(event)
        try:
            account = self._resolve_query_account(event, alias)
        except (AccountNotFoundError, AccountSelectionError) as exc:
            return event.plain_result(str(exc))

        client = self._make_client(account)
        try:
            info = await run_blocking(client.get_account_info, account.alias)
        except QQMusicClientError:
            return event.plain_result("QQ 音乐接口请求失败，请检查 uin 或 qqmusic_key 是否有效")
        return event.plain_result(build_account_info_text(info))

    @qqyy.command("全部信息", desc="查询所有绑定账号的信息，用法：/qqyy 全部信息")
    async def all_account_info(self, event: AstrMessageEvent) -> MessageEventResult:
        if not self._is_event_allowed(event):
            return self._access_denied_result(event)
        _, _, _, user_key = self._get_sender_context(event)
        accounts = self.store.list_accounts(user_key)
        if not accounts:
            return event.plain_result("你还没有绑定 QQ 音乐账号，请先使用 /qqyy 登录 或 /qqyy 绑定")

        def _fetch(account: StoredAccount) -> tuple[str, str | None]:
            client = self._make_client(account)
            try:
                info = client.get_account_info(account.alias)
                return account.alias, build_account_info_text(info)
            except QQMusicClientError:
                return account.alias, None

        done = await run_blocking_batch(accounts, _fetch, return_exceptions=True)
        results: dict[str, str | None] = {}
        for account, item in zip(accounts, done):
            if isinstance(item, BaseException):
                logger.warning("批量查询账号信息失败: %s", item)
                results[account.alias] = None
                continue
            alias, text = item
            results[alias] = text

        lines: list[str] = []
        for account in accounts:
            text = results.get(account.alias)
            if text:
                lines.append(text)
            else:
                lines.append(f"账号 \"{account.alias}\" 查询失败")
            lines.append("")

        return event.plain_result("\n".join(lines).strip())

    @qqyy.command("全部刷时长", desc="为所有绑定账号各刷一次时长（10 分钟），用法：/qqyy 全部刷时长")
    async def all_play_time(self, event: AstrMessageEvent) -> MessageEventResult:
        if not self._is_event_allowed(event):
            return self._access_denied_result(event)
        _, _, _, user_key = self._get_sender_context(event)
        accounts = self.store.list_accounts(user_key)
        if not accounts:
            return event.plain_result("你还没有绑定 QQ 音乐账号，请先使用 /qqyy 登录 或 /qqyy 绑定")

        def _report(account: StoredAccount) -> tuple[str, bool]:
            try:
                report_play_time(account.uin, account.qqmusic_key)
                return account.alias, True
            except Exception:
                return account.alias, False

        done = await run_blocking_batch(
            accounts,
            _report,
            submit_delay_seconds=BATCH_PLAY_ACCOUNT_DELAY_SECONDS,
            return_exceptions=True,
        )
        results: dict[str, bool] = {}
        for account, item in zip(accounts, done):
            if isinstance(item, BaseException):
                logger.warning("批量刷时长失败: %s", item)
                results[account.alias] = False
                continue
            alias, ok = item
            results[alias] = ok

        lines: list[str] = []
        for account in accounts:
            ok = results.get(account.alias, False)
            status = "成功" if ok else "失败"
            lines.append(f"- {account.alias}：{status}")

        return event.plain_result(f"全部刷时长完成（10 分钟，账号间隔 {BATCH_PLAY_ACCOUNT_DELAY_SECONDS} 秒）\n" + "\n".join(lines))

    @qqyy.command("全部自动刷时长", desc="为所有账号后台自动刷时长直到各 24 小时，用法：/qqyy 全部自动刷时长")
    async def all_auto_play_time(self, event: AstrMessageEvent) -> MessageEventResult:
        if not self._is_event_allowed(event):
            return self._access_denied_result(event)
        _, _, _, user_key = self._get_sender_context(event)
        accounts = self.store.list_accounts(user_key)
        if not accounts:
            return event.plain_result("你还没有绑定 QQ 音乐账号，请先使用 /qqyy 登录 或 /qqyy 绑定")

        def _calc_interval(account: StoredAccount) -> tuple[str, str]:
            client = self._make_client(account)
            current_play_seconds = client.get_account_play_time_seconds()
            return account.alias, format_auto_play_interval(calculate_auto_play_interval(current_play_seconds))

        interval_results = await run_blocking_batch(accounts, _calc_interval, return_exceptions=True)
        interval_by_alias: dict[str, str] = {}
        for account, item in zip(accounts, interval_results):
            if isinstance(item, BaseException):
                logger.warning("批量自动刷时长查询上报间隔失败: %s", item)
                interval_by_alias[account.alias] = format_auto_play_interval(
                    calculate_auto_play_interval(estimate_play_seconds_from_report_count(0))
                )
                continue
            alias, interval_text = item
            interval_by_alias[alias] = interval_text

        started: list[str] = []
        running: list[str] = []
        for index, account in enumerate(accounts):
            if index > 0:
                await asyncio.sleep(BATCH_PLAY_ACCOUNT_DELAY_SECONDS)
            if self.auto_play.start(user_key, account, event):
                started.append(account.alias)
            else:
                running.append(account.alias)

        lines = [
            f"全部自动刷时长已提交：新启动 {len(started)} 个，已在运行 {len(running)} 个",
            f"账号之间已按 {BATCH_PLAY_ACCOUNT_DELAY_SECONDS} 秒间隔错峰提交，后台任务会按当天剩余时间自适应安排上报间隔，并在每次上报后延迟检测进度，连续 3 次时长不变会先放慢，连续 6 次不变才会停止。",
        ]
        if started:
            lines.append("新启动：" + "、".join(started))
            lines.append(
                "当前预计上报间隔："
                + "、".join(f"{alias} {interval_by_alias.get(alias, '后台首次检测后自动计算')}" for alias in started)
            )
        if running:
            lines.append("已在运行：" + "、".join(running))
        return event.plain_result("\n".join(lines))

    async def terminate(self):
        await self.auto_play.shutdown()

    async def _send_report(
        self,
        event: AstrMessageEvent,
        report_type: int,
        alias: str = "",
    ):
        if not self._is_event_allowed(event):
            yield self._access_denied_result(event)
            return
        try:
            account = self._resolve_query_account(event, alias)
        except (AccountNotFoundError, AccountSelectionError) as exc:
            yield event.plain_result(str(exc))
            return

        client = self._make_client(account)
        try:
            image_path = await run_blocking(client.download_summary_image, report_type, self.temp_dir)
        except QQMusicClientError:
            yield event.plain_result("生成总结图片失败，请稍后重试")
            return
        except OSError as exc:
            logger.warning("保存总结图片失败: %s", exc)
            yield event.plain_result("保存总结图片失败，请稍后重试")
            return

        self._pending_report_paths(event).append(str(image_path))
        yield event.image_result(str(image_path))

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        pending_paths = self._pending_report_paths(event)
        if not pending_paths:
            return

        while pending_paths:
            image_path = Path(pending_paths.pop())
            try:
                image_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("删除临时图片失败: %s", exc)

    @qqyy.command("年报", desc="发送年度听歌报告图片，用法：/qqyy 年报 [别名]")
    async def yearly_report(self, event: AstrMessageEvent, alias: str = ""):
        async for result in self._send_report(event, REPORT_TYPE_MAP["年报"], alias):
            yield result

    @qqyy.command("月报", desc="发送月度听歌报告图片，用法：/qqyy 月报 [别名]")
    async def monthly_report(self, event: AstrMessageEvent, alias: str = ""):
        async for result in self._send_report(event, REPORT_TYPE_MAP["月报"], alias):
            yield result

    @qqyy.command("周报", desc="发送周度听歌报告图片，用法：/qqyy 周报 [别名]")
    async def weekly_report(self, event: AstrMessageEvent, alias: str = ""):
        async for result in self._send_report(event, REPORT_TYPE_MAP["周报"], alias):
            yield result

    @qqyy.command("日报", desc="发送日度听歌报告图片，用法：/qqyy 日报 [别名]")
    async def daily_report(self, event: AstrMessageEvent, alias: str = ""):
        async for result in self._send_report(event, REPORT_TYPE_MAP["日报"], alias):
            yield result

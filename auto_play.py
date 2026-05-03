from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from math import ceil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from astrbot.api.event import AstrMessageEvent, MessageChain

from .account_store import StoredAccount
from .qqyy import QQMusicAuth, QQMusicClient, QQMusicClientError, format_play_time, report_play_time


AUTO_PLAY_TARGET_SECONDS = 24 * 3600
AUTO_PLAY_INTERVAL_SECONDS = 10 * 60
AUTO_PLAY_MIN_INTERVAL_SECONDS = 60
AUTO_PLAY_QUERY_DELAY_SECONDS = 15
AUTO_PLAY_CHECK_EVERY = 1
AUTO_PLAY_MAX_UNCHANGED_CHECKS = 3
AUTO_PLAY_SLOWDOWN_UNCHANGED_CHECKS = AUTO_PLAY_MAX_UNCHANGED_CHECKS
AUTO_PLAY_STOP_UNCHANGED_CHECKS = AUTO_PLAY_MAX_UNCHANGED_CHECKS * 2
AUTO_PLAY_MAX_REPORTS = ceil(AUTO_PLAY_TARGET_SECONDS / AUTO_PLAY_INTERVAL_SECONDS)
AUTO_PLAY_MAX_WORKERS = 16


def seconds_until_next_day(now: datetime | None = None) -> int:
    now = now or datetime.now().astimezone()
    next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((next_day - now).total_seconds()))


def calculate_auto_play_interval(current_play_seconds: int, now: datetime | None = None) -> int:
    remaining_play_seconds = max(0, AUTO_PLAY_TARGET_SECONDS - max(0, current_play_seconds))
    if remaining_play_seconds <= 0:
        return 0

    remaining_reports = ceil(remaining_play_seconds / AUTO_PLAY_INTERVAL_SECONDS)
    required_interval = seconds_until_next_day(now) // remaining_reports
    required_interval -= AUTO_PLAY_QUERY_DELAY_SECONDS
    return max(
        AUTO_PLAY_MIN_INTERVAL_SECONDS,
        min(AUTO_PLAY_INTERVAL_SECONDS, int(required_interval)),
    )


def format_auto_play_interval(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"

    minutes, remain_seconds = divmod(seconds, 60)
    if remain_seconds == 0:
        return f"{minutes} 分钟"
    return f"{minutes} 分 {remain_seconds:02d} 秒"


def estimate_play_seconds_from_report_count(count: int) -> int:
    return min(AUTO_PLAY_TARGET_SECONDS, max(0, count) * AUTO_PLAY_INTERVAL_SECONDS)


@dataclass(frozen=True)
class AutoPlayProgressState:
    last_checked_play_time: int
    unchanged_checks: int
    next_interval_seconds: int
    should_stop: bool


def calculate_auto_play_progress_state(
    current_play_seconds: int,
    last_checked_play_time: int | None,
    unchanged_checks: int,
) -> AutoPlayProgressState:
    if last_checked_play_time is not None and current_play_seconds <= last_checked_play_time:
        unchanged_checks += 1
    else:
        unchanged_checks = 0

    should_stop = unchanged_checks >= AUTO_PLAY_STOP_UNCHANGED_CHECKS
    if should_stop or unchanged_checks >= AUTO_PLAY_SLOWDOWN_UNCHANGED_CHECKS:
        next_interval = AUTO_PLAY_INTERVAL_SECONDS
    else:
        next_interval = calculate_auto_play_interval(current_play_seconds)

    return AutoPlayProgressState(
        last_checked_play_time=current_play_seconds,
        unchanged_checks=unchanged_checks,
        next_interval_seconds=next_interval,
        should_stop=should_stop,
    )


@dataclass
class AutoPlayTaskState:
    account: StoredAccount
    task: asyncio.Task
    event: AstrMessageEvent | None = None


class AutoPlayManager:
    def __init__(self, logger: Any) -> None:
        self.logger = logger
        self.tasks: dict[tuple[str, str], AutoPlayTaskState] = {}
        self.executor = ThreadPoolExecutor(max_workers=AUTO_PLAY_MAX_WORKERS)

    @staticmethod
    def build_task_key(user_key: str, alias: str) -> tuple[str, str]:
        return user_key, alias

    @staticmethod
    def _make_client(account: StoredAccount) -> QQMusicClient:
        return QQMusicClient(QQMusicAuth(uin=account.uin, qqmusic_key=account.qqmusic_key))

    async def send_background_text(self, event: AstrMessageEvent | None, text: str) -> None:
        if event is None:
            return
        try:
            await event.send(MessageChain().message(text))
        except Exception as exc:
            self.logger.warning("发送自动刷时长后台消息失败: %s", exc)

    def cleanup(self) -> None:
        for key, state in list(self.tasks.items()):
            if state.task.done():
                self.tasks.pop(key, None)

    def is_running(self, user_key: str, alias: str) -> bool:
        self.cleanup()
        state = self.tasks.get(self.build_task_key(user_key, alias))
        return state is not None and not state.task.done()

    async def report_once(self, account: StoredAccount) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self.executor, report_play_time, account.uin, account.qqmusic_key)

    def start(
        self,
        user_key: str,
        account: StoredAccount,
        event: AstrMessageEvent | None = None,
        initial_count: int = 0,
        initial_delay: bool = False,
    ) -> bool:
        self.cleanup()
        key = self.build_task_key(user_key, account.alias)
        state = self.tasks.get(key)
        if state is not None and not state.task.done():
            return False

        task = asyncio.create_task(
            self._worker(
                user_key,
                account,
                initial_count=initial_count,
                initial_delay=initial_delay,
            )
        )
        self.tasks[key] = AutoPlayTaskState(account=account, task=task, event=event)
        task.add_done_callback(lambda done_task, task_key=key: self._on_task_done(task_key, done_task))
        return True

    def _on_task_done(self, key: tuple[str, str], task: asyncio.Task) -> None:
        state = self.tasks.pop(key, None)
        alias = state.account.alias if state else key[1]
        try:
            message = task.result()
        except asyncio.CancelledError:
            message = f'账号 "{alias}" 自动刷时长已取消'
        except Exception as exc:
            self.logger.exception("自动刷时长后台任务异常: %s", exc)
            message = f'账号 "{alias}" 自动刷时长异常停止：{exc}'

        if state is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self.logger.warning("自动刷时长结束消息无法发送：事件循环已关闭")
            else:
                loop.create_task(self.send_background_text(state.event, message))

    async def _worker(
        self,
        user_key: str,
        account: StoredAccount,
        initial_count: int = 0,
        initial_delay: bool = False,
    ) -> str:
        client = self._make_client(account)
        loop = asyncio.get_running_loop()

        count = initial_count
        last_checked_play_time: int | None = None
        unchanged_checks = 0
        next_interval = calculate_auto_play_interval(estimate_play_seconds_from_report_count(count))
        if initial_delay:
            await asyncio.sleep(AUTO_PLAY_QUERY_DELAY_SECONDS)
            try:
                current = await loop.run_in_executor(self.executor, client.get_account_play_time_seconds)
            except QQMusicClientError as exc:
                self.logger.warning("自动刷时长首刷进度检测失败，将按上报次数继续: %s", exc)
                next_interval = calculate_auto_play_interval(estimate_play_seconds_from_report_count(count))
            else:
                if current >= AUTO_PLAY_TARGET_SECONDS:
                    return f'账号 "{account.alias}" 已播放时长已达到 24 小时，自动刷时长结束（共刷 {count} 次）'

                last_checked_play_time = current
                next_interval = calculate_auto_play_interval(current)
            await asyncio.sleep(next_interval)

        while True:
            try:
                await self.report_once(account)
            except Exception as exc:
                self.logger.warning("自动刷时长失败: %s", exc)
                return f'账号 "{account.alias}" 刷时长请求失败，自动刷时长已停止'

            count += 1
            if count >= AUTO_PLAY_MAX_REPORTS:
                return f'账号 "{account.alias}" 已累计上报 24 小时，自动刷时长结束（共刷 {count} 次）'

            if count % AUTO_PLAY_CHECK_EVERY == 0:
                await asyncio.sleep(AUTO_PLAY_QUERY_DELAY_SECONDS)
                try:
                    current = await loop.run_in_executor(self.executor, client.get_account_play_time_seconds)
                except QQMusicClientError as exc:
                    self.logger.warning("自动刷时长进度检测失败，将按上报次数继续: %s", exc)
                    next_interval = calculate_auto_play_interval(estimate_play_seconds_from_report_count(count))
                    await asyncio.sleep(next_interval)
                    continue

                if current >= AUTO_PLAY_TARGET_SECONDS:
                    return f'账号 "{account.alias}" 已播放时长已达到 24 小时，自动刷时长结束（共刷 {count} 次）'

                progress_state = calculate_auto_play_progress_state(
                    current,
                    last_checked_play_time,
                    unchanged_checks,
                )
                last_checked_play_time = progress_state.last_checked_play_time
                unchanged_checks = progress_state.unchanged_checks
                next_interval = progress_state.next_interval_seconds
                if progress_state.should_stop:
                    return (
                        f'账号 "{account.alias}" 连续 {AUTO_PLAY_STOP_UNCHANGED_CHECKS} 次检测播放时长未增长，'
                        f'自动刷时长已停止（当前 {format_play_time(current)}，共刷 {count} 次）'
                    )

            await asyncio.sleep(next_interval)

    async def shutdown(self) -> None:
        for state in list(self.tasks.values()):
            state.task.cancel()
        if self.tasks:
            await asyncio.gather(
                *(state.task for state in self.tasks.values()),
                return_exceptions=True,
            )
            self.tasks.clear()
        self.executor.shutdown(wait=False, cancel_futures=True)

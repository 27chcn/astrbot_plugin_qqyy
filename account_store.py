from __future__ import annotations

import json
import os
import tempfile
from threading import RLock
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Any


class AccountStoreError(ValueError):
    pass


class AccountNotFoundError(AccountStoreError):
    pass


class AccountSelectionError(AccountStoreError):
    pass


@dataclass(frozen=True)
class StoredAccount:
    alias: str
    uin: str
    qqmusic_key: str
    updated_at: int
    credential: str | None = None


class AccountStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()

    def _load(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                return {}

            try:
                with self.path.open("r", encoding="utf-8") as file:
                    payload = json.load(file)
            except json.JSONDecodeError as exc:
                raise AccountStoreError("账户存储文件已损坏") from exc

            if not isinstance(payload, dict):
                raise AccountStoreError("账户存储文件已损坏")

            return payload

    def _save(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=str(self.path.parent),
                text=True,
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    json.dump(payload, file, ensure_ascii=False, indent=2)
                    file.write("\n")
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(tmp_path, self.path)
            finally:
                tmp_path.unlink(missing_ok=True)

    @staticmethod
    def _require_non_empty(value: str, field_name: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise AccountStoreError(f"{field_name} 不能为空")
        return normalized

    @staticmethod
    def _get_user_record(payload: dict[str, Any], user_key: str) -> dict[str, Any] | None:
        user_record = payload.get(user_key)
        return user_record if isinstance(user_record, dict) else None

    @staticmethod
    def _get_accounts(user_record: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(user_record, dict):
            return {}
        accounts = user_record.get("accounts")
        return accounts if isinstance(accounts, dict) else {}

    @staticmethod
    def _build_stored_account(alias: Any, account: Any) -> StoredAccount | None:
        if not isinstance(alias, str) or not isinstance(account, dict):
            return None

        uin = account.get("uin")
        qqmusic_key = account.get("qqmusic_key")
        updated_at = account.get("updated_at")
        if not isinstance(uin, str) or not isinstance(qqmusic_key, str) or not isinstance(updated_at, int):
            return None

        credential = account.get("credential")
        if not isinstance(credential, str):
            credential = None

        return StoredAccount(
            alias=alias,
            uin=uin,
            qqmusic_key=qqmusic_key,
            updated_at=updated_at,
            credential=credential,
        )

    def upsert_account(self, user_key: str, alias: str, uin: str, qqmusic_key: str, credential: str | None = None) -> None:
        with self._lock:
            user_key = self._require_non_empty(user_key, "user_key")
            alias = self._require_non_empty(alias, "alias")
            uin = self._require_non_empty(uin, "uin")
            qqmusic_key = self._require_non_empty(qqmusic_key, "qqmusic_key")

            payload = self._load()
            user_record = self._get_user_record(payload, user_key)
            if user_record is None:
                user_record = {"default_account": None, "accounts": {}}
                payload[user_key] = user_record

            accounts = self._get_accounts(user_record)
            user_record["accounts"] = accounts
            updated_at = int(time())

            entry: dict[str, Any] = {
                "uin": uin,
                "qqmusic_key": qqmusic_key,
                "updated_at": updated_at,
            }
            if credential is not None:
                entry["credential"] = credential
            accounts[alias] = entry

            if not isinstance(user_record.get("default_account"), str) or not user_record.get("default_account"):
                user_record["default_account"] = alias

            self._save(payload)

    @classmethod
    def _list_valid_accounts(cls, accounts: dict[str, Any]) -> list[StoredAccount]:
        result: list[StoredAccount] = []
        for alias, account in accounts.items():
            stored_account = cls._build_stored_account(alias, account)
            if stored_account is not None:
                result.append(stored_account)
        return result

    def list_accounts(self, user_key: str) -> list[StoredAccount]:
        payload = self._load()
        user_record = self._get_user_record(payload, user_key)
        accounts = self._get_accounts(user_record)
        return self._list_valid_accounts(accounts)

    def get_default_alias(self, user_key: str) -> str:
        payload = self._load()
        user_record = self._get_user_record(payload, user_key)
        default_account = user_record.get("default_account") if user_record else None
        return default_account if isinstance(default_account, str) else ""

    def set_default_account(self, user_key: str, alias: str) -> None:
        with self._lock:
            payload = self._load()
            user_record = self._get_user_record(payload, user_key)
            if not user_record:
                raise AccountNotFoundError(f"未找到别名为 {alias} 的账号")

            accounts = self._get_accounts(user_record)
            if alias in accounts:
                user_record["accounts"] = accounts
                user_record["default_account"] = alias
                self._save(payload)
                return

            raise AccountNotFoundError(f"未找到别名为 {alias} 的账号")

    def delete_account(self, user_key: str, alias: str) -> None:
        with self._lock:
            payload = self._load()
            user_record = self._get_user_record(payload, user_key)
            if not user_record:
                raise AccountNotFoundError(f"未找到别名为 {alias} 的账号")

            accounts = self._get_accounts(user_record)
            if alias not in accounts:
                raise AccountNotFoundError(f"未找到别名为 {alias} 的账号")

            del accounts[alias]

            if not accounts:
                payload.pop(user_key, None)
            else:
                user_record["accounts"] = accounts
                if user_record.get("default_account") == alias:
                    user_record["default_account"] = next(iter(accounts))

            self._save(payload)

    def update_account_key(self, user_key: str, alias: str, qqmusic_key: str, credential: str | None = None) -> None:
        with self._lock:
            user_key = self._require_non_empty(user_key, "user_key")
            alias = self._require_non_empty(alias, "alias")
            qqmusic_key = self._require_non_empty(qqmusic_key, "qqmusic_key")

            payload = self._load()
            user_record = self._get_user_record(payload, user_key)
            if not user_record:
                raise AccountNotFoundError(f"未找到别名为 {alias} 的账号")

            accounts = self._get_accounts(user_record)
            account = accounts.get(alias)
            if not isinstance(account, dict):
                raise AccountNotFoundError(f"未找到别名为 {alias} 的账号")

            account["qqmusic_key"] = qqmusic_key
            account["updated_at"] = int(time())
            if credential is not None:
                account["credential"] = credential

            self._save(payload)

    def resolve_account(self, user_key: str, alias: str | None = None) -> StoredAccount:
        payload = self._load()
        user_record = self._get_user_record(payload, user_key)
        accounts = self._get_accounts(user_record)
        valid_accounts = self._list_valid_accounts(accounts)
        if not valid_accounts:
            raise AccountNotFoundError(
                "你还没有绑定 QQ 音乐账号，请先使用 /qqyy 绑定 <别名> <uin> <qqmusic_key>"
            )

        if alias is not None:
            stored_account = self._build_stored_account(alias, accounts.get(alias))
            if stored_account is None:
                raise AccountNotFoundError(f"未找到别名为 {alias} 的账号")
            return stored_account

        default_alias = user_record.get("default_account") if user_record else None
        if isinstance(default_alias, str) and default_alias:
            default_account = self._build_stored_account(default_alias, accounts.get(default_alias))
            if default_account is not None:
                return default_account

        if len(valid_accounts) == 1:
            return valid_accounts[0]

        raise AccountSelectionError(
            "你有多个账号，但未设置默认账号，请先使用 /qqyy 切换 <别名>"
        )

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


class CredentialParseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedCredential:
    credential_str: str
    musickey: str
    uin: str | None = None


def parse_credential(raw: Any, require_uin: bool = False) -> ParsedCredential:
    if isinstance(raw, str):
        credential_str = raw
        try:
            credential_obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CredentialParseError("credential 不是合法 JSON 字符串") from exc
    elif isinstance(raw, dict):
        credential_obj = raw
        credential_str = json.dumps(raw, ensure_ascii=False)
    else:
        raise CredentialParseError("credential 格式异常")

    if not isinstance(credential_obj, dict):
        raise CredentialParseError("credential 内容格式异常")

    musickey = str(credential_obj.get("musickey") or "").strip()
    if not musickey:
        raise CredentialParseError("credential 缺少 musickey")

    raw_uin = credential_obj.get("str_musicid")
    uin = str(raw_uin).strip() if raw_uin is not None else ""
    if require_uin and not uin:
        raise CredentialParseError("credential 缺少 str_musicid")

    return ParsedCredential(
        credential_str=credential_str,
        musickey=musickey,
        uin=uin or None,
    )

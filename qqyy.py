"""
QQ音乐听歌报告图片下载
- 额外详情图片
- 年 / 月 / 周 / 日总结图片
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests


QR_LOGIN_URL = "https://tk.27xk.cn/zmcw/qqyylogin.php"
API_URL = "https://u6.y.qq.com/cgi-bin/musics.fcg"
IMGSHARE_URL = "https://imgshare.y.qq.com/imgshare/"
SOUND_POWER_WEBCKEY = "QueryLevelDetailPage_GetRank_GetSoundPowerEntry_GetUserSafetyTips_GetTaskModules"
SUMMARY_WEBCKEY = "GetBar_GetRank"
DETAIL_WEBCKEY = "GetDetail"
SUMMARY_PAGE_URL = "https://y.qq.com/m/client/listen_record_report/index.html"
DETAIL_PAGE_URL = "https://y.qq.com/m/client/listen_record_report/index_new.html"
DEFAULT_TIMEOUT = 15
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "qqyy_images"
REPORT_TYPES = {4: "year", 3: "month", 2: "week", 1: "day"}
DETAIL_RING_PIC_COLOR = '["#4066a1","#3e6ba2","#3b729d"]'


class QQMusicClientError(RuntimeError):
    pass


def _require_mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise QQMusicClientError(f"解析 QQ 音乐响应失败: {field_name} 缺失或格式异常")
    return value


def _require_non_empty_str(value: Any, field_name: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise QQMusicClientError(f"解析 QQ 音乐响应失败: {field_name} 缺失")
    return text


def _require_first(value: Any, field_name: str) -> Any:
    if not isinstance(value, list) or not value:
        raise QQMusicClientError(f"解析 QQ 音乐响应失败: {field_name} 缺失或为空")
    return value[0]


def _decode_data_url_image(data_url: str) -> bytes:
    if not isinstance(data_url, str) or "," not in data_url:
        raise QQMusicClientError("图片数据格式异常: 缺少 data URL 内容")

    header, encoded = data_url.split(",", 1)
    if not header.startswith("data:image/") or ";base64" not in header:
        raise QQMusicClientError("图片数据格式异常: 不是 base64 图片 data URL")

    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise QQMusicClientError("图片数据格式异常: base64 解码失败") from exc


def zzc_sign(content: str) -> str:
    h = hashlib.sha1(content.encode()).hexdigest()
    p1 = "".join(h[i] for i in [23, 14, 6, 36, 16, 40, 7, 19] if i < 40)
    p2 = "".join(h[i] for i in [16, 1, 32, 12, 19, 27, 8, 5] if i < 40)
    b = base64.b64encode(
        bytes(
            a ^ b
            for a, b in zip(
                [89, 39, 179, 150, 218, 82, 58, 252, 177, 52, 186, 123, 120, 64, 242, 133, 143, 161, 121, 179],
                bytes.fromhex(h),
            )
        )
    ).decode()
    return f"zzc{p1}{b.replace('+', '').replace('/', '').replace('=', '').lower()}{p2}"


def gtk(cookie_value: str) -> str:
    text = cookie_value.split("p_skey=")[1].split(";")[0] if "p_skey=" in cookie_value else cookie_value
    h = 5381
    for c in text:
        h = h + (h << 5) + ord(c)
    return str(h & 0x7FFFFFFF)


def build_comm(g_tk: str | int, uin: str) -> dict[str, Any]:
    return {
        "g_tk": int(g_tk),
        "uin": int(uin) if str(uin).isdigit() else uin,
        "format": "json",
        "inCharset": "utf-8",
        "outCharset": "utf-8",
        "notice": 0,
        "platform": "h5",
        "needNewCode": 1,
        "ct": 23,
        "cv": 0,
    }


def build_summary_payload(g_tk: str | int, uin: str, report_type: int) -> dict[str, Any]:
    return {
        "comm": build_comm(g_tk, uin),
        "req_0": {
            "module": "music.individuation.ListenReportSvr",
            "method": "GetBar",
            "param": {"type": report_type, "optionType": 1, "uin": "", "version": "v2", "token": ""},
        },
        "req_1": {
            "module": "music.individuation.ListenReportSvr",
            "method": "GetRank",
            "param": {"type": report_type, "uin": "", "version": "v2", "token": ""},
        },
    }


def build_sound_power_payload(g_tk: str | int, uin: str) -> dict[str, Any]:
    return {
        "comm": build_comm(g_tk, uin),
        "req_0": {"module": "music.soundPower.SoundPowerSvr", "method": "QueryLevelDetailPage", "param": {}},
        "req_1": {
            "module": "music.activeCenter.FriendRankSvr",
            "method": "GetRank",
            "param": {"rank_type": 1, "offset": 0, "limit": 60, "last_uin": "", "rankno": 0},
        },
        "req_2": {"module": "music.medalHall.MedalHallEntrySrv", "method": "GetSoundPowerEntry", "param": {"EncUin": ""}},
        "req_3": {"module": "music.basicSvr.SafetyTipsSvr", "method": "GetUserSafetyTips", "param": {"Source": 1}},
        "req_4": {"module": "music.activeCenter.ActTaskNewSvr", "method": "GetTaskModules", "param": {"actID": "1nsAQf", "taskModuleIDs": ["Z1jtHy7"]}},
    }


def build_detail_payload(g_tk: str | int, uin: str) -> dict[str, Any]:
    return {
        "comm": build_comm(g_tk, uin),
        "req_0": {
            "module": "music.individuation.ListenReportSvr",
            "method": "GetDetail",
            "param": {"optionType": 1, "uin": "", "version": "v2", "token": ""},
        },
    }


def build_request_parts(webcgikey: str, payload: dict[str, Any]) -> tuple[dict[str, str], str]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return {"_webcgikey": webcgikey, "_": str(int(time.time() * 1000)), "sign": zzc_sign(body)}, body


@dataclass(frozen=True)
class QQMusicAuth:
    uin: str
    qqmusic_key: str

    @property
    def cookies(self) -> dict[str, str]:
        return {"uin": f"o{self.uin}", "qm_keyst": self.qqmusic_key}

    @property
    def g_tk(self) -> str:
        return gtk(self.qqmusic_key)


@dataclass(frozen=True)
class SummaryImageMeta:
    report_type: int
    period_data: str
    token: str

    @property
    def file_name(self) -> str:
        return f"{REPORT_TYPES.get(self.report_type, self.report_type)}_{self.period_data}.jpg"


@dataclass(frozen=True)
class DetailImageMeta:
    token: str
    enc_uin: str

    @property
    def file_name(self) -> str:
        return "detail.jpg"


def extract_summary_image_meta(report_type: int, response: dict[str, Any]) -> SummaryImageMeta:
    req_0 = _require_mapping(response.get("req_0"), "req_0")
    data = _require_mapping(req_0.get("data"), "req_0.data")
    period_data = _require_non_empty_str(_require_first(data.get("availableDates"), "availableDates"), "availableDates[0]")
    token = _require_non_empty_str(data.get("token"), "token")
    return SummaryImageMeta(report_type, period_data, token)


def extract_detail_image_meta(response: dict[str, Any]) -> DetailImageMeta:
    req_0 = _require_mapping(response.get("req_0"), "req_0")
    data = _require_mapping(req_0.get("data"), "req_0.data")
    user_info = _require_mapping(data.get("userInfo"), "userInfo")
    token = _require_non_empty_str(data.get("token"), "token")
    enc_uin = _require_non_empty_str(user_info.get("uin"), "userInfo.uin")
    return DetailImageMeta(token, enc_uin)


def extract_sound_power_info(response: dict[str, Any]) -> dict[str, Any]:
    req_0 = _require_mapping(response.get("req_0"), "req_0")
    req_1 = _require_mapping(response.get("req_1"), "req_1")
    power = _require_mapping(req_0.get("data"), "req_0.data")
    rank = _require_mapping(req_1.get("data"), "req_1.data")
    info = _require_mapping(power.get("powerInfo"), "powerInfo")

    value = info.get("value")
    next_value = info.get("nextValue")
    try:
        need = int(next_value) - int(value)
    except (TypeError, ValueError) as exc:
        raise QQMusicClientError("解析 QQ 音乐响应失败: powerInfo.value/nextValue 格式异常") from exc

    return {
        "nick": _require_non_empty_str(power.get("nick"), "nick"),
        "level": _require_non_empty_str(info.get("name"), "powerInfo.name"),
        "value": value,
        "need": need,
        "rank": rank.get("my_rank_no", "未知"),
        "play_time": power.get("todayLT", 0),
    }


def build_summary_share_page_url(uin: str, meta: SummaryImageMeta) -> str:
    return f"{SUMMARY_PAGE_URL}?{urlencode({'_hidehd':'1','_miniplayer':'1','_fontscale':'1','uin':uin,'share_image_template':'1','isTest':'0','type':str(meta.report_type),'data':meta.period_data,'option_type':'1','getbar_token':meta.token,'getrank_token':meta.token})}"


def build_detail_share_page_url(meta: DetailImageMeta) -> str:
    return f"{DETAIL_PAGE_URL}?{urlencode({'_hidehd':'1','_fontscale':'1','enc_uin':meta.enc_uin,'share_image_template':'1','isTest':'0','type':'1','data':'undefined','option_type':'1','token':meta.token,'ring_pic_color':DETAIL_RING_PIC_COLOR})}"


def build_imgshare_params() -> dict[str, str]:
    return {"_": str(int(time.time() * 1000))}


def build_base_imgshare_form_data(g_tk: str | int, uin: str, share_url: str) -> dict[str, str]:
    return {
        "g_tk": str(g_tk),
        "uin": str(uin),
        "format": "json",
        "inCharset": "utf-8",
        "outCharset": "utf-8",
        "notice": "0",
        "platform": "h5",
        "needNewCode": "1",
        "ct": "23",
        "cv": "0",
        "cmd": "2",
        "url": share_url,
        "data": "%7B%7D",
        "waitType": "1",
        "width": "750",
        "quality": "85",
        "isTest": "0",
    }


def build_imgshare_headers(share_url: str) -> dict[str, str]:
    return {
        "Origin": "https://y.qq.com",
        "Referer": share_url,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    }


def save_data_url_image(data_url: str, output_path: str | Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(_decode_data_url_image(data_url))
    return output_path


def fetch_qr_code(timeout: int = DEFAULT_TIMEOUT) -> tuple[str, str]:
    result = _request_json(
        "GET",
        QR_LOGIN_URL,
        "获取二维码失败",
        params={"action": "get_qr"},
        timeout=timeout,
    )
    data = result.get("data", {})
    qrcode = data.get("qrcode", {}) if isinstance(data, dict) else {}
    base64_image = qrcode.get("data", "") if isinstance(qrcode, dict) else ""
    identifier = qrcode.get("identifier", "") if isinstance(qrcode, dict) else ""
    if not base64_image or not identifier:
        raise QQMusicClientError("获取二维码失败: 响应缺少二维码数据")
    return base64_image, identifier


def poll_qr_status(identifier: str, timeout: int = 60, request_timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any] | None:
    result = _request_json(
        "GET",
        QR_LOGIN_URL,
        "查询二维码状态失败",
        params={"action": "checking_qr", "identifier": identifier, "timeout": timeout},
        timeout=request_timeout + timeout,
    )
    results = result.get("data", {}).get("results")
    if isinstance(results, list) and results:
        return results[0]
    return None


def refresh_credential(credential_str: str, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    return _request_json(
        "GET",
        QR_LOGIN_URL,
        "刷新登录密钥失败",
        params={"action": "refresh_key", "credential": credential_str},
        timeout=timeout,
    )



PLAY_TIME_URL = "https://stat6.y.qq.com/android/fcgi-bin/imusic_tj"
PLAY_TIME_HEADERS = {
    "User-Agent": "QQMusic 20010508(android 16)",
    "Content-Type": "application/x-www-form-urlencoded",
    "Host": "stat6.y.qq.com",
}
PLAY_TIME_SALT = r"gk2$Lh-&l4#!4iow"
PLAY_TIME_DURATION = "600"
PLAY_TIME_XML_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<root>"
    "<qq>1780350196</qq>"
    "<authst>Q_H_L_63k3NxShn1rV0NuHK3-KS-GaScONY4OjnrTHFQjF4i8fFvtUdaek4xyAa5SH_DNov4ygU0SkmF85ySgXzp81Id4WlmvEueVY4866ue70ywclsEUiba2WeUca6YiRAe51AJZXtCk5rFdqXfRsQ_NS12C33</authst>"
    "<tmeLoginMethod>3</tmeLoginMethod>"
    "<fPersonality>0</fPersonality>"
    "<tmeLoginType>2</tmeLoginType>"
    "<psrf_qqaccess_token>02CD5229B2D21B5384FC26B87DB2BB97</psrf_qqaccess_token>"
    "<psrf_qqopenid>740324228EB88B0BFBE821284DFB26C8</psrf_qqopenid>"
    "<psrf_access_token_expiresAt>1778818889</psrf_access_token_expiresAt>"
    "<OpenUDID>ffffffff90063567000000000033c587</OpenUDID>"
    "<udid>ffffffff90063567000000000033c587</udid>"
    "<ct>11</ct>"
    "<cv>20030508</cv>"
    "<v>20030508</v>"
    "<chid>60009</chid>"
    "<os_ver>16</os_ver>"
    "<tyt_exp_env>0</tyt_exp_env>"
    "<OpenUDID2>ffffffff900635670000019db5b7edb7</OpenUDID2>"
    "<QIMEI36>ccbc5a1ab84c81ec83affacc100011019a14</QIMEI36>"
    "<tmeAppID>qqmusic</tmeAppID>"
    "<tid>7104387857717793792</tid>"
    "<teenMode>0</teenMode>"
    "<M-Value>x2Uzbx0/TZpKqTrpdUogRw==</M-Value>"
    "<ui_mode>1</ui_mode>"
    "<rom>OnePlus/OnePlus/OPD2407/OP615AL1:16/UKQ1.231108.001/V.56a94d5-33cc06a:user/release-keys/</rom>"
    "<uid>6639037901</uid>"
    "<sid>202604252129096639037901</sid>"
    "<aid>03756f9921ed6ae2</aid>"
    "<phonetype>OPD2407</phonetype>"
    "<v4ip>111.23.44.228</v4ip>"
    "<devicelevel>50</devicelevel>"
    "<newdevicelevel>40</newdevicelevel>"
    "<deviceScore>748.332</deviceScore>"
    "<modeSwitch>6</modeSwitch>"
    "<nettype>1030</nettype>"
    "<wid>6639037901</wid>"
    "<hotfix>200000000</hotfix>"
    "<traceid>11_16639037901_1777123792</traceid>"
    "<cid>228</cid>\n"
    '<item cmd="1" optime="1777123806" nettype="1030" QQ="1780350196" uid="6639037901" '
    'os="16" model="OPD2407" version="20.3.5.8" songtype="1" playtype="5" from="1,15," '
    'openstore="0" crytype="2" paytype="1" hijackflag="19" abt="85199_85199003" '
    "ext=\"eyJwbGF5ZXJpZCI6WyIxMCJdLCJhYnQiOlsiODk4MjBfODk4MjAwMTUsODUxOTlfODUxOTkwMDMiXSwidGpyZXBvcnQiOlsiMTVfMTAwMDAyMDlfMV80XzEwMDc0Xzk0MTI4MTE4MTMiXSwiaW50ZXJhY3RpdmVGcm9tIjpbIm15VGFiU2VsZkNyZWF0ZWQiXX0=\" "
    'int12="0" tjreport="15_10000209_1_4_10074_9412811813" desktoplyric="0" playdevice="0" '
    'playlist_mode="0" toptype="10014" parentid="9412811813" string23="folder:9412811813" '
    'outdev="0" url="26" playmode="1" repeat_times="-1" string25="normal" string26="n" '
    'string29="0" supersound="0" cdn="" cdnip="" hasFirstBuffer="3" filetype="4" err="0" '
    'time2="1139" issoftdecode="1" '
    'string30="{&quot;firstBufferActions&quot;:{&quot;7&quot;:[883],&quot;0&quot;:[0]},&quot;secondBufferTimes&quot;:[]}" '
    'component_type="-1" wait_time="15705" player_retry="0" audiotime="249360" '
    'timekey="45ADAAE7DE147CDAEE2DA878B9A941F7" co_singer="蔡健雅" '
    "vkey=\"3A999093C77D4E34981AD07BD3C5616F6861C597582E23628D9DB2136DFE2505F479785824F29221AAB0F5E6B93A33DD3E36DC7E98779DFB\" "
    'time="600" play_duration_mi="26857" errcode="" play_speed="1.0" vip_level="65540" '
    'audio_effect="0:0" mode_string="eyJkZWNvZGVyX3R5cGUiOiIxIiwic291bmRfYmFsYW5jZSI6IjAiLCJwbGF5X3RpbWVfcmV2IjoiMCIsInVzYl9vdXRwdXRfdHlwZSI6IjAiLCJwbGF5X2xpc3RfdHlwZV9pZCI6Ijk0MTI4MTE4MTMiLCJwMnBfaW5fZmlsZXNfZGlyIjoiMSIsInZvbHVtZSI6IjAuNDA2MjUiLCJwbGF5X2xpc3RfdHlwZSI6IjIiLCJzdXBlcl9yZXNvbHV0aW9uIjoiMCIsInNlcnZlcl9zaHVmZmxlX2xpc3QiOiIwIiwiYWxjIjoiMCIsIm91dHB1dF9zZGtfdHlwZSI6IjAiLCJyZXRyeV90eXBlIjoiMCJ9" '
    'string27="eyJzY3JlZW5fb24iOjEsImFwcF9pbiI6MSwiYXBwX3RpbWUiOjM0LCJwbGF5cGFnZV90aW1lIjozNCwic3RhcnRfcGxheXR5cGUiOjAsInN0YXJ0X3BsYXl0aW1lIjowfQ==" '
    'songid="102191338" singerid="112" fversion="0" buildver="1" dts="2"/>'
    "</root>"
)


def replace_xml_fields(xml_string: str, **replacements: str) -> str:
    root = ET.fromstring(xml_string)

    for elem in root.iter():
        tag_local = elem.tag.split("}", 1)[-1] if "}" in elem.tag else elem.tag
        if tag_local in replacements:
            elem.text = replacements[tag_local]
        for attr_name in list(elem.attrib.keys()):
            if attr_name in replacements:
                elem.attrib[attr_name] = replacements[attr_name]

    raw = ET.tostring(root, encoding="unicode")
    if not raw.startswith("<?xml"):
        raw = '<?xml version="1.0" encoding="UTF-8"?>\n' + raw
    return raw


def build_play_time_key(optime: str, qq: str) -> str:
    timekey_str = f"{optime}{PLAY_TIME_DURATION}{qq}{PLAY_TIME_SALT}"
    return hashlib.md5(timekey_str.encode("utf-8")).hexdigest().upper()


def report_play_time(uin: str, authst: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    qq = uin
    optime = str(int(time.time()))
    timekey = build_play_time_key(optime, qq)

    xml_body = replace_xml_fields(
        PLAY_TIME_XML_TEMPLATE,
        qq=qq,
        QQ=qq,
        authst=authst,
        optime=optime,
        time=PLAY_TIME_DURATION,
        timekey=timekey,
    )

    return _request_text(
        "POST",
        PLAY_TIME_URL,
        "上报播放时长失败",
        data=xml_body.encode("utf-8"),
        headers=PLAY_TIME_HEADERS,
        timeout=timeout,
    )


def _request_json(method: str, url: str, error_message: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = requests.request(method, url, **kwargs)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.JSONDecodeError as exc:
        raise QQMusicClientError(f"{error_message}: JSON 响应解析失败: {exc}") from exc
    except requests.RequestException as exc:
        raise QQMusicClientError(f"{error_message}: 请求失败: {exc}") from exc


def _request_text(method: str, url: str, error_message: str, **kwargs: Any) -> str:
    try:
        response = requests.request(method, url, **kwargs)
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        raise QQMusicClientError(f"{error_message}: 请求失败: {exc}") from exc


def format_play_time(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} 秒"

    minutes, remain_seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {remain_seconds:02d} 秒"

    hours, remain_minutes = divmod(minutes, 60)
    return f"{hours} 小时 {remain_minutes} 分"


class QQMusicClient:
    def __init__(self, auth: QQMusicAuth, session: requests.Session | None = None, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.auth = auth
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.cookies.update(auth.cookies)

    def _post_json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.session.post(url, timeout=self.timeout, **kwargs)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.JSONDecodeError as exc:
            raise QQMusicClientError(f"请求 QQ 音乐接口失败: JSON 响应解析失败: {exc}") from exc
        except requests.RequestException as exc:
            raise QQMusicClientError(f"请求 QQ 音乐接口失败: {exc}") from exc

    def _call_api(self, webcgikey: str, payload: dict[str, Any]) -> dict[str, Any]:
        params, body = build_request_parts(webcgikey, payload)
        return self._post_json(API_URL, params=params, data=body)

    def _download(self, share_url: str, file_name: str, output_dir: str | Path) -> Path:
        response = self._post_json(
            IMGSHARE_URL,
            params=build_imgshare_params(),
            headers=build_imgshare_headers(share_url),
            data=build_base_imgshare_form_data(self.auth.g_tk, self.auth.uin, share_url),
        )
        data = _require_non_empty_str(response.get("data"), "imgshare.data")
        return save_data_url_image(data, Path(output_dir) / file_name)

    def fetch_summary_response(self, report_type: int) -> dict[str, Any]:
        return self._call_api(SUMMARY_WEBCKEY, build_summary_payload(self.auth.g_tk, self.auth.uin, report_type))

    def fetch_sound_power_response(self) -> dict[str, Any]:
        return self._call_api(SOUND_POWER_WEBCKEY, build_sound_power_payload(self.auth.g_tk, self.auth.uin))

    def fetch_detail_response(self) -> dict[str, Any]:
        return self._call_api(DETAIL_WEBCKEY, build_detail_payload(self.auth.g_tk, self.auth.uin))

    def get_account_play_time_seconds(self) -> int:
        try:
            info = extract_sound_power_info(self.fetch_sound_power_response())
            return int(info["play_time"])
        except (KeyError, TypeError, ValueError) as exc:
            raise QQMusicClientError("解析 QQ 音乐账户信息失败") from exc

    def get_account_info(self, alias: str) -> dict[str, Any]:
        try:
            info = extract_sound_power_info(self.fetch_sound_power_response())
            play_time = int(info["play_time"])
        except (KeyError, TypeError, ValueError) as exc:
            raise QQMusicClientError("解析 QQ 音乐账户信息失败") from exc

        info["alias"] = alias
        info["play_time"] = format_play_time(play_time)
        return info

    def download_summary_image(self, report_type: int, output_dir: str | Path = DEFAULT_OUTPUT_DIR) -> Path:
        meta = extract_summary_image_meta(report_type, self.fetch_summary_response(report_type))
        return self._download(build_summary_share_page_url(self.auth.uin, meta), meta.file_name, output_dir)

    def download_detail_image(self, output_dir: str | Path = DEFAULT_OUTPUT_DIR) -> Path:
        meta = extract_detail_image_meta(self.fetch_detail_response())
        return self._download(build_detail_share_page_url(meta), meta.file_name, output_dir)

    def download_all_images(self, output_dir: str | Path = DEFAULT_OUTPUT_DIR) -> list[Path]:
        return [self.download_detail_image(output_dir), *(self.download_summary_image(t, output_dir) for t in REPORT_TYPES)]

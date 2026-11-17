"""Client for the user-authorized Ocean Engine audio endpoints."""
from __future__ import annotations

import requests


BASE_URL = "https://aic.oceanengine.com/api/agw/smart_clip"
BASE_PAGE = "https://aic.oceanengine.com/tools/smart_clip/edit"


class AudioProviderError(Exception):
    pass


class CredentialsExpired(AudioProviderError):
    pass


def _headers(bpid: str, cookie: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://aic.oceanengine.com",
        "Referer": f"{BASE_PAGE}?bpId={bpid}&mode=0&industry=0",
        "Cookie": cookie,
    }


def _post(endpoint: str, payload: dict, bpid: str, cookie: str) -> dict:
    try:
        response = requests.post(
            f"{BASE_URL}/{endpoint}",
            params={"bpId": bpid},
            json=payload,
            headers=_headers(bpid, cookie),
            timeout=(10, 90),
            allow_redirects=False,
        )
    except requests.Timeout:
        raise AudioProviderError("音频平台请求超时，请稍后重试") from None
    except requests.RequestException:
        raise AudioProviderError("无法连接音频平台，请检查网络") from None

    try:
        if response.status_code in (401, 403):
            raise CredentialsExpired("Cookie 已失效或无权访问，请更新 Cookie 后重试")
        if response.status_code != 200:
            raise AudioProviderError(f"音频平台请求失败，HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise AudioProviderError("音频平台返回了无法识别的响应") from None
    finally:
        response.close()

    if not isinstance(data, dict):
        raise AudioProviderError("音频平台返回了无效响应")
    def error_text(value):
        if isinstance(value, dict):
            return " ".join(
                [str(value.get(key, "")) for key in ("message", "msg", "error", "detail")]
                + [error_text(child) for child in value.values() if isinstance(child, (dict, list))]
            )
        if isinstance(value, list):
            return " ".join(error_text(child) for child in value)
        return ""

    message = error_text(data).lower()
    if any(word in message for word in (
        "登录", "失效", "过期", "unauthorized", "forbidden", "login required", "cookie",
    )):
        raise CredentialsExpired("Cookie 已失效或需要重新登录，请更新 Cookie 后重试")
    if data.get("code") not in (None, 0, "0", 200, "200"):
        raise AudioProviderError("音频平台处理失败，请检查账号权限或请求参数")
    return data


def split_sentences(text: str, bpid: str, cookie: str) -> list[dict]:
    response = _post(
        "split_sentences", {"text": text, "withUseAs": True}, bpid, cookie,
    )
    segments = (response.get("data") or {}).get("segments")
    if not isinstance(segments, list) or not segments:
        raise AudioProviderError("断句接口未返回有效分段")
    if any(not isinstance(item, dict) or not isinstance(item.get("text"), str)
           or not item["text"] for item in segments):
        raise AudioProviderError("断句接口返回了无效分段")
    return segments


def text_to_speech(
    text: str,
    segments: list[dict],
    bpid: str,
    cookie: str,
    voice_element_id: str,
    voice_speed: float,
) -> str:
    response = _post(
        "text_to_speech",
        {
            "text": text,
            "segments": segments,
            "voiceOption": {
                "voiceElementId": voice_element_id,
                "voiceSpeed": voice_speed,
            },
        },
        bpid,
        cookie,
    )
    data = response.get("data") or {}
    audio_url = data.get("audioUrl") or data.get("audio_url")
    if not isinstance(audio_url, str) or not audio_url.startswith("https://"):
        raise AudioProviderError("语音生成接口未返回有效 HTTPS 音频地址")
    return audio_url

"""Select speech-safe background music from the shared online catalog."""
from __future__ import annotations

from collections import Counter
import json

from . import media_library


_PROFILE_TERMS = {
    "professional": ("科技", "现代", "专业", "商务", "企业", "明亮", "简洁", "大气", "品质", "品牌", "产品", "广告", "technology", "corporate", "modern", "bright"),
    "serious": ("紧张", "悬疑", "严肃", "警示", "克制", "沉稳", "深沉", "serious", "suspense", "warning"),
    "energetic": ("动感", "活力", "运动", "节奏", "律动", "激励", "热血", "强劲", "嘻哈", "电子", "energetic", "dynamic", "sport", "electronic"),
    "warm": ("温暖", "轻松", "柔和", "治愈", "生活", "清新", "舒缓", "warm", "soft", "lifestyle"),
    "positive": ("积极", "明快", "轻快", "欢快", "希望", "鼓舞", "上扬", "行动", "positive", "upbeat", "uplifting"),
    "calm": ("舒缓", "平稳", "轻柔", "克制", "简洁", "背景", "纯音乐", "氛围", "慢", "calm", "ambient", "instrumental"),
}
_PROFILE_LABELS = {
    "professional": "专业科技感", "serious": "严肃警示感", "energetic": "动感活力",
    "warm": "温暖生活感", "positive": "积极行动感", "calm": "克制口播背景",
}
_INTENT_PROFILES = {
    "pain_point": ("serious",),
    "product_overview": ("professional",),
    "product_detail": ("professional",),
    "instruction_manual": ("professional", "calm"),
    "otc_certification": ("professional", "serious"),
    "qr_traceability": ("professional",),
    "authenticity_comparison": ("serious", "professional"),
    "usage_instruction": ("professional", "calm"),
    "medication_moment": ("calm", "warm"),
    "factory_process": ("professional", "positive"),
    "service_support": ("warm", "professional"),
    "body_condition": ("energetic",),
    "exercise_action": ("energetic",),
    "mechanism_explanation": ("professional",),
    "lifestyle_scene": ("warm",),
    "experience_proof": ("positive", "warm"),
    "cta": ("positive", "energetic"),
    "talking_head": ("calm",),
}
_SCRIPT_PROFILES = {
    "运动": "energetic", "跑步": "energetic", "身材": "energetic", "减脂": "energetic",
    "痛点": "serious", "真假": "serious", "正品": "serious", "警惕": "serious",
    "工厂": "professional", "生产": "professional", "OTC": "professional", "溯源码": "professional",
    "说明书": "professional", "机理": "professional", "成分": "professional",
    "生活": "warm", "客服": "warm", "体验": "warm",
    "链接": "positive", "行动": "positive", "购买": "positive", "库存": "positive",
}


def _flatten(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in ("[", "{"):
            try:
                return _flatten(json.loads(stripped))
            except (TypeError, ValueError):
                pass
        return [stripped] if stripped else []
    if isinstance(value, dict):
        return [text for item in value.values() for text in _flatten(item)]
    if isinstance(value, (list, tuple, set)):
        return [text for item in value for text in _flatten(item)]
    return [str(value)]


def _profiles(script: str, shots) -> Counter:
    profiles = Counter({"calm": 1, "professional": 1})
    for shot in shots or ():
        intent = getattr(shot, "visual_intent", None)
        if intent is None and isinstance(shot, dict):
            intent = shot.get("visual_intent")
        for profile in _INTENT_PROFILES.get(str(intent), ("calm",)):
            profiles[profile] += 1
    upper_script = script.upper()
    for word, profile in _SCRIPT_PROFILES.items():
        if word.upper() in upper_script:
            profiles[profile] += 2
    return profiles


def _row_text(row: dict) -> str:
    fields = (
        "suggested_title", "source_filename", "content_summary", "genres", "moods",
        "instruments", "tempo_description", "energy", "usage_scenes",
    )
    return " ".join(text for field in fields for text in _flatten(row.get(field))).lower()


def rank_rows(rows: list[dict], script: str, shots) -> list[dict]:
    profiles = _profiles(script, shots)
    ranked = []
    for row in rows:
        if row.get("has_vocals") not in (None, 0, False, "0"):
            continue
        try:
            duration_us = int(float(row.get("duration") or 0) * 1_000_000)
        except (TypeError, ValueError):
            continue
        url = str(row.get("oss_url") or "").strip()
        if duration_us <= 0 or not url:
            continue
        text = _row_text(row)
        reasons, score = [], 0.0
        for profile, weight in profiles.items():
            hits = [term for term in _PROFILE_TERMS[profile] if term.lower() in text]
            if hits:
                score += weight * min(len(hits), 3)
                reasons.append(_PROFILE_LABELS[profile])
        if score <= 0:
            continue
        ranked.append({
            "id": row.get("id"),
            "title": row.get("suggested_title") or row.get("source_filename") or f"music-{row.get('id')}",
            "url": url,
            "duration_us": duration_us,
            "match_score": score,
            "match_reasons": list(dict.fromkeys(reasons)),
        })
    ranked.sort(key=lambda item: (-item["match_score"], item["duration_us"], str(item["id"])))
    if ranked:
        top = ranked[0]["match_score"]
        ranked = [item for item in ranked if item["match_score"] >= top * 0.45]
        for item in ranked:
            item["match_score"] = round(item["match_score"] / top, 4)
    return ranked


def select_for_versions(script: str, shots, duration_us: int, count: int) -> list[dict | None]:
    """Return one suitable catalog track per draft, reusing only after candidates are exhausted."""
    with media_library._connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT id,source_filename,duration,oss_url,suggested_title,content_summary,
                          genres,moods,instruments,has_vocals,tempo_description,energy,
                          usage_scenes
                   FROM myh_music_archive
                   WHERE upload_status=%s AND analysis_status=%s
                     AND oss_url IS NOT NULL AND oss_url<>'' AND duration>=%s
                     AND (has_vocals=0 OR has_vocals IS NULL)
                   LIMIT 500""",
                ("completed", "completed", duration_us / 1_000_000),
            )
            rows = list(cursor.fetchall())
    ranked = rank_rows(rows, script, shots)
    if not ranked:
        return [None] * count
    return [ranked[index % len(ranked)].copy() for index in range(count)]

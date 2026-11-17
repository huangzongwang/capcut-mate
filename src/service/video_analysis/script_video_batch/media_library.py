"""Read-only catalog access plus idempotent use reservations for batch drafts."""
from __future__ import annotations

from contextlib import contextmanager
from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import quote, urlsplit, urlunsplit

from dotenv import dotenv_values

from .. import model_config_store


def _settings() -> dict:
    values = dotenv_values(model_config_store.DB_FILE, encoding="utf-8-sig")
    for suffix in ("TYPE", "HOST", "PORT", "USER", "PASSWORD", "NAME"):
        if values.get("ANALYSIS_DB_" + suffix):
            values["DATABASE_" + suffix] = values["ANALYSIS_DB_" + suffix]
    required = ("DATABASE_HOST", "DATABASE_PORT", "DATABASE_USER", "DATABASE_PASSWORD", "DATABASE_NAME")
    if any(not values.get(key) for key in required):
        raise RuntimeError("env.db.dev 缺少线上素材数据库配置")
    if values.get("DATABASE_TYPE", "mysql").lower() != "mysql":
        raise RuntimeError("素材库当前仅支持 MySQL")
    return values


@contextmanager
def _connection(write: bool = False):
    import pymysql

    settings = _settings()
    connection = pymysql.connect(
        host=settings["DATABASE_HOST"], port=int(settings["DATABASE_PORT"]),
        user=settings["DATABASE_USER"], password=settings["DATABASE_PASSWORD"],
        database=settings["DATABASE_NAME"], charset="utf8mb4", autocommit=False,
        connect_timeout=10, read_timeout=30, write_timeout=30,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        if not write:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
        yield connection
        if write:
            connection.commit()
        else:
            connection.rollback()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _ensure_usage_tables(connection) -> None:
    """Create only this feature's usage tables; catalog tables belong to the media platform."""
    statements = (
        """CREATE TABLE IF NOT EXISTS myh_media_usage_events (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            idempotency_key VARCHAR(191) NOT NULL,
            batch_task_id VARCHAR(64) NOT NULL,
            variant_id VARCHAR(64) NOT NULL,
            source_scene_id VARCHAR(64) NOT NULL,
            asset_id BIGINT UNSIGNED NOT NULL,
            temporal_feature_id BIGINT UNSIGNED NOT NULL,
            source_start_us BIGINT UNSIGNED NOT NULL,
            source_end_us BIGINT UNSIGNED NOT NULL,
            match_score DECIMAL(7,4) NULL,
            match_reason JSON NULL,
            draft_id VARCHAR(128) NULL,
            status VARCHAR(16) NOT NULL,
            reserved_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
            used_at DATETIME(3) NULL,
            released_at DATETIME(3) NULL,
            error_message TEXT NULL,
            created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
            updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
            PRIMARY KEY (id), UNIQUE KEY uk_myh_media_usage_idem (idempotency_key),
            KEY idx_myh_media_usage_batch (batch_task_id, variant_id),
            KEY idx_myh_media_usage_clip_status (asset_id, temporal_feature_id, status),
            KEY idx_myh_media_usage_status_time (status, reserved_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        """CREATE TABLE IF NOT EXISTS myh_media_usage_stats (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            asset_id BIGINT UNSIGNED NOT NULL,
            temporal_feature_id BIGINT UNSIGNED NOT NULL DEFAULT 0,
            successful_use_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
            reserved_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
            last_used_at DATETIME(3) NULL,
            created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
            updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
            PRIMARY KEY (id), UNIQUE KEY uk_myh_media_usage_stat_scope (asset_id, temporal_feature_id),
            KEY idx_myh_media_usage_count (successful_use_count, last_used_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    )
    with connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)


def ensure_usage_tables() -> None:
    with _connection(write=True) as connection:
        _ensure_usage_tables(connection)


def ensure_catalog_tables() -> None:
    """Create the two authorized catalog tables only when absent; never alter existing tables."""
    statements = {
        "myh_media_analysis": """CREATE TABLE IF NOT EXISTS myh_media_analysis (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            asset_id BIGINT UNSIGNED NOT NULL,
            task_id VARCHAR(64) NOT NULL,
            analysis_version VARCHAR(32) NOT NULL,
            is_effective TINYINT(1) NOT NULL DEFAULT 0,
            taxonomy_version VARCHAR(32) NULL,
            prompt_version VARCHAR(32) NULL,
            vision_model VARCHAR(100) NULL,
            asr_model VARCHAR(100) NULL,
            content_summary TEXT NULL,
            scene_category VARCHAR(100) NULL,
            sub_categories JSON NULL,
            objects JSON NULL,
            people_count INT NULL,
            person_attributes JSON NULL,
            actions JSON NULL,
            environment JSON NULL,
            camera_type JSON NULL,
            camera_angle JSON NULL,
            camera_motion JSON NULL,
            composition JSON NULL,
            lighting JSON NULL,
            color_style JSON NULL,
            emotion JSON NULL,
            has_text TINYINT(1) NULL,
            has_watermark TINYINT(1) NULL,
            has_product TINYINT(1) NULL,
            product_category VARCHAR(100) NULL,
            speech_text TEXT NULL,
            embedding JSON NULL,
            embedding_model VARCHAR(100) NULL,
            embedding_dimension INT NULL,
            quality_score DECIMAL(5,2) NULL,
            commercial_score DECIMAL(5,2) NULL,
            confidence DECIMAL(5,4) NULL,
            missing_fields JSON NULL,
            analysis_status VARCHAR(32) NOT NULL DEFAULT 'queued',
            analysis_stage VARCHAR(64) NOT NULL DEFAULT 'queued',
            progress INT NOT NULL DEFAULT 0,
            retry_count INT NOT NULL DEFAULT 0,
            error_message TEXT NULL,
            warnings JSON NULL,
            raw_response JSON NULL,
            created_by BIGINT UNSIGNED NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            completed_at DATETIME NULL,
            PRIMARY KEY (id),
            UNIQUE KEY uk_myh_media_analysis_task_id (task_id),
            KEY idx_myh_media_analysis_status (analysis_status),
            KEY idx_myh_media_analysis_updated_at (updated_at),
            KEY idx_myh_media_analysis_asset_effective_status (asset_id,is_effective,analysis_status),
            KEY idx_myh_media_analysis_status_scene (analysis_status,scene_category),
            CONSTRAINT fk_myh_media_analysis_asset_id_myh_assets
                FOREIGN KEY (asset_id) REFERENCES myh_assets(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
          COMMENT='视频素材AI分析结果'""",
        "myh_media_temporal_features": """CREATE TABLE IF NOT EXISTS myh_media_temporal_features (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            analysis_id BIGINT UNSIGNED NOT NULL,
            asset_id BIGINT UNSIGNED NOT NULL,
            shot_start_us BIGINT UNSIGNED NOT NULL,
            shot_end_us BIGINT UNSIGNED NOT NULL,
            shot_index INT NOT NULL,
            usable_start_us BIGINT UNSIGNED NULL,
            usable_end_us BIGINT UNSIGNED NULL,
            usable_duration_us BIGINT UNSIGNED NULL,
            content_summary TEXT NULL,
            scene_category VARCHAR(64) NULL,
            actions JSON NULL,
            primary_usage_role VARCHAR(32) NULL,
            usage_roles JSON NULL,
            product_id VARCHAR(64) NULL,
            brand_id VARCHAR(64) NULL,
            detected_product_name VARCHAR(255) NULL,
            detected_brand_name VARCHAR(255) NULL,
            has_speech TINYINT(1) NULL,
            has_subtitles TINYINT(1) NULL,
            has_overlay_text TINYINT(1) NULL,
            has_watermark TINYINT(1) NULL,
            motion_intensity DOUBLE NULL,
            brightness_avg DOUBLE NULL,
            color_histogram JSON NULL,
            face_direction JSON NULL,
            subject_position JSON NULL,
            camera_type JSON NULL,
            camera_motion JSON NULL,
            lighting JSON NULL,
            color_style JSON NULL,
            semantic_embedding JSON NULL,
            semantic_embedding_model VARCHAR(100) NULL,
            semantic_embedding_dimension INT NULL,
            semantic_embedding_version VARCHAR(32) NULL,
            first_frame_embedding JSON NULL,
            last_frame_embedding JSON NULL,
            embedding_model VARCHAR(100) NULL,
            embedding_dimension INT NULL,
            confidence DECIMAL(5,4) NULL,
            missing_fields JSON NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            UNIQUE KEY uk_myh_media_temporal_analysis_shot (analysis_id,shot_index),
            KEY idx_myh_media_temporal_asset_time (asset_id,shot_start_us),
            KEY idx_myh_media_temporal_analysis_usage (analysis_id,primary_usage_role),
            KEY idx_myh_media_temporal_product_usage (product_id,primary_usage_role),
            KEY idx_myh_media_temporal_asset_duration (asset_id,usable_duration_us),
            CONSTRAINT fk_myh_media_temporal_features_analysis_id_myh_media_analysis
                FOREIGN KEY (analysis_id) REFERENCES myh_media_analysis(id) ON DELETE CASCADE,
            CONSTRAINT fk_myh_media_temporal_features_asset_id_myh_assets
                FOREIGN KEY (asset_id) REFERENCES myh_assets(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
          COMMENT='视频镜头时序及视觉特征'""",
    }
    with _connection(write=True) as connection, connection.cursor() as cursor:
        cursor.execute("""SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()
            AND TABLE_NAME IN ('myh_assets','myh_media_analysis','myh_media_temporal_features')""")
        present = {row["TABLE_NAME"] for row in cursor.fetchall()}
        if "myh_assets" not in present:
            raise RuntimeError("素材库缺少主表 myh_assets，不能创建关联分析表")
        for table in ("myh_media_analysis", "myh_media_temporal_features"):
            if table not in present:
                cursor.execute(statements[table])


def _json(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _join_access_url(access_url: str, storage_path: str) -> str:
    parsed = urlsplit(access_url or "")
    replacement = (
        os.getenv("MEDIA_LIBRARY_PUBLIC_BASE_URL", "")
        or str(_settings().get("MEDIA_LIBRARY_PUBLIC_BASE_URL") or "")
    ).strip()
    if replacement:
        base = urlsplit(replacement)
        if base.scheme not in ("http", "https") or not base.netloc:
            raise RuntimeError("MEDIA_LIBRARY_PUBLIC_BASE_URL 必须是 HTTP(S) base URL")
        path = "/".join(part for part in (base.path.rstrip("/"), quote(storage_path.lstrip("/"), safe="/%:@")) if part)
        return urlunsplit((base.scheme, base.netloc, "/" + path.lstrip("/"), "", ""))
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError("素材缺少可用的 HTTP(S) access_url；请配置 MEDIA_LIBRARY_PUBLIC_BASE_URL")
    if parsed.hostname in ("127.0.0.1", "localhost", "0.0.0.0"):
        raise RuntimeError("素材库 access_url 指向 loopback；请配置 MEDIA_LIBRARY_PUBLIC_BASE_URL")
    return access_url


def _action_event_text(event: dict) -> str:
    return " ".join(str(event.get(key) or "").strip() for key in (
        "subject", "action", "object", "description",
    )).strip()


def _best_action_event(events: list[dict], *, query: str, usage_role: str,
                       product_names: list[str], visual_intent: str | None,
                       required_elements: list[str], duration_us: int,
                       usable_start_us: int, usable_end_us: int) -> dict | None:
    best = None
    for event in events:
        event_text = _action_event_text(event)
        if not event_text:
            continue
        required_matches = {
            item: _element_matches(item, event_text) for item in required_elements
        }
        coverage = sum(required_matches.values()) / len(required_matches) if required_matches else 0.0
        intents = _visual_intent_matches(
            query, event_text, product_names, visual_intent=visual_intent,
        )
        intent_match = any(intents.values())
        similarity = _semantic_similarity(query, event_text)
        if not (coverage or intent_match or similarity >= 0.25):
            continue

        confidence = max(0.0, min(1.0, float(event.get("confidence") or 0)))
        if confidence < 0.5:
            continue
        event_start = max(usable_start_us, int(event.get("start_us") or 0))
        event_end = int(event.get("end_us") or 0)
        if event_end <= event_start or event_start + duration_us > usable_end_us:
            continue
        score = coverage * 0.45 + float(intent_match) * 0.35 + similarity * 0.15 + confidence * 0.05
        candidate = {
            "id": int(event["id"]) if event.get("id") is not None else None,
            "subject": str(event.get("subject") or ""),
            "action": str(event.get("action") or ""),
            "object": str(event.get("object") or ""),
            "description": str(event.get("description") or ""),
            "start_us": event_start,
            "end_us": event_end,
            "evidence_frame_times_us": _json(event.get("evidence_frame_times_us")) or [],
            "confidence": confidence,
            "match_score": round(score, 4),
            "required_element_coverage": round(coverage, 4),
        }
        if best is None or (candidate["match_score"], confidence) > (
            best["match_score"], best["confidence"],
        ):
            best = candidate
    return best


def _load_action_events(cursor, rows: list[dict]) -> dict[int, list[dict]]:
    feature_ids = sorted({int(row["temporal_feature_id"]) for row in rows if row.get("temporal_feature_id")})
    if not feature_ids:
        return {}
    placeholders = ",".join(["%s"] * len(feature_ids))
    cursor.execute(f"""
        SELECT tf.id AS temporal_feature_id, e.id, e.subject, e.action, e.object,
               e.start_us, e.end_us, e.evidence_frame_times_us, e.description, e.confidence
        FROM myh_media_temporal_features tf
        JOIN myh_media_action_events e
          ON e.analysis_id=tf.analysis_id AND e.asset_id=tf.asset_id
         AND e.start_us < tf.usable_end_us AND e.end_us > tf.usable_start_us
        WHERE tf.id IN ({placeholders}) AND e.end_us > e.start_us
        ORDER BY e.confidence DESC, e.start_us ASC
    """, feature_ids)
    result: dict[int, list[dict]] = {}
    for event in cursor.fetchall():
        result.setdefault(int(event["temporal_feature_id"]), []).append(event)
    return result


def search_clips(*, query: str, usage_role: str, product_names: list[str], duration_us: int,
                 visual_intent: str | None = None,
                 required_elements: list[str] | None = None,
                 forbidden_elements: list[str] | None = None, limit: int = 30,
                 semantic_visual_only: bool = False) -> list[dict]:
    """Hard-filter by valid analysis and time range, then lightweight semantic reranking."""
    visual_intent = matching_visual_intent(query, visual_intent)
    if visual_intent == "qr_code_visible" and not semantic_visual_only:
        required_elements = ["二维码或条形码"]
    sql = """
        SELECT a.id AS asset_id, a.asset_name, a.asset_type, a.mime_type, a.storage_path,
               a.access_url, a.file_size, a.sha256, a.width, a.height, a.duration,
               ma.id AS analysis_id, ma.content_summary AS asset_summary,
               ma.scene_category AS asset_category, ma.objects, ma.actions AS asset_actions,
               ma.product_category, ma.has_product, ma.has_text, ma.has_watermark,
               ma.quality_score, ma.commercial_score,
               tf.id AS temporal_feature_id, tf.shot_index, tf.shot_start_us, tf.shot_end_us,
               tf.usable_start_us, tf.usable_end_us,
               tf.content_summary, tf.scene_category, tf.actions, tf.primary_usage_role,
               tf.usage_roles, tf.product_id, tf.brand_id, tf.detected_product_name,
               tf.detected_brand_name, tf.has_speech, tf.has_subtitles, tf.has_overlay_text,
               tf.has_watermark AS clip_has_watermark, tf.motion_intensity, tf.camera_motion,
               COALESCE(us_clip.successful_use_count,0) AS usage_count
        FROM myh_assets a
    """
    sql += """
        JOIN myh_media_analysis ma ON ma.asset_id=a.id AND ma.is_effective=1
            AND ma.analysis_status IN ('succeeded','completed')
        JOIN myh_media_temporal_features tf ON tf.analysis_id=ma.id AND tf.asset_id=a.id
        LEFT JOIN myh_media_usage_stats us_clip ON us_clip.asset_id=a.id AND us_clip.temporal_feature_id=tf.id
        WHERE a.deleted_at IS NULL AND a.status=1 AND a.asset_type='video'
          AND tf.usable_start_us IS NOT NULL AND tf.usable_end_us IS NOT NULL
          -- Do not trust the optional usable_duration_us summary as a hard gate.
          -- _fit_clip_range below validates the actual temporal boundaries against
          -- the requested scene duration.
          AND tf.usable_end_us > tf.usable_start_us
          AND (tf.primary_usage_role IS NULL OR tf.primary_usage_role NOT IN ('talking_head','oral_delivery'))
        ORDER BY COALESCE(us_clip.successful_use_count,0) ASC,
                 COALESCE(tf.confidence,0) DESC,
                 COALESCE(ma.commercial_score,ma.quality_score,0) DESC
        LIMIT %s
    """
    with _connection() as connection, connection.cursor() as cursor:
        cursor.execute("""SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()
            AND TABLE_NAME IN ('myh_assets','myh_media_analysis','myh_media_temporal_features',
                               'myh_media_action_events')""")
        present = {row["TABLE_NAME"] for row in cursor.fetchall()}
        if not {"myh_assets", "myh_media_analysis", "myh_media_temporal_features"}.issubset(present):
            raise RuntimeError("素材库缺少 myh_assets、myh_media_analysis 或 myh_media_temporal_features")
        cursor.execute(sql, (limit * 8,))
        rows = cursor.fetchall()
        events_by_feature = (
            _load_action_events(cursor, rows) if "myh_media_action_events" in present else {}
        )
    result = []
    for row in rows:
        roles = _json(row.get("usage_roles")) or []
        if isinstance(roles, str):
            roles = [roles]
        role = row.get("primary_usage_role") or ""
        usage_aliases = {
            "product_empty_scene": {"product_empty_scene", "product_beauty", "product_detail", "product_demo"},
            "scene_empty_scene": {"scene_empty_scene", "lifestyle", "environment"},
            "motion": {"motion", "exercise", "action", "demonstration", "pain_point"},
            "body_display": {"body_display", "pain_point", "demonstration"},
            "mechanism": {"mechanism", "demonstration"},
        }
        allowed_roles = _VISUAL_INTENT_ROLE_ALIASES.get(
            visual_intent, usage_aliases.get(usage_role, {usage_role}),
        )
        if not semantic_visual_only and not (set(roles) | {role}).intersection(allowed_roles):
            continue
        if usage_role == "product_empty_scene":
            product_roles = ("product_beauty", "product_detail", "product_empty_scene")
            if not semantic_visual_only and role not in product_roles and not any(
                tag in product_roles for tag in roles
            ):
                continue
            product_text = " ".join(str(row.get(key) or "") for key in (
                "detected_product_name", "detected_brand_name", "product_category", "asset_summary", "content_summary",
            ))
            row["product_identity_match"] = _product_identity_matches(product_names, row, product_text)
            if not semantic_visual_only and product_names and not row["product_identity_match"]:
                continue
        if (
            not semantic_visual_only
            and usage_role == "scene_empty_scene"
            and role in ("product_beauty", "product_detail")
        ):
            continue
        text = " ".join(str(row.get(key) or "") for key in (
            "content_summary", "asset_summary", "scene_category", "asset_category",
            "objects", "asset_actions", "actions", "asset_name",
        ))
        identity_text = " ".join(str(row.get(key) or "") for key in (
            "detected_product_name", "detected_brand_name",
        ))
        matching_text = text + " " + identity_text
        local_matching_text = " ".join(str(row.get(key) or "") for key in (
            "content_summary", "scene_category", "actions",
        )) + " " + identity_text
        action_events = events_by_feature.get(int(row["temporal_feature_id"]), [])
        event_text = " ".join(_action_event_text(event) for event in action_events)
        if event_text:
            text = f"{text} {event_text}"
            local_matching_text = f"{local_matching_text} {event_text}"
        required_matches = {
            element: _element_matches(element, local_matching_text)
            for element in (required_elements or [])
        }
        element_coverage = (
            sum(required_matches.values()) / len(required_matches)
            if required_matches else 0.0
        )
        if not semantic_visual_only and not required_elements_satisfied(required_matches, usage_role):
            continue
        forbidden_matches = {
            element: _forbidden_element_matches(element, local_matching_text)
            for element in (forbidden_elements or [])
        }
        if not semantic_visual_only and any(forbidden_matches.values()):
            continue
        similarity = _semantic_similarity(query, matching_text)
        visual_intents = _visual_intent_matches(
            query, local_matching_text, product_names,
            visual_intent=visual_intent, roles=[role, *roles],
        )
        if (
            not semantic_visual_only
            and visual_intent
            and not visual_intents.get(f"intent:{visual_intent}")
        ):
            continue
        if not semantic_visual_only and any(not matched for matched in visual_intents.values()):
            continue
        visual_intent_score = (
            sum(visual_intents.values()) / len(visual_intents)
            if visual_intents else 0.0
        )
        score = _candidate_match_score(
            similarity=similarity,
            element_coverage=element_coverage,
            product_identity_match=bool(row.get("product_identity_match")),
            usage_role=usage_role,
            visual_intent_score=visual_intent_score,
            semantic_visual_only=semantic_visual_only,
        )
        row.update({"usage_roles": roles, "match_score": round(score, 4),
                    "successful_use_count": int(row.get("usage_count") or 0),
                    "match_reason": {
                        "role": role, "semantic_similarity": round(similarity, 4),
                        "product_identity_match": bool(row.get("product_identity_match")),
                        "required_elements": required_matches,
                        "required_element_coverage": round(element_coverage, 4),
                        "motion_intensity": float(row.get("motion_intensity") or 0),
                        "visual_intents": visual_intents,
                        "visual_intent_score": round(visual_intent_score, 4),
                        "forbidden_elements": forbidden_matches,
                        "analysis_id": row["analysis_id"],
                    }})
        event_match = _best_action_event(
            action_events, query=query, usage_role=usage_role, product_names=product_names,
            visual_intent=visual_intent, required_elements=required_elements or [],
            duration_us=duration_us, usable_start_us=int(row["usable_start_us"]),
            usable_end_us=int(row["usable_end_us"]),
        )
        if event_match:
            row["action_event_match"] = event_match
            row["clip_start_us"] = event_match["start_us"]
            row["clip_end_us"] = event_match["start_us"] + duration_us
            row["source_duration_us"] = duration_us
            row["playback_rate"] = 1.0
            row["match_reason"]["action_event"] = event_match
            row["match_reason"]["action_event"]["anchor_applied"] = True
        source_video_url = _candidate_source_url(row)
        if not source_video_url:
            continue
        row["source_video_url"] = source_video_url
        if not _fit_clip_range(row, duration_us):
            continue
        result.append(row)
    if semantic_visual_only:
        return sorted(result, key=lambda row: row["match_score"], reverse=True)[:limit]
    return sorted(result, key=lambda row: (
        row["match_score"] - row["successful_use_count"] * 0.005,
        float((row.get("action_event_match") or {}).get("match_score") or 0),
    ), reverse=True)[:limit]


def _candidate_source_url(row: dict) -> str | None:
    local_source = resolve_local_source(row)
    if local_source:
        return local_source.as_uri()
    try:
        return _join_access_url(row.get("access_url", ""), row.get("storage_path", ""))
    except RuntimeError:
        return None


def _fit_clip_range(row: dict, target_duration_us: int) -> bool:
    usable_start = int(row["usable_start_us"])
    usable_end = int(row["usable_end_us"])
    start = int(row.get("clip_start_us") or usable_start) if row.get("action_event_match") else usable_start
    if start < usable_start or start + target_duration_us > usable_end:
        return False
    available = usable_end - start
    if target_duration_us <= 0 or available < target_duration_us:
        return False
    source_duration = target_duration_us
    row["clip_start_us"] = start
    row["clip_end_us"] = start + source_duration
    row["source_duration_us"] = source_duration
    row["playback_rate"] = 1.0
    return True


def _terms(text: str) -> list[str]:
    return re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]+", text or "")


def _normalized_text(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", (value or "").casefold())


def _semantic_similarity(query: str, text: str) -> float:
    left, right = _normalized_text(query), _normalized_text(text)
    if not left or not right:
        return 0.0
    sequence = SequenceMatcher(None, left, right, autojunk=False).ratio()
    left_pairs = {left[index:index + 2] for index in range(max(0, len(left) - 1))}
    right_pairs = {right[index:index + 2] for index in range(max(0, len(right) - 1))}
    shared = len(left_pairs & right_pairs)
    pair_dice = 2 * shared / max(1, len(left_pairs) + len(right_pairs))
    return max(sequence, pair_dice)


def _candidate_match_score(*, similarity: float, element_coverage: float,
                           product_identity_match: bool, usage_role: str,
                           visual_intent_score: float = 0.0,
                           semantic_visual_only: bool = False) -> float:
    if semantic_visual_only:
        return round(min(1.0, (similarity + visual_intent_score) / 2), 4)
    score = similarity * 0.40 + element_coverage * 0.30 + visual_intent_score * 0.25
    if usage_role == "product_empty_scene" and product_identity_match:
        score += 0.05
    return round(min(1.0, score), 4)


def required_elements_satisfied(matches: dict[str, bool], category: str | None) -> bool:
    if not matches:
        return True
    matched = matches.values()
    if str(category or "").lower() in {"product_empty_scene", "mechanism"}:
        return any(matched)
    return all(matched)


_ELEMENT_CONCEPTS = (
    ("otc",),
    ("溯源码", "溯源", "追溯码", "追溯二维码"),
    ("扫码", "扫描二维码", "手机扫描", "扫二维码"),
    ("手机", "移动电话"),
    ("成分", "主要成分"),
    ("功能", "功效", "适应症"),
    ("验证", "查验", "验真", "正品校验"),
)

_CODE_ALIASES = ("二维码", "qr码", "qr code", "条形码", "条码", "barcode", "bar code", "扫码画面")
_CODE_NOT_VISIBLE = (
    "没有二维码或条形码", "没有二维码和条形码", "没有二维码或条码", "没有二维码和条码",
    "无二维码或条形码", "无二维码和条形码", "二维码或条形码都不可见",
    "二维码和条形码都不可见", "二维码或条码不可见", "二维码和条码不可见",
    "没有二维码", "无二维码", "未出现二维码", "未见二维码", "看不到二维码", "不含二维码",
    "未识别出二维码", "二维码不可见", "二维码未见", "二维码没有出现",
    "没有条形码", "没有条码", "无条形码", "无条码", "未出现条形码", "未出现条码",
    "未见条形码", "未见条码", "看不到条形码", "看不到条码", "不含条形码", "不含条码",
    "条形码不可见", "条码不可见", "条形码未见", "条码未见",
    "no qr code", "qr code not visible", "qr code is not visible", "no visible qr code",
    "no barcode", "barcode not visible", "barcode is not visible", "no visible barcode",
    "barcode not detected", "barcode is not present",
)
_INSTRUCTION_ALIASES = ("说明书", "药品说明", "使用说明", "说明页", "说明文字")
_EMPHASIS_ALIASES = (
    "手指", "指向", "指着", "指示", "放大", "放大镜", "特写", "近景", "聚焦", "突出",
)
_OTC_EMPHASIS_ALIASES = (
    "手指", "指向", "指着", "指示", "放大", "放大镜", "特写", "聚焦", "推进",
    "otc动作", "otc强调", "otc指示",
)
_PRODUCT_BOX_ALIASES = ("药盒", "产品盒", "包装盒", "盒子", "小绿盒")
_PRODUCT_PRESENTATION_ALIASES = (
    "摆放", "陈列", "静置", "排放", "出现", "突然出现", "弹出", "入镜", "拿出", "拿起",
    "手持", "手拿", "展示", "特写", "近景",
)
_VISUAL_INTENT_ROLE_ALIASES = {
    "pain_point": {"pain_point", "body_display", "scene_empty_scene", "lifestyle", "demonstration"},
    "product_overview": {"product_overview", "product_empty_scene", "product_beauty", "product_detail", "product_demo"},
    "product_detail": {"product_detail", "product_empty_scene", "product_beauty", "product_demo", "demonstration"},
    "instruction_manual": {"instruction_manual", "product_detail", "product_empty_scene", "product_beauty", "product_demo", "demonstration"},
    "otc_certification": {"otc_certification", "product_detail", "product_empty_scene", "product_beauty", "product_demo", "demonstration"},
    "qr_traceability": {"qr_traceability", "product_detail", "product_empty_scene", "product_beauty", "product_demo", "demonstration"},
    "qr_code_visible": {"qr_traceability", "product_detail", "product_empty_scene", "product_beauty", "product_demo", "demonstration"},
    "authenticity_comparison": {"authenticity_comparison", "product_detail", "product_empty_scene", "product_beauty", "product_demo", "demonstration"},
    "usage_instruction": {"usage_instruction", "product_detail", "product_demo", "demonstration"},
    "medication_moment": {
        "medication_moment", "medication", "medicine_intake", "pill_taking",
        "usage_instruction", "scene_empty_scene", "lifestyle", "demonstration",
    },
    "factory_process": {"factory_process", "factory", "production", "shipping", "scene_empty_scene", "environment"},
    "service_support": {"service_support", "customer_service", "service", "scene_empty_scene", "lifestyle"},
    "body_condition": {"body_condition", "body_display", "pain_point", "demonstration"},
    "exercise_action": {"exercise_action", "motion", "exercise", "action", "demonstration"},
    "mechanism_explanation": {"mechanism_explanation", "mechanism", "demonstration"},
    "lifestyle_scene": {"lifestyle_scene", "scene_empty_scene", "lifestyle", "environment"},
    "experience_proof": {"experience_proof", "proof", "body_display", "scene_empty_scene", "demonstration"},
    "cta": {"cta", "product_demo", "product_beauty", "scene_empty_scene", "lifestyle"},
}
_VISUAL_INTENT_TEXT_ALIASES = {
    "pain_point": (
        "痛点", "困扰", "赘肉", "肥胖", "腰围", "脂肪堆积", "身材焦虑",
        "气喘", "喘气", "扶膝", "吃力", "乏力", "无法扣合",
    ),
    "product_overview": _PRODUCT_BOX_ALIASES + _PRODUCT_PRESENTATION_ALIASES,
    "product_detail": ("包装细节", "包装局部", "标签", "规格", "侧面", "背面", "胶囊", "说明文字"),
    "instruction_manual": _INSTRUCTION_ALIASES + ("成分表", "适应症", "禁忌", "注意事项", "用法用量页"),
    "otc_certification": ("otc", "批准文号", "国药准字", "资质", "认证标识"),
    "qr_traceability": _CODE_ALIASES + _INSTRUCTION_ALIASES + ("溯源码", "溯源", "追溯码", "一盒一码"),
    "qr_code_visible": _CODE_ALIASES,
    "authenticity_comparison": ("真假", "假货", "辨别", "验真", "真伪", "查验", "正品对比"),
    "usage_instruction": ("使用方法", "用法", "用量", "服用", "随餐", "每次", "每日", "吞服", "说明书"),
    "medication_moment": (
        "服药", "吃药", "吞服", "口服", "随餐服用", "喝水服药", "喝水吞服",
        "送入口中", "放入口中", "拿起胶囊", "拿药",
    ),
    "factory_process": ("工厂", "厂房", "生产线", "车间", "质检", "包装线", "发货", "物流", "仓库"),
    "service_support": ("客服", "一对一", "指导", "咨询", "售后", "服务人员"),
    "body_condition": ("身材", "身材展示", "体型", "体态", "腰腹", "赘肉", "肚子", "腰围", "围度", "衣着松紧"),
    "exercise_action": (
        "运动", "跑步", "健身", "跳绳", "骑行", "游泳", "瑜伽", "爬楼", "上楼梯",
    ),
    "mechanism_explanation": ("减肥机理", "排油机理", "机理", "机制", "作用过程", "脂肪吸收", "脂肪酶", "排油", "分解", "抑制", "结构示意"),
    "lifestyle_scene": ("饮食", "进餐", "工作", "办公室", "居家", "生活场景", "餐桌"),
    "experience_proof": ("体验", "效果", "变化", "亲测", "反馈", "前后", "证据"),
    "cta": ("链接", "购买", "下单", "领取", "点击", "购物车", "立即行动"),
    "talking_head": ("口播", "人物讲解", "主播讲解"),
}


def _contains_any(text: str, aliases: tuple[str, ...]) -> bool:
    return any(_normalized_text(alias) in text for alias in aliases)


def matching_visual_intent(query: str, visual_intent: str | None) -> str | None:
    """Treat code/traceability wording as a request to show a visible code."""
    if _contains_any(_normalized_text(query), (
        "二维码", "qr码", "qr code", "扫码", "扫描", "溯源码", "溯源", "追溯码",
        "一盒一码", "追溯信息", "条形码", "条码", "barcode", "bar code",
    )):
        return "qr_code_visible"
    return visual_intent


def qr_code_visible(text: str) -> bool:
    """Backward-compatible name; accepts visible QR codes and barcodes."""
    normalized = _normalized_text(text)
    for negative in _CODE_NOT_VISIBLE:
        normalized = normalized.replace(_normalized_text(negative), "")
    return _contains_any(normalized, _CODE_ALIASES)


def _medication_action_matches(text: str) -> bool:
    strong_actions = (
        "送入口中", "放入口中", "吃药", "吞服", "喝水服用", "喝水服药",
        "喝水吞服", "拿起胶囊后吞服",
    )
    if _contains_any(text, strong_actions):
        return True
    return _contains_any(text, ("服药", "随餐服用")) and _contains_any(
        text, ("人物", "女性", "男性", "患者", "拿起", "手拿", "入口"),
    )


def _explicit_visual_intent_matches(visual_intent: str, text: str, roles: list[str]) -> bool:
    # Analysis providers may return mixed-case Latin labels (for example `OTC`).
    # Normalize once here so direct callers and DB rows use the same matching rules.
    text = _normalized_text(text)
    if visual_intent == "qr_code_visible":
        return qr_code_visible(text)
    if visual_intent == "otc_certification":
        return "otc" in text and _contains_any(text, _OTC_EMPHASIS_ALIASES)
    if visual_intent == "medication_moment":
        return _medication_action_matches(text)
    if visual_intent == "product_overview":
        return _contains_any(text, _PRODUCT_BOX_ALIASES) and _contains_any(
            text, _PRODUCT_PRESENTATION_ALIASES,
        )
    if visual_intent == "product_detail":
        return _contains_any(text, _PRODUCT_BOX_ALIASES) and _contains_any(
            text, ("包装细节", "包装局部", "标签", "规格", "侧面", "背面", "胶囊", "说明文字"),
        )
    if visual_intent == "qr_traceability":
        has_code = _contains_any(text, _CODE_ALIASES + ("溯源码", "追溯码", "溯源"))
        has_action = _contains_any(text, ("扫码", "扫描", "指向", "特写", "近景", "聚焦", "放大"))
        return has_code and has_action
    if visual_intent == "authenticity_comparison":
        return _contains_any(text, ("真假对比", "真伪对比", "正品和假货", "正品与假货", "真假辨别"))
    if visual_intent == "usage_instruction":
        has_instruction = _contains_any(text, ("用法", "用量", "剂量", "每次", "每日", "随餐", "服用方法"))
        has_context = _contains_any(text, _INSTRUCTION_ALIASES + ("人物服用", "按说明服用", "药品标签"))
        return has_instruction and has_context
    if visual_intent in {"motion", "exercise", "action"}:
        return _contains_any(text, _VISUAL_INTENT_TEXT_ALIASES["exercise_action"])
    if visual_intent in {"product_empty_scene", "product_beauty", "product_detail"}:
        return _contains_any(text, _PRODUCT_BOX_ALIASES) and _contains_any(
            text, _PRODUCT_PRESENTATION_ALIASES + ("包装细节", "标签", "规格"),
        )
    if visual_intent in {"scene_empty_scene", "intro", "environment"}:
        return _contains_any(text, _VISUAL_INTENT_TEXT_ALIASES["lifestyle_scene"])
    if visual_intent in {"body_display", "body_condition"}:
        return _contains_any(text, _VISUAL_INTENT_TEXT_ALIASES["body_condition"])
    if visual_intent in {"mechanism", "mechanism_explanation"}:
        return _contains_any(text, _VISUAL_INTENT_TEXT_ALIASES["mechanism_explanation"])
    aliases = _VISUAL_INTENT_TEXT_ALIASES.get(visual_intent, ())
    return bool(aliases) and _contains_any(text, aliases)


def _visual_intent_matches(query: str, text: str, product_names: list[str], *,
                           visual_intent: str | None = None,
                           roles: list[str] | None = None) -> dict[str, bool]:
    """Map spoken intent to acceptable visual evidence from catalog analysis text."""
    expected = _normalized_text(query)
    actual = _normalized_text(text)
    if not expected or not actual:
        return {}

    matches: dict[str, bool] = {}
    if "otc" in expected:
        product_introduction = _contains_any(
            expected, ("手上的", "手上的是", "真正的", "长这样", "长这样的"),
        )
        matches["otc_emphasis"] = (
            ("otc" in actual and _contains_any(actual, _EMPHASIS_ALIASES))
            or (
                product_introduction
                and _contains_any(actual, ("放大", "放大镜", "聚焦", "特写"))
                and _contains_any(actual, _PRODUCT_BOX_ALIASES)
            )
        )
    if visual_intent != "qr_code_visible":
        if _contains_any(expected, ("扫码", "扫描二维码", "扫二维码", "直接扫", "扫这个")):
            matches["qr_scan"] = _contains_any(actual, _CODE_ALIASES)
        if _contains_any(expected, ("溯源码", "溯源", "追溯码", "一盒一码", "查验", "验真")):
            matches["traceability"] = _contains_any(
                actual, _CODE_ALIASES + _INSTRUCTION_ALIASES + ("溯源码", "溯源", "追溯码"),
            )
    if _contains_any(expected, ("成分", "功能标签", "成分功能", "适应症")):
        matches["ingredient_label"] = _contains_any(
            actual, ("成分", "主要成分", "功能标签", "适应症", "说明书", "说明文字"),
        )

    product_mentioned = any(
        _normalized_text(name) in expected
        for name in product_names
        if _normalized_text(name)
    )
    strong_introduction = _contains_any(
        expected, ("手上的", "手上的是", "真正的", "长这样", "长这样的"),
    )
    generic_introduction = _contains_any(expected, ("介绍产品", "展示产品"))
    if product_mentioned and (strong_introduction or (not matches and generic_introduction)):
        matches["product_presentation"] = (
            _contains_any(actual, _PRODUCT_BOX_ALIASES)
            and _contains_any(actual, _PRODUCT_PRESENTATION_ALIASES)
        )
    if visual_intent:
        matches[f"intent:{visual_intent}"] = _explicit_visual_intent_matches(
            visual_intent, actual, roles or [],
        )
    return matches


def required_visual_intents(query: str) -> set[str]:
    expected = _normalized_text(query)
    intents = set()
    if "otc" in expected:
        intents.add("otc_emphasis")
    if _contains_any(expected, ("扫码", "扫描二维码", "扫二维码", "直接扫", "扫这个")):
        intents.add("qr_scan")
    if _contains_any(expected, ("溯源码", "溯源", "追溯码", "一盒一码", "查验", "验真")):
        intents.add("traceability")
    if _contains_any(expected, ("成分", "功能标签", "成分功能", "适应症")):
        intents.add("ingredient_label")
    return intents


def _element_matches(element: str, text: str, *, threshold: float = 0.30) -> bool:
    expected = _normalized_text(element)
    actual = _normalized_text(text)
    if not expected or not actual:
        return False
    if _contains_any(expected, _CODE_ALIASES) and qr_code_visible(actual):
        return True
    if _contains_any(expected, ("扫码", "扫描二维码", "扫二维码")) and qr_code_visible(actual):
        return True
    if _contains_any(expected, ("溯源码", "溯源", "追溯码", "一盒一码", "查验", "验真")) \
            and (qr_code_visible(actual) or _contains_any(actual, _INSTRUCTION_ALIASES)):
        return True
    if _contains_any(expected, ("爬楼梯", "上楼梯", "楼梯攀爬", "爬楼")):
        return _contains_any(actual, ("爬楼梯", "上楼梯", "沿楼梯向上", "连续上楼", "向上爬楼"))
    if _contains_any(expected, ("走两步就喘", "行走后气喘", "短距离行走")):
        walks = _contains_any(actual, ("走路", "散步", "步行", "行走", "走两步", "短距离行走", "走动"))
        breathless = _contains_any(actual, ("气喘", "喘气", "气促", "喘息", "上气不接下气"))
        return walks and breathless
    if _contains_any(expected, ("裤子穿不上", "裤腰穿不上", "穿衣情境")) and _contains_any(
        actual, ("裤腰无法正常闭合", "无法扣合", "尝试扣合", "拉扯裤腰", "裤腰", "牛仔裤"),
    ):
        return True
    concepts = [
        tuple(_normalized_text(alias) for alias in aliases)
        for aliases in _ELEMENT_CONCEPTS
        if any(_normalized_text(alias) in expected for alias in aliases)
    ]
    if concepts:
        return all(any(alias in actual for alias in aliases) for aliases in concepts)
    return expected in actual or _semantic_similarity(expected, actual) >= threshold


_NEGATION_PREFIXES = ("没有", "无法", "不含", "不是", "非", "无", "未")


def _forbidden_element_matches(element: str, text: str) -> bool:
    """Match exclusions without treating a positive element as its negated form."""
    expected = _normalized_text(element)
    actual = _normalized_text(text)
    if not expected or not actual:
        return False
    for prefix in _NEGATION_PREFIXES:
        if prefix not in expected:
            continue
        core = expected.replace(prefix, "", 1)
        if expected in actual:
            return True
        return bool(core and core in actual and any(negation in actual for negation in _NEGATION_PREFIXES))
    return _element_matches(element, text, threshold=0.55)


def _product_identity_matches(product_names: list[str], row: dict, product_text: str) -> bool:
    targets = [_normalized_text(name) for name in product_names if _normalized_text(name)]
    if not targets:
        return False
    combined = _normalized_text(product_text)
    detected_brand = _normalized_text(str(row.get("detected_brand_name") or ""))
    detected_product = _normalized_text(str(row.get("detected_product_name") or ""))
    if detected_brand and detected_product.startswith(detected_brand):
        detected_product = detected_product[len(detected_brand):]
    product_core = re.sub(r"(包装盒|包装|药品|产品|胶囊|片剂|片|颗粒|口服液)+$", "", detected_product)
    for target in targets:
        if target in combined:
            return True
        if detected_brand and detected_brand not in target:
            continue
        if len(product_core) >= 2 and product_core in target:
            if detected_brand:
                return True
            common = SequenceMatcher(None, target, combined, autojunk=False).find_longest_match()
            if common.size >= max(4, len(target) // 2):
                return True
    return False


def resolve_local_source(clip: dict) -> Path | None:
    configured = (
        os.getenv("MEDIA_LIBRARY_LOCAL_ROOT", "")
        or str(_settings().get("MEDIA_LIBRARY_LOCAL_ROOT") or "")
    ).strip()
    if not configured:
        return None
    root = Path(configured).expanduser().resolve()
    if not root.is_dir():
        return None
    relative = Path(str(clip.get("storage_path") or "").replace("\\", "/"))
    exact = (root / relative).resolve()
    if exact.is_relative_to(root) and _source_matches(exact, clip):
        return exact
    parent = (root / relative.parent).resolve()
    if not parent.is_relative_to(root) or not parent.is_dir():
        return None
    expected_size = int(clip.get("file_size") or 0)
    suffix = relative.suffix.lower()
    candidates = [
        path for path in parent.iterdir()
        if path.is_file() and path.suffix.lower() == suffix
        and (not expected_size or path.stat().st_size == expected_size)
    ]
    matches = [path for path in candidates if _source_matches(path, clip)]
    return matches[0] if len(matches) == 1 else None


def _source_matches(path: Path, clip: dict) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    expected_size = int(clip.get("file_size") or 0)
    if expected_size and path.stat().st_size != expected_size:
        return False
    expected_hash = str(clip.get("sha256") or "").lower()
    if not expected_hash:
        return True
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == expected_hash


def record_reservation(*, idempotency_key: str, batch_task_id: str, variant_id: str,
                       source_scene_id: str, clip: dict) -> None:
    with _connection(write=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("""INSERT INTO myh_media_usage_stats(asset_id,temporal_feature_id)
                VALUES (%s,%s) ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)""",
                (clip["asset_id"], clip["temporal_feature_id"]))
            cursor.execute("""INSERT INTO myh_media_usage_stats(asset_id,temporal_feature_id)
                VALUES (%s,0) ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)""", (clip["asset_id"],))
            cursor.execute("""INSERT IGNORE INTO myh_media_usage_events
                (idempotency_key,batch_task_id,variant_id,source_scene_id,asset_id,temporal_feature_id,
                 source_start_us,source_end_us,match_score,match_reason,status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'reserved')""",
                (idempotency_key,batch_task_id,variant_id,source_scene_id,clip["asset_id"],
                 clip["temporal_feature_id"],clip["clip_start_us"],clip["clip_end_us"],clip["match_score"],
                 _json_text(clip["match_reason"])))
            if cursor.rowcount:
                cursor.execute("""UPDATE myh_media_usage_stats SET reserved_count=reserved_count+1
                    WHERE asset_id=%s AND temporal_feature_id=%s""",
                    (clip["asset_id"],clip["temporal_feature_id"]))
                cursor.execute("""UPDATE myh_media_usage_stats SET reserved_count=reserved_count+1
                    WHERE asset_id=%s AND temporal_feature_id=0""", (clip["asset_id"],))


def finish_reservation(idempotency_key: str, *, draft_id: str | None, error: str = "") -> None:
    with _connection(write=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM myh_media_usage_events WHERE idempotency_key=%s FOR UPDATE", (idempotency_key,))
            event = cursor.fetchone()
            if not event or event["status"] != "reserved":
                return
            if draft_id:
                cursor.execute("""UPDATE myh_media_usage_events SET status='used',draft_id=%s,
                    used_at=CURRENT_TIMESTAMP(3),updated_at=CURRENT_TIMESTAMP(3) WHERE id=%s AND status='reserved'""",
                    (draft_id,event["id"]))
                if cursor.rowcount:
                    cursor.execute("""UPDATE myh_media_usage_stats SET reserved_count=GREATEST(0,reserved_count-1),
                        successful_use_count=successful_use_count+1,last_used_at=CURRENT_TIMESTAMP(3)
                        WHERE asset_id=%s AND temporal_feature_id=%s""",
                        (event["asset_id"],event["temporal_feature_id"]))
                    cursor.execute("""UPDATE myh_media_usage_stats SET reserved_count=GREATEST(0,reserved_count-1),
                        successful_use_count=successful_use_count+1,last_used_at=CURRENT_TIMESTAMP(3)
                        WHERE asset_id=%s AND temporal_feature_id=0""", (event["asset_id"],))
            else:
                cursor.execute("""UPDATE myh_media_usage_events SET status='released',error_message=%s,
                    released_at=CURRENT_TIMESTAMP(3),updated_at=CURRENT_TIMESTAMP(3) WHERE id=%s AND status='reserved'""",
                    (error[:2000],event["id"]))
                if cursor.rowcount:
                    cursor.execute("""UPDATE myh_media_usage_stats SET reserved_count=GREATEST(0,reserved_count-1)
                        WHERE asset_id=%s AND temporal_feature_id=%s""",
                        (event["asset_id"],event["temporal_feature_id"]))
                    cursor.execute("""UPDATE myh_media_usage_stats SET reserved_count=GREATEST(0,reserved_count-1)
                        WHERE asset_id=%s AND temporal_feature_id=0""", (event["asset_id"],))


def usage_count(asset_id: int, temporal_feature_id: int) -> int:
    with _connection() as connection, connection.cursor() as cursor:
        cursor.execute("""SELECT successful_use_count FROM myh_media_usage_stats
            WHERE asset_id=%s AND temporal_feature_id=%s""", (asset_id,temporal_feature_id))
        row = cursor.fetchone()
        return int(row["successful_use_count"]) if row else 0


def release_batch(batch_task_id: str, error: str) -> None:
    with _connection(write=True) as connection, connection.cursor() as cursor:
        cursor.execute("""SELECT asset_id,temporal_feature_id,COUNT(*) AS n FROM myh_media_usage_events
            WHERE batch_task_id=%s AND status='reserved' GROUP BY asset_id,temporal_feature_id FOR UPDATE""",
            (batch_task_id,))
        rows = cursor.fetchall()
        cursor.execute("""UPDATE myh_media_usage_events SET status='released',error_message=%s,
            released_at=CURRENT_TIMESTAMP(3),updated_at=CURRENT_TIMESTAMP(3)
            WHERE batch_task_id=%s AND status='reserved'""", (error[:2000],batch_task_id))
        for row in rows:
            cursor.execute("""UPDATE myh_media_usage_stats SET reserved_count=GREATEST(0,reserved_count-%s)
                WHERE asset_id=%s AND temporal_feature_id=%s""",
                (row["n"],row["asset_id"],row["temporal_feature_id"]))
            cursor.execute("""UPDATE myh_media_usage_stats SET reserved_count=GREATEST(0,reserved_count-%s)
                WHERE asset_id=%s AND temporal_feature_id=0""", (row["n"],row["asset_id"]))

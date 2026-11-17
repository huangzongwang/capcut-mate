from __future__ import annotations

import json
import threading

from ..online_db import connection


_schema_lock = threading.Lock()
_schema_ready = False


def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with connection(write=True) as conn, conn.cursor() as cursor:
            cursor.execute("""CREATE TABLE IF NOT EXISTS myh_video_analysis_task_results (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                task_id VARCHAR(80) NOT NULL,
                task_type VARCHAR(40) NOT NULL,
                owner_node_id VARCHAR(64) NOT NULL,
                owner_api_base_url VARCHAR(512) NULL,
                status VARCHAR(32) NOT NULL,
                stage VARCHAR(64) NOT NULL,
                progress INT NOT NULL DEFAULT 0,
                revision BIGINT UNSIGNED NOT NULL,
                result_json JSON NULL,
                result_manifest_url VARCHAR(1024) NULL,
                error_message TEXT NULL,
                created_at VARCHAR(40) NOT NULL,
                started_at VARCHAR(40) NULL,
                heartbeat_at VARCHAR(40) NULL,
                completed_at VARCHAR(40) NULL,
                updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                    ON UPDATE CURRENT_TIMESTAMP(3),
                PRIMARY KEY (id),
                UNIQUE KEY uk_myh_va_task_result_task (task_id),
                KEY idx_myh_va_task_result_node_status (owner_node_id,status),
                KEY idx_myh_va_task_result_type_created (task_type,created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        _schema_ready = True


def upsert(snapshot: dict) -> None:
    ensure_schema()
    values = dict(snapshot)
    values["result_json"] = json.dumps(values.get("result_json") or {}, ensure_ascii=False)
    with connection(write=True) as conn, conn.cursor() as cursor:
        cursor.execute("""INSERT INTO myh_video_analysis_task_results (
            task_id,task_type,owner_node_id,owner_api_base_url,status,stage,progress,
            revision,result_json,result_manifest_url,error_message,created_at,started_at,
            heartbeat_at,completed_at
        ) VALUES (
            %(task_id)s,%(task_type)s,%(owner_node_id)s,%(owner_api_base_url)s,%(status)s,
            %(stage)s,%(progress)s,%(revision)s,%(result_json)s,%(result_manifest_url)s,
            %(error_message)s,%(created_at)s,%(started_at)s,%(heartbeat_at)s,%(completed_at)s
        ) ON DUPLICATE KEY UPDATE
            task_type=IF(VALUES(revision)>=revision,VALUES(task_type),task_type),
            owner_node_id=IF(VALUES(revision)>=revision,VALUES(owner_node_id),owner_node_id),
            owner_api_base_url=IF(VALUES(revision)>=revision,VALUES(owner_api_base_url),owner_api_base_url),
            status=IF(VALUES(revision)>=revision,VALUES(status),status),
            stage=IF(VALUES(revision)>=revision,VALUES(stage),stage),
            progress=IF(VALUES(revision)>=revision,VALUES(progress),progress),
            result_json=IF(VALUES(revision)>=revision,VALUES(result_json),result_json),
            result_manifest_url=IF(VALUES(revision)>=revision,VALUES(result_manifest_url),result_manifest_url),
            error_message=IF(VALUES(revision)>=revision,VALUES(error_message),error_message),
            created_at=IF(VALUES(revision)>=revision,VALUES(created_at),created_at),
            started_at=IF(VALUES(revision)>=revision,VALUES(started_at),started_at),
            heartbeat_at=IF(VALUES(revision)>=revision,VALUES(heartbeat_at),heartbeat_at),
            completed_at=IF(VALUES(revision)>=revision,VALUES(completed_at),completed_at),
            revision=GREATEST(revision,VALUES(revision))""", values)


def get(task_id: str, task_type: str | None = None) -> dict | None:
    ensure_schema()
    sql = "SELECT * FROM myh_video_analysis_task_results WHERE task_id=%s"
    params: tuple = (task_id,)
    if task_type:
        sql += " AND task_type=%s"
        params = (task_id, task_type)
    with connection() as conn, conn.cursor() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
    if not row:
        return None
    item = dict(row)
    value = item.get("result_json")
    item["result_json"] = json.loads(value) if isinstance(value, str) else (value or {})
    item["updated_at"] = str(item.get("updated_at") or "")
    return item

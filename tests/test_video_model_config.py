"""Offline coverage for the online-only model directory and public contracts."""
import base64
import hashlib
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import ValidationError

from src.service.video_analysis import model_config_store as store
from src.schemas.video_analysis import AnalyzeVideoRequest
from src.service.video_analysis.storyboard.schemas import GenerateRequest, StoredGenerateRequest


def row(**changes):
    return {
        "id": 14, "provider_id": 5, "model": "video-test", "name": "Video",
        "capabilities": '["text","video","video_file"]',
        "metadata": '{"protocol":"responses","scene_timeout_seconds":180}',
        "model_updated_at": "v1", "provider_updated_at": "v1",
        "provider": "volcengine", "base_url": "https://example.test/api/v3/responses",
        "timeout_seconds": 60, "is_default": 0,
        "model_status": 1, "provider_status": 1,
        "model_deleted_at": None, "provider_deleted_at": None,
        "api_key_ciphertext": "cipher", **changes,
    }


class OnlineModelTests(unittest.TestCase):
    def resolve(self, value=None, purpose="scene", expected=None):
        with patch.object(store, "_rows", return_value=[value or row()]), \
             patch.object(store, "_decrypt", return_value="private-api-key"):
            return store.resolve_model(14, purpose, expected)

    def test_resolves_and_normalizes_without_secret_in_snapshot(self):
        value = self.resolve()
        self.assertEqual(value["base_url"], "https://example.test/api/v3")
        self.assertEqual(value["request_timeout_seconds"], 180)
        self.assertEqual(value["api_key"], "private-api-key")
        self.assertNotIn("api_key_ciphertext", value)
        self.assertNotIn("private-api-key", json.dumps(store.snapshot(value)))
        self.assertEqual(self.resolve(purpose="script")["request_timeout_seconds"], 60)

    def test_disabled_deleted_missing_and_invalid_settings(self):
        for change in (
            {"model_status": 0}, {"provider_status": 0},
            {"model_deleted_at": "now"}, {"provider_deleted_at": "now"},
            {"metadata": '{"protocol":"responses","scene_timeout_seconds":0}'},
            {"base_url": "https://example.test/api/v3/files"},
            {"base_url": "https://user:password@example.test/v3"},
        ):
            with self.subTest(change=change), self.assertRaises(store.ModelConfigError):
                self.resolve(row(**change))
        with patch.object(store, "_rows", return_value=[]), self.assertRaises(store.ModelConfigError):
            store.resolve_model(14, "scene")
        with self.assertRaises(store.ModelConfigError):
            store.resolve_model(None, "scene")

    def test_catalog_protocol_and_capabilities_do_not_gate_analysis(self):
        for purpose in ("scene", "global", "script"):
            for changes in (
                {"capabilities": None, "metadata": None},
                {"capabilities": "[]", "metadata": "{}"},
                {"capabilities": '["text"]', "metadata": '{"protocol":"chat_completions"}'},
            ):
                with self.subTest(purpose=purpose, changes=changes):
                    value = self.resolve(row(**changes), purpose=purpose)
                    self.assertEqual(value["id"], 14)
                    self.assertEqual(value["model"], "video-test")
                    self.assertEqual(value["request_timeout_seconds"], 60)
        with self.assertRaisesRegex(store.ModelConfigError, "Unknown analysis model purpose"):
            self.resolve(purpose="unsupported")

    def test_queued_version_change_rejected(self):
        expected = store.snapshot(self.resolve())
        self.assertEqual(self.resolve(expected=expected)["id"], 14)
        with self.assertRaisesRegex(store.ModelConfigError, "changed while queued"):
            self.resolve(row(model_updated_at="v2"), expected=expected)

    def test_fernet_current_legacy_rotation_and_bad_keys(self):
        secret = "test-upstream-key"
        derived = base64.urlsafe_b64encode(HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None,
            info=b"fastapiadmin:data-encryption:v1",
        ).derive(secret.encode()))
        cipher = Fernet(derived).encrypt(b"test-api-key").decode()
        with patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": secret}, clear=True):
            self.assertEqual(store._decrypt(cipher), "test-api-key")
        with patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "new", "DATA_ENCRYPTION_OLD_KEYS": secret}, clear=True):
            self.assertEqual(store._decrypt(cipher), "test-api-key")
        legacy = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))
        with patch.dict(os.environ, {"SECRET_KEY": secret}, clear=True):
            self.assertEqual(store._decrypt(legacy.encrypt(b"old-api-key").decode()), "old-api-key")
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(store.ModelConfigError):
            store._decrypt(cipher)
        with patch.dict(os.environ, {"DATA_ENCRYPTION_KEY": "wrong"}, clear=True), self.assertRaises(store.ModelConfigError):
            store._decrypt(cipher)
        with self.assertRaises(store.ModelConfigError):
            store._decrypt("plaintext-not-allowed")

    def test_read_only_parameterized_query_and_masked_db_error(self):
        import pymysql
        connection = MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [row()]
        settings = dict(DATABASE_HOST="private-host", DATABASE_PORT="3306",
                        DATABASE_USER="private-user", DATABASE_PASSWORD="private-password",
                        DATABASE_NAME="test")
        with patch.object(store, "_settings", return_value=settings), \
             patch.object(pymysql, "connect", return_value=connection):
            store._rows(14, True)
        self.assertEqual(cursor.execute.call_args_list[0].args, ("SET SESSION TRANSACTION READ ONLY",))
        self.assertEqual(cursor.execute.call_args_list[1].args[1], (14,))
        with patch.object(store, "_settings", return_value=settings), \
             patch.object(pymysql, "connect", side_effect=pymysql.OperationalError("private-password")):
            with self.assertRaises(store.ModelConfigError) as error:
                store._rows()
            self.assertNotIn("private-password", str(error.exception))

    def test_public_list_is_allowlisted_and_old_management_is_retired(self):
        from src.router.video_analysis import list_online_models, retired_model_configs
        from fastapi import HTTPException
        value = store._normalize(row())
        value["metadata"] = {"accidental_secret": "must-not-leak"}
        with patch.object(store, "list_configs", return_value=[value]):
            result = list_online_models()
        text = json.dumps(result)
        self.assertNotIn("cipher", text)
        self.assertNotIn("must-not-leak", text)
        self.assertEqual(result["models"][0]["id"], 14)
        with self.assertRaises(HTTPException) as error:
            retired_model_configs(14)
        self.assertEqual(error.exception.status_code, 410)

    def test_new_requests_require_explicit_ids_and_reject_legacy(self):
        with self.assertRaises(ValidationError):
            AnalyzeVideoRequest(video_path="x.mp4")
        with self.assertRaises(ValidationError):
            AnalyzeVideoRequest(video_path="x.mp4", model_config_id=2,
                                scene_model_id=14, global_model_id=13)
        AnalyzeVideoRequest(video_path="x.mp4", use_external_model=False)
        AnalyzeVideoRequest(video_path="x.mp4", scene_model_id=14, global_model_id=13)
        with self.assertRaises(ValidationError):
            GenerateRequest(script="text", model_config_id=2)
        GenerateRequest(script="text", model_id=13)
        old = StoredGenerateRequest.model_validate({"script": "text", "model_config_id": 2})
        self.assertIsNone(old.model_id)

    def test_video_and_global_models_resolve_independently(self):
        from src.service.video_analysis.semantic_analysis import resolve_analysis_models
        from src.service.video_analysis import semantic_analysis
        request = AnalyzeVideoRequest(video_path="x.mp4", scene_model_id=14, global_model_id=13)
        with patch.object(semantic_analysis, "resolve_model", side_effect=[
            {"model": "video"}, {"model": "text"},
        ]) as resolve:
            models = resolve_analysis_models(request)
        self.assertEqual([c.args for c in resolve.call_args_list], [(14, "scene", None), (13, "global", None)])
        self.assertNotEqual(models["scene"], models["global"])


if __name__ == "__main__":
    unittest.main()

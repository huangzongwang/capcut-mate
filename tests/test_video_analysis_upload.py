from fastapi.testclient import TestClient

from main import app


client = TestClient(app)


def test_upload_video_returns_analysis_path(tmp_path, monkeypatch):
    monkeypatch.setenv("SCENE_MEDIA_ROOT", str(tmp_path))
    response = client.post(
        "/openapi/capcut-mate/v1/upload_video",
        files={"file": ("sample.mp4", b"video-bytes", "video/mp4")},
    )

    body = response.json()
    assert body["code"] == 0
    assert body["size_bytes"] == len(b"video-bytes")
    assert body["video_path"].startswith("uploads/")
    assert (tmp_path / body["video_path"]).read_bytes() == b"video-bytes"


def test_upload_video_rejects_empty_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SCENE_MEDIA_ROOT", str(tmp_path))
    response = client.post(
        "/openapi/capcut-mate/v1/upload_video",
        files={"file": ("empty.mp4", b"", "video/mp4")},
    )

    assert response.json()["code"] == 413
    assert not list((tmp_path / "uploads").glob("*"))


def test_upload_video_removes_partial_file_when_over_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("SCENE_MEDIA_ROOT", str(tmp_path))
    monkeypatch.setenv("SCENE_MAX_FILE_BYTES", "4")
    response = client.post(
        "/openapi/capcut-mate/v1/upload_video",
        files={"file": ("large.mp4", b"12345", "video/mp4")},
    )

    assert response.json()["code"] == 413
    assert not list((tmp_path / "uploads").iterdir())

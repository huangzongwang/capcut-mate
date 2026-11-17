from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
import os

from src.utils.logger import logger

from . import client, model, store, first_frame
from .prompt import render
from .output_video import ensure_geometry, OutputVideoError


def _task_id() -> str:
    return f"gv_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"


class GrokVideoManager:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None
        self._guard = threading.Lock()
        self._owner = None

    def _lock(self):
        path = store.database_path().with_suffix(".worker.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if path.stat().st_size == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError("Grok Video worker already owns this database; use one process") from None
        self._owner = handle

    def start(self):
        with self._guard:
            if self._thread and self._thread.is_alive():
                if self._stop.is_set():
                    raise RuntimeError("Grok Video worker is stopping")
                return
            self._lock()
            try:
                store.ensure_schema()
                for task in store.pending():
                    if (task["status"] == "running" and task["stage"] == "image_submitting"
                            and not task["image_response_json"]):
                        store.update(task["task_id"], status="submission_unknown", stage="image_submission_unknown",
                                     error_message="Interrupted during image submission; check image provider before retrying")
                    elif task["status"] == "running" and task["stage"] == "submitting" and not task["provider_request_id"]:
                        store.update(task["task_id"], status="submission_unknown", stage="submission_unknown",
                                     error_message="Service interrupted during submission; check provider before retrying")
                    else:
                        store.update(task["task_id"], status="queued")
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="grok-video-worker", daemon=True)
                self._thread.start()
            except BaseException:
                self._owner.close()
                raise

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def submit(self, request):
        config = model.resolve(request.model_id)
        image_config = model.resolve(request.image_model_id, image=True) if request.product_image_url else None
        self.start()
        task_id = _task_id()
        data = request.model_dump()
        scene_url = data.pop("image_url")
        if scene_url:
            data["image"] = {"url": scene_url}
        data["model"] = config["model"]
        data["prompt"] = render(request)
        task = store.create(task_id, data, config["snapshot"],
                            image_config["snapshot"] if image_config else None)
        return task

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    task = store.claim()
                    if task:
                        self._execute(task["task_id"])
                        continue
                except Exception:
                    logger.error("Grok Video queue unavailable")
                self._stop.wait(0.5)
        finally:
            self._owner.close()

    def _execute(self, task_id):
        task = store.get(task_id)
        if not task or task["status"] != "running":
            return
        downloading = False
        image_stage = False
        try:
            request = json.loads(task["request_json"])
            # This field is prompt metadata; the video provider receives only its supported payload.
            request.pop("product_reference_image_urls", None)
            binding = json.loads(task["model_snapshot_json"])
            config = model.resolve(request["model_id"] if "model_id" in request else binding["id"], binding)
            request_id = task["provider_request_id"]
            started_at = task["started_at"] or store._now()
            if not request_id:
                if self._stop.is_set():
                    raise client.Stopped
                if request.get("product_image_url"):
                    image_stage = True
                    frame = json.loads(task["first_frame_json"]) if task["first_frame_json"] else None
                    if frame is None:
                        if task["image_response_json"]:
                            response = json.loads(task["image_response_json"])
                        else:
                            image_config = model.resolve(
                                request["image_model_id"], json.loads(task["image_snapshot_json"]), image=True,
                            )
                            store.update(task_id, stage="preparing_product", progress=3)

                            def submitting_image():
                                if self._stop.is_set():
                                    raise client.Stopped
                                store.update(task_id, stage="image_submitting", progress=5)

                            response = first_frame.generate(
                                image_config, request, on_submit=submitting_image,
                            )
                            try:
                                store.update(task_id, image_response_json=json.dumps(response))
                            except Exception:
                                raise client.SubmissionUnknown(
                                    "Image returned but could not be saved; check provider before retrying"
                                ) from None
                        store.update(task_id, stage="validating_first_frame", progress=20)
                        frame = first_frame.prepare(task_id, response, request)
                        store.update(task_id, first_frame_json=json.dumps(frame))
                    if request.get("confirm_first_frame") and task["stage"] != "first_frame_confirmed":
                        store.update(task_id, status="awaiting_frame_confirmation",
                                     stage="awaiting_frame_confirmation", progress=25)
                        return
                    if self._stop.is_set():
                        raise client.Stopped
                    request["image"] = first_frame.video_image(task_id)
                    request["prompt"] += (
                        "\nThe input is the composed opening scene. Preserve its presenter, clothing, "
                        "product placement and setting throughout; animate only the requested performance."
                    )
                    image_stage = False
                    # The video polling deadline begins after image generation and confirmation.
                    started_at = store._now()
                store.update(task_id, stage="submitting",
                             progress=30 if request.get("product_image_url") else 5, started_at=started_at)
                request_id = client.submit(config, request)
                try:
                    store.update(task_id, stage="generating",
                                 progress=35 if request.get("product_image_url") else 10,
                                 provider_request_id=request_id)
                except Exception:
                    raise client.SubmissionUnknown(
                        "Upstream accepted but task ID could not be saved; check provider before resubmitting"
                    ) from None

            def progress(status):
                store.update(task_id, stage="generating", progress=50 if status == "pending" else 75)

            if task["result_json"]:
                result = json.loads(task["result_json"])
            else:
                provider_result = client.wait(config, request_id, progress, self._stop, started_at)
                result = {
                    "model_id": request["model_id"], "model": request["model"],
                    "provider_request_id": request_id,
                    "duration": provider_result.get("video", {}).get("duration", request["duration"]),
                    "requested_duration": request["duration"],
                    "resolution": request["resolution"], "aspect_ratio": request["aspect_ratio"],
                    "content_url": f"/openapi/capcut-mate/v1/grok_video_tasks/{task_id}/content",
                }
                current = store.get(task_id)
                if current["first_frame_json"]:
                    result["first_frame"] = json.loads(current["first_frame_json"])
                store.update(task_id, result_json=json.dumps(result, ensure_ascii=False))
            output = store.result_root() / f"{task_id}.mp4"
            downloading = True
            store.update(task_id, stage="downloading", progress=90)
            client.download(config, request_id, output, self._stop)
            store.update(task_id, stage="validating_output", progress=95)
            result.update(ensure_geometry(output, request, self._stop))
            store.update(
                task_id, status="completed", stage="completed", progress=100,
                result_json=json.dumps(result, ensure_ascii=False),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        except client.Stopped:
            store.update(task_id, status="queued")
        except Exception as exc:
            state = ("submission_unknown" if isinstance(exc, client.SubmissionUnknown)
                     else "first_frame_failed" if image_stage
                     else "download_failed" if downloading else "failed")
            safe_message = str(exc) if isinstance(exc, (client.GrokVideoClientError, model.GrokVideoModelError, OutputVideoError)) else (
                "Video task failed; check configuration, network and storage"
            )
            store.update(
                task_id, status=state, stage=("image_submission_unknown" if image_stage and state == "submission_unknown" else state),
                error_message=safe_message,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            logger.error("grok_video task failed: %s state=%s", task_id, state)


manager = GrokVideoManager()

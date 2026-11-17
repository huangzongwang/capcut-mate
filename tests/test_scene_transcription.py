"""Pure transcription formatting and scene-alignment tests."""

from pathlib import Path
import sys
import asyncio
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src/service/video_analysis"))
from transcription import _decode_clip, map_segments_to_scenes, to_srt, transcribe_clips
from src.service.video_analysis.semantic_analysis import _scene_caption_items
from src.service.video_analysis import semantic_analysis
from src.schemas.video_analysis import DetectScenesResponse, TranscribeVideoRequest


class SceneTranscriptionTests(unittest.TestCase):
    def test_partial_transcription_preserves_status_warnings_and_events(self):
        detection = DetectScenesResponse(
            detector="enhanced", threshold=3, min_scene_len=15, fps=25,
            frame_count=50, duration_us=2000000, scene_count=2, warnings=[],
            scenes=[dict(index=i + 1, start_frame=i * 25, end_frame=(i + 1) * 25,
                         start_us=i * 1000000, end_us=(i + 1) * 1000000,
                         duration_us=1000000, start_timecode="", end_timecode="")
                    for i in range(2)],
        )
        progress = Mock()
        results = [
            dict(status="failed", error="broken audio", segments=[], device="not_used",
                 timings={"total_seconds": 0.2}, retry_count=1),
            dict(status="completed", segments=[], device="cuda", compute_type="float16"),
        ]
        with (
            patch.object(semantic_analysis, "build_worker_input", return_value={}),
            patch.object(semantic_analysis, "_run_worker", AsyncMock(return_value={"clips": results})),
        ):
            response = asyncio.run(semantic_analysis._transcribe_scene_clips(
                TranscribeVideoRequest(video_path="test.mp4"), detection,
                [{"path": "1.mp4"}, {"path": "2.mp4"}], progress,
            ))
        self.assertEqual(response.transcription_status, "partial")
        self.assertEqual(response.scenes[0].transcription_error, "broken audio")
        self.assertEqual(response.scenes[0].retry_count, 1)
        self.assertEqual(response.device, "cuda")
        self.assertTrue(any("broken audio" in warning for warning in response.warnings))
        self.assertEqual(progress.call_count, 2)

    def test_missing_audio_skips_whisper_decode(self):
        result = {"timings": {}}
        audio_module = ModuleType("faster_whisper.audio")
        audio_module.decode_audio = Mock()
        whisper_module = ModuleType("faster_whisper")
        whisper_module.audio = audio_module
        with (
            patch.dict(sys.modules, {
                "faster_whisper": whisper_module,
                "faster_whisper.audio": audio_module,
            }),
            patch("transcription._media_command", return_value=b'{"streams":[{"codec_type":"video"}]}'),
        ):
            audio = _decode_clip(Path("silent.mp4"), result)

        self.assertIsNone(audio)
        self.assertEqual(result["status"], "no_audio")
        audio_module.decode_audio.assert_not_called()

    def test_pyav_decode_error_retries_through_ffmpeg_wav(self):
        result = {"timings": {}}
        audio_module = ModuleType("faster_whisper.audio")
        audio_module.decode_audio = Mock(side_effect=[
            RuntimeError("PyAV decode failed"), SimpleNamespace(size=1),
        ])
        whisper_module = ModuleType("faster_whisper")
        whisper_module.audio = audio_module
        with (
            patch.dict(sys.modules, {
                "faster_whisper": whisper_module,
                "faster_whisper.audio": audio_module,
            }),
            patch("transcription._media_command", side_effect=[
                b'{"streams":[{"codec_type":"audio"}]}', b"",
            ]) as media_command,
        ):
            audio = _decode_clip(Path("broken.mp4"), result)

        self.assertEqual(audio.size, 1)
        self.assertEqual(result["retry_count"], 1)
        self.assertIn("PyAV decode failed", result["decode_error"])
        self.assertEqual(audio_module.decode_audio.call_count, 2)
        self.assertIn("audio.wav", media_command.call_args_list[1].args[0][-1])

    def test_scene_transcription_isolates_missing_audio_and_decode_failures(self):
        def decode(path, result):
            if path.name == "silent.mp4":
                result["status"] = "no_audio"
                return None
            if path.name == "broken.mp4":
                result["decode_error"] = "IndexError: missing audio stream"
                raise RuntimeError("FFmpeg fallback failed")
            return [0.1]

        with (
            patch("transcription._decode_clip", side_effect=decode),
            patch("transcription._load_model", return_value=(object(), "cuda", "float16")) as load_model,
            patch("transcription._recognize", return_value={
                "language": "zh", "language_probability": 0.99,
                "duration_seconds": 1.0, "segments": [{"id": 0, "text": "hello"}],
            }),
        ):
            result = transcribe_clips([
                Path("silent.mp4"), Path("broken.mp4"), Path("spoken.mp4"),
            ])

        self.assertEqual([item["status"] for item in result], ["no_audio", "failed", "completed"])
        self.assertEqual(result[0]["device"], "not_used")
        self.assertIn("PyAV: IndexError", result[1]["error"])
        self.assertEqual(result[2]["segments"][0]["text"], "hello")
        self.assertEqual(load_model.call_count, 1)

    def test_lazy_inference_error_does_not_discard_next_clip(self):
        def broken_iterator():
            raise RuntimeError("inference failed")
            yield

        info = SimpleNamespace(language="zh", language_probability=1.0, duration=1)
        model = Mock()
        model.transcribe.side_effect = [(broken_iterator(), info), (iter([]), info)]
        with (
            patch("transcription._decode_clip", return_value=[0.1]),
            patch("transcription._load_model", return_value=(model, "cpu", "int8")),
        ):
            result = transcribe_clips([Path("first.mp4"), Path("next.mp4")])
        self.assertEqual([r["status"] for r in result], ["failed", "completed"])
        self.assertIn("inference failed", result[0]["error"])

    def test_srt_formatting(self):
        srt = to_srt([{"start": 1.234, "end": 62.005, "text": "测试字幕"}])
        self.assertEqual(srt, "1\n00:00:01,234 --> 00:01:02,005\n测试字幕\n")

    def test_segments_are_mapped_by_overlap(self):
        segments = [
            {"id": 0, "start": 0.5, "end": 1.5, "text": "第一句"},
            {"id": 1, "start": 1.8, "end": 2.2, "text": "跨镜头"},
            {"id": 2, "start": 2.5, "end": 2.8, "text": "第二句"},
        ]
        scenes = [
            {"index": 1, "start_us": 0, "end_us": 2_000_000},
            {"index": 2, "start_us": 2_000_000, "end_us": 3_000_000},
        ]
        mapped = map_segments_to_scenes(segments, scenes)
        self.assertEqual(mapped[0]["text"], "第一句 跨镜头")
        self.assertEqual(mapped[0]["segment_ids"], [0, 1])
        self.assertEqual(mapped[0]["speech_seconds"], 1.2)
        self.assertEqual(mapped[1]["text"], "跨镜头 第二句")
        self.assertEqual(mapped[1]["speech_density"], 0.5)

    def test_scene_caption_items_are_clipped_to_scene(self):
        transcript = SimpleNamespace(segments=[
            SimpleNamespace(
                id=3, start=0.5, end=1.5, text="跨镜头",
                words=[
                    SimpleNamespace(start=0.5, end=0.9, word="跨", probability=0.9),
                    SimpleNamespace(start=0.9, end=1.5, word="镜头", probability=0.8),
                ],
            )
        ])
        scene = SimpleNamespace(start_us=1_000_000, end_us=2_000_000)
        items = _scene_caption_items(scene, transcript)
        self.assertEqual(items[0]["start_us"], 1_000_000)
        self.assertEqual(items[0]["end_us"], 1_500_000)
        self.assertEqual(items[0]["words"][0]["start_us"], 1_000_000)

    def test_cross_scene_segment_text_is_split_by_words(self):
        transcript = SimpleNamespace(segments=[
            SimpleNamespace(
                id=9, start=8.56, end=9.34, text="人生就是这样",
                words=[
                    SimpleNamespace(start=8.56, end=8.76, word="人生", probability=0.9),
                    SimpleNamespace(start=8.76, end=9.00, word="就是", probability=0.9),
                    SimpleNamespace(start=9.00, end=9.34, word="这样", probability=0.9),
                ],
            )
        ])
        first = _scene_caption_items(
            SimpleNamespace(start_us=8_560_000, end_us=8_760_000), transcript,
        )
        second = _scene_caption_items(
            SimpleNamespace(start_us=8_760_000, end_us=9_340_000), transcript,
        )
        self.assertEqual(first[0]["text"], "人生")
        self.assertEqual(second[0]["text"], "就是这样")
        self.assertNotEqual(first[0]["words"][0]["word_id"], second[0]["words"][0]["word_id"])
        self.assertTrue(all(8_560_000 <= w["start_us"] < 8_760_000 for w in first[0]["words"]))
        self.assertTrue(all(8_760_000 <= w["start_us"] < 9_340_000 for w in second[0]["words"]))


if __name__ == "__main__":
    unittest.main()

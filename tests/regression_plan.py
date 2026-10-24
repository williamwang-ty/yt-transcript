"""Regression tests for the normalize and plan stages: the normalized document and the text operations."""

from tests._support import *
from kernel.text import chunking

def _youtube(language: str) -> dict:
    return {"type": "youtube", "title": "Sample", "channel": "Channel", "source_language": language,
            "subtitle_source": "YouTube Subtitles"}


def _write_segments(path: Path, language: str, segments: list[dict], diagnostics: dict | None = None) -> Path:
    payload = {"source": "vtt", "language": language, "segments": segments}
    if diagnostics:
        payload["diagnostics"] = diagnostics
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _normalize(tmpdir: str, *, raw_text: Path | None = None, segments: Path | None = None,
               source: dict | None = None, mode: str = "chinese") -> dict:
    return kernel_normalize.build_normalized_document(
        document_id="vid001",
        raw_text_path=str(raw_text or Path(tmpdir) / "missing_raw.txt"),
        segments_path=str(segments or Path(tmpdir) / "missing_segments.json"),
        normalized_document_path=str(Path(tmpdir) / "normalized_document.json"),
        source_payload=source or {},
        workflow_payload={"mode": mode},
    )


def _plan(tmpdir: str, *, duration: int, mode: str, source: str = "youtube", normalization: dict | None = None,
          raw_text: Path | None = None) -> dict:
    return kernel_plan.plan_text_operations(
        video_id="vid001",
        duration=duration,
        mode=mode,
        source=source,
        normalization=normalization or {},
        raw_text_path=str(raw_text or Path(tmpdir) / "raw.txt"),
        segments_path=str(Path(tmpdir) / "segments.json"),
        work_dir=str(Path(tmpdir) / "text"),
        structured_text=str(Path(tmpdir) / "text" / "structured.md"),
        optimized_text=str(Path(tmpdir) / "optimized.md"),
        config=kernel_config.default_config_values(),
    )


class NormalizedDocumentTests(unittest.TestCase):
    def test_raw_text_is_normalized_when_there_are_no_segments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_text = Path(tmpdir) / "raw.txt"
            raw_text.write_text("Line one.\r\n\r\nLine two.  \n", encoding="utf-8")
            result = _normalize(tmpdir, raw_text=raw_text)
            payload = json.loads(Path(result["normalized_document_path"]).read_text(encoding="utf-8"))

        self.assertTrue(result["passed"], result)
        self.assertTrue(result["materialized"])
        self.assertEqual(payload["source_adapter"], "raw_text_file")
        self.assertEqual(payload["content"]["text"], "Line one.\n\nLine two.")

    def test_segments_are_preferred_when_available(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_text = Path(tmpdir) / "raw.txt"
            raw_text.write_text("fallback raw text", encoding="utf-8")
            segments = _write_segments(Path(tmpdir) / "segments.json", "en", [
                {"id": 0, "text": " First line ", "start_time": 0.0, "end_time": 1.0},
                {"id": 1, "text": "Second line", "start_time": 1.0, "end_time": 2.0},
            ])
            result = _normalize(tmpdir, raw_text=raw_text, segments=segments, source=_youtube("en"), mode="bilingual")
            payload = json.loads(Path(result["normalized_document_path"]).read_text(encoding="utf-8"))

        self.assertTrue(result["passed"], result)
        self.assertEqual(result["source_adapter"], "segments_json")
        self.assertEqual(result["segment_count"], 2)
        self.assertEqual(payload["content"]["preferred_chunk_source"], "segments")
        self.assertEqual(payload["content"]["text"], "First line\nSecond line")

    def test_chinese_youtube_subtitles_keep_segments_but_chunk_the_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_text = Path(tmpdir) / "raw.txt"
            raw_text.write_text("你 好，\n\n第 二 行。", encoding="utf-8")
            segments = _write_segments(Path(tmpdir) / "segments.json", "zh-Hans", [
                {"id": 0, "text": "你 好，", "start_time": 0.0, "end_time": 1.0},
                {"id": 1, "text": "第 二 行。", "start_time": 1.0, "end_time": 2.0},
            ], diagnostics={"cue_count": 2, "overlap_trim_count": 1})
            result = _normalize(tmpdir, raw_text=raw_text, segments=segments, source=_youtube("zh-Hans"))
            payload = json.loads(Path(result["normalized_document_path"]).read_text(encoding="utf-8"))
            chunked = chunking.chunk_document(result["normalized_document_path"], str(Path(tmpdir) / "text"),
                                              "cleanup_zh", kernel_config.default_config_values())

        self.assertTrue(result["passed"], result)
        self.assertEqual(result["source_adapter"], "segments_json")
        self.assertEqual(result["preferred_chunk_source"], "text")
        self.assertEqual(result["cleanup_diagnostics"]["overlap_trim_count"], 1)
        self.assertEqual(payload["content"]["text"], "你好，\n\n第二行。")
        self.assertEqual([segment["text"] for segment in payload["segments"]], ["你好，", "第二行。"])
        self.assertEqual(payload["diagnostics"]["subtitle_cleanup"]["overlap_trim_count"], 1)
        self.assertEqual(chunked["source_kind"], "text")


class TextPlanTests(unittest.TestCase):
    def test_short_chinese_video_runs_cleanup_zh_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            plan = _plan(tmpdir, duration=120, mode="chinese")

        self.assertEqual(plan["video_path"], "short")
        self.assertEqual([(op["kind"], op["prompt"]) for op in plan["operations"]], [("prompt", "cleanup_zh")])
        self.assertEqual(plan["operations"][0]["extra_instruction"], "")

    def test_short_bilingual_video_structures_then_translates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            plan = _plan(tmpdir, duration=120, mode="bilingual")

        self.assertEqual(plan["video_path"], "short")
        self.assertEqual([op["prompt"] for op in plan["operations"]], ["structure_only", "translate_only"])

    def test_long_deepgram_video_is_one_long_text_task_with_the_asr_hint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            chinese = _plan(tmpdir, duration=3600, mode="chinese", source="deepgram")
            bilingual = _plan(tmpdir, duration=3600, mode="bilingual", source="deepgram")

        self.assertEqual(chinese["video_path"], "long")
        self.assertEqual(chinese["routing_reason"], "duration_threshold")
        self.assertEqual([(op["kind"], op["prompt"], op["input_key"]) for op in chinese["operations"]],
                         [("chunk", "cleanup_zh", "raw_path")])
        self.assertIn("Deepgram ASR", chinese["operations"][0]["extra_instruction"])
        self.assertTrue(any("Deepgram fallback is active" in warning for warning in chinese["warnings"]))
        self.assertEqual([(op["prompt"], op["input_key"]) for op in bilingual["operations"]],
                         [("structure_only", "raw_path"), ("translate_only", "processed_path")])

    def test_oversized_short_input_takes_the_long_text_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            raw_text = Path(tmpdir) / "raw.txt"
            raw_text.write_text(("word " * 5000).strip(), encoding="utf-8")
            plan = _plan(tmpdir, duration=1200, mode="bilingual", raw_text=raw_text)

        self.assertEqual(plan["duration_bucket"], "short")
        self.assertEqual(plan["video_path"], "long")
        self.assertEqual(plan["routing_reason"], "oversized_short_input")
        self.assertTrue(all(op["kind"] == "chunk" for op in plan["operations"]))
        self.assertGreater(plan["estimated_input_tokens"], plan["single_pass_token_limit"])

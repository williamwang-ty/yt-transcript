"""Regression tests for the chunking step of the text stage and the post-merge cleanup."""

from tests._support import *
from kernel.long_text import chunking
from kernel.text_cleanup import post_merge


def _chunks(work_dir: Path) -> list[str]:
    return [path.read_text(encoding="utf-8") for path in sorted(work_dir.glob("chunk_*.txt"))]


def _segment_document(path: Path, segments: list[dict]) -> Path:
    path.write_text(json.dumps({
        "content": {"text": "\n".join(segment["text"] for segment in segments), "preferred_chunk_source": "segments"},
        "segments": segments,
    }, ensure_ascii=False), encoding="utf-8")
    return path


class ChunkingTests(unittest.TestCase):
    """`chunking.chunk_document`: chunk files and the manifest the window executor reads."""

    def test_text_is_packed_by_sentence_and_keeps_every_character(self):
        text = (FIXTURES_DIR / "chinese_transcript_sample.txt").read_text(encoding="utf-8").strip()
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = chunk_plain_text(text, Path(tmpdir) / "text", chunk_size_override=40)
            chunks = _chunks(work_dir)
            manifest = json.loads((work_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunk.replace("\n\n", "") for chunk in chunks), text.replace("\n", ""))
        self.assertEqual(manifest["source_kind"], "text")
        self.assertEqual([entry["raw_path"] for entry in manifest["chunks"]],
                         [f"chunk_{index:03d}.txt" for index in range(len(chunks))])
        self.assertTrue(manifest["normalized_document_path"].endswith("text_normalized_document.json"))
        self.assertEqual(manifest["chapters_path"], "")

    def test_sentence_over_the_hard_cap_is_cut_with_a_warning(self):
        text = "这是一段没有标点的中文转录文本" * 20
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                normalized = Path(tmpdir) / "normalized.json"
                normalized.write_text(json.dumps({"content": {"text": text}}, ensure_ascii=False), encoding="utf-8")
                config = {**utils._default_config_values(), "chunk_size_override": 60}
                result = chunking.chunk_document(str(normalized), str(Path(tmpdir) / "text"), "cleanup_zh", config)
            chunks = _chunks(Path(tmpdir) / "text")

        self.assertGreater(len(chunks), 1)
        self.assertTrue(any("fixed-width segment" in warning for warning in result["warnings"]))
        self.assertTrue(all(utils._estimate_tokens(chunk) <= result["hard_cap_tokens"] for chunk in chunks))
        self.assertEqual("".join(chunks), text)

    def test_chunk_size_follows_the_task_prompt(self):
        text = "这是一个用于测试分块预算的句子。" * 800
        with tempfile.TemporaryDirectory() as tmpdir:
            structure = chunk_plain_text(text, Path(tmpdir) / "structure", "structure_only")
            translate = chunk_plain_text(text, Path(tmpdir) / "translate", "translate_only")
            structure_manifest = json.loads((structure / "manifest.json").read_text(encoding="utf-8"))
            translate_manifest = json.loads((translate / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(structure_manifest["target_tokens"], utils.TASK_CHUNK_TOKEN_DEFAULTS["structure_only"])
        self.assertEqual(translate_manifest["target_tokens"], utils.TASK_CHUNK_TOKEN_DEFAULTS["translate_only"])
        self.assertLess(len(structure_manifest["chunks"]), len(translate_manifest["chunks"]))

    def test_segments_start_a_new_chunk_at_each_chapter(self):
        segments = [
            {"id": 0, "text": "A" * 10, "start_time": 0.0, "end_time": 10.0},
            {"id": 1, "text": "B" * 10, "start_time": 10.0, "end_time": 20.0},
            {"id": 2, "text": "C" * 10, "start_time": 20.0, "end_time": 30.0},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            normalized = _segment_document(Path(tmpdir) / "normalized.json", segments)
            chapters = Path(tmpdir) / "chapters.json"
            chapters.write_text(json.dumps({"chapters": [{"title": "Intro", "start_time": 0.0},
                                                         {"title": "Part", "start_time": 20.0}]}),
                                encoding="utf-8")
            config = {**utils._default_config_values(), "chunk_size_override": 1000}
            result = chunking.chunk_document(str(normalized), str(Path(tmpdir) / "text"), "cleanup_zh", config,
                                             chapters_path=str(chapters))
            chunks = _chunks(Path(tmpdir) / "text")

        self.assertEqual(result["source_kind"], "segments")
        self.assertEqual(result["chapters_path"], str(chapters.absolute()))
        self.assertEqual(chunks, ["A" * 10 + "\n\n" + "B" * 10, "C" * 10])

    def test_tiny_chunk_at_a_chapter_start_merges_forward(self):
        segments = [
            {"id": 0, "text": "甲" * 50, "start_time": 0.0, "end_time": 10.0},
            {"id": 1, "text": "乙" * 50, "start_time": 10.0, "end_time": 20.0},
            {"id": 2, "text": "丙" * 50, "start_time": 20.0, "end_time": 30.0},
            {"id": 3, "text": "丁" * 5, "start_time": 30.0, "end_time": 31.0},
            {"id": 4, "text": "戊" * 90, "start_time": 31.0, "end_time": 50.0},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            normalized = _segment_document(Path(tmpdir) / "normalized.json", segments)
            chapters = Path(tmpdir) / "chapters.json"
            chapters.write_text(json.dumps({"chapters": [{"title": "Two", "start_time": 30.0}]}), encoding="utf-8")
            config = {**utils._default_config_values(), "chunk_size_override": 100}
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                result = chunking.chunk_document(str(normalized), str(Path(tmpdir) / "text"), "cleanup_zh", config,
                                                 chapters_path=str(chapters))
            chunks = _chunks(Path(tmpdir) / "text")

        self.assertTrue(any("undersized chunk fragment" in warning for warning in result["warnings"]), result)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[-1], "丁" * 5 + "\n\n" + "戊" * 90)

    def test_text_is_used_when_the_document_prefers_it(self):
        document = {
            "content": {"text": "第一句。第二句。", "preferred_chunk_source": "text"},
            "segments": [{"id": 0, "text": "第一句", "start_time": 0.0, "end_time": 1.0},
                         {"id": 1, "text": "第二句", "start_time": 1.0, "end_time": 2.0}],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            normalized = Path(tmpdir) / "normalized.json"
            normalized.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            result = chunking.chunk_document(str(normalized), str(Path(tmpdir) / "text"), "cleanup_zh",
                                             utils._default_config_values())
            chunks = _chunks(Path(tmpdir) / "text")

        self.assertEqual(result["source_kind"], "text")
        self.assertEqual(chunks, ["第一句。\n\n第二句。"])


class PostMergeCleanupTests(unittest.TestCase):
    """`post_merge.post_merge_cleanup` over the merged window paragraphs."""

    def test_repeated_paragraph_start_is_trimmed(self):
        cleaned, diagnostics = post_merge.post_merge_cleanup("第一句。\n这是重复句。\n\n这是重复句。\n第二句。")
        self.assertEqual(diagnostics["seam_overlap_trim_count"], 1)
        self.assertEqual(cleaned.count("这是重复句。"), 1)

    def test_list_blocks_and_short_paragraphs_stay_apart(self):
        text = "- item\n\ncontinued\n\n他说，\n\n我们继续"
        cleaned, _ = post_merge.post_merge_cleanup(text)
        self.assertEqual(cleaned, text)

    def test_duplicate_heading_and_paragraph_are_dropped(self):
        cleaned, diagnostics = post_merge.post_merge_cleanup("## 开场\n\n## 开场\n\n第一段。\n\n第一段。")
        self.assertEqual(cleaned, "## 开场\n\n第一段。")
        self.assertEqual(diagnostics["heading_line_dedup_count"], 1)
        self.assertEqual(diagnostics["duplicate_body_blocks_removed"], 1)

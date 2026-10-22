"""Regression tests for the quality gate, the glossary and the atomic file write."""

import threading

from tests._support import *
from kernel import files as kernel_files
from kernel.text import glossary as kernel_glossary


class AtomicWriteTests(unittest.TestCase):
    def test_concurrent_writers_to_one_target_use_separate_temp_files(self):
        """Two writers racing on the same file each rename their own temp file; no temp file is left behind."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "state.json"
            writer_b_replaced = threading.Event()
            sources = []
            failures = []
            original_replace = kernel_files.os.replace

            def patched_replace(src, dst, *args, **kwargs):
                if Path(dst) == target:
                    sources.append(Path(src).name)
                    if threading.current_thread().name == "writer_a":
                        self.assertTrue(writer_b_replaced.wait(timeout=2))
                    result = original_replace(src, dst, *args, **kwargs)
                    if threading.current_thread().name == "writer_b":
                        writer_b_replaced.set()
                    return result
                return original_replace(src, dst, *args, **kwargs)

            def writer(content):
                try:
                    kernel_files.atomic_write_text(target, content)
                except Exception as exc:  # pragma: no cover - captured for assertion below
                    failures.append(exc)

            with mock.patch.object(kernel_files.os, "replace", side_effect=patched_replace):
                thread_a = threading.Thread(target=writer, name="writer_a", args=('{"writer":"a"}',))
                thread_b = threading.Thread(target=writer, name="writer_b", args=('{"writer":"b"}',))
                thread_a.start()
                thread_b.start()
                thread_a.join()
                thread_b.join()

            self.assertEqual(failures, [], failures)
            self.assertEqual(len(set(sources)), 2, sources)
            self.assertEqual(target.read_text(encoding="utf-8"), '{"writer":"a"}')
            self.assertEqual([path.name for path in Path(tmpdir).iterdir()], ["state.json"])


class GlossaryTests(unittest.TestCase):
    def test_build_glossary_extracts_terms_from_work_dir(self):
        """Test build glossary extracts terms from work dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "raw.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("OpenAI API 与 Deepgram SDK 在 YouTube 工作流里一起使用。", encoding="utf-8")
            chunk_plain_text(source.read_text(encoding="utf-8"), work_dir, "structure_only")

            result = kernel_glossary.build_glossary(str(work_dir), max_terms=10, min_occurrences=1)

            self.assertTrue(result["success"])
            glossary_path = Path(result["glossary_path"])
            self.assertTrue(glossary_path.exists())
            terms = [entry["term"] for entry in result["terms"]]
            self.assertIn("OpenAI", terms)
            self.assertIn("API", terms)
            self.assertIn("Deepgram", terms)

    def test_build_glossary_transcript_mode_prioritizes_document_context(self):
        """Test build glossary transcript mode prioritizes document context."""
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir) / "chunks"
            work_dir.mkdir(parents=True, exist_ok=True)
            raw_chunk = work_dir / "chunk_000.txt"
            raw_chunk.write_text("这一段主要解释 SDK 的使用方式。", encoding="utf-8")
            normalized_document = Path(tmpdir) / "normalized_document.json"
            normalized_document.write_text(
                json.dumps(
                    {
                        "source": {
                            "title": "OpenAI API Walkthrough",
                            "channel": "DeepgramLabs",
                        },
                        "content": {
                            "text": "这一段主要解释 SDK 的使用方式。",
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            chapters = Path(tmpdir) / "chapters.json"
            chapters.write_text(json.dumps({"chapters": [{"title": "GPT4 API Setup", "start_time": 0}]}),
                                encoding="utf-8")
            (work_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "normalized_document_path": str(normalized_document),
                        "chunks": [
                            {"id": 0, "raw_path": "chunk_000.txt"},
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = kernel_glossary.build_glossary(
                str(work_dir),
                mode="transcript",
                chapters_path=str(chapters),
                max_terms=20,
                min_occurrences=2,
            )

            self.assertTrue(result["success"], result)
            self.assertEqual(result["mode"], "transcript")
            terms = [entry["term"] for entry in result["terms"]]
            self.assertIn("OpenAI", terms)
            self.assertIn("API", terms)
            self.assertIn("DeepgramLabs", terms)
            self.assertIn("GPT4", terms)

    def test_glossary_selection_avoids_ascii_substring_false_positives(self):
        """Test glossary matching avoids selecting acronyms from unrelated substrings."""
        payload = {
            "terms": [
                {"term": "API", "score": 10, "count": 3},
                {"term": "OpenAI", "score": 9, "count": 2},
            ]
        }
        source_text = "This chapter discusses rapid iteration and capital allocation."

        selected = kernel_glossary.select_glossary_terms(payload, source_text, max_terms=8)
        drift = kernel_glossary.evaluate_glossary_drift(
            payload,
            source_text,
            "这段讲快速迭代和资本配置。",
            max_terms=8,
        )

        self.assertEqual(selected, [])
        self.assertEqual(drift["glossary_drift_count"], 0)
        self.assertEqual(drift["warnings"], [])


class VerifyQualityTests(unittest.TestCase):
    def test_verify_quality_reports_missing_semantic_anchors(self):
        """Test verify quality reports missing semantic anchors."""
        with tempfile.TemporaryDirectory() as tmpdir:
            raw = Path(tmpdir) / "raw.txt"
            optimized = Path(tmpdir) / "optimized.txt"
            raw.write_text("访问 https://example.com 于 2026-03-08 完成 32% 进度。", encoding="utf-8")
            optimized.write_text("## 结果\n\n已完成进度。", encoding="utf-8")

            result = kernel_verify.verify_quality(str(optimized), raw_text_path=str(raw), bilingual=False)

            self.assertFalse(result["checks"]["semantic_anchor_coverage_ok"])
            self.assertGreater(result["checks"]["semantic_missing_count"], 0)
            self.assertTrue(any("Semantic anchors" in warning for warning in result["warnings"]))

    def test_verify_quality_rejects_missing_structure(self):
        """Test verify quality rejects missing structure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            optimized = Path(tmpdir) / "opt.md"
            raw = Path(tmpdir) / "raw.txt"
            optimized.write_text("这是一大段没有章节标题的文本。" * 120, encoding="utf-8")
            raw.write_text("这是一大段没有章节标题的文本。" * 100, encoding="utf-8")

            result = kernel_verify.verify_quality(str(optimized), str(raw), bilingual=False)

            self.assertFalse(result["passed"])
            self.assertTrue(any("section headers" in failure for failure in result["hard_failures"]))

    def test_verify_quality_checks_bilingual_pairs(self):
        """Test verify quality checks bilingual pairs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            optimized = Path(tmpdir) / "opt.md"
            raw = Path(tmpdir) / "raw.txt"
            optimized.write_text(
                "## Section\n\n"
                "English paragraph one.\n\n"
                "中文段落一。\n\n"
                "English paragraph two.\n\n"
                "中文段落二。\n",
                encoding="utf-8",
            )
            raw.write_text("English paragraph one. English paragraph two.", encoding="utf-8")

            result = kernel_verify.verify_quality(str(optimized), str(raw), bilingual=True)

            self.assertTrue(result["passed"], result)
            self.assertGreaterEqual(result["checks"]["bilingual_pairs"], 1)

    def test_verify_quality_accepts_bilingual_pairs_after_intro_paragraph(self):
        """Test bilingual pair detection tolerates a leading intro paragraph."""
        with tempfile.TemporaryDirectory() as tmpdir:
            optimized = Path(tmpdir) / "opt.md"
            raw = Path(tmpdir) / "raw.txt"
            optimized.write_text(
                "## Section\n\n"
                "这一段是导语，解释下面会按英中对照展开。\n\n"
                "English paragraph one.\n\n"
                "中文段落一。\n\n"
                "English paragraph two.\n\n"
                "中文段落二。\n",
                encoding="utf-8",
            )
            raw.write_text("English paragraph one. English paragraph two.", encoding="utf-8")

            result = kernel_verify.verify_quality(str(optimized), str(raw), bilingual=True)

            self.assertTrue(result["passed"], result)
            self.assertGreaterEqual(result["checks"]["bilingual_pairs"], 1)

    def test_verify_quality_rejects_bilingual_without_pairs(self):
        """Test verify quality rejects bilingual without pairs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            optimized = Path(tmpdir) / "opt.md"
            raw = Path(tmpdir) / "raw.txt"
            optimized.write_text(
                "## Section\n\n"
                "English paragraph one.\n\n"
                "English paragraph two.\n",
                encoding="utf-8",
            )
            raw.write_text("English paragraph one. English paragraph two.", encoding="utf-8")

            result = kernel_verify.verify_quality(str(optimized), str(raw), bilingual=True)

            self.assertFalse(result["passed"])
            self.assertTrue(any("paragraph pairs" in failure for failure in result["hard_failures"]))

    def test_verify_quality_passes_when_only_warnings_exist(self):
        """Test verify quality passes when only warnings exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            optimized = Path(tmpdir) / "opt.md"
            raw = Path(tmpdir) / "raw.txt"
            optimized.write_text(
                "## Section\n\n"
                "第一段内容，结构完整。\n\n"
                "第二段内容，也有句号。\n",
                encoding="utf-8",
            )
            raw.write_text("原文。", encoding="utf-8")

            result = kernel_verify.verify_quality(str(optimized), str(raw), bilingual=False)

            self.assertTrue(result["passed"], result)
            self.assertEqual(result["hard_failures"], [])
            self.assertTrue(result["warnings"])

    def test_verify_quality_warns_on_chinese_spacing_anomalies(self):
        """Test verify quality warns on chinese spacing anomalies."""
        with tempfile.TemporaryDirectory() as tmpdir:
            optimized = Path(tmpdir) / "opt.md"
            optimized.write_text(
                "## Section\n\n"
                "这 是 一 段 被 字 幕 空 格 污 染 的 中 文 文 本 ， 读 起 来 很 生 硬 。\n\n"
                "我 们 还 会 看 到 很 多 词 被 错 误 地 拆 开 ， 让 阅 读 节 奏 明 显 变 差 。\n\n"
                "如 果 最 终 输 出 还 保 留 这 种 空 格 ， 就 应 该 给 出 质 量 告 警 。\n",
                encoding="utf-8",
            )

            result = kernel_verify.verify_quality(str(optimized), bilingual=False)

            self.assertTrue(result["passed"], result)
            self.assertFalse(result["checks"]["cjk_spacing_ok"])
            self.assertTrue(any("Chinese spacing anomalies" in warning for warning in result["warnings"]))

    def test_verify_quality_warns_on_repeated_chunk_seam_phrases(self):
        """Test verify quality warns on repeated chunk seam phrases."""
        with tempfile.TemporaryDirectory() as tmpdir:
            optimized = Path(tmpdir) / "opt.md"
            optimized.write_text(
                "## Section\n\n"
                "我们需要稳定输出结构化结果，并保持上下文一致、术语一致、段落一致。\n\n"
                "保持上下文一致、术语一致、段落一致。这一段因为 chunk seam 重复了一次，然后才继续展开说明。\n\n"
                "保持上下文一致、术语一致、段落一致。接下来再说明第二个关键约束，以及为什么需要人工复核。\n",
                encoding="utf-8",
            )

            result = kernel_verify.verify_quality(str(optimized), bilingual=False)

            self.assertTrue(result["passed"], result)
            self.assertGreater(result["checks"]["chunk_seam_warning_count"], 0)
            self.assertFalse(result["checks"]["duplicate_ngram_ok"])
            self.assertTrue(any("Repeated phrase density" in warning for warning in result["warnings"]))

    def test_verify_quality_warns_on_glossary_drift(self):
        """Test verify quality warns on glossary drift."""
        with tempfile.TemporaryDirectory() as tmpdir:
            raw = Path(tmpdir) / "raw.txt"
            optimized = Path(tmpdir) / "opt.md"
            source = Path(tmpdir) / "source.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("OpenAI API 与 Deepgram SDK 发布。", encoding="utf-8")
            chunk_plain_text(source.read_text(encoding="utf-8"), work_dir)
            kernel_glossary.build_glossary(str(work_dir), mode="transcript", max_terms=10, min_occurrences=1)
            raw.write_text("OpenAI API 与 Deepgram SDK 发布。", encoding="utf-8")
            optimized.write_text(
                "## Section\n\n"
                "Open AI 接口与 Deepgram SDK 发布。\n",
                encoding="utf-8",
            )

            result = kernel_verify.verify_quality(
                str(optimized),
                str(raw),
                bilingual=False,
                work_dir=str(work_dir),
            )

            self.assertTrue(result["passed"], result)
            self.assertTrue(result["checks"]["glossary_drift_applicable"])
            self.assertFalse(result["checks"]["glossary_drift_ok"])
            self.assertGreater(result["checks"]["glossary_drift_count"], 0)
            self.assertTrue(any("Glossary drift detected" in warning for warning in result["warnings"]))

    def test_verify_quality_accepts_preserved_glossary_terms(self):
        """Test verify quality accepts preserved glossary terms."""
        with tempfile.TemporaryDirectory() as tmpdir:
            raw = Path(tmpdir) / "raw.txt"
            optimized = Path(tmpdir) / "opt.md"
            source = Path(tmpdir) / "source.txt"
            work_dir = Path(tmpdir) / "chunks"
            source.write_text("OpenAI API 与 Deepgram SDK 发布。", encoding="utf-8")
            chunk_plain_text(source.read_text(encoding="utf-8"), work_dir)
            kernel_glossary.build_glossary(str(work_dir), mode="transcript", max_terms=10, min_occurrences=1)
            raw.write_text("OpenAI API 与 Deepgram SDK 发布。", encoding="utf-8")
            optimized.write_text(
                "## Section\n\n"
                "OpenAI API 与 Deepgram SDK 发布。\n",
                encoding="utf-8",
            )

            result = kernel_verify.verify_quality(
                str(optimized),
                str(raw),
                bilingual=False,
                work_dir=str(work_dir),
            )

            self.assertTrue(result["passed"], result)
            self.assertTrue(result["checks"]["glossary_drift_applicable"])
            self.assertTrue(result["checks"]["glossary_drift_ok"])
            self.assertEqual(result["checks"]["glossary_drift_count"], 0)
            self.assertFalse(any("Glossary drift detected" in warning for warning in result["warnings"]))

    def test_verify_quality_ignores_case_only_glossary_differences(self):
        """Test verify quality does not count a recased term (Transdigm -> TransDigm) as drift."""
        with tempfile.TemporaryDirectory() as tmpdir:
            raw = Path(tmpdir) / "raw.txt"
            optimized = Path(tmpdir) / "opt.md"
            work_dir = Path(tmpdir) / "chunks"
            raw.write_text("Transdigm 和 OpenAI 发布了财报。", encoding="utf-8")
            chunk_plain_text(raw.read_text(encoding="utf-8"), work_dir)
            (work_dir / "glossary.json").write_text(json.dumps({
                "terms": [{"term": "Transdigm", "aliases": [], "preferred_output": "Transdigm", "source": "transcript", "count": 2}],
            }), encoding="utf-8")
            optimized.write_text("## Section\n\nTransDigm 和 OpenAI 发布了财报。\n", encoding="utf-8")

            result = kernel_verify.verify_quality(str(optimized), str(raw), bilingual=False, work_dir=str(work_dir))

            self.assertTrue(result["checks"]["glossary_drift_applicable"], result["checks"])
            self.assertEqual(result["checks"]["glossary_drift_count"], 0)


    def test_verify_quality_warns_on_fragmented_chinese_paragraphs(self):
        """Test verify quality warns on fragmented chinese paragraphs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            optimized = Path(tmpdir) / "opt.md"
            optimized.write_text(
                "## Section\n\n"
                "先说背景\n\n"
                "再说问题\n\n"
                "继续补充\n\n"
                "最后总结\n\n"
                "还没说完\n\n"
                "接着展开\n",
                encoding="utf-8",
            )

            result = kernel_verify.verify_quality(str(optimized), bilingual=False)

            self.assertTrue(result["passed"], result)
            self.assertFalse(result["checks"]["short_paragraph_ratio_ok"])
            self.assertTrue(any("Too many short Chinese body paragraphs" in warning for warning in result["warnings"]))

    def test_verify_quality_avoids_false_positives_for_healthy_chinese_output(self):
        """Test verify quality avoids false positives for healthy chinese output."""
        with tempfile.TemporaryDirectory() as tmpdir:
            optimized = Path(tmpdir) / "opt.md"
            optimized.write_text(
                "## 背景\n\n"
                "这份整理稿先交代问题背景，说明为什么当前流程需要更稳定的质量门禁，并给出清晰的执行边界。\n\n"
                "随后我们补充约束条件，强调输出必须保留结构、术语和段落层次，不能只是把字幕机械拼接起来。\n\n"
                "## 方法\n\n"
                "在实现层面，我们优先做确定性的机械修复，再把剩余问题通过告警暴露给人工复核，而不是直接阻断流程。\n\n"
                "最终成稿会保留自然的中文标点、合理的段落长度，以及稳定可解释的检查指标，便于持续回归验证。\n",
                encoding="utf-8",
            )

            result = kernel_verify.verify_quality(str(optimized), bilingual=False)

            self.assertTrue(result["passed"], result)
            self.assertEqual(result["warnings"], [], result)
            self.assertTrue(result["checks"]["cjk_spacing_ok"])
            self.assertTrue(result["checks"]["duplicate_ngram_ok"])
            self.assertTrue(result["checks"]["short_paragraph_ratio_ok"])
            self.assertTrue(result["checks"]["punctuation_density_ok"])

    def test_verify_quality_records_long_chinese_paragraphs_without_failing(self):
        """Test the longest Chinese paragraph is counted in characters and never warns or fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            optimized = Path(tmpdir) / "opt.md"
            long_paragraph = "这一段一直在讲同一件事，没有找到可以分开的转折。" * 25
            optimized.write_text(
                "## 背景\n\n"
                f"{long_paragraph}\n\n"
                "这是一段长度正常的中文段落，讲的是另一件事。\n"
                "这一行前面只有单个换行。\n",
                encoding="utf-8",
            )

            result = kernel_verify.verify_quality(str(optimized), bilingual=False)

            self.assertTrue(result["passed"], result)
            self.assertEqual(result["checks"]["paragraph_length_unit"], "chars")
            self.assertEqual(result["checks"]["max_paragraph_len"], len(long_paragraph))
            self.assertEqual(result["checks"]["over_limit_paragraph_count"], 1)
            self.assertEqual(result["checks"]["paragraph_line_break_count"], 1)
            self.assertFalse(any("paragraph" in warning.lower() and "long" in warning.lower()
                                 for warning in result["warnings"]))

    def test_verify_quality_measures_bilingual_paragraphs_in_english_words(self):
        """Test bilingual output is measured by its English paragraphs, in words."""
        with tempfile.TemporaryDirectory() as tmpdir:
            optimized = Path(tmpdir) / "opt.md"
            optimized.write_text(
                "## Section\n\n"
                + "word " * 260 + "\n\n"
                + "这是上一段很长的英文段落的中文翻译。" * 30 + "\n\n"
                "A short English paragraph.\n\n"
                "一段很短的中文翻译。\n",
                encoding="utf-8",
            )

            result = kernel_verify.verify_quality(str(optimized), bilingual=True)

            self.assertEqual(result["checks"]["paragraph_length_unit"], "words")
            self.assertEqual(result["checks"]["max_paragraph_len"], 260)
            self.assertEqual(result["checks"]["over_limit_paragraph_count"], 1)

    def test_verify_quality_does_not_flag_complete_chinese_tail_without_punctuation(self):
        """Test verify quality does not flag complete chinese tail without punctuation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            optimized = Path(tmpdir) / "opt.md"
            raw = Path(tmpdir) / "raw.txt"
            optimized.write_text(
                "## Section\n\n"
                "第一段内容，结构完整。\n\n"
                "这是最后一段没有句号但语义完整",
                encoding="utf-8",
            )
            raw.write_text(
                "第一段内容，结构完整。\n这是最后一段没有句号但语义完整",
                encoding="utf-8",
            )

            result = kernel_verify.verify_quality(str(optimized), str(raw), bilingual=False)

            self.assertTrue(result["passed"], result)
            self.assertTrue(result["checks"]["no_truncation"])
            self.assertFalse(any("Possible truncation" in warning for warning in result["warnings"]))


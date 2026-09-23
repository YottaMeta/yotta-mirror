#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""元镜 yotta-mirror 内核契约测试（TDD：先红后绿）。

覆盖：规范化 / 表格解析 / 映射与配置校验 / 数据闸门 / 逐题统计 /
知识点聚合 / 学生分层 / 薄弱点诊断 / 匿名化 / 报告渲染 / CLI / 安全边界 / 确定性。
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import yotta_mirror as ym  # noqa: E402

FIXED_NOW = "2026-09-24T00:00:00Z"


def make_items(spec):
    """spec: list of (id, max_score, knowledge[, difficulty])"""
    items = []
    for entry in spec:
        item = {"id": entry[0], "max_score": entry[1], "knowledge": list(entry[2])}
        if len(entry) > 3:
            item["difficulty"] = entry[3]
        items.append(item)
    return {"schema_version": ym.SCHEMA_VERSION, "items": items}


def analyze_text(text, items, config=None, **kwargs):
    table = ym.parse_table(ym.normalize_text(text))
    item_map = ym.load_items(items, "items")
    thresholds = ym.load_config(config, "config")
    return ym.analyze(table, item_map, thresholds, now=kwargs.pop("now", FIXED_NOW), **kwargs)


SAMPLE_CSV = (
    "student,T1,T2,T3\n"
    "甲,5,0,1\n"
    "乙,5,2,0\n"
    "丙,3,2,1\n"
    "丁,0,0,0\n"
)

SAMPLE_ITEMS = make_items(
    [
        ("T1", 5, ["一元一次方程", "计算"]),
        ("T2", 2, ["一元一次方程"]),
        ("T3", 1, ["统计图表"], "easy"),
    ]
)


class NormalizeAndParseTest(unittest.TestCase):
    def test_bom_and_crlf_are_normalized(self):
        text = ym.normalize_text("\ufeffstudent,T1\r\n甲,5\r\n")
        self.assertNotIn("\ufeff", text)
        self.assertNotIn("\r", text)
        table = ym.parse_table(text)
        self.assertEqual(table["columns"], ["student", "T1"])
        self.assertEqual(table["rows"], [["甲", "5"]])

    def test_tsv_is_detected(self):
        table = ym.parse_table(ym.normalize_text("student\tT1\n甲\t5\n"))
        self.assertEqual(table["columns"], ["student", "T1"])
        self.assertEqual(table["rows"], [["甲", "5"]])

    def test_blank_rows_are_skipped(self):
        table = ym.parse_table(ym.normalize_text("student,T1\n\n甲,5\n,\n"))
        self.assertEqual(len(table["rows"]), 1)

    def test_header_column_mismatch_is_input_error(self):
        with self.assertRaises(ym.InputError):
            ym.parse_table(ym.normalize_text("student,T1\n甲,5,6\n"))

    def test_empty_table_is_input_error(self):
        with self.assertRaises(ym.InputError):
            ym.parse_table(ym.normalize_text("\n\n"))


class ItemMapTest(unittest.TestCase):
    def test_items_are_loaded_with_index(self):
        item_map = ym.load_items(SAMPLE_ITEMS, "items")
        self.assertEqual(item_map["by_id"]["T1"]["max_score"], 5)
        self.assertEqual(item_map["order"], ["T1", "T2", "T3"])

    def test_duplicate_item_id_rejected(self):
        payload = make_items([("T1", 5, ["a"]), ("T1", 2, ["b"])])
        with self.assertRaises(ym.ConfigError):
            ym.load_items(payload, "items")

    def test_non_positive_max_score_rejected(self):
        with self.assertRaises(ym.ConfigError):
            ym.load_items(make_items([("T1", 0, ["a"])]), "items")

    def test_empty_knowledge_rejected(self):
        with self.assertRaises(ym.ConfigError):
            ym.load_items({"schema_version": ym.SCHEMA_VERSION, "items": [
                {"id": "T1", "max_score": 5, "knowledge": []}]}, "items")

    def test_schema_version_mismatch_rejected(self):
        with self.assertRaises(ym.ConfigError):
            ym.load_items({"schema_version": "9.9", "items": [
                {"id": "T1", "max_score": 5, "knowledge": ["a"]}]}, "items")

    def test_unknown_difficulty_rejected(self):
        with self.assertRaises(ym.ConfigError):
            ym.load_items(make_items([("T1", 5, ["a"], "impossible")]), "items")


class ConfigTest(unittest.TestCase):
    def test_defaults_are_used_when_config_missing(self):
        cfg = ym.load_config(None, "config")
        self.assertEqual(cfg["mastery"]["weak_below"], 0.6)
        self.assertEqual(cfg["mastery"]["solid_at_or_above"], 0.8)
        self.assertEqual(cfg["min_respondents"], 3)

    def test_partial_config_overrides_only_given_keys(self):
        cfg = ym.load_config({"mastery": {"weak_below": 0.5}}, "config")
        self.assertEqual(cfg["mastery"]["weak_below"], 0.5)
        self.assertEqual(cfg["mastery"]["solid_at_or_above"], 0.8)

    def test_invalid_threshold_order_rejected(self):
        with self.assertRaises(ym.ConfigError):
            ym.load_config({"mastery": {"weak_below": 0.9, "solid_at_or_above": 0.5}}, "config")

    def test_negative_min_respondents_rejected(self):
        with self.assertRaises(ym.ConfigError):
            ym.load_config({"min_respondents": 0}, "config")

    def test_non_numeric_threshold_rejected(self):
        with self.assertRaises(ym.ConfigError):
            ym.load_config({"layers": {"a_at_or_above": "high"}}, "config")


class DataGateTest(unittest.TestCase):
    def test_unknown_column_rejected(self):
        text = "student,T1,T9\n甲,5,1\n乙,5,1\n"
        with self.assertRaises(ym.InputError) as ctx:
            analyze_text(text, SAMPLE_ITEMS)
        self.assertIn("T9", str(ctx.exception))

    def test_declared_item_missing_from_table_rejected(self):
        text = "student,T1\n甲,5\n乙,5\n"
        with self.assertRaises(ym.InputError) as ctx:
            analyze_text(text, SAMPLE_ITEMS)
        self.assertIn("T2", str(ctx.exception))

    def test_duplicate_student_rejected(self):
        text = "student,T1,T2,T3\n甲,5,2,1\n甲,4,2,1\n乙,5,2,1\n"
        with self.assertRaises(ym.InputError):
            analyze_text(text, SAMPLE_ITEMS)

    def test_non_numeric_score_rejected_with_position(self):
        text = "student,T1,T2,T3\n甲,好,2,1\n乙,5,2,1\n丙,5,2,1\n"
        with self.assertRaises(ym.InputError) as ctx:
            analyze_text(text, SAMPLE_ITEMS)
        self.assertIn("T1", str(ctx.exception))

    def test_score_above_max_rejected(self):
        text = "student,T1,T2,T3\n甲,6,2,1\n乙,5,2,1\n丙,5,2,1\n"
        with self.assertRaises(ym.InputError):
            analyze_text(text, SAMPLE_ITEMS)

    def test_negative_score_rejected(self):
        text = "student,T1,T2,T3\n甲,-1,2,1\n乙,5,2,1\n丙,5,2,1\n"
        with self.assertRaises(ym.InputError):
            analyze_text(text, SAMPLE_ITEMS)

    def test_missing_cells_are_counted_not_fatal(self):
        text = "student,T1,T2,T3\n甲,5,,1\n乙,5,2,1\n丙,5,2,1\n"
        result = analyze_text(text, SAMPLE_ITEMS)
        self.assertEqual(result["quality"]["missing_cells"], 1)
        self.assertTrue(any("缺失" in w for w in result["quality"]["warnings"]))

    def test_single_student_input_is_allowed_but_reviewed(self):
        text = "student,T1,T2,T3\n甲,5,2,1\n"
        result = analyze_text(text, SAMPLE_ITEMS)
        self.assertEqual(result["summary"]["students"], 1)
        self.assertTrue(result["review_items"])


class ItemStatsTest(unittest.TestCase):
    def test_item_rate_uses_valid_respondents_only(self):
        text = "student,T1,T2,T3\n甲,5,2,1\n乙,5,,1\n"
        result = analyze_text(text, SAMPLE_ITEMS)
        stats = {row["id"]: row for row in result["items_stats"]}
        self.assertEqual(stats["T1"]["rate"], 1.0)
        self.assertEqual(stats["T2"]["respondents"], 1)
        self.assertEqual(stats["T2"]["missing"], 1)
        self.assertAlmostEqual(stats["T2"]["mean"], 2.0)

    def test_full_marks_and_zero_counts(self):
        text = "student,T1,T2,T3\n甲,5,2,1\n乙,5,0,0\n丙,0,0,0\n"
        result = analyze_text(text, SAMPLE_ITEMS)
        stats = {row["id"]: row for row in result["items_stats"]}
        self.assertEqual(stats["T1"]["full_marks"], 2)
        self.assertEqual(stats["T1"]["zero"], 1)
        self.assertEqual(stats["T3"]["zero"], 2)

    def test_item_without_respondents_is_reviewed(self):
        text = "student,T1,T2,T3\n甲,,2,1\n乙,,2,1\n丙,,2,1\n"
        result = analyze_text(text, SAMPLE_ITEMS)
        stats = {row["id"]: row for row in result["items_stats"]}
        self.assertIsNone(stats["T1"]["rate"])
        self.assertTrue(any(item.get("reason") == "item_no_data" for item in result["review_items"]))

    def test_stats_summary_counts(self):
        result = analyze_text(SAMPLE_CSV, SAMPLE_ITEMS)
        self.assertEqual(result["summary"]["students"], 4)
        self.assertEqual(result["summary"]["items"], 3)


class KnowledgeTest(unittest.TestCase):
    def test_knowledge_rate_is_score_weighted(self):
        text = "student,T1,T2,T3\n甲,5,2,1\n乙,0,0,1\n"
        result = analyze_text(text, SAMPLE_ITEMS)
        points = {row["name"]: row for row in result["knowledge"]}
        # 一元一次方程 = T1(5) + T2(2)：甲 7/7、乙 0/7 → 分子 7 / 分母 14
        self.assertAlmostEqual(points["一元一次方程"]["rate"], 7 / 14)
        self.assertEqual(points["一元一次方程"]["items"], ["T1", "T2"])

    def test_knowledge_levels_follow_thresholds(self):
        text = "student,T1,T2,T3\n甲,5,2,1\n乙,5,2,1\n丙,0,0,1\n丁,0,0,1\n"
        result = analyze_text(text, SAMPLE_ITEMS)
        points = {row["name"]: row for row in result["knowledge"]}
        self.assertEqual(points["一元一次方程"]["level"], "weak")
        self.assertEqual(points["统计图表"]["level"], "solid")

    def test_item_can_belong_to_multiple_knowledge_points(self):
        result = analyze_text(SAMPLE_CSV, SAMPLE_ITEMS)
        points = {row["name"]: row for row in result["knowledge"]}
        self.assertIn("T1", points["一元一次方程"]["items"])
        self.assertIn("T1", points["计算"]["items"])

    def test_knowledge_rows_are_sorted_by_rate(self):
        result = analyze_text(SAMPLE_CSV, SAMPLE_ITEMS)
        rates = [row["rate"] for row in result["knowledge"]]
        self.assertEqual(rates, sorted(rates))

    def test_low_sample_knowledge_is_reviewed_not_diagnosed(self):
        text = "student,T1,T2,T3\n甲,0,0,1\n"
        result = analyze_text(text, SAMPLE_ITEMS)
        names = [d["knowledge"] for d in result["diagnoses"]]
        self.assertNotIn("一元一次方程", names)
        self.assertTrue(any(item.get("reason") == "knowledge_low_sample" for item in result["review_items"]))


class LayerTest(unittest.TestCase):
    def test_layer_assignment_follows_thresholds(self):
        text = (
            "student,T1,T2,T3\n"
            "甲,5,2,1\n"
            "乙,3,2,1\n"
            "丙,1,0,0\n"
        )
        result = analyze_text(text, SAMPLE_ITEMS)
        layers = result["layers"]
        self.assertEqual(layers["a"], ["甲"])
        self.assertIn("乙", layers["b"])
        self.assertEqual(layers["c"], ["丙"])

    def test_critical_students_are_flagged(self):
        text = "student,T1,T2,T3\n甲,4,2,1\n乙,5,2,1\n丙,4,2,1\n"
        result = analyze_text(text, SAMPLE_ITEMS)
        self.assertIn("甲", result["layers"]["critical"])
        self.assertTrue(any(item.get("reason") == "critical_student" for item in result["review_items"]))

    def test_student_with_missing_answers_is_reviewed(self):
        text = "student,T1,T2,T3\n甲,5,,1\n乙,5,2,1\n丙,5,2,1\n"
        result = analyze_text(text, SAMPLE_ITEMS)
        self.assertTrue(any(item.get("reason") == "student_missing_answers" for item in result["review_items"]))

    def test_layer_counts_in_summary(self):
        result = analyze_text(SAMPLE_CSV, SAMPLE_ITEMS)
        self.assertEqual(sum(result["summary"]["layer_counts"].values()), 4)


class DiagnosisTest(unittest.TestCase):
    def test_weak_knowledge_produces_diagnosis_with_evidence(self):
        text = "student,T1,T2,T3\n甲,0,0,1\n乙,0,0,1\n丙,0,0,1\n"
        result = analyze_text(text, SAMPLE_ITEMS)
        diagnosis = [d for d in result["diagnoses"] if d["knowledge"] == "一元一次方程"]
        self.assertEqual(len(diagnosis), 1)
        evidence = diagnosis[0]["evidence"]
        self.assertEqual(evidence["items"], ["T1", "T2"])
        self.assertEqual(evidence["respondents"], 3)
        self.assertAlmostEqual(evidence["rate"], 0.0)
        self.assertEqual(evidence["threshold"], 0.6)

    def test_very_low_rate_is_high_severity(self):
        text = "student,T1,T2,T3\n甲,0,0,1\n乙,0,0,1\n丙,0,0,1\n"
        result = analyze_text(text, SAMPLE_ITEMS)
        diagnosis = [d for d in result["diagnoses"] if d["knowledge"] == "一元一次方程"][0]
        self.assertEqual(diagnosis["severity"], "high")

    def test_moderate_weak_rate_is_medium_severity(self):
        text = "student,T1,T2,T3\n甲,3,1,1\n乙,3,1,1\n丙,3,1,1\n"
        result = analyze_text(text, SAMPLE_ITEMS)
        diagnosis = [d for d in result["diagnoses"] if d["knowledge"] == "一元一次方程"]
        self.assertEqual(diagnosis[0]["severity"], "medium")

    def test_solid_knowledge_is_not_diagnosed(self):
        text = "student,T1,T2,T3\n甲,5,2,1\n乙,5,2,1\n丙,5,2,1\n丁,4,2,1\n"
        result = analyze_text(text, SAMPLE_ITEMS)
        points = {row["name"]: row for row in result["knowledge"]}
        self.assertEqual(points["统计图表"]["level"], "solid")
        names = [d["knowledge"] for d in result["diagnoses"]]
        self.assertNotIn("统计图表", names)

    def test_diagnoses_are_sorted_by_rate_then_name(self):
        result = analyze_text(SAMPLE_CSV, SAMPLE_ITEMS)
        keys = [(d["rate"], d["knowledge"]) for d in result["diagnoses"]]
        self.assertEqual(keys, sorted(keys))


class AnonymizeTest(unittest.TestCase):
    def test_anonymized_id_is_stable_and_prefixed(self):
        first = ym.anonymize_id("张三")
        second = ym.anonymize_id("张三")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("S-"))
        self.assertEqual(len(first), 10)

    def test_anonymize_replaces_student_identifiers(self):
        text = "student,T1,T2,T3\n张三,5,2,1\n李四,5,2,1\n王五,5,2,1\n"
        result = analyze_text(text, SAMPLE_ITEMS, anonymize=True)
        self.assertNotIn("张三", json.dumps(result, ensure_ascii=False))
        self.assertIn(ym.anonymize_id("张三"), result["layers"]["a"] + result["layers"]["b"] + result["layers"]["c"])

    def test_anonymize_keeps_statistics_identical(self):
        plain = analyze_text(SAMPLE_CSV, SAMPLE_ITEMS)
        hidden = analyze_text(SAMPLE_CSV, SAMPLE_ITEMS, anonymize=True)
        self.assertEqual(
            [(row["id"], row["rate"]) for row in plain["items_stats"]],
            [(row["id"], row["rate"]) for row in hidden["items_stats"]],
        )


class ReportTest(unittest.TestCase):
    def test_markdown_contains_key_sections(self):
        result = analyze_text(SAMPLE_CSV, SAMPLE_ITEMS)
        text = ym.render_markdown(result)
        for section in ("学情分析报告", "逐题统计", "知识点掌握", "薄弱点诊断", "人工复核", "免责声明"):
            self.assertIn(section, text)

    def test_markdown_lists_diagnosis_evidence(self):
        text = "student,T1,T2,T3\n甲,0,0,1\n乙,0,0,1\n丙,0,0,1\n"
        result = analyze_text(text, SAMPLE_ITEMS)
        text_out = ym.render_markdown(result)
        self.assertIn("一元一次方程", text_out)
        self.assertIn("T1", text_out)

    def test_json_contract_keys(self):
        result = analyze_text(SAMPLE_CSV, SAMPLE_ITEMS)
        payload = json.loads(ym.render_json(result))
        for key in ("schema_version", "tool", "tool_version", "generated_at", "input",
                    "quality", "items_stats", "knowledge", "layers", "diagnoses",
                    "review_items", "summary", "disclaimer"):
            self.assertIn(key, payload)
        self.assertEqual(payload["schema_version"], ym.SCHEMA_VERSION)
        self.assertEqual(payload["tool"], "yotta-mirror")

    def test_json_is_deterministic_except_timestamp(self):
        first = analyze_text(SAMPLE_CSV, SAMPLE_ITEMS)
        second = analyze_text(SAMPLE_CSV, SAMPLE_ITEMS)
        first.pop("generated_at")
        second.pop("generated_at")
        self.assertEqual(json.dumps(first, sort_keys=True, ensure_ascii=False),
                         json.dumps(second, sort_keys=True, ensure_ascii=False))

    def test_report_carries_disclaimer(self):
        result = analyze_text(SAMPLE_CSV, SAMPLE_ITEMS)
        self.assertIn("教学改进", result["disclaimer"])
        self.assertNotIn("排名", "".join(result["layers"]["a"]))


class SecurityBoundaryTest(unittest.TestCase):
    def test_engine_has_no_network_imports(self):
        source = (SCRIPTS_DIR / "yotta_mirror.py").read_text(encoding="utf-8")
        for marker in ("import urllib", "import socket", "import requests", "http.client"):
            self.assertNotIn(marker, source)

    def test_output_path_inside_directory_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ym.InputError):
                ym.resolve_output_path(td)

    def test_output_path_parent_escape_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ym.InputError):
                ym.resolve_output_path(os.path.join(td, "..", "escape.json"))

    def test_output_path_normal_file_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            path = ym.resolve_output_path(os.path.join(td, "report.json"))
            self.assertTrue(str(path).endswith("report.json"))


class TemplateTest(unittest.TestCase):
    def test_template_command_writes_three_files(self):
        with tempfile.TemporaryDirectory() as td:
            code = ym.main(["template", "--output-dir", td])
            self.assertEqual(code, 0)
            names = sorted(p.name for p in Path(td).iterdir())
            self.assertEqual(names, ["item-map.json", "scores.csv", "thresholds.json"])

    def test_template_files_are_utf8_and_parseable(self):
        with tempfile.TemporaryDirectory() as td:
            ym.main(["template", "--output-dir", td])
            csv_text = (Path(td) / "scores.csv").read_text(encoding="utf-8")
            self.assertTrue(csv_text.startswith("student,"))
            items = json.loads((Path(td) / "item-map.json").read_text(encoding="utf-8"))
            self.assertEqual(items["schema_version"], ym.SCHEMA_VERSION)
            ym.load_items(items, "template")
            cfg = json.loads((Path(td) / "thresholds.json").read_text(encoding="utf-8"))
            ym.load_config(cfg, "template")


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.csv = self.dir / "scores.csv"
        self.csv.write_text(SAMPLE_CSV, encoding="utf-8")
        self.items = self.dir / "items.json"
        self.items.write_text(json.dumps(SAMPLE_ITEMS, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_cli_version(self):
        self.assertEqual(ym.main(["--version"]), 0)

    def test_cli_analyze_writes_markdown(self):
        out = self.dir / "report.md"
        code = ym.main(["analyze", "--input", str(self.csv), "--items", str(self.items),
                        "--out", str(out)])
        self.assertEqual(code, 0)
        self.assertIn("学情分析报告", out.read_text(encoding="utf-8"))

    def test_cli_json_format(self):
        out = self.dir / "report.json"
        ym.main(["analyze", "--input", str(self.csv), "--items", str(self.items),
                 "--format", "json", "--out", str(out)])
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["tool"], "yotta-mirror")

    def test_cli_gate_triggers_exit_one(self):
        text = "student,T1,T2,T3\n甲,0,0,1\n乙,0,0,1\n丙,0,0,1\n"
        gap = self.dir / "gap.csv"
        gap.write_text(text, encoding="utf-8")
        code = ym.main(["analyze", "--input", str(gap), "--items", str(self.items),
                        "--gate", "weak=1"])
        self.assertEqual(code, 1)

    def test_cli_gate_not_triggered(self):
        code = ym.main(["analyze", "--input", str(self.csv), "--items", str(self.items),
                        "--gate", "weak=99"])
        self.assertEqual(code, 0)

    def test_cli_missing_input_file_exit_two(self):
        code = ym.main(["analyze", "--input", str(self.dir / "nope.csv"),
                        "--items", str(self.items)])
        self.assertEqual(code, 2)

    def test_cli_bad_item_map_exit_three(self):
        bad = self.dir / "bad.json"
        bad.write_text("{\"schema_version\": \"9.9\", \"items\": []}", encoding="utf-8")
        code = ym.main(["analyze", "--input", str(self.csv), "--items", str(bad)])
        self.assertEqual(code, 3)

    def test_cli_stdin_path(self):
        out = self.dir / "stdin.json"
        code = ym.main(["analyze", "--stdin", "--items", str(self.items),
                        "--format", "json", "--out", str(out)],
                       stdin_text=SAMPLE_CSV)
        self.assertEqual(code, 0)
        self.assertTrue(out.exists())

    def test_cli_config_validate_ok(self):
        cfg = self.dir / "cfg.json"
        cfg.write_text(json.dumps({"mastery": {"weak_below": 0.5}}, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(ym.main(["config", "validate", "--pack", str(cfg)]), 0)

    def test_cli_config_validate_bad_exit_three(self):
        cfg = self.dir / "bad-cfg.json"
        cfg.write_text('{"min_respondents": 0}', encoding="utf-8")
        self.assertEqual(ym.main(["config", "validate", "--pack", str(cfg)]), 3)

    def test_cli_anonymize_flag(self):
        out = self.dir / "anon.json"
        ym.main(["analyze", "--input", str(self.csv), "--items", str(self.items),
                 "--format", "json", "--anonymize", "--out", str(out)])
        text = out.read_text(encoding="utf-8")
        self.assertNotIn("甲", text)
        self.assertIn("S-", text)

    def test_cli_unknown_command_exit_two(self):
        self.assertEqual(ym.main(["nope"]), 2)


class FixtureIntegrationTest(unittest.TestCase):
    fixtures = SCRIPTS_DIR / "fixtures"

    def test_weak_class_fixture_triggers_gate(self):
        code = ym.main([
            "analyze",
            "--input", str(self.fixtures / "samples" / "class-weak.csv"),
            "--items", str(self.fixtures / "samples" / "item-map.json"),
            "--gate", "weak=1",
        ])
        self.assertEqual(code, 1)

    def test_balanced_class_fixture_is_clean(self):
        code = ym.main([
            "analyze",
            "--input", str(self.fixtures / "samples" / "class-balanced.csv"),
            "--items", str(self.fixtures / "samples" / "item-map.json"),
            "--gate", "weak=1",
        ])
        self.assertEqual(code, 0)

    def test_balanced_fixture_report_has_no_high_diagnosis(self):
        table = ym.parse_table(ym.normalize_text(
            (self.fixtures / "samples" / "class-balanced.csv").read_text(encoding="utf-8")))
        items = ym.load_items(json.loads(
            (self.fixtures / "samples" / "item-map.json").read_text(encoding="utf-8")), "items")
        cfg = ym.load_config(None, "config")
        result = ym.analyze(table, items, cfg, now=FIXED_NOW)
        self.assertEqual([d for d in result["diagnoses"] if d["severity"] == "high"], [])


class PathSafetyTest(unittest.TestCase):
    def test_engine_source_has_no_absolute_author_paths(self):
        source = (SCRIPTS_DIR / "yotta_mirror.py").read_text(encoding="utf-8")
        self.assertNotIn("D:\\AI_WorkDir", source)
        self.assertNotIn("C:\\Users", source)

    def test_cli_writes_are_local_only(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "r.md"
            ym.main(["analyze", "--input", str(SCRIPTS_DIR / "fixtures" / "samples" / "class-weak.csv"),
                     "--items", str(SCRIPTS_DIR / "fixtures" / "samples" / "item-map.json"),
                     "--out", str(out)])
            self.assertTrue(out.exists())


class DocContractTest(unittest.TestCase):
    root = SCRIPTS_DIR.parent

    def _read(self, *parts):
        return (self.root.joinpath(*parts)).read_text(encoding="utf-8")

    def test_skill_frontmatter_matches_engine(self):
        text = self._read("SKILL.md")
        front = text.split("---", 2)[1]
        self.assertIn("name: yotta-mirror", front)
        self.assertIn("version: %s" % ym.VERSION, front)
        self.assertIn("license: MIT", front)
        self.assertIn("description:", front)

    def test_skill_description_has_trigger_and_boundary(self):
        front = self._read("SKILL.md").split("---", 2)[1]
        self.assertIn("触发", front)
        self.assertIn("边界", front)
        self.assertIn("不联网", front)

    def test_references_are_complete(self):
        for name, marker in (
            ("data-format.md", "max_score"),
            ("report-format.md", "schema_version"),
            ("privacy.md", "anonymize"),
        ):
            text = self._read("references", name)
            self.assertGreater(len(text), 800, name)
            self.assertIn(marker, text)

    def test_readme_language_switch_and_four_ways(self):
        readme = self._read("README.md")
        self.assertRegex(readme, r"Language</b>: English")
        self.assertIn("npx -y @yottameta/yotta-mirror", readme)
        self.assertIn("git clone https://github.com/YottaMeta/yotta-mirror", readme)
        self.assertIn("Download ZIP", readme)
        self.assertIn("install.sh --agent", readme)
        self.assertNotIn("npx skills", readme)

    def test_chinese_readme_four_ways(self):
        readme = self._read("README.zh-CN.md")
        self.assertIn("方式一", readme)
        self.assertIn("方式四", readme)
        self.assertIn("install.sh --agent", readme)
        self.assertIn("npx -y @yottameta/yotta-mirror", readme)
        self.assertNotIn("npx skills", readme)

    def test_disclaimer_is_documented(self):
        for name in ("SKILL.md", "README.zh-CN.md", "references/privacy.md"):
            text = self._read(*name.split("/"))
            self.assertIn("教学改进", text, name)
        for marker in ("teaching improvement", "not for student evaluation"):
            self.assertIn(marker, self._read("README.md"))

    def test_report_doc_covers_actual_json_keys(self):
        table = ym.parse_table(ym.normalize_text(SAMPLE_CSV))
        items = ym.load_items(SAMPLE_ITEMS, "items")
        result = ym.analyze(table, items, ym.load_config(None, "config"), now=FIXED_NOW)
        doc = self._read("references", "report-format.md")
        for key in result:
            self.assertIn("`%s`" % key, doc, key)

    def test_data_format_doc_lists_review_reasons(self):
        doc = self._read("references", "report-format.md")
        for reason in ("item_no_data", "item_low_sample", "knowledge_low_sample",
                       "student_missing_answers", "critical_student"):
            self.assertIn(reason, doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Tests for declared job settings, stored values and hot reload."""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from streambot.config import ConfigurationError
from streambot.job_config import (
    ConfigSchema,
    JobConfig,
    read_values,
    values_path,
    write_values,
)

IDLE_FIELD = {
    "key": "idle_seconds",
    "label": "Idle",
    "type": "integer",
    "min": 30,
    "max": 3600,
    "default": 210,
    "unit": "s",
}


def manifest(**overrides) -> dict:
    block = {
        "fields": [dict(IDLE_FIELD)],
        "presets": [
            {"label": "Short", "values": {"idle_seconds": 210}},
            {"label": "Long", "values": {"idle_seconds": 930}},
        ],
    }
    block.update(overrides)
    return block


class SchemaTests(unittest.TestCase):
    def test_every_job_gets_stop_conditions_without_declaring_them(self) -> None:
        schema = ConfigSchema.from_manifest(None)
        defaults = schema.defaults()
        self.assertEqual(defaults["max_cycles"], 0)
        self.assertEqual(defaults["max_seconds"], 0)
        self.assertEqual(defaults["poll_seconds"], 3.0)

    def test_zero_means_unlimited_and_is_in_range(self) -> None:
        schema = ConfigSchema.from_manifest(None)
        self.assertEqual(schema.validate({"max_cycles": 0}), {"max_cycles": 0})
        with self.assertRaises(ConfigurationError):
            schema.validate({"max_cycles": -1})

    def test_declared_fields_come_before_the_builtins(self) -> None:
        schema = ConfigSchema.from_manifest(manifest())
        self.assertEqual(
            [field.key for field in schema.fields],
            ["idle_seconds", "max_cycles", "max_seconds", "poll_seconds"],
        )

    def test_a_job_may_tighten_a_builtin(self) -> None:
        schema = ConfigSchema.from_manifest(
            manifest(
                fields=[
                    dict(IDLE_FIELD),
                    {
                        "key": "poll_seconds",
                        "type": "number",
                        "min": 1.0,
                        "max": 10.0,
                        "default": 3.0,
                    },
                ]
            )
        )
        self.assertEqual(len(schema.by_key["poll_seconds"].choices), 0)
        self.assertEqual(schema.by_key["poll_seconds"].maximum, 10.0)
        with self.assertRaises(ConfigurationError):
            schema.validate({"poll_seconds": 30.0})

    def test_out_of_range_values_are_rejected(self) -> None:
        schema = ConfigSchema.from_manifest(manifest())
        with self.assertRaises(ConfigurationError):
            schema.validate({"idle_seconds": 5})

    def test_unknown_setting_is_rejected(self) -> None:
        schema = ConfigSchema.from_manifest(manifest())
        with self.assertRaises(ConfigurationError):
            schema.validate({"nope": 1})

    def test_number_field_must_declare_bounds(self) -> None:
        with self.assertRaises(ConfigurationError):
            ConfigSchema.from_manifest(
                {"fields": [{"key": "x", "type": "integer", "default": 1}]}
            )

    def test_default_that_the_field_would_reject_is_a_declaration_bug(self) -> None:
        bad = dict(IDLE_FIELD)
        bad["default"] = 5
        with self.assertRaises(ConfigurationError):
            ConfigSchema.from_manifest({"fields": [bad]})

    def test_preset_must_use_declared_keys_and_valid_values(self) -> None:
        with self.assertRaises(ConfigurationError):
            ConfigSchema.from_manifest(
                manifest(presets=[{"label": "X", "values": {"nope": 1}}])
            )
        with self.assertRaises(ConfigurationError):
            ConfigSchema.from_manifest(
                manifest(presets=[{"label": "X", "values": {"idle_seconds": 5}}])
            )

    def test_enum_requires_choices(self) -> None:
        with self.assertRaises(ConfigurationError):
            ConfigSchema.from_manifest(
                {"fields": [{"key": "mode", "type": "enum", "default": "a"}]}
            )

    def test_schema_serialises_for_the_console(self) -> None:
        payload = ConfigSchema.from_manifest(manifest()).as_dict()
        self.assertEqual(payload["fields"][0]["key"], "idle_seconds")
        self.assertEqual(payload["fields"][0]["unit"], "s")
        self.assertEqual(payload["presets"][1]["label"], "Long")

    def test_multiline_text_field_serialises_and_allows_a_long_value(self) -> None:
        schema = ConfigSchema.from_manifest(
            {
                "fields": [
                    {
                        "key": "watchlist",
                        "type": "text",
                        "multiline": True,
                        "default": "a <= 1\nb <= 2",
                    }
                ]
            }
        )
        payload = schema.as_dict()
        self.assertTrue(payload["fields"][0]["multiline"])
        # A multi-line list needs more room than a single-line box allows.
        long_list = "\n".join(f"item{i} <= {i}万" for i in range(150))
        self.assertGreater(len(long_list), 200)
        self.assertEqual(schema.validate({"watchlist": long_list})["watchlist"], long_list)

    def test_multiline_is_rejected_on_a_non_text_field(self) -> None:
        with self.assertRaises(ConfigurationError):
            ConfigSchema.from_manifest(
                {
                    "fields": [
                        {"key": "n", "type": "integer", "min": 0, "max": 9,
                         "default": 1, "multiline": True}
                    ]
                }
            )


class ValuesFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.root = Path(self._directory.name)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def test_write_then_read_round_trips(self) -> None:
        path = values_path("demo", self.root)
        write_values(path, {"idle_seconds": 300})
        self.assertEqual(read_values(path), {"idle_seconds": 300})

    def test_write_is_atomic_and_leaves_no_debris(self) -> None:
        path = values_path("demo", self.root)
        write_values(path, {"idle_seconds": 300})
        write_values(path, {"idle_seconds": 400})
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["demo.json"])

    def test_unreadable_values_are_distinct_from_cleared_ones(self) -> None:
        # None means "could not read"; {} means "the operator cleared these".
        path = values_path("demo", self.root)
        self.assertIsNone(read_values(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(read_values(path))
        write_values(path, {})
        self.assertEqual(read_values(path), {})


class HotReloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.schema = ConfigSchema.from_manifest(manifest())
        self.path = values_path("demo", self.root)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _config(self) -> JobConfig:
        return JobConfig(self.schema, self.path)

    def test_starts_from_declared_defaults(self) -> None:
        config = self._config()
        self.assertEqual(config.get("idle_seconds"), 210)
        self.assertEqual(config.get("max_cycles"), 0)

    def test_an_edit_is_adopted_without_a_restart(self) -> None:
        config = self._config()
        write_values(self.path, {"idle_seconds": 930})
        changed = config.poll()
        self.assertEqual(changed, {"idle_seconds": 930})
        self.assertEqual(config.get("idle_seconds"), 930)

    def test_polling_an_unchanged_file_reports_nothing(self) -> None:
        config = self._config()
        write_values(self.path, {"idle_seconds": 930})
        config.poll()
        self.assertEqual(config.poll(), {})

    def test_a_half_written_file_keeps_the_values_it_has(self) -> None:
        config = self._config()
        write_values(self.path, {"idle_seconds": 930})
        config.poll()
        # Caught mid-write: unparseable now, complete a moment later.
        self.path.write_text('{"idle_seconds": 3', encoding="utf-8")
        os.utime(self.path, ns=(time.time_ns(), time.time_ns()))
        self.assertEqual(config.poll(), {})
        self.assertEqual(config.get("idle_seconds"), 930)
        write_values(self.path, {"idle_seconds": 300})
        self.assertEqual(config.poll(), {"idle_seconds": 300})

    def test_a_stored_value_that_no_longer_fits_is_dropped_not_fatal(self) -> None:
        # The job tightened a bound after the operator had saved a value; the
        # old preference must not be able to stop the job from starting.
        write_values(self.path, {"idle_seconds": 3599})
        tightened = ConfigSchema.from_manifest(
            manifest(
                fields=[
                    {
                        "key": "idle_seconds",
                        "type": "integer",
                        "min": 30,
                        "max": 600,
                        "default": 210,
                    }
                ],
                presets=[],
            )
        )
        config = JobConfig(tightened, self.path)
        self.assertEqual(config.get("idle_seconds"), 210)

    def test_an_unknown_stored_key_is_ignored(self) -> None:
        write_values(self.path, {"idle_seconds": 300, "removed_setting": 7})
        config = self._config()
        self.assertEqual(config.get("idle_seconds"), 300)
        self.assertNotIn("removed_setting", config.values)


if __name__ == "__main__":
    unittest.main()

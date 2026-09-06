from __future__ import annotations

import unittest

from plachem_fast_gateway.loop_detector import (
    LoopDetectorConfig, RunScope, RunScopedLoopDetector, normalize,
)


BLOCK = "bounded worker output advances one verified operation"


class LoopDetectorTests(unittest.TestCase):
    def scope(self, suffix="a"):
        return RunScope(f"core-{suffix}", f"oc-{suffix}", f"agent:local:{suffix}", "local")

    def detector(self, **updates):
        values = {"cooldown_seconds": 300}
        values.update(updates)
        return RunScopedLoopDetector(LoopDetectorConfig(**values), clock=lambda: 10.0)

    def test_normal_output_has_no_event(self):
        self.assertIsNone(self.detector().observe(self.scope(), "step one then step two then complete"))

    def test_four_repeated_blocks_are_detected(self):
        event = self.detector().observe(self.scope(), BLOCK * 4)
        self.assertEqual("LOOP_SUSPECTED", event["event"])
        self.assertGreaterEqual(event["repeat_count"], 4)

    def test_three_repeated_blocks_are_not_detected(self):
        self.assertIsNone(self.detector().observe(self.scope(), BLOCK * 3))

    def test_whitespace_and_punctuation_are_normalized(self):
        variants = [
            "Bounded worker output advances one verified operation!",
            "bounded   worker output advances one verified operation.",
            "BOUNDED worker output advances one verified operation?",
            "bounded worker output advances one verified operation;",
        ]
        self.assertIsNotNone(self.detector().observe(self.scope(), " ".join(variants)))
        self.assertEqual("helloworld", normalize(" Hello,  WORLD! "))

    def test_exception_is_excluded(self):
        block = "model loading normal system runtime progress marker"
        self.assertIsNone(self.detector().observe(self.scope(), block * 4))

    def test_runs_are_isolated(self):
        detector = self.detector()
        for _ in range(3):
            self.assertIsNone(detector.observe(self.scope("a"), BLOCK))
        self.assertIsNone(detector.observe(self.scope("b"), BLOCK))
        self.assertIsNotNone(detector.observe(self.scope("a"), BLOCK))

    def test_duplicate_is_suppressed(self):
        detector = self.detector()
        self.assertIsNotNone(detector.observe(self.scope(), BLOCK * 4))
        self.assertIsNone(detector.observe(self.scope(), BLOCK))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Run offline behavioral checks for the feedback helper; no OJ calls."""

import json
import random
import struct
import unittest
from fractions import Fraction
from pathlib import Path

from feedback_channel import (copysign_round3_counts, display_code, error_interval,
                              merge_crt, parse_percent, replay_copysign, round_count)


class ChannelTests(unittest.TestCase):
    def test_inverse_matches_brute_force(self):
        # Compare inverse bounds with every actual reachable error count.
        for scale in (100, 1000):
            for mode in ('half-up', 'floor'):
                for length in range(1, 45):
                    buckets = {}
                    for errors in range(length + 1):
                        ratio = Fraction(errors * scale, length)
                        expected = (ratio.numerator // ratio.denominator if mode == 'floor' else
                                    (ratio + Fraction(1, 2)).numerator //
                                    (ratio + Fraction(1, 2)).denominator)
                        self.assertEqual(display_code(length, errors, scale, mode), expected)
                        buckets.setdefault(expected, []).append(errors)
                    for code in range(scale + 1):
                        candidates = buckets.get(code, [])
                        expected_interval = (candidates[0], candidates[-1]) if candidates else None
                        self.assertEqual(error_interval(length, code, scale, mode), expected_interval)

    def test_slack_is_union_of_reachable_buckets(self):
        for mode in ('half-up', 'floor'):
            for length in (1, 7, 101, 10001):
                for code in (0, 1, 13, 50, 99, 100):
                    actual = [e for e in range(length + 1)
                              if abs(display_code(length, e, 100, mode) - code) <= 1]
                    expected = (actual[0], actual[-1]) if actual else None
                    self.assertEqual(error_interval(length, code, 100, mode, 1), expected)

    def test_encoding_recovery_and_int64_limits(self):
        generator = random.Random(42)
        for length in (10000, 10001, 65537, 2**31-1, 2**63-1, 2**64-1):
            for code in [0, 1, 4999, 5000, 9999, 10000] + [generator.randrange(10001) for _ in range(50)]:
                errors = round_count(length, code, 10000)
                self.assertLessEqual(errors, length)
                self.assertEqual(display_code(length, errors, 10000), code)
        self.assertNotEqual(display_code(7, round_count(7, 1000, 10000), 10000), 1000)

    def test_float32_counterexample(self):
        f32 = lambda number: struct.unpack('f', struct.pack('f', number))[0]
        length, errors = 10001, round_count(10001, 5000, 10000)
        self.assertEqual(display_code(length, errors, 10000), 5000)
        rounded_intermediate = f32(100 * errors / length)
        self.assertGreater(rounded_intermediate, 50.005)

    def test_generalized_crt_by_brute_force(self):
        self.assertEqual(merge_crt([2, 3, 2], [3, 5, 7]), (23, 105))
        for m in range(1, 9):
            for n in range(1, 9):
                period = m * n // __import__('math').gcd(m, n)
                for a in range(m):
                    for b in range(n):
                        matches = [x for x in range(period) if x % m == a and x % n == b]
                        if not matches:
                            with self.assertRaises(ValueError):
                                merge_crt([a, b], [m, n])
                        else:
                            self.assertEqual(merge_crt([a, b], [m, n]), (matches[0], period))

    def test_documented_case_from_raw_feedback(self):
        observations = Path(__file__).resolve().parents[1] / 'references' / 'copysign-observations.json'
        decoded = replay_copysign(json.loads(observations.read_text(encoding='utf-8')))['results']
        expected_sizes = [1009, 1024, 4097, 8192, 65537, 262144, 1000003, 2000000,
                          4194304, 8000003, 10000000, 16777216, 25000003, 33554432, 39999997]
        names = ['int8', 'int16', 'int32', 'float16', 'float32'] * 3
        self.assertEqual(len(decoded), 15)
        for row, size, name in zip(decoded, expected_sizes, names):
            self.assertEqual(row['status'], 'unique')
            self.assertEqual(row['candidates'][0]['N'], size)
            self.assertEqual(row['candidates'][0]['dtype'], name)
        for row, positive_count in zip(decoded, [500, 529, 2028, 4060]):
            self.assertEqual(row['candidates'][0]['positive_count_interval'], [positive_count, positive_count])

    def test_round3_bands_and_overflow(self):
        for high in range(64):
            lower = 10000 - 10000 // (high + 1)
            upper = 10000 - 10000 // (high + 2) - 1
            for length in (max(10000, high * 5000000), (high + 1) * 5000000 - 1):
                base, sample = copysign_round3_counts(length)
                self.assertTrue(0 <= base <= base + sample <= length)
                for positives in (0, sample // 2, sample):
                    code = display_code(length, base + positives, 10000)
                    self.assertTrue(lower <= code <= upper)
        self.assertEqual(copysign_round3_counts(320000000), (320000000, 0))

    def test_percent_parsing_and_invalid_inputs(self):
        self.assertEqual(parse_percent('25.11%', 2), 2511)
        for text in ('NaN', 'Infinity', '100.01', '-0.01', '25.111'):
            with self.assertRaises(ValueError):
                parse_percent(text, 2)
        with self.assertRaises(ValueError):
            round_count(0, 0, 10000)
        with self.assertRaises(ValueError):
            error_interval(1, 10001, 10000)


if __name__ == '__main__':
    unittest.main(verbosity=2)

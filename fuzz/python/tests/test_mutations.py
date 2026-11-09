"""Field and duplicate mutation selection."""

import unittest

from coverage_analysis.mutations import MutationError, expand_trace, validate_mutations


def trace():
    return [
        {"name": "login_first", "step": "user"},
        {"name": "login_second", "step": "pass"},
        {"name": "listen_once", "step": "listen"},
        {"name": "quit", "step": "quit"},
    ]


def model():
    return {
        "user": {"action": "send"},
        "pass": {"action": "send"},
        "listen": {"action": "listen"},
        "quit": {"action": "send"},
    }


class MutationTests(unittest.TestCase):
    def test_field_single_multi_and_unmodeled(self):
        items = trace()
        validate_mutations(items, [{
            "op": "field", "steps": ["login_first"], "field": "username",
            "suggestion": "empty", "evidence": "src/auth.c:120",
        }], model())
        validate_mutations(items, [{
            "op": "field", "steps": "[login_first: login_second]", "field": "token",
            "suggestion": "boundary", "evidence": "src/auth.c:130",
        }], model())
        with self.assertRaises(MutationError):
            validate_mutations(items, [{
                "op": "field", "steps": ["listen_once"], "field": "port",
                "suggestion": "x", "evidence": "src/a.c:1",
            }], model())

    def test_duplicate_each_and_range_counts(self):
        items = trace()
        each = expand_trace(items, [{
            "op": "duplicate", "steps": ["login_first", "quit"], "count_param": "n",
        }], {"n": 1})
        self.assertEqual([item["name"] for item in each],
                         ["login_first", "login_first", "login_second", "listen_once", "quit", "quit"])
        block = expand_trace(items, [{
            "op": "duplicate", "steps": "[login_first:login_second]", "count_param": "n",
        }], {"n": 2})
        self.assertEqual([item["name"] for item in block][:6],
                         ["login_first", "login_second"] * 3)
        self.assertEqual(len(expand_trace(items, [{
            "op": "duplicate", "steps": ["quit"], "count_param": "n",
        }], {"n": 0})), 4)
        self.assertEqual(len(expand_trace(items, [{
            "op": "duplicate", "steps": ["quit"], "count_param": "n",
        }], {"n": 1.5})), 4)
        self.assertEqual(len(expand_trace(items, [{
            "op": "duplicate", "steps": ["quit"], "count_param": "n",
        }], {"n": True})), 4)

    def test_overlap_cap_and_combined_field(self):
        items = trace()
        with self.assertRaisesRegex(MutationError, "overlap"):
            validate_mutations(items, [
                {"op": "duplicate", "steps": "[login_first:login_second]", "count_param": "n"},
                {"op": "duplicate", "steps": ["login_second"], "count_param": "m"},
            ], model())
        with self.assertRaisesRegex(MutationError, "repeated"):
            validate_mutations(items, [
                {"op": "field", "steps": ["login_first"], "field": "username",
                 "suggestion": "a", "evidence": "s:1"},
                {"op": "field", "steps": ["login_first"], "field": "username",
                 "suggestion": "b", "evidence": "s:2"},
            ], model())
        validate_mutations(items, [
            {"op": "field", "steps": ["login_first"], "field": "username",
             "suggestion": "a", "evidence": "s:1"},
            {"op": "duplicate", "steps": ["login_first"], "count_param": "n"},
        ], model())
        with self.assertRaisesRegex(MutationError, "reversed"):
            validate_mutations(items, [{
                "op": "duplicate", "steps": "[quit:login_first]", "count_param": "n",
            }], model())
        huge = [{"name": f"s{index}", "step": "user"} for index in range(4096)]
        with self.assertRaisesRegex(MutationError, "4096"):
            expand_trace(huge, [{
                "op": "duplicate", "steps": ["s0"], "count_param": "n",
            }], {"n": 1})
        with self.assertRaisesRegex(MutationError, "outside"):
            expand_trace(items, [{
                "op": "duplicate", "steps": ["quit"], "count_param": "n",
            }], {"n": 33})


if __name__ == "__main__":
    unittest.main()

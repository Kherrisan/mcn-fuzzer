"""Field mutation selection and kind validation."""

import unittest

from coverage_analysis.mutations import (
    MutationError,
    field_type,
    validate_mutations,
)


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

    def test_duplicate_is_rejected(self):
        with self.assertRaisesRegex(MutationError, "only field mutations"):
            validate_mutations(trace(), [{
                "op": "duplicate", "steps": ["quit"], "count_param": "n",
            }], model())

    def test_kind_and_encoding_are_validated(self):
        self.assertEqual(field_type({"kind": "bytes", "encoding": "hex"}), ("bytes", "hex"))
        self.assertEqual(field_type({"kind": "bytes"}), ("bytes", "hex"))
        self.assertEqual(field_type({"kind": "bytes", "encoding": "base64"}), ("bytes", "base64"))
        self.assertEqual(field_type({"kind": "integer"}), ("integer", None))
        self.assertEqual(field_type({}), ("string", None))
        for item, message in [
            ({"kind": "hex"}, "kind"),
            ({"kind": "string", "encoding": "hex"}, "encoding"),
            ({"kind": "bytes", "encoding": "utf8"}, "encoding"),
        ]:
            with self.subTest(item=item):
                with self.assertRaisesRegex(MutationError, message):
                    field_type(item)

    def test_repeated_field_and_reversed_range(self):
        items = trace()
        with self.assertRaisesRegex(MutationError, "repeated"):
            validate_mutations(items, [
                {"op": "field", "steps": ["login_first"], "field": "username",
                 "suggestion": "a", "evidence": "s:1"},
                {"op": "field", "steps": ["login_first"], "field": "username",
                 "suggestion": "b", "evidence": "s:2"},
            ], model())
        with self.assertRaisesRegex(MutationError, "reversed"):
            validate_mutations(items, [{
                "op": "field", "steps": "[quit:login_first]", "field": "x",
                "suggestion": "a", "evidence": "s:1",
            }], model())


if __name__ == "__main__":
    unittest.main()

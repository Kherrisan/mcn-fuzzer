"""Field kind and default validation for proposal mutable fields."""

import unittest

from coverage_analysis.mutations import MutationError, field_type, validate_default


class MutationTests(unittest.TestCase):
    def test_kind_and_encoding_are_validated(self):
        self.assertEqual(field_type({"kind": "bytes", "encoding": "hex"}), ("bytes", "hex"))
        self.assertEqual(field_type({"kind": "bytes"}), ("bytes", "hex"))
        self.assertEqual(field_type({"kind": "bytes", "encoding": "base64"}), ("bytes", "base64"))
        self.assertEqual(field_type({"kind": "integer"}), ("integer", None))
        self.assertEqual(field_type({"kind": "map"}), ("map", None))
        self.assertEqual(field_type({}), ("string", None))
        for item, message in [
            ({"kind": "hex"}, "kind"),
            ({"kind": "string", "encoding": "hex"}, "encoding"),
            ({"kind": "bytes", "encoding": "utf8"}, "encoding"),
        ]:
            with self.subTest(item=item):
                with self.assertRaisesRegex(MutationError, message):
                    field_type(item)

    def test_default_must_match_its_kind(self):
        validate_default({"default": "x"}, "string", None)
        validate_default({"default": 771}, "integer", None)
        validate_default({"default": True}, "boolean", None)
        validate_default({"default": [1, {"group": 29}]}, "list", None)
        validate_default({"default": {"group": 29}}, "map", None)
        validate_default({"default": "00ff"}, "bytes", "hex")
        validate_default({"default": "AP8="}, "bytes", "base64")
        validate_default({}, "integer", None)
        for item, kind, encoding, message in [
            ({"default": 1}, "string", None, "must be a string"),
            ({"default": "x"}, "integer", None, "must be an integer"),
            ({"default": 1}, "boolean", None, "must be a boolean"),
            ({"default": {"a": 1}}, "list", None, "must be an array"),
            ({"default": [1]}, "map", None, "must be an object"),
            ({"default": "0ff"}, "bytes", "hex", "even number"),
            ({"default": "!!"}, "bytes", "base64", "invalid base64"),
            ({"default": [None]}, "list", None, "representable"),
            ({"default": {"": 1}}, "map", None, "nonempty"),
            ({"default": 1.5}, "integer", None, "must be an integer"),
            ({"default": 1 << 64}, "integer", None, "signed 64-bit"),
        ]:
            with self.subTest(item=item):
                with self.assertRaisesRegex(MutationError, message):
                    validate_default(item, kind, encoding)


if __name__ == "__main__":
    unittest.main()

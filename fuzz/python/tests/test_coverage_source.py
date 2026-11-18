import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from coverage_analysis import source


class SourceRangeTests(unittest.TestCase):
    def test_c_boundaries_and_cache_invalidation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.c"
            path.write_text('int foo(int x) {\nconst char *s = "}";\n'
                            'if (x) x++;\n/* } */\nreturn x;\n}\n')
            branches = [SimpleNamespace(line=3, end_line=3)]
            self.assertEqual(source.function_range(path, "test.c:foo", branches), (1, 6))
            self.assertEqual(source.function_range(path, "foo", branches), (1, 6))
            path.write_text('int foo(int x) {\nreturn x;\n}\n')
            self.assertEqual(source.function_range(path, "foo", []), (1, 3))
            with self.assertRaises(source.SourceError):
                source.function_range(path, "foo", [SimpleNamespace(line=10, end_line=10)])

    def test_cpp_overloads_use_branch_coordinates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.cpp"
            path.write_text('int foo(int x) { return x; }\n'
                            'double foo(double x) { if (x) return x; return 0; }\n')
            with self.assertRaises(source.SourceError):
                source.function_range(path, "foo", [])
            self.assertEqual(source.function_range(path, "_Z3food",
                             [SimpleNamespace(line=2, end_line=2)]), (2, 2))

    def test_missing_closing_brace_is_not_a_complete_function(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.c"
            path.write_text('int foo(int x) {\nreturn x;\n')
            with self.assertRaises(source.SourceError):
                source.function_range(path, "foo", [])


if __name__ == "__main__":
    unittest.main()

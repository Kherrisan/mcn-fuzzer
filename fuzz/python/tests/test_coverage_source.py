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

    def test_macro_branch_before_the_definition_still_resolves(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.c"
            path.write_text("#define GUARD(x) ((x) == 0 \\\n    && (x) != 1)\n"
                            "int foo(int x) {\nif (GUARD(x)) return 1;\nreturn 0;\n}\n")
            # The macro definition sits above the function, so coverage records
            # a branch at line 2 while the body starts at line 3.
            branches = [SimpleNamespace(line=2, end_line=2),
                        SimpleNamespace(line=4, end_line=4)]
            self.assertEqual(source.function_range(path, "foo", branches), (3, 6))

    def test_near_duplicate_definitions_use_branch_density(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.c"
            path.write_text("static int foo(int x) {\nreturn x;\n}\n"
                            "static int foo(int x) {\nif (x) x++;\nreturn x;\n}\n")
            branches = [SimpleNamespace(line=5, end_line=5),
                        SimpleNamespace(line=6, end_line=6)]
            self.assertEqual(source.function_range(path, "foo", branches), (4, 7))

    def test_no_branch_inside_any_definition_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.c"
            path.write_text("int foo(void) {\nreturn 0;\n}\n")
            with self.assertRaisesRegex(source.SourceError, "disagree"):
                source.function_range(path, "foo", [SimpleNamespace(line=40, end_line=40)])
    def test_macro_generated_symbol_says_it_has_no_definition(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.h"
            path.write_text("#define MAKE(x) int x(void) { return 0; }\nMAKE(generated_fn)\n")
            with self.assertRaisesRegex(source.SourceError, "macro-generated or"):
                source.function_range(path, "generated_fn", [])

    def test_missing_closing_brace_is_not_a_complete_function(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.c"
            path.write_text('int foo(int x) {\nreturn x;\n')
            with self.assertRaises(source.SourceError):
                source.function_range(path, "foo", [])


if __name__ == "__main__":
    unittest.main()

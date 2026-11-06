import unittest

from coverage_analysis.cfg import source_map


FILE = "/src/server.c"


class SourceMapTests(unittest.TestCase):
    def test_source_locations_are_scoped_by_function_and_block(self):
        ir = r'''define i32 @f() !dbg !1 {
entry:
  %cmp = icmp eq i32 1, 2, !dbg !3
  br i1 %cmp, label %a, label %b, !dbg !4
a:
  call void @g(), !dbg !4
  ret i32 0
b:
  ret i32 1
}
!1 = distinct !DISubprogram(name: "f", file: !2)
!2 = !DIFile(filename: "server.c", directory: "/src")
!3 = !DILocation(line: 7, column: 9, scope: !1)
!4 = !DILocation(line: 7, column: 2, scope: !1)
'''
        blocks = source_map(ir)["functions"]["f"]["blocks"]
        self.assertEqual(blocks["entry"]["condition"], {"file": FILE, "line": 7, "column": 9})
        self.assertEqual(blocks["a"]["calls"][0]["target"], "g")


if __name__ == "__main__":
    unittest.main()

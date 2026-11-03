import json
import os
import tempfile
import unittest

from codetree.core import ScanOptions, classify_lines, detect_language, scan, to_csv, to_json


def write(root, rel, text="", mode="w"):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, mode, **({"encoding": "utf-8"} if "b" not in mode else {})) as fh:
        fh.write(text)


class ClassifyTests(unittest.TestCase):
    def test_python(self):
        src = ["# c\n", "\n", "x = 1\n", '"""doc\n', "more\n", '"""\n', "y = 2  # t\n"]
        self.assertEqual(classify_lines(src, ("#",), (('"""', '"""'),)), (2, 4, 1))

    def test_c_block(self):
        src = ["/* a\n", " b */\n", "int x; /* open\n", "still */\n", "// z\n"]
        self.assertEqual(classify_lines(src, ("//",), (("/*", "*/"),)), (1, 4, 0))

    def test_language(self):
        self.assertEqual(detect_language("a.PY"), "Python")
        self.assertEqual(detect_language("Dockerfile"), "Dockerfile")
        self.assertEqual(detect_language("x.unknown"), "Other")


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        write(self.root, "a.py", "x = 1\n# c\n\n")
        write(self.root, "sub/b.js", "let a;\n")
        write(self.root, "node_modules/pkg/i.js", "ignored\n")
        write(self.root, ".hidden/h.py", "hidden\n")
        write(self.root, ".secret.py", "hidden\n")
        write(self.root, "bin.dat", b"\x00\x01\x02", "wb")
        write(self.root, "latin.txt", "caf\xe9\n".encode("latin-1"), "wb")

    def tearDown(self):
        self.tmp.cleanup()

    def test_basic(self):
        r = scan(self.root)
        names = sorted(f.name for f in r.files)
        self.assertEqual(names, ["a.py", "b.js", "latin.txt"])  # latin-1 text still counted
        self.assertEqual(r.skipped_binary, 1)
        self.assertEqual((r.root.code, r.root.comment, r.root.blank), (3, 1, 1))
        self.assertEqual(r.total_files, 3)

    def test_current_dir_root_files_counted(self):
        old = os.getcwd()
        os.chdir(self.root)
        try:
            self.assertEqual(scan(".").total_files, 3)
        finally:
            os.chdir(old)

    def test_options(self):
        self.assertEqual(scan(self.root, ScanOptions(include_hidden=True)).total_files, 5)
        self.assertEqual(scan(self.root, ScanOptions(use_default_ignores=False)).total_files, 4)
        self.assertEqual(scan(self.root, ScanOptions(extensions=["py"])).total_files, 1)
        self.assertEqual(scan(self.root, ScanOptions(ignore=["sub"])).total_files, 2)

    def test_gitignore(self):
        write(self.root, ".gitignore", "*.js\n")
        self.assertEqual(scan(self.root).total_files, 2)

    def test_exports(self):
        r = scan(self.root)
        self.assertEqual(json.loads(to_json(r))["summary"]["files"], 3)
        self.assertEqual(len(to_csv(r).strip().splitlines()), 4)

    def test_cancel(self):
        self.assertTrue(scan(self.root, cancel=lambda: True).cancelled)


if __name__ == "__main__":
    unittest.main()

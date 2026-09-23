"""Analyzer contract tests; real compiler invocation is an explicit CLI action."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import static_analysis


class StaticAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "controller.c").write_text("int main(void) { return 0; }\n")
        self.root_patch = patch.object(static_analysis, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def test_missing_analyzer_is_not_zero_bugs_detected(self):
        with patch.object(static_analysis, "_select_tool", return_value=(None, None)), \
                patch.object(static_analysis, "_run") as execute:
            result = static_analysis.analyze_controller()
        execute.assert_not_called()
        self.assertEqual(result["status"], "not_run")
        self.assertIsNone(result["detected"])
        self.assertTrue((self.root / ".build/static-analysis.log").exists())

    def test_warning_count_does_not_become_detection_count(self):
        diagnostic = "../controller.c:15:4: warning: dereference of NULL [-Wanalyzer-null-dereference]\n"
        with patch.object(static_analysis, "_select_tool", return_value=("GCC -fanalyzer", "/usr/bin/gcc")), \
                patch.object(static_analysis, "_run", return_value=(0, diagnostic, False)) as execute:
            result = static_analysis.analyze_controller()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["diagnostic_count"], 1)
        self.assertEqual(result["diagnostics"][0]["code"], "-Wanalyzer-null-dereference")
        self.assertIsNone(result["detected"])
        self.assertEqual(execute.call_args.args[1], self.root / ".build")
        self.assertEqual((self.root / ".build/static-analysis.log").read_text(), diagnostic)
        self.assertEqual(result["command"][-2:], ["-o", "static-analysis.o"])

    def test_msvc_relative_paths_and_environment(self):
        diagnostic = "..\\controller.c(20): warning C6011: Dereferencing NULL pointer 'p'.\n"
        with patch.dict("os.environ", {"CL": "/FeC:\\bad.exe", "_CL_": "/FoC:\\bad.obj"}), \
                patch.object(static_analysis, "_select_tool", return_value=("MSVC /analyze", "C:\\Program Files\\VC\\vcvars64.bat")), \
                patch.object(static_analysis, "_run", return_value=(0, diagnostic, False)) as execute:
            result = static_analysis.analyze_controller()
        batch = (self.root / ".build/static-analysis.cmd").read_text()
        self.assertIn('"..\\controller.c"', batch)
        self.assertNotIn(str(self.root), batch)
        self.assertIn('/analyze:log "static-analysis.xml"', batch)
        self.assertNotIn("CL", execute.call_args.args[2])
        self.assertNotIn("_CL_", execute.call_args.args[2])
        self.assertEqual(result["diagnostics"][0]["code"], "C6011")
        self.assertEqual(result["diagnostics"][0]["line"], 20)

    def test_timeout_and_compile_failure_are_errors(self):
        for reply in ((1, "cc: error: unsupported option -fanalyzer\n", False),
                      (-9, "Analyzer timeout exceeded.\n", True)):
            with self.subTest(reply=reply), \
                    patch.object(static_analysis, "_select_tool", return_value=("GCC -fanalyzer", "gcc")), \
                    patch.object(static_analysis, "_run", return_value=reply):
                result = static_analysis.analyze_controller()
            self.assertEqual(result["status"], "error")
            self.assertIsNone(result["detected"])
            json.dumps(result, allow_nan=False)

    def test_clang_notes_do_not_inflate_diagnostic_count(self):
        output = ("../controller.c:9:7: warning: bad access [core.NullDereference]\n"
                  "../controller.c:8:3: note: assuming pointer is NULL\n")
        result = static_analysis._parse_diagnostics(output)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["code"], "core.NullDereference")


if __name__ == "__main__":
    unittest.main()

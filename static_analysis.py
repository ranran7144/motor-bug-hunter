"""Optional local compiler analysis; diagnostics are not planted-bug detections.

References:
https://learn.microsoft.com/en-us/cpp/build/reference/analyze-code-analysis
https://gcc.gnu.org/onlinedocs/gcc/Static-Analyzer-Options.html
https://clang.llvm.org/docs/ClangStaticAnalyzer.html
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parent
TIMEOUT_SECONDS = 120
LIMITATION = (
    "The analyzer examines one controller translation unit containing all runtime-selected "
    "mutants. Diagnostics have not been mapped to activated bug types. Warning count is "
    "not a detection rate; zero warnings does not establish correctness."
)


def _select_tool():
    if os.name == "nt":
        from simulation import _find_msvc
        vcvars = _find_msvc()
        if vcvars:
            return "MSVC /analyze", str(vcvars)
    gcc = shutil.which("gcc")
    if gcc:
        return "GCC -fanalyzer", gcc
    clang = shutil.which("clang")
    if clang:
        return "Clang --analyze", clang
    return None, None


def _parse_diagnostics(output):
    diagnostics = []
    msvc = re.compile(
        r"^(?P<file>.+?)\((?P<line>\d+)(?:,(?P<column>\d+))?\)\s*:\s*"
        r"(?P<severity>warning|fatal error|error)\s+(?P<code>[A-Z]\d+)\s*:\s*(?P<message>.*)$"
    )
    unix = re.compile(
        r"^(?P<file>.+?):(?P<line>\d+):(?:(?P<column>\d+):)?\s*"
        r"(?P<severity>warning|fatal error|error):\s*(?P<message>.*)$"
    )
    generic = re.compile(
        r"^.*?\b(?P<severity>warning|fatal error|error)\b\s*(?P<code>[A-Z]\d+)?\s*:\s*(?P<message>.*)$"
    )
    for raw in output.splitlines():
        match = msvc.match(raw.strip()) or unix.match(raw.strip()) or generic.match(raw.strip())
        if not match:
            continue
        record = match.groupdict()
        code = record.get("code")
        if not code:
            suffix = re.search(r"\[(-W[^\]]+|[a-zA-Z][\w.]+)\]\s*$", record["message"])
            code = suffix.group(1) if suffix else None
        diagnostics.append({
            "severity": record["severity"], "file": record.get("file"),
            "line": int(record["line"]) if record.get("line") else None,
            "column": int(record["column"]) if record.get("column") else None,
            "code": code, "message": record["message"], "raw": raw,
        })
    return diagnostics


def _run(command, directory, environment):
    """Bound analysis time and clean up its own compiler subprocess tree."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process = subprocess.Popen(command, cwd=directory, env=environment,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, errors="replace", creationflags=flags,
                               start_new_session=os.name != "nt")
    try:
        output, _ = process.communicate(timeout=TIMEOUT_SECONDS)
        return process.returncode, output, False
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            try:
                subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=15, creationflags=flags, check=False)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        else:
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            output, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            # Do not block forever on an inherited pipe in an orphaned child.
            process.stdout.close()
            process.wait(timeout=10)
            output = "Compiler output unavailable after timeout."
        return process.returncode, output + "\nAnalyzer timeout exceeded.\n", True


def analyze_controller() -> dict:
    """Run an installed analyzer, save .build/static-analysis.log, return facts.

    No install or network operation. `detected` is always None because warnings
    have not been adjudicated against the 30 mutation identities.
    """
    build_dir = ROOT / ".build"
    build_dir.mkdir(exist_ok=True)
    source = ROOT / "controller.c"
    log_path = build_dir / "static-analysis.log"
    result = {"status": "not_run", "tool": None, "diagnostics": [],
              "diagnostic_count": 0, "detected": None, "output": "",
              "command": None, "cwd": str(build_dir), "returncode": None,
              "log_path": str(log_path), "elapsed_ms": None,
              "source_sha256": None, "timeout_seconds": TIMEOUT_SECONDS,
              "created_at": datetime.now(timezone.utc).isoformat(),
              "limitation": LIMITATION}
    started = time.monotonic()
    try:
        result["source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        tool, executable = _select_tool()
        result["tool"] = tool
        environment = os.environ.copy()
        environment.update({"VSLANG": "1033", "LC_ALL": "C"})
        # Remove optional compiler argument injection; all requested flags below
        # are explicit and all generated output paths are within .build.
        for variable in ("CL", "_CL_"):
            environment.pop(variable, None)
        if tool == "MSVC /analyze":
            if any(char in executable for char in '%!"\r\n'):
                raise ValueError("Unsupported characters in Visual Studio setup path")
            batch = build_dir / "static-analysis.cmd"
            compiler_command = (
                'cl /nologo /W4 /analyze /analyze:only /analyze:WX- /c /TC '
                '/analyze:log "static-analysis.xml" /Fo:"static-analysis.obj" "..\\controller.c"'
            )
            batch.write_text('@echo off\ncall "' + executable + '" >nul\n'
                             'if errorlevel 1 exit /b %errorlevel%\n' + compiler_command + '\n',
                             encoding="utf-8")
            command = ["cmd.exe", "/d", "/c", "static-analysis.cmd"]
            result["compiler_command"] = compiler_command
            result["vcvars"] = executable
        elif tool == "GCC -fanalyzer":
            command = [executable, "-std=c99", "-Wall", "-Wextra", "-fanalyzer",
                       "-fdiagnostics-color=never", "-c", "../controller.c", "-o", "static-analysis.o"]
        elif tool == "Clang --analyze":
            command = [executable, "--analyze", "-std=c99", "-Wall", "-Wextra",
                       "-fno-color-diagnostics", "-Xanalyzer", "-analyzer-output=text",
                       "../controller.c", "-o", "static-analysis.plist"]
        else:
            command = None
            result["output"] = "No installed MSVC, GCC or Clang analyzer was found. No installation was attempted.\n"
        if command:
            result["command"] = command
            code, output, timed_out = _run(command, build_dir, environment)
            result.update(returncode=code, output=output, timed_out=timed_out,
                          status="completed" if code == 0 and not timed_out else "error")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result.update(status="error", output=f"Analyzer could not complete: {error}\n")
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
    result["diagnostics"] = _parse_diagnostics(result["output"])
    result["diagnostic_count"] = len(result["diagnostics"])
    log_path.write_text(result["output"], encoding="utf-8")
    return result


if __name__ == "__main__":
    print(json.dumps(analyze_controller(), ensure_ascii=False, indent=2))

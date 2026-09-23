"""Compile the real C controller and run reproducible record/replay SIL cases.

Public API: run_suite(seed=20260920) -> {cases, build, requirements}.
No network access, package install, or generated Python-side mutant outputs.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parent
STATES = ("OFF", "STARTING", "RUNNING", "STOPPING", "FAULT", "EMERGENCY_STOP")
INPUT_FIELDS = (
    "start_switch", "stop_switch", "emergency_stop", "motor_current", "motor_speed",
    "encoder_position", "limit_switch", "temperature", "communication_alive",
    "reset_fault", "sensor_valid", "target_pwm", "watchdog_due", "isr_window",
)
OUTPUT_FIELDS = (
    "cycle", "state", "pwm", "brake", "estimated_speed", "start_elapsed",
    "stop_elapsed", "stall_count", "start_count", "fault_code",
)


class CInput(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int32) for name in INPUT_FIELDS]


class COutput(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32 if name in {
        "cycle", "start_elapsed", "stop_elapsed", "stall_count", "start_count"
    } else ctypes.c_int32) for name in OUTPUT_FIELDS]


def _find_msvc() -> Path | None:
    candidates: list[Path] = []
    for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
        base = Path(os.environ.get(env_name, "C:/Program Files (x86)"))
        vswhere = base / "Microsoft Visual Studio/Installer/vswhere.exe"
        if vswhere.exists():
            result = subprocess.run(
                [str(vswhere), "-all", "-products", "*", "-requires",
                 "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"],
                capture_output=True, text=True, check=False,
            )
            candidates.extend(Path(p.strip()) / "VC/Auxiliary/Build/vcvars64.bat"
                              for p in result.stdout.splitlines() if p.strip())
        candidates.extend(base.glob("Microsoft Visual Studio/*/*/VC/Auxiliary/Build/vcvars64.bat"))
    for candidate in candidates:
        vc_root = candidate.parents[2]
        if candidate.exists() and any(vc_root.glob("Tools/MSVC/*/bin/Hostx64/x64/cl.exe")):
            return candidate
    return None


def build_controller() -> tuple[ctypes.CDLL, dict[str, Any]]:
    source = ROOT / "controller.c"
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    build_dir = ROOT / ".build"
    build_dir.mkdir(exist_ok=True)
    suffix = ".dll" if os.name == "nt" else (".dylib" if sys.platform == "darwin" else ".so")
    library = build_dir / ("motor_" + source_hash[:16] + suffix)
    gcc = shutil.which("gcc") or shutil.which("clang")
    metadata_file = library.with_suffix(".json")
    metadata: dict[str, Any]
    if library.exists() and metadata_file.exists():
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    else:
        if gcc:
            command = [gcc, "-std=c99", "-O2", "-Wall", "-Wextra", "-Werror", "-shared"]
            if os.name != "nt":
                command.append("-fPIC")
            command += [str(source), "-o", str(library)]
            result = subprocess.run(command, cwd=build_dir, capture_output=True, text=True)
            metadata = {"compiler": gcc, "command": command}
        else:
            vcvars = _find_msvc() if os.name == "nt" else None
            if not vcvars:
                raise RuntimeError("No C compiler found. Install GCC/Clang or Visual Studio C++ Build Tools; no download was attempted.")
            # A fixed batch file avoids Powershell/cmd quoting ambiguity. Only known
            # local paths are inserted; all compilation output stays in .build.
            batch = build_dir / "compile_controller.cmd"
            batch.write_text(
                '@echo off\r\ncall "' + str(vcvars) + '" >nul\r\n'
                'if errorlevel 1 exit /b %errorlevel%\r\n'
                'cl /nologo /W4 /WX /O2 /LD "' + str(source) + '" /Fe:"' + str(library) + '"\r\n',
                encoding="utf-8",
            )
            result = subprocess.run(["cmd.exe", "/d", "/c", str(batch)], cwd=build_dir,
                                    capture_output=True, text=True, errors="replace")
            metadata = {"compiler": "MSVC", "vcvars": str(vcvars), "command": "cl /W4 /WX /O2 /LD controller.c"}
        if result.returncode:
            raise RuntimeError("C compilation failed:\n" + result.stdout + result.stderr)
        metadata.update({"source_sha256": source_hash, "library": str(library),
                         "compiler_output": (result.stdout + result.stderr).strip()})
        metadata_file.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    dll = ctypes.CDLL(str(library))
    dll.motor_create.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32]
    dll.motor_create.restype = ctypes.c_void_p
    dll.motor_destroy.argtypes = [ctypes.c_void_p]
    dll.motor_step.argtypes = [ctypes.c_void_p, ctypes.POINTER(CInput), ctypes.POINTER(COutput)]
    if dll.motor_input_size() != ctypes.sizeof(CInput) or dll.motor_output_size() != ctypes.sizeof(COutput):
        raise RuntimeError("C/Python ABI sizes do not agree")
    return dll, metadata


class Motor:
    def __init__(self, dll: ctypes.CDLL, bug: int, initial_tick: int = 0, initial_start_count: int = 0):
        self.dll = dll
        self.handle = dll.motor_create(bug, initial_tick, initial_start_count)
        if not self.handle:
            raise MemoryError("motor_create failed")

    def step(self, values: dict[str, int]) -> dict[str, Any]:
        inputs = CInput(*(int(values[name]) for name in INPUT_FIELDS))
        output = COutput()
        self.dll.motor_step(self.handle, ctypes.byref(inputs), ctypes.byref(output))
        row = {**values, **{name: int(getattr(output, name)) for name in OUTPUT_FIELDS}}
        row["state"] = STATES[row["state"]]
        return row

    def close(self) -> None:
        if self.handle:
            self.dll.motor_destroy(self.handle)
            self.handle = None


class Plant:
    """Simple first-order inertial plant; PWM=0 still permits coasting."""
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.speed = 0.0
        self.position = 65000.0
        self.current = 0.0
        self.temperature = 25.0

    def sensors(self) -> dict[str, int]:
        return {"motor_speed": round(self.speed), "motor_current": round(self.current),
                "encoder_position": int(self.position) % 65536,
                "temperature": round(self.temperature)}

    def advance(self, pwm: int) -> None:
        drive = max(0, min(100, pwm))
        self.speed += (drive * 1.1 - self.speed) * 0.22
        self.speed = max(0.0, self.speed + self.rng.uniform(-0.08, 0.08))
        self.position += self.speed * 0.6
        self.current = drive * 0.45 + abs(drive * 1.1 - self.speed) * 0.18
        self.temperature += (25 + drive * 0.09 - self.temperature) * 0.01


def _scenario_input(scenario: str, i: int, plant: Plant) -> dict[str, int]:
    data = {
        "start_switch": int(5 <= i < 12), "stop_switch": int(i == 75),
        "emergency_stop": 0, "limit_switch": 0, "communication_alive": 1,
        "reset_fault": 0, "sensor_valid": 1, "target_pwm": 60,
        "watchdog_due": int(i % 20 == 0), "isr_window": 0, **plant.sensors(),
    }
    if scenario in {"estop_edge", "isr_snapshot", "watchdog_order"}:
        data["emergency_stop"] = int(i >= 40)
        data["isr_window"] = int(scenario == "isr_snapshot" and i == 40)
    elif scenario == "current_boundary":
        if i >= 40: data["motor_current"] = 120
    elif scenario in {"counter_wrap", "startup_timeout"}:
        data["motor_speed"] = 0
        data["encoder_position"] = 65000
    elif scenario in {"stop_timer", "brake_release"}:
        data["stop_switch"] = int(i == 40)
        if i >= 40: data["motor_speed"] = 0
    elif scenario == "startup_or":
        if i < 15: data["motor_speed"] = 0
    elif scenario == "sensor_invalid":
        if i >= 40: data["sensor_valid"] = 0
    elif scenario in {"comm_edge", "cached_comm"}:
        if i >= 40: data["communication_alive"] = 0
    elif scenario == "rapid_restart":
        data["stop_switch"] = int(i == 40)
        data["start_switch"] = int(5 <= i < 12 or i == 41)
        if i >= 40: data["motor_speed"] = 0
    elif scenario == "encoder_wrap":
        data["encoder_position"] = (64736 + i * 20) % 65536
    elif scenario == "reset_inhibit":
        data["communication_alive"] = int(i != 35)
        data["reset_fault"] = int(i == 42)
        data["start_switch"] = int(5 <= i < 12 or 46 <= i < 55)
        if 35 <= i <= 45: data["motor_speed"] = 0
    elif scenario == "power_on":
        pass
    elif scenario == "thousandth_start":
        pass
    elif scenario == "stop_priority":
        data["stop_switch"] = int(i == 5 or i == 75)
    elif scenario == "limit_running":
        if i >= 40: data["limit_switch"] = 1
    elif scenario == "temperature_boundary":
        if i >= 40: data["temperature"] = 90
    elif scenario == "stall_recovery":
        if i in {35, 36, 38, 39, 41}: data["motor_speed"] = 0
    elif scenario == "current_narrowing":
        if i >= 40: data["motor_current"] = 256
    elif scenario == "pwm_saturation":
        data["target_pwm"] = 140
    elif scenario == "held_start":
        data["start_switch"] = int(i >= 5)
        data["stop_switch"] = int(i == 40)
        if i >= 40: data["motor_speed"] = 0
    elif scenario == "comm_recovery":
        data["communication_alive"] = int(not 35 <= i < 40)
    elif scenario == "estop_reset":
        data["emergency_stop"] = int(i >= 35)
        data["reset_fault"] = int(i == 42)
        if i >= 40: data["motor_speed"] = 0
    elif scenario == "reset_moving":
        data["communication_alive"] = int(i != 35)
        data["reset_fault"] = int(i == 40)
        if 35 <= i <= 45: data["motor_speed"] = 40
    elif scenario == "pwm_underflow":
        data["target_pwm"] = 5 if i < 35 else 0
        if i >= 10: data["motor_speed"] = 40
    elif scenario == "pwm_slew":
        pass
    elif scenario == "reset_start_edge":
        data["start_switch"] = int(i >= 5)
        data["communication_alive"] = int(i != 35)
        data["reset_fault"] = int(i == 42)
        if i >= 35: data["motor_speed"] = 0
    return data


def _record(dll: ctypes.CDLL, mutation: dict[str, Any], seed: int) -> tuple[list[dict], list[dict]]:
    plant = Plant(seed)
    controller = Motor(dll, 0, mutation.get("initial_tick", 0), mutation.get("initial_start_count", 0))
    inputs, outputs = [], []
    try:
        for i in range(100):
            snapshot = _scenario_input(mutation["scenario"], i, plant)
            output = controller.step(snapshot)
            inputs.append(snapshot)
            outputs.append(output)
            plant.advance(output["pwm"])
    finally:
        controller.close()
    return inputs, outputs


def _replay(dll: ctypes.CDLL, mutation: dict[str, Any], inputs: list[dict]) -> list[dict]:
    controller = Motor(dll, mutation["id"], mutation.get("initial_tick", 0), mutation.get("initial_start_count", 0))
    try:
        return [controller.step(row) for row in inputs]
    finally:
        controller.close()


def conventional_check(trace: list[dict]) -> dict[str, Any]:
    """Requirements-derived checks; never receives a mutation ID/reference trace.

    This initial baseline checks output interlocks, ranges, slew, phase timing,
    startup qualification and fault latching. It does not implement a duplicate
    full controller, e.g. the entire start-edge acceptance/inhibit protocol.
    """
    violations: list[dict[str, Any]] = []
    start_tick: int | None = None
    stop_tick: int | None = None
    previous: dict | None = None

    def require(ok: bool, row: dict, rule: str) -> None:
        if not ok:
            violations.append({"cycle": row["cycle"], "rule": rule})

    for row in trace:
        state, pwm = row["state"], row["pwm"]
        require(0 <= pwm <= 100, row, "PWM_RANGE")
        require(state not in {"OFF", "STOPPING", "FAULT", "EMERGENCY_STOP"} or pwm == 0,
                row, "INACTIVE_PWM_ZERO")
        require(not row["emergency_stop"] or (state == "EMERGENCY_STOP" and pwm == 0),
                row, "ESTOP_SAME_TICK")
        unsafe = (not row["sensor_valid"] or not row["communication_alive"] or
                  row["motor_current"] >= 120 or row["temperature"] >= 90 or row["limit_switch"])
        require(not unsafe or (pwm == 0 and state in {"FAULT", "EMERGENCY_STOP"}), row, "SAFETY_INTERLOCK")
        require(state != "OFF" or row["brake"] == 0, row, "OFF_BRAKE_RELEASED")
        require(not row["stop_switch"] or pwm == 0, row, "STOP_PRIORITY_PWM")
        if state == "STARTING" and (previous is None or previous["state"] != "STARTING"):
            start_tick = row["cycle"]
        if state == "STOPPING" and (previous is None or previous["state"] != "STOPPING"):
            stop_tick = row["cycle"]
        if state == "STARTING" and start_tick is not None:
            require(row["cycle"] - start_tick < 20, row, "START_TIMEOUT")
        if previous is not None:
            if state in {"STARTING", "RUNNING"}:
                require(abs(pwm - previous["pwm"]) <= 10, row, "PWM_SLEW")
            if previous["state"] == "STARTING" and state == "RUNNING":
                require(start_tick is not None and row["cycle"] - start_tick >= 5 and row["motor_speed"] >= 30,
                        row, "START_QUALIFICATION")
            if previous["state"] == "STOPPING" and state not in {"STOPPING", "FAULT", "EMERGENCY_STOP"}:
                require(state == "OFF" and stop_tick is not None and row["cycle"] - stop_tick >= 5 and row["motor_speed"] <= 5,
                        row, "STOP_COMPLETION")
            if previous["state"] in {"FAULT", "EMERGENCY_STOP"} and state not in {"FAULT", "EMERGENCY_STOP"}:
                require(row["reset_fault"] and row["motor_speed"] <= 5 and not unsafe and not row["emergency_stop"] and state == "OFF",
                        row, "LATCH_RESET_GUARD")
            wrapped_delta = (row["encoder_position"] - previous["encoder_position"] + 32768) % 65536 - 32768
            require(row["estimated_speed"] == wrapped_delta, row, "ENCODER_WRAP")
        previous = row
    return {"detected": bool(violations), "violations": violations}


def activation_witness(mutation: dict[str, Any], trace: list[dict], reference: list[dict]) -> dict[str, Any]:
    """Check the authored defect's observable symptom independently of C code.

    This is the injection experiment's ground-truth oracle, not a detector being
    scored. It knows the scenario, compares paired runs, and is never exposed
    to Jev or to conventional_check. A selected mutation alone is insufficient.
    Every returned witness must satisfy an explicit requirement-violation test.
    """
    scenario = mutation["scenario"]
    for i, (row, good) in enumerate(zip(trace, reference)):
        previous = trace[i - 1] if i else None
        differs = any(row[key] != good[key] for key in ("state", "pwm", "brake", "estimated_speed"))
        if not differs:
            continue
        active = row["state"] in {"STARTING", "RUNNING"}
        tests = {
            "estop_edge": row["emergency_stop"] and row["pwm"] > 0,
            "current_boundary": row["motor_current"] == 120 and active,
            "counter_wrap": row["state"] == "FAULT" and good["state"] == "STARTING" and row["cycle"] >= 65536,
            "stop_timer": row["state"] == "OFF" and good["state"] == "STOPPING" and good["stop_elapsed"] < 5,
            "startup_or": row["state"] == "RUNNING" and row["motor_speed"] < 30,
            "sensor_invalid": not row["sensor_valid"] and active,
            "comm_edge": not row["communication_alive"] and row["pwm"] > 0,
            "rapid_restart": row["state"] == "STARTING" and good["state"] == "STOPPING" and good["stop_elapsed"] < 5,
            "encoder_wrap": previous is not None and row["estimated_speed"] !=
                (row["encoder_position"] - previous["encoder_position"] + 32768) % 65536 - 32768,
            "reset_inhibit": row["state"] == "OFF" and good["state"] == "STARTING" and row["start_switch"],
            "isr_snapshot": row["isr_window"] and row["emergency_stop"] and row["pwm"] > 0,
            "cached_comm": not row["communication_alive"] and active and row["pwm"] > 0,
            "power_on": i == 0 and row["state"] == "OFF" and row["pwm"] > 0,
            "watchdog_order": row["watchdog_due"] and row["emergency_stop"] and row["pwm"] > 0,
            "thousandth_start": row["start_count"] == 1000 and row["state"] == "OFF" and good["state"] == "STARTING",
            "stop_priority": row["start_switch"] and row["stop_switch"] and row["pwm"] > 0,
            "limit_running": row["limit_switch"] and row["state"] == "RUNNING" and row["pwm"] > 0,
            "temperature_boundary": row["temperature"] == 90 and active,
            "startup_timeout": row["state"] == "STARTING" and row["start_elapsed"] == 20,
            "stall_recovery": row["state"] == "FAULT" and row["fault_code"] == 8 and good["state"] == "RUNNING" and
                any(sample["motor_speed"] >= 5 for sample in trace[max(0, i - 4):i + 1]),
            "current_narrowing": row["motor_current"] == 256 and active,
            "pwm_saturation": row["pwm"] > 100,
            "brake_release": row["state"] == "OFF" and row["brake"] != 0,
            "held_start": previous is not None and previous["start_switch"] and row["start_switch"] and
                row["state"] == "STARTING" and good["state"] == "OFF",
            "comm_recovery": previous is not None and previous["state"] == "FAULT" and active and not row["reset_fault"],
            "estop_reset": row["emergency_stop"] and row["reset_fault"] and row["state"] != "EMERGENCY_STOP",
            "reset_moving": row["reset_fault"] and row["motor_speed"] > 5 and row["state"] == "OFF",
            "pwm_underflow": row["pwm"] == 251 and row["target_pwm"] == 0,
            "pwm_slew": previous is not None and active and abs(row["pwm"] - previous["pwm"]) > 10,
            "reset_start_edge": previous is not None and previous["reset_fault"] and previous["start_switch"] and
                row["start_switch"] and row["state"] == "STARTING" and good["state"] == "OFF",
        }
        if tests[scenario]:
            return {"cycle": row["cycle"], "requirement": mutation["expected_violation"],
                    "observed": {key: row[key] for key in ("state", "pwm", "brake", "estimated_speed")},
                    "reference": {key: good[key] for key in ("state", "pwm", "brake", "estimated_speed")}}
    raise AssertionError(f"Mutation {mutation['id']} diverged without its expected violation: {scenario}")


def run_suite(seed: int = 20260920) -> dict[str, Any]:
    dll, build = build_controller()
    requirements = json.loads((ROOT / "requirements.json").read_text(encoding="utf-8"))
    mutations = json.loads((ROOT / "mutations.json").read_text(encoding="utf-8"))["mutations"]
    cases: list[dict[str, Any]] = []
    observable = ("state", "pwm", "brake", "estimated_speed")
    for mutation in mutations:
        scenario_seed = seed + mutation["id"] * 7919
        inputs, reference = _record(dll, mutation, scenario_seed)
        fixed_checks = conventional_check(reference)
        if fixed_checks["detected"]:
            raise AssertionError(f"Reference violates requirements for scenario {mutation['scenario']}: {fixed_checks['violations'][:3]}")
        mutated = _replay(dll, mutation, inputs)
        divergences = [a["cycle"] for a, b in zip(mutated, reference)
                       if any(a[key] != b[key] for key in observable)]
        if not divergences:
            raise AssertionError(f"Mutation {mutation['id']} did not activate")
        witness = activation_witness(mutation, mutated, reference)
        context = (
            "100 consecutive post-step snapshots; one step is 10 ms. Initial state OFF, PWM=0, brake=0. "
            "Sensors are a reference-driven inertial plant recording replayed unchanged; forced boundary/safety inputs are intentional test stimuli. "
            f"Initial tick={mutation.get('initial_tick', 0)}; initial start-attempt counter={mutation.get('initial_start_count', 0)}. "
            "Counter fixtures seed the boundary directly. Motor coasting after PWM=0 is normal."
        )
        for bug_id, trace, truth in ((0, reference, False), (mutation["id"], mutated, True)):
            opaque = hashlib.sha256(f"{seed}|{mutation['scenario']}|{bug_id}".encode()).hexdigest()[:16]
            cases.append({
                "case_id": "case_" + opaque,
                "bug_id": bug_id,
                "title": mutation["title"] if truth else "Matched reference: " + mutation["scenario"],
                "category": mutation["category"] if truth else "OTHER",
                "severity": mutation["severity"] if truth else "MINOR",
                "truth": truth,
                "trace": trace,
                "reference_trace": reference,
                "divergence_cycles": divergences if truth else [],
                "conventional": conventional_check(trace),
                "activation_verified": bool(divergences) if truth else True,
                "public_context": context,
                "scenario": mutation["scenario"],
                "seed": scenario_seed,
                "ground_truth_basis": mutation["expected_violation"] if truth else "Reference controller and requirements checks agree.",
                "ground_truth_witness": witness if truth else None,
                "model_only": mutation.get("model_only", False),
            })
    random.Random(seed).shuffle(cases)
    return {"cases": cases, "build": build, "requirements": requirements, "seed": seed,
            "method": requirements["sil_method"]}


if __name__ == "__main__":
    suite = run_suite()
    faulty = [case for case in suite["cases"] if case["truth"]]
    print(json.dumps({"cases": len(suite["cases"]), "activated": sum(c["activation_verified"] for c in faulty),
                      "conventional_detected": sum(c["conventional"]["detected"] for c in faulty),
                      "compiler": suite["build"]["compiler"]}, indent=2))

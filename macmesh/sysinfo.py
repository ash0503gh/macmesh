"""What this machine is and how much work it should take on."""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
from functools import lru_cache

IS_MAC = platform.system() == "Darwin"


def _run(*argv: str, timeout: float = 10) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _sysctl_int(name: str) -> int:
    out = _run("sysctl", "-n", name)
    return int(out) if out.isdigit() else 0


@lru_cache(maxsize=1)
def hardware() -> dict:
    """Static hardware facts. Cached: system_profiler takes about a second."""
    info = {
        "os": platform.system(),
        "arch": platform.machine(),
        "model": platform.node(),
        "chip": platform.processor() or platform.machine(),
        "perf_cores": os.cpu_count() or 1,
        "eff_cores": 0,
        "gpu_cores": 0,
        "memory_gb": 0,
        "fanless": False,
    }
    if IS_MAC:
        info["chip"] = _run("sysctl", "-n", "machdep.cpu.brand_string") or info["chip"]
        info["perf_cores"] = _sysctl_int("hw.perflevel0.physicalcpu") or info["perf_cores"]
        info["eff_cores"] = _sysctl_int("hw.perflevel1.physicalcpu")
        info["memory_gb"] = round(_sysctl_int("hw.memsize") / 2**30)
        try:
            hw = json.loads(_run("system_profiler", "SPHardwareDataType", "-json"))["SPHardwareDataType"][0]
            info["model"] = hw.get("machine_name", info["model"])
        except (ValueError, KeyError, IndexError):
            pass
        try:
            gpus = json.loads(_run("system_profiler", "SPDisplaysDataType", "-json"))["SPDisplaysDataType"]
            info["gpu_cores"] = sum(int(g.get("sppci_cores", 0) or 0) for g in gpus)
        except (ValueError, KeyError, TypeError):
            pass
        # Every MacBook Air since the M1 is fanless and throttles under sustained load.
        info["fanless"] = "MacBook Air" in info["model"]
    else:
        try:
            with open("/proc/meminfo") as f:
                kb = int(re.search(r"MemTotal:\s+(\d+)", f.read()).group(1))
            info["memory_gb"] = round(kb / 2**20)
        except (OSError, AttributeError):
            pass
    info["score"] = capacity_score(info)
    return info


def capacity_score(hw: dict) -> float:
    """Rough sustained-throughput estimate used before any real timings exist.

    The scheduler replaces it with measured speed as soon as tasks complete,
    so it only needs to be in the right ballpark.
    """
    score = hw["perf_cores"] + 0.35 * hw["eff_cores"] + 0.1 * hw["gpu_cores"]
    if hw["fanless"]:
        score *= 0.7
    return round(max(score, 1.0), 2)


def power() -> dict:
    """Battery state. On desktops (or Linux) reports AC with no battery."""
    if not IS_MAC:
        return {"on_battery": False, "battery_pct": None}
    out = _run("pmset", "-g", "batt")
    pct = re.search(r"(\d+)%", out)
    return {
        "on_battery": "Battery Power" in out,
        "battery_pct": int(pct.group(1)) if pct else None,
    }

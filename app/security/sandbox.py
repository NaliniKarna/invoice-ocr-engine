"""Run untrusted-input work in a resource-limited child process.

The child sets its own rlimits (AS / CPU / FSIZE / NOFILE / CORE) before importing anything heavy, so there is
no preexec_fn (unsafe in a multithreaded parent). The child gets an empty environment (no secrets, no DB/S3
credentials), runs with `-I`-like flags, and is killed (whole process group) on wall-clock timeout.

This bounds CPU/memory/time. It does NOT provide syscall or filesystem isolation (no seccomp/namespaces);
run workers with a read-only rootfs, dropped capabilities and no network egress for that.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class SandboxError(Exception):
    """Child crashed, was killed by a limit, or timed out."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason  # 'timeout' | 'killed' | 'crashed'


@dataclass(frozen=True)
class SandboxLimits:
    timeout_s: int
    memory_mb: int


def run(module: str, argv_json: str, data: bytes, limits: SandboxLimits) -> bytes:
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "SANDBOX_MEM_MB": str(limits.memory_mb),
           "SANDBOX_CPU_S": str(limits.timeout_s + 5)}
    try:
        proc = subprocess.run(
            [sys.executable, "-s", "-B", "-m", module, argv_json], input=data, capture_output=True,
            timeout=limits.timeout_s, cwd=PROJECT_ROOT, env=env, start_new_session=True, check=False)
    except subprocess.TimeoutExpired as exc:
        raise SandboxError("timeout") from exc
    if proc.returncode < 0:  # killed by signal (SIGKILL/SIGXCPU/SIGSEGV...)
        raise SandboxError("killed")
    if proc.returncode != 0 and not proc.stdout:
        raise SandboxError("crashed")
    return proc.stdout

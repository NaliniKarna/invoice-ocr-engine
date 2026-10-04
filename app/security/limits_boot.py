"""Imported FIRST by sandbox children: applies rlimits before any heavy import or parsing."""
import os
import resource

_mem = int(os.environ.get("SANDBOX_MEM_MB", "1024")) * 1024 * 1024
_cpu = int(os.environ.get("SANDBOX_CPU_S", "60"))
for _res, _val in ((resource.RLIMIT_AS, _mem), (resource.RLIMIT_CPU, _cpu), (resource.RLIMIT_FSIZE, 1024 * 1024),
                   (resource.RLIMIT_NOFILE, 64), (resource.RLIMIT_CORE, 0)):
    try:
        resource.setrlimit(_res, (_val, _val))
    except (ValueError, OSError):
        pass

import app.security.limits_boot  # noqa: F401
import json, os, resource
print(json.dumps({"env": sorted(os.environ), "as": resource.getrlimit(resource.RLIMIT_AS)[0], "core": resource.getrlimit(resource.RLIMIT_CORE)[0]}))

import app.security.limits_boot  # noqa: F401
x = bytearray(2 * 1024 * 1024 * 1024)   # far above the child's RLIMIT_AS
print("survived")

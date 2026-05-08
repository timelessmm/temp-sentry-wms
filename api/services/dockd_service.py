"""v1.9.0 dockd shared utilities.

The dockd surface has its own body-size cap (separate from the inbound
SENTRY_INBOUND_MAX_BODY_KB) because dockd POST bodies are tiny (a few
hundred bytes for a ship or void) where inbound bodies can be large
canonical-shape JSON. A 64 KB default refuses abusive clients early
without constraining legitimate traffic.

Boot validation lives in app.create_app(); the helper here trusts the
boot guard and reads the env var on each request.
"""

import os


SENTRY_DOCKD_MAX_BODY_KB_DEFAULT = 64
SENTRY_DOCKD_MAX_BODY_KB_FLOOR = 16
SENTRY_DOCKD_MAX_BODY_KB_CEILING = 1024


def get_max_body_kb() -> int:
    """SENTRY_DOCKD_MAX_BODY_KB env var (16..1024; default 64).

    Boot validates the range in app.create_app(); this helper is the
    read-side path the handler uses on each request and trusts the boot
    guard rather than re-clamping silently. A typo'd value would have
    refused to boot the app, so on-the-request-path we just int() it.
    """
    return int(os.getenv("SENTRY_DOCKD_MAX_BODY_KB", str(SENTRY_DOCKD_MAX_BODY_KB_DEFAULT)))

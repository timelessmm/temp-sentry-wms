"""v1.10.0 POS shared utilities.

The POS surface has its own body-size cap (separate from the inbound
SENTRY_INBOUND_MAX_BODY_KB and the dockd SENTRY_DOCKD_MAX_BODY_KB)
because POS POST bodies sit between the two: a checkout body with
many cart lines + tender details is bigger than a dockd ship body
but smaller than an inbound canonical-shape upsert. The 256 KB
default refuses abusive clients early without constraining legitimate
multi-line counter sales.

Boot validation lives in app.create_app(); the helper here trusts
the boot guard and reads the env var on each request.
"""

import os


SENTRY_POS_MAX_BODY_KB_DEFAULT = 256
SENTRY_POS_MAX_BODY_KB_FLOOR = 16
SENTRY_POS_MAX_BODY_KB_CEILING = 4096


def get_max_body_kb() -> int:
    """SENTRY_POS_MAX_BODY_KB env var (16..4096; default 256).

    Boot validates the range in app.create_app(); this helper is the
    read-side path the handler uses on each request and trusts the
    boot guard rather than re-clamping silently. A typo'd value would
    have refused to boot the app, so on the request path we just
    int() it.
    """
    return int(
        os.getenv("SENTRY_POS_MAX_BODY_KB", str(SENTRY_POS_MAX_BODY_KB_DEFAULT))
    )

"""First-party official QQ bot adapter (tencent-connect/botpy).

Runs in-process next to the web runtime, but is isolated from g3ku core: it
only talks to the local External Agent API (``/api/v1``) over loopback with a
Bearer token, exactly like any third-party bridge. The QQ platform specifics
(event field names, intents, message posting) stay inside this package; g3ku
core and the generic ``/api/v1`` surface are untouched.
"""

from g3ku.qq_official.service import QqOfficialService

__all__ = ["QqOfficialService"]
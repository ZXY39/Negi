"""Heartbeat helpers for session-driven internal turns."""

__all__ = [
    "WebSessionHeartbeatService",
    "build_web_session_heartbeat",
    "start_web_session_heartbeat",
]


def __getattr__(name: str):
    if name == "WebSessionHeartbeatService":
        from g3ku.heartbeat.session_service import WebSessionHeartbeatService

        return WebSessionHeartbeatService
    if name == "build_web_session_heartbeat":
        from g3ku.heartbeat.bootstrap import build_web_session_heartbeat

        return build_web_session_heartbeat
    if name == "start_web_session_heartbeat":
        from g3ku.heartbeat.bootstrap import start_web_session_heartbeat

        return start_web_session_heartbeat
    raise AttributeError(name)

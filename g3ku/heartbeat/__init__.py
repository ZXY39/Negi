"""Heartbeat service for periodic agent wake-ups."""

__all__ = ["HeartbeatService", "WebSessionHeartbeatService"]


def __getattr__(name: str):
    if name == "HeartbeatService":
        from g3ku.heartbeat.service import HeartbeatService

        return HeartbeatService
    if name == "WebSessionHeartbeatService":
        from g3ku.heartbeat.session_service import WebSessionHeartbeatService

        return WebSessionHeartbeatService
    raise AttributeError(name)

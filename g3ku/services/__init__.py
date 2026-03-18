"""Service-layer re-exports for the converged runtime architecture."""

__all__ = [
    "CronService",
    "HeartbeatService",
    "SessionCommitService",
]


def __getattr__(name: str):
    if name == "CronService":
        from g3ku.services.cron import CronService

        return CronService
    if name == "HeartbeatService":
        from g3ku.services.heartbeat import HeartbeatService

        return HeartbeatService
    if name == "SessionCommitService":
        from g3ku.agent.session_commit import SessionCommitService

        return SessionCommitService
    raise AttributeError(name)

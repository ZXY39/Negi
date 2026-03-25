"""Service-layer re-exports for the converged runtime architecture."""

__all__ = [
    "CronService",
    "SessionCommitService",
]


def __getattr__(name: str):
    if name == "CronService":
        from g3ku.services.cron import CronService

        return CronService
    if name == "SessionCommitService":
        from g3ku.agent.session_commit import SessionCommitService

        return SessionCommitService
    raise AttributeError(name)

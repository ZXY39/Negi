from __future__ import annotations


class EscalationHelper:
    @staticmethod
    def unit_failure_text(*, project_title: str, role_title: str, error: str) -> str:
        return f'Project {project_title} has a failed unit: {role_title}. Error: {error}'

    @staticmethod
    def project_failed_text(*, project_title: str, error: str) -> str:
        return f'Project {project_title} failed. Error: {error}'

    @staticmethod
    def stage_rework_text(*, stage_title: str, error: str) -> str:
        return f'Stage {stage_title} requires rework because a work unit failed: {error}'

from __future__ import annotations

import asyncio

from g3ku.org_graph.errors import EngineeringFailureError, ModelChainUnavailableError, OrdinaryTaskFailureError, PermissionBlockedError
from g3ku.org_graph.execution.failure_reporter import FailureReporter
from g3ku.org_graph.protocol import now_iso


class ProjectRunner:
    def __init__(self, service):
        self._service = service
        self._failure_reporter = FailureReporter(service)

    async def run(self, project_id: str) -> None:
        project = self._service.get_project(project_id)
        if project is None or project.status in {"completed", "failed", "canceled", "archived"}:
            return
        try:
            root_unit = self._service.store.get_unit(project.root_unit_id)
            if root_unit is None:
                raise EngineeringFailureError(f"Missing execution root unit for {project.project_id}")
            root_checkpoint = self._service.checkpoint_store.get(root_unit.unit_id)
            root_has_progress = bool(root_checkpoint.get("stages")) or any(
                stage.unit_id == root_unit.unit_id for stage in self._service.list_stages(project.project_id)
            )
            if not root_has_progress:
                project = project.model_copy(
                    update={
                        "status": "planning",
                        "started_at": project.started_at or now_iso(),
                        "updated_at": now_iso(),
                        "finished_at": None,
                        "summary": "Execution root is planning the project",
                    }
                )
                self._service.store.upsert_project(project)
                await self._service.publish_summary(project)
                await self._service.emit_event(project=project, scope="project", event_name="project.planning_started", text="Project planning started")
            if project.status != "running":
                project = self._service.get_project(project_id) or project
                project = project.model_copy(
                    update={
                        "status": "running",
                        "started_at": project.started_at or now_iso(),
                        "updated_at": now_iso(),
                        "finished_at": None,
                        "summary": "Project execution is running",
                    }
                )
                self._service.store.upsert_project(project)
                await self._service.publish_summary(project)
                await self._service.emit_event(project=project, scope="project", event_name="project.running", text="Project execution is running")
            project = self._service.get_project(project_id) or project
            result = await self._service.unit_runner.run(project, root_unit)
            artifact = next(
                (
                    item
                    for item in self._service.list_artifacts(project.project_id)
                    if item.unit_id == root_unit.unit_id and item.kind == "report" and item.title == f"{project.title} result"
                ),
                None,
            )
            if artifact is None:
                artifact = self._service.artifact_store.create_text_artifact(
                    project_id=project.project_id,
                    unit_id=root_unit.unit_id,
                    kind="report",
                    title=f"{project.title} result",
                    content=result,
                )
                await self._service.emit_event(
                    project=project,
                    scope="project",
                    event_name="artifact.created",
                    text=f"Artifact created: {artifact.title}",
                    unit_id=root_unit.unit_id,
                    data=artifact.model_dump(mode="json"),
                )
                await self._service.publish_artifact(project, artifact)
            project = (self._service.get_project(project_id) or project).model_copy(
                update={
                    "status": "completed",
                    "updated_at": now_iso(),
                    "finished_at": now_iso(),
                    "summary": "Project completed",
                    "final_result": result,
                    "error_summary": "",
                }
            )
            self._service.store.upsert_project(project)
            await self._service.emit_event(project=project, scope="project", event_name="project.completed", text="Project completed")
            notice = self._service.notice_service.create(
                session_id=project.session_id,
                project_id=project.project_id,
                kind="project_completed",
                title="Project completed",
                text=f"Project {project.title} completed successfully.",
            )
            await self._service.publish_notice(project.session_id, notice)
            await self._service.publish_summary(project)
        except asyncio.CancelledError:
            project = self._service.get_project(project_id)
            user_canceled = await self._service.registry.is_canceled(project_id)
            if project is not None and user_canceled and project.status != "canceled":
                project = project.model_copy(update={"status": "canceled", "updated_at": now_iso(), "finished_at": now_iso(), "summary": "Project canceled"})
                self._service.store.upsert_project(project)
                await self._service.emit_event(project=project, scope="project", event_name="project.canceled", text="Project canceled", level="warn")
                await self._service.publish_summary(project)
            raise
        except PermissionBlockedError:
            project = self._service.get_project(project_id)
            if project is not None:
                self._service.refresh_project_governance_summary(project.project_id)
                await self._service.publish_summary(project)
        except OrdinaryTaskFailureError as exc:
            project = self._service.get_project(project_id)
            if project is not None:
                await self._failure_reporter.record_project_failure(project=project, error=exc, failure_kind="ordinary")
        except ModelChainUnavailableError as exc:
            project = self._service.get_project(project_id)
            if project is not None:
                updated = project.model_copy(update={'status': 'blocked', 'updated_at': now_iso(), 'summary': 'Waiting for model availability', 'error_summary': ''})
                self._service.store.upsert_project(updated)
                root_unit = self._service.get_unit(updated.root_unit_id)
                if root_unit is not None:
                    self._service.monitor_service.ensure_node(project=updated, unit=root_unit)
                    record = self._service.monitor_service._store.get_node(root_unit.unit_id)
                    if record is not None:
                        self._service.monitor_service._store.upsert_node(record.model_copy(update={'state': 'waiting', 'wait_reason': 'model_chain_unavailable', 'latest_progress_text': str(exc), 'updated_at': now_iso()}))
                await self._service.emit_event(project=updated, scope="project", event_name="project.blocked", text=str(exc), level="warn", data={'failure_kind': 'model_chain_unavailable'})
                await self._service.publish_summary(updated)
                asyncio.create_task(self._retry_after_delay(project_id, delay_s=15))
        except EngineeringFailureError as exc:
            project = self._service.get_project(project_id)
            if project is not None:
                await self._failure_reporter.record_project_failure(project=project, error=exc, failure_kind="engineering")
        except Exception as exc:
            project = self._service.get_project(project_id)
            if project is not None:
                wrapped = EngineeringFailureError(str(exc))
                await self._failure_reporter.record_project_failure(project=project, error=wrapped, failure_kind="engineering")
        finally:
            await self._service.registry.clear_task(project_id)

    async def _retry_after_delay(self, project_id: str, *, delay_s: int) -> None:
        await asyncio.sleep(max(5, int(delay_s or 15)))
        project = self._service.get_project(project_id)
        if project is None or project.status != 'blocked':
            return
        try:
            await self.run(project_id)
        except Exception:
            return


from __future__ import annotations

import os
from types import SimpleNamespace

import g3ku.web.windows_job as windows_job
import g3ku.web.worker_control as worker_control


def test_windows_job_assigns_process_to_kill_on_close_job(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    class _Kernel32:
        def CreateJobObjectW(self, _attrs, _name):
            calls.append(("CreateJobObjectW", None))
            return 101

        def SetInformationJobObject(self, job_handle, info_class, info_ptr, info_size):
            calls.append(("SetInformationJobObject", job_handle, info_class, info_size))
            assert info_ptr is not None
            return 1

        def OpenProcess(self, access, inherit_handle, pid):
            calls.append(("OpenProcess", access, inherit_handle, pid))
            return 202

        def AssignProcessToJobObject(self, job_handle, process_handle):
            calls.append(("AssignProcessToJobObject", job_handle, process_handle))
            return 1

        def CloseHandle(self, handle):
            calls.append(("CloseHandle", handle))
            return 1

    monkeypatch.setattr(windows_job, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(windows_job, "_get_kernel32", lambda: _Kernel32())
    monkeypatch.setattr(windows_job, "_JOB_HANDLE", None)
    monkeypatch.setattr(windows_job, "_JOB_ATEXIT_REGISTERED", False)
    atexit_calls: list[object] = []
    monkeypatch.setattr(windows_job.atexit, "register", lambda func: atexit_calls.append(func))

    assigned = windows_job.assign_process_to_kill_on_close_job(SimpleNamespace(pid=777))

    assert assigned is True
    assert windows_job._JOB_HANDLE == 101
    assert atexit_calls == [windows_job.close_kill_on_close_job]
    assert ("OpenProcess", windows_job.PROCESS_SET_QUOTA | windows_job.PROCESS_TERMINATE | windows_job.PROCESS_QUERY_LIMITED_INFORMATION, False, 777) in calls
    assert ("AssignProcessToJobObject", 101, 202) in calls
    assert ("CloseHandle", 202) in calls


def test_start_managed_task_worker_binds_process_to_windows_job(monkeypatch) -> None:
    job_assignments: list[int] = []

    class _Security:
        def active_master_key(self) -> str:
            return "secret-key"

    class _Process:
        def __init__(self) -> None:
            self.pid = 321

        def poll(self):
            return None

    monkeypatch.setattr(worker_control, "_MANAGED_WORKER_PROCESS", None)
    monkeypatch.setattr(worker_control, "auto_worker_enabled", lambda: True)
    monkeypatch.setattr(worker_control, "get_bootstrap_security_service", lambda *_args, **_kwargs: _Security())
    monkeypatch.setattr(
        worker_control,
        "assign_process_to_kill_on_close_job",
        lambda process: job_assignments.append(int(process.pid)) or True,
    )
    monkeypatch.setattr(
        worker_control,
        "os",
        SimpleNamespace(name="nt", environ=os.environ),
    )
    monkeypatch.setattr(
        worker_control,
        "subprocess",
        SimpleNamespace(Popen=lambda *args, **kwargs: _Process(), CREATE_NO_WINDOW=0),
    )

    started = worker_control.start_managed_task_worker()

    assert started is True
    assert job_assignments == [321]

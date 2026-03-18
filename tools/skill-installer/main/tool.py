from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from g3ku.resources.tool_settings import SkillInstallerToolSettings, runtime_tool_settings
from g3ku.runtime.cancellation import ToolCancellationRequested

_DEFAULT_REF = "main"
_GITHUB_HOST = "github.com"
_SAFE_SKILL_ID = re.compile(r"[^0-9A-Za-z._-]+")


class InstallError(Exception):
    """Raised when a skill import cannot be completed."""


class GitHubSource:
    def __init__(self, *, owner: str, repo: str, ref: str, path: str, url: str) -> None:
        self.owner = owner
        self.repo = repo
        self.ref = ref
        self.path = path
        self.url = url


def _normalize_skill_id(value: str, *, fallback: str = "imported-skill") -> str:
    normalized = _SAFE_SKILL_ID.sub("-", str(value or "").strip()).strip("-.")
    return normalized or fallback


def _validate_repo_path(path: str) -> str:
    normalized = str(path or "").replace("\\", "/").strip().strip("/")
    if not normalized:
        raise InstallError("Missing skill path inside the repository.")
    parts = [part for part in normalized.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise InstallError("Skill path must stay inside the repository.")
    return "/".join(parts)


def _request(url: str, *, timeout: int, cancel_token: Any | None = None) -> bytes:
    headers = {
        "User-Agent": "g3ku-skill-installer/1.0",
        "Accept": "application/octet-stream, application/zip, text/plain;q=0.9, */*;q=0.1",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=max(1, int(timeout or 30))) as response:
            chunks: list[bytes] = []
            while True:
                if cancel_token is not None and hasattr(cancel_token, "raise_if_cancelled"):
                    cancel_token.raise_if_cancelled(default_message="用户已请求暂停，正在安全停止...")
                chunk = response.read(1024 * 64)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
    except TimeoutError as exc:
        raise InstallError(f"Download timed out after {max(1, int(timeout or 30))} seconds.") from exc


def _safe_extract_zip(zip_file: zipfile.ZipFile, dest_dir: str, *, cancel_token: Any | None = None) -> None:
    dest_root = os.path.realpath(dest_dir)
    for info in zip_file.infolist():
        if cancel_token is not None and hasattr(cancel_token, "raise_if_cancelled"):
            cancel_token.raise_if_cancelled(default_message="用户已请求暂停，正在安全停止...")
        extracted_path = os.path.realpath(os.path.join(dest_dir, info.filename))
        if extracted_path == dest_root or extracted_path.startswith(dest_root + os.sep):
            continue
        raise InstallError("Archive contains files outside the destination.")
    for info in zip_file.infolist():
        if cancel_token is not None and hasattr(cancel_token, "raise_if_cancelled"):
            cancel_token.raise_if_cancelled(default_message="用户已请求暂停，正在安全停止...")
        zip_file.extract(info, dest_dir)


def _run_git(args: list[str], *, timeout: int, cancel_token: Any | None = None) -> None:
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GCM_INTERACTIVE", "never")
    process: subprocess.Popen[str] | None = None
    timeout_seconds = max(1, int(timeout or 120))
    deadline = time.monotonic() + timeout_seconds
    try:
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        if cancel_token is not None and hasattr(cancel_token, "register_process"):
            cancel_token.register_process(process)
        while True:
            if cancel_token is not None and hasattr(cancel_token, "is_cancelled") and cancel_token.is_cancelled():
                if process.poll() is None:
                    try:
                        process.terminate()
                    except Exception:
                        pass
                    try:
                        process.wait(timeout=2.0)
                    except Exception:
                        try:
                            process.kill()
                        except Exception:
                            pass
                raise ToolCancellationRequested(str(getattr(cancel_token, "reason", "") or "用户已请求暂停，正在安全停止..."))
            if process.poll() is not None:
                break
            if time.monotonic() >= deadline:
                try:
                    process.terminate()
                except Exception:
                    pass
                try:
                    process.wait(timeout=2.0)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
                raise InstallError(
                    f"Git command timed out after {timeout_seconds} seconds: {' '.join(args[:4])} ..."
                )
            time.sleep(0.05)
        stdout, stderr = process.communicate()
        result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    finally:
        if process is not None and cancel_token is not None and hasattr(cancel_token, "unregister_process"):
            cancel_token.unregister_process(process)
    if result.returncode != 0:
        raise InstallError(result.stderr.strip() or "Git command failed.")


class SkillInstallerTool:
    def __init__(
        self,
        *,
        workspace: Path,
        main_task_service: Any = None,
        settings: SkillInstallerToolSettings | None = None,
    ) -> None:
        self._workspace = Path(workspace).resolve(strict=False)
        self._main_task_service = main_task_service
        self._settings = settings or SkillInstallerToolSettings()

    async def execute(
        self,
        url: str | None = None,
        repo: str | None = None,
        path: str | None = None,
        ref: str | None = None,
        dest: str | None = None,
        name: str | None = None,
        method: str | None = None,
        __g3ku_runtime: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        runtime_payload = kwargs.pop("__g3ku_runtime", None)
        runtime = runtime_payload if isinstance(runtime_payload, dict) else (__g3ku_runtime if isinstance(__g3ku_runtime, dict) else {})
        del kwargs
        denied = self._authorize("install", runtime)
        if denied is not None:
            return json.dumps({"ok": False, "error": denied}, ensure_ascii=False)
        loop = asyncio.get_running_loop()
        return await asyncio.to_thread(
            self._execute_blocking,
            loop,
            runtime,
            url,
            repo,
            path,
            ref,
            dest,
            name,
            method,
        )

    def _execute_blocking(
        self,
        loop: asyncio.AbstractEventLoop,
        runtime: dict[str, Any],
        url: str | None,
        repo: str | None,
        path: str | None,
        ref: str | None,
        dest: str | None,
        name: str | None,
        method: str | None,
    ) -> str:
        try:
            cancel_token = runtime.get("cancel_token") if isinstance(runtime, dict) else None
            self._check_cancel(cancel_token)
            source = self._resolve_source(url=url, repo=repo, path=path, ref=ref)
            self._emit_progress_sync(loop, runtime, f"skill-installer: resolving {source.owner}/{source.repo}:{source.path}")
            requested_name = _normalize_skill_id(name or Path(source.path).name or source.repo)
            destination = self._resolve_destination(dest=dest, requested_name=requested_name)
            if destination.exists():
                raise InstallError(f"Destination already exists: {destination}")

            destination.parent.mkdir(parents=True, exist_ok=True)

            with tempfile.TemporaryDirectory(
                prefix="g3ku-skill-installer-",
                ignore_cleanup_errors=True,
            ) as tmp_dir:
                self._check_cancel(cancel_token)
                self._emit_progress_sync(loop, runtime, "skill-installer: fetching upstream repository")
                repo_root, method_used = self._prepare_repo(
                    source=source,
                    method=str(method or "auto").strip().lower() or "auto",
                    tmp_dir=tmp_dir,
                    cancel_token=cancel_token,
                )
                self._emit_progress_sync(loop, runtime, f"skill-installer: upstream fetched via {method_used}")
                self._check_cancel(cancel_token)
                skill_root = self._resolve_skill_root(repo_root=repo_root, repo_path=source.path)
                self._copy_skill_tree(skill_root, destination, cancel_token=cancel_token)
                self._emit_progress_sync(loop, runtime, f"skill-installer: copied files into {destination}")

            manifest_created = self._ensure_resource_manifest(
                skill_root=destination,
                requested_name=requested_name,
                source=source,
            )
            detected_skill_id = self._read_manifest_name(destination / "resource.yaml") or requested_name

            refresh_payload = self._refresh_resources(destination)
            catalog_payload = self._sync_catalog_blocking(loop, skill_id=detected_skill_id)
            self._emit_progress_sync(loop, runtime, f"skill-installer: installed {detected_skill_id}")

            warnings: list[str] = []
            if name and not manifest_created and detected_skill_id != requested_name:
                warnings.append(
                    "The upstream resource.yaml was preserved, so the installed skill id "
                    f"remains '{detected_skill_id}' instead of the requested name '{requested_name}'."
                )

            return json.dumps(
                {
                    "ok": True,
                    "tool": "skill-installer",
                    "skill_id": detected_skill_id,
                    "installed_path": str(destination),
                    "manifest_created": manifest_created,
                    "files_copied": self._count_files(destination),
                    "method": method_used,
                    "source": {
                        "url": source.url,
                        "repo": f"{source.owner}/{source.repo}",
                        "ref": source.ref,
                        "path": source.path,
                    },
                    "resource_refresh": refresh_payload,
                    "catalog": catalog_payload,
                    "warnings": warnings,
                },
                ensure_ascii=False,
            )
        except (InstallError, ToolCancellationRequested) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"Unexpected install error: {exc}"}, ensure_ascii=False)

    def _resolve_source(
        self,
        *,
        url: str | None,
        repo: str | None,
        path: str | None,
        ref: str | None,
    ) -> GitHubSource:
        if url:
            source = self._parse_github_url(str(url or "").strip(), default_ref=str(ref or _DEFAULT_REF).strip() or _DEFAULT_REF)
            if path:
                source.path = _validate_repo_path(path)
                source.url = f"https://{_GITHUB_HOST}/{source.owner}/{source.repo}/tree/{source.ref}/{source.path}"
            return source

        repo_text = str(repo or "").strip()
        if not repo_text:
            raise InstallError("Provide either url or repo + path.")
        repo_parts = [part for part in repo_text.split("/") if part]
        if len(repo_parts) != 2:
            raise InstallError("repo must use owner/repo format.")
        repo_path = _validate_repo_path(path or "")
        resolved_ref = str(ref or _DEFAULT_REF).strip() or _DEFAULT_REF
        owner, repo_name = repo_parts
        return GitHubSource(
            owner=owner,
            repo=repo_name,
            ref=resolved_ref,
            path=repo_path,
            url=f"https://{_GITHUB_HOST}/{owner}/{repo_name}/tree/{resolved_ref}/{repo_path}",
        )

    @staticmethod
    def _parse_github_url(url: str, *, default_ref: str) -> GitHubSource:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.netloc != _GITHUB_HOST:
            raise InstallError("Only full GitHub URLs are supported.")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            raise InstallError("Invalid GitHub URL.")

        owner, repo = parts[0], parts[1]
        ref = default_ref
        repo_path = ""
        if len(parts) > 2:
            if parts[2] in {"tree", "blob"}:
                if len(parts) < 4:
                    raise InstallError("GitHub URL must include both a ref and a path.")
                ref = parts[3]
                repo_path = "/".join(parts[4:])
            else:
                repo_path = "/".join(parts[2:])
        repo_path = _validate_repo_path(repo_path)
        return GitHubSource(
            owner=owner,
            repo=repo,
            ref=ref,
            path=repo_path,
            url=f"https://{_GITHUB_HOST}/{owner}/{repo}/tree/{ref}/{repo_path}",
        )

    def _resolve_destination(self, *, dest: str | None, requested_name: str) -> Path:
        if dest:
            raw = Path(str(dest or "").strip()).expanduser()
            candidate = raw if raw.is_absolute() else (self._workspace / raw)
            resolved = candidate.resolve(strict=False)
            if (resolved.exists() and resolved.is_dir()) or resolved.name.lower() == "skills":
                resolved = (resolved / requested_name).resolve(strict=False)
        else:
            resolved = (self._workspace / "skills" / requested_name).resolve(strict=False)
        try:
            resolved.relative_to(self._workspace)
        except ValueError as exc:
            raise InstallError("Destination must stay inside the current workspace.") from exc
        return resolved

    def _prepare_repo(self, *, source: GitHubSource, method: str, tmp_dir: str, cancel_token: Any | None = None) -> tuple[Path, str]:
        normalized = method if method in {"auto", "download", "git"} else ""
        if not normalized:
            raise InstallError("method must be one of auto, download, or git.")

        auto_prefer = str(getattr(self._settings, "auto_prefer", "git") or "git").strip().lower()
        if normalized == "auto":
            strategies = ["git", "download"] if auto_prefer == "git" else ["download", "git"]
            last_error: InstallError | None = None
            for strategy in strategies:
                try:
                    if strategy == "git":
                        return self._git_sparse_checkout(source=source, tmp_dir=tmp_dir, cancel_token=cancel_token), "git"
                    return self._download_repo_zip(source=source, tmp_dir=tmp_dir, cancel_token=cancel_token), "download"
                except InstallError as exc:
                    last_error = exc
                    continue
            if last_error is not None:
                raise last_error
            raise InstallError("Failed to fetch upstream repository.")
        if normalized == "download":
            return self._download_repo_zip(source=source, tmp_dir=tmp_dir, cancel_token=cancel_token), "download"
        if normalized == "git":
            return self._git_sparse_checkout(source=source, tmp_dir=tmp_dir, cancel_token=cancel_token), "git"
        raise InstallError("Unsupported install method.")

    def _download_repo_zip(self, *, source: GitHubSource, tmp_dir: str, cancel_token: Any | None = None) -> Path:
        zip_url = f"https://codeload.github.com/{source.owner}/{source.repo}/zip/{source.ref}"
        zip_path = Path(tmp_dir) / "repo.zip"
        try:
            payload = _request(
                zip_url,
                timeout=max(1, int(getattr(self._settings, "download_timeout", 30) or 30)),
                cancel_token=cancel_token,
            )
        except urllib.error.HTTPError as exc:
            raise InstallError(f"Download failed: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise InstallError(f"Download failed: {exc.reason}") from exc

        self._check_cancel(cancel_token)
        zip_path.write_bytes(payload)
        with zipfile.ZipFile(zip_path, "r") as zip_file:
            _safe_extract_zip(zip_file, tmp_dir, cancel_token=cancel_token)
            top_levels = {name.split("/")[0] for name in zip_file.namelist() if name}
        if not top_levels:
            raise InstallError("Downloaded archive was empty.")
        if len(top_levels) != 1:
            raise InstallError("Unexpected archive layout.")
        return (Path(tmp_dir) / next(iter(top_levels))).resolve(strict=False)

    def _git_sparse_checkout(self, *, source: GitHubSource, tmp_dir: str, cancel_token: Any | None = None) -> Path:
        if shutil.which("git") is None:
            raise InstallError("git is not available for sparse checkout fallback.")

        repo_url = f"https://{_GITHUB_HOST}/{source.owner}/{source.repo}.git"
        clone_attempts = [
            {"repo_dir": Path(tmp_dir) / "repo-branch", "use_branch": True},
            {"repo_dir": Path(tmp_dir) / "repo-fallback", "use_branch": False},
        ]
        last_error: InstallError | None = None
        for attempt in clone_attempts:
            repo_dir = Path(attempt["repo_dir"])
            clone_cmd = [
                "git",
                "clone",
                "--filter=blob:none",
                "--depth",
                "1",
                "--sparse",
                "--single-branch",
            ]
            if bool(attempt["use_branch"]):
                clone_cmd.extend(["--branch", source.ref])
            clone_cmd.extend([repo_url, str(repo_dir)])
            try:
                _run_git(
                    clone_cmd,
                    timeout=max(1, int(getattr(self._settings, "git_timeout", 120) or 120)),
                    cancel_token=cancel_token,
                )
                _run_git(
                    ["git", "-C", str(repo_dir), "sparse-checkout", "set", source.path],
                    timeout=max(1, int(getattr(self._settings, "git_timeout", 120) or 120)),
                    cancel_token=cancel_token,
                )
                _run_git(
                    ["git", "-C", str(repo_dir), "checkout", source.ref],
                    timeout=max(1, int(getattr(self._settings, "git_timeout", 120) or 120)),
                    cancel_token=cancel_token,
                )
                return repo_dir.resolve(strict=False)
            except InstallError as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        raise InstallError("git sparse checkout failed for an unknown reason.")

    @staticmethod
    def _resolve_skill_root(*, repo_root: Path, repo_path: str) -> Path:
        candidate = repo_root.joinpath(*repo_path.split("/"))
        if candidate.is_file():
            if candidate.name != "SKILL.md":
                raise InstallError(f"Path must point at a skill directory or SKILL.md, got: {repo_path}")
            candidate = candidate.parent
        if not candidate.exists() or not candidate.is_dir():
            raise InstallError(f"Skill path not found in repository: {repo_path}")
        if not (candidate / "SKILL.md").exists():
            raise InstallError(f"SKILL.md not found in selected directory: {repo_path}")
        return candidate

    @staticmethod
    def _copy_skill_tree(source_root: Path, destination: Path, *, cancel_token: Any | None = None) -> None:
        try:
            for current_root, dirnames, filenames in os.walk(source_root):
                if cancel_token is not None and hasattr(cancel_token, "raise_if_cancelled"):
                    cancel_token.raise_if_cancelled(default_message="用户已请求暂停，正在安全停止...")
                dirnames[:] = [
                    name for name in dirnames
                    if name not in {".git", "__pycache__", ".pytest_cache", ".ruff_cache"}
                ]
                current_path = Path(current_root)
                relative = current_path.relative_to(source_root)
                target_root = destination / relative
                target_root.mkdir(parents=True, exist_ok=True)
                for filename in filenames:
                    if filename in {".DS_Store"}:
                        continue
                    if cancel_token is not None and hasattr(cancel_token, "raise_if_cancelled"):
                        cancel_token.raise_if_cancelled(default_message="用户已请求暂停，正在安全停止...")
                    shutil.copy2(current_path / filename, target_root / filename)
        except FileExistsError as exc:
            raise InstallError(f"Destination already exists: {destination}") from exc
        except OSError as exc:
            raise InstallError(f"Failed to copy skill into workspace: {exc}") from exc

    def _ensure_resource_manifest(self, *, skill_root: Path, requested_name: str, source: GitHubSource) -> bool:
        manifest_path = skill_root / "resource.yaml"
        if manifest_path.exists():
            return False

        skill_md_path = skill_root / "SKILL.md"
        frontmatter, body = self._split_frontmatter(skill_md_path.read_text(encoding="utf-8"))
        description = str(frontmatter.get("description") or "").strip() or self._first_paragraph(body)
        if not description:
            description = (
                f"Imported from {source.owner}/{source.repo} ({source.ref}) at {source.path}."
            )

        keywords = [requested_name, Path(source.path).name]
        frontmatter_name = str(frontmatter.get("name") or "").strip()
        if frontmatter_name:
            keywords.append(frontmatter_name)
        deduped_keywords: list[str] = []
        for item in keywords:
            token = str(item or "").strip()
            if token and token not in deduped_keywords:
                deduped_keywords.append(token)

        manifest_lines = [
            "schema_version: 1",
            "kind: skill",
            f"name: {json.dumps(requested_name, ensure_ascii=False)}",
            f"description: {json.dumps(description, ensure_ascii=False)}",
            "trigger:",
        ]
        if deduped_keywords:
            manifest_lines.append("  keywords:")
            manifest_lines.extend(
                f"    - {json.dumps(keyword, ensure_ascii=False)}"
                for keyword in deduped_keywords
            )
        else:
            manifest_lines.append("  keywords: []")
        manifest_lines.extend(
            [
                "  always: false",
                "requires:",
                "  tools: []",
                "  bins: []",
                "  env: []",
                "content:",
                "  main: SKILL.md",
                "exposure:",
                "  agent: true",
                "  main_runtime: true",
                "",
            ]
        )
        manifest_path.write_text("\n".join(manifest_lines), encoding="utf-8")
        return True

    @staticmethod
    def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}, text
        end_index = None
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                end_index = index
                break
        if end_index is None:
            return {}, text
        frontmatter: dict[str, str] = {}
        for raw_line in lines[1:end_index]:
            if ":" not in raw_line:
                continue
            key, value = raw_line.split(":", 1)
            frontmatter[key.strip()] = value.strip().strip('"').strip("'")
        body = "\n".join(lines[end_index + 1 :])
        return frontmatter, body

    @staticmethod
    def _first_paragraph(body: str) -> str:
        lines: list[str] = []
        in_code_block = False
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if line.startswith("```"):
                in_code_block = not in_code_block
                if lines:
                    break
                continue
            if in_code_block:
                continue
            if not line:
                if lines:
                    break
                continue
            if line.startswith("#"):
                continue
            lines.append(line)
        return " ".join(lines).strip()

    @staticmethod
    def _read_manifest_name(path: Path) -> str | None:
        if not path.exists():
            return None
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            if key.strip() != "name":
                continue
            return value.strip().strip('"').strip("'")
        return None

    @staticmethod
    def _count_files(path: Path) -> int:
        return sum(1 for item in path.rglob("*") if item.is_file())

    def _authorize(self, action_id: str, runtime: dict[str, Any]) -> str | None:
        service = self._main_task_service
        checker = getattr(service, "is_tool_action_allowed", None) if service is not None else None
        if checker is None:
            return None
        actor_role = str(runtime.get("actor_role") or "ceo").strip().lower() or "ceo"
        session_id = str(runtime.get("session_key") or "web:shared").strip() or "web:shared"
        allowed = checker(
            actor_role=actor_role,
            session_id=session_id,
            tool_id="skill-installer",
            action_id=action_id,
            task_id=str(runtime.get("task_id") or "").strip() or None,
            node_id=str(runtime.get("node_id") or "").strip() or None,
        )
        if allowed:
            return None
        return f"Action not allowed for role {actor_role}: skill-installer.{action_id}"

    def _refresh_resources(self, target_path: Path) -> dict[str, Any]:
        service = self._main_task_service
        if service is None or not hasattr(service, "refresh_resource_paths"):
            return {"ok": False, "reason": "main_task_service_unavailable"}
        session_id = "web:shared"
        try:
            return service.refresh_resource_paths(
                [target_path],
                trigger="tool:skill-installer.install",
                session_id=session_id,
            )
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}

    async def _sync_catalog(self, *, skill_id: str) -> dict[str, Any]:
        service = self._main_task_service
        memory_manager = getattr(service, "memory_manager", None) if service is not None else None
        if memory_manager is None or not hasattr(memory_manager, "sync_catalog"):
            return {"ok": False, "reason": "memory_manager_unavailable"}
        try:
            payload = await memory_manager.sync_catalog(service, skill_ids={skill_id})
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}
        return {"ok": True, "payload": payload}

    def _sync_catalog_blocking(self, loop: asyncio.AbstractEventLoop, *, skill_id: str) -> dict[str, Any]:
        service = self._main_task_service
        memory_manager = getattr(service, "memory_manager", None) if service is not None else None
        if memory_manager is None or not hasattr(memory_manager, "sync_catalog"):
            return {"ok": False, "reason": "memory_manager_unavailable"}
        try:
            future = asyncio.run_coroutine_threadsafe(
                memory_manager.sync_catalog(service, skill_ids={skill_id}),
                loop,
            )
            payload = future.result(timeout=max(5, int(getattr(self._settings, "git_timeout", 120) or 120)))
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}
        return {"ok": True, "payload": payload}

    def _emit_progress_sync(
        self,
        loop: asyncio.AbstractEventLoop,
        runtime: dict[str, Any],
        message: str,
    ) -> None:
        callback = runtime.get("on_progress") if isinstance(runtime, dict) else None
        if callback is None:
            return
        try:
            result = callback(str(message), event_kind="tool")
            if inspect.isawaitable(result):
                asyncio.run_coroutine_threadsafe(result, loop).result(timeout=5)
        except Exception:
            return

    @staticmethod
    def _check_cancel(cancel_token: Any | None) -> None:
        if cancel_token is None or not hasattr(cancel_token, "raise_if_cancelled"):
            return
        cancel_token.raise_if_cancelled(default_message="用户已请求暂停，正在安全停止...")


def build(runtime):
    service = getattr(runtime.services, "main_task_service", None)
    settings = runtime_tool_settings(runtime, SkillInstallerToolSettings, tool_name="skill-installer")
    return SkillInstallerTool(workspace=runtime.workspace, main_task_service=service, settings=settings)

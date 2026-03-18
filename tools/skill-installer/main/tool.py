from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

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


def _request(url: str) -> bytes:
    headers = {
        "User-Agent": "g3ku-skill-installer/1.0",
        "Accept": "application/octet-stream, application/zip, text/plain;q=0.9, */*;q=0.1",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def _safe_extract_zip(zip_file: zipfile.ZipFile, dest_dir: str) -> None:
    dest_root = os.path.realpath(dest_dir)
    for info in zip_file.infolist():
        extracted_path = os.path.realpath(os.path.join(dest_dir, info.filename))
        if extracted_path == dest_root or extracted_path.startswith(dest_root + os.sep):
            continue
        raise InstallError("Archive contains files outside the destination.")
    zip_file.extractall(dest_dir)


def _run_git(args: list[str]) -> None:
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise InstallError(result.stderr.strip() or "Git command failed.")


class SkillInstallerTool:
    def __init__(self, *, workspace: Path, main_task_service: Any = None) -> None:
        self._workspace = Path(workspace).resolve(strict=False)
        self._main_task_service = main_task_service

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
        del kwargs
        runtime = __g3ku_runtime if isinstance(__g3ku_runtime, dict) else {}
        denied = self._authorize("install", runtime)
        if denied is not None:
            return json.dumps({"ok": False, "error": denied}, ensure_ascii=False)

        try:
            source = self._resolve_source(url=url, repo=repo, path=path, ref=ref)
            requested_name = _normalize_skill_id(name or Path(source.path).name or source.repo)
            destination = self._resolve_destination(dest=dest, requested_name=requested_name)
            if destination.exists():
                raise InstallError(f"Destination already exists: {destination}")

            destination.parent.mkdir(parents=True, exist_ok=True)

            with tempfile.TemporaryDirectory(prefix="g3ku-skill-installer-") as tmp_dir:
                repo_root, method_used = self._prepare_repo(
                    source=source,
                    method=str(method or "auto").strip().lower() or "auto",
                    tmp_dir=tmp_dir,
                )
                skill_root = self._resolve_skill_root(repo_root=repo_root, repo_path=source.path)
                self._copy_skill_tree(skill_root, destination)

            manifest_created = self._ensure_resource_manifest(
                skill_root=destination,
                requested_name=requested_name,
                source=source,
            )
            detected_skill_id = self._read_manifest_name(destination / "resource.yaml") or requested_name

            refresh_payload = self._refresh_resources(destination)
            catalog_payload = await self._sync_catalog(skill_id=detected_skill_id)

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
        except InstallError as exc:
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

    def _prepare_repo(self, *, source: GitHubSource, method: str, tmp_dir: str) -> tuple[Path, str]:
        normalized = method if method in {"auto", "download", "git"} else ""
        if not normalized:
            raise InstallError("method must be one of auto, download, or git.")

        if normalized in {"auto", "download"}:
            try:
                return self._download_repo_zip(source=source, tmp_dir=tmp_dir), "download"
            except InstallError:
                if normalized == "download":
                    raise
        if normalized in {"auto", "git"}:
            return self._git_sparse_checkout(source=source, tmp_dir=tmp_dir), "git"
        raise InstallError("Unsupported install method.")

    def _download_repo_zip(self, *, source: GitHubSource, tmp_dir: str) -> Path:
        zip_url = f"https://codeload.github.com/{source.owner}/{source.repo}/zip/{source.ref}"
        zip_path = Path(tmp_dir) / "repo.zip"
        try:
            payload = _request(zip_url)
        except urllib.error.HTTPError as exc:
            raise InstallError(f"Download failed: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise InstallError(f"Download failed: {exc.reason}") from exc

        zip_path.write_bytes(payload)
        with zipfile.ZipFile(zip_path, "r") as zip_file:
            _safe_extract_zip(zip_file, tmp_dir)
            top_levels = {name.split("/")[0] for name in zip_file.namelist() if name}
        if not top_levels:
            raise InstallError("Downloaded archive was empty.")
        if len(top_levels) != 1:
            raise InstallError("Unexpected archive layout.")
        return (Path(tmp_dir) / next(iter(top_levels))).resolve(strict=False)

    def _git_sparse_checkout(self, *, source: GitHubSource, tmp_dir: str) -> Path:
        if shutil.which("git") is None:
            raise InstallError("git is not available for sparse checkout fallback.")

        repo_url = f"https://{_GITHUB_HOST}/{source.owner}/{source.repo}.git"
        repo_dir = Path(tmp_dir) / "repo"
        clone_attempts = [
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--depth",
                "1",
                "--sparse",
                "--single-branch",
                "--branch",
                source.ref,
                repo_url,
                str(repo_dir),
            ],
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--depth",
                "1",
                "--sparse",
                "--single-branch",
                repo_url,
                str(repo_dir),
            ],
        ]
        last_error: InstallError | None = None
        for clone_cmd in clone_attempts:
            self._reset_clone_dir(repo_dir)
            try:
                _run_git(clone_cmd)
                last_error = None
                break
            except InstallError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        _run_git(["git", "-C", str(repo_dir), "sparse-checkout", "set", source.path])
        _run_git(["git", "-C", str(repo_dir), "checkout", source.ref])
        return repo_dir.resolve(strict=False)

    @staticmethod
    def _reset_clone_dir(repo_dir: Path) -> None:
        if not repo_dir.exists():
            return
        try:
            if repo_dir.is_dir():
                shutil.rmtree(repo_dir)
            else:
                repo_dir.unlink()
        except OSError as exc:
            raise InstallError(f"Failed to reset temporary clone directory {repo_dir}: {exc}") from exc

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
    def _copy_skill_tree(source_root: Path, destination: Path) -> None:
        try:
            shutil.copytree(
                source_root,
                destination,
                ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".ruff_cache"),
            )
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


def build(runtime):
    service = getattr(runtime.services, "main_task_service", None)
    return SkillInstallerTool(workspace=runtime.workspace, main_task_service=service)

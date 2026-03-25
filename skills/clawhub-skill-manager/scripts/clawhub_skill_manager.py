from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

SITE_URL = 'https://clawhub.ai'
API_BASE = f'{SITE_URL}/api/v1'
USER_AGENT = 'g3ku-clawhub-skill-manager/1.0'
SAFE_SKILL_ID = re.compile(r'[^0-9A-Za-z._-]+')
ZIP_TIMEOUT_SECONDS = 60
JSON_TIMEOUT_SECONDS = 30
BACKUP_TIMESTAMP_FORMAT = '%Y%m%d-%H%M%S'


class ClawHubSkillError(RuntimeError):
    pass


def workspace_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_skills_dir() -> Path:
    return workspace_root() / 'skills'


def default_temp_root() -> Path:
    return workspace_root() / '.tmp' / 'clawhub-skill-manager'


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def iso_from_millis(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC).replace(microsecond=0).isoformat()


def normalize_skill_id(value: str, *, fallback: str = 'clawhub-skill') -> str:
    normalized = SAFE_SKILL_ID.sub('-', str(value or '').strip()).strip('-.')
    return normalized or fallback


def build_public_skill_url(*, slug: str, owner_handle: str | None) -> str:
    owner = str(owner_handle or '').strip()
    if owner:
        return f'{SITE_URL}/{owner}/{slug}'
    return f'{SITE_URL}/skills/{slug}'


def request_bytes(url: str, *, timeout: int, accept: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            'User-Agent': USER_AGENT,
            'Accept': accept,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=max(1, int(timeout or 30))) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', errors='replace').strip()
        message = body or exc.reason or f'HTTP {exc.code}'
        raise ClawHubSkillError(f'HTTP {exc.code} for {url}: {message}') from exc
    except urllib.error.URLError as exc:
        raise ClawHubSkillError(f'Request failed for {url}: {exc.reason}') from exc
    except TimeoutError as exc:
        raise ClawHubSkillError(f'Request timed out after {timeout} seconds: {url}') from exc


def request_json(url: str) -> dict[str, Any]:
    payload = request_bytes(url, timeout=JSON_TIMEOUT_SECONDS, accept='application/json, text/plain;q=0.9, */*;q=0.1')
    try:
        data = json.loads(payload.decode('utf-8'))
    except json.JSONDecodeError as exc:
        raise ClawHubSkillError(f'Invalid JSON from {url}: {exc}') from exc
    if not isinstance(data, dict):
        raise ClawHubSkillError(f'Unexpected JSON payload from {url}: top-level object required.')
    return data


def safe_extract_zip(zip_bytes: bytes, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        dest_root = os.path.realpath(destination)
        for info in archive.infolist():
            extracted_path = os.path.realpath(os.path.join(destination, info.filename))
            if extracted_path == dest_root or extracted_path.startswith(dest_root + os.sep):
                continue
            raise ClawHubSkillError('Archive contains files outside the destination.')
        archive.extractall(destination)


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    normalized = text.replace('\r\n', '\n').replace('\r', '\n')
    if not normalized.startswith('---\n'):
        return {}, text
    end_index = normalized.find('\n---\n', 4)
    if end_index == -1:
        return {}, text
    frontmatter_text = normalized[4:end_index]
    body = normalized[end_index + 5 :]
    try:
        data = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError:
        data = {}
    return (data if isinstance(data, dict) else {}), body


def first_paragraph(body: str) -> str:
    lines: list[str] = []
    in_code_block = False
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if line.startswith('```'):
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
        if line.startswith('#'):
            continue
        lines.append(line)
    return ' '.join(lines).strip()


def load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    except yaml.YAMLError as exc:
        raise ClawHubSkillError(f'Failed to parse YAML: {path}: {exc}') from exc
    if not isinstance(data, dict):
        raise ClawHubSkillError(f'YAML must be an object: {path}')
    return data


def read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise ClawHubSkillError(f'Failed to parse JSON: {path}: {exc}') from exc
    if not isinstance(data, dict):
        raise ClawHubSkillError(f'JSON must be an object: {path}')
    return data


def dedupe_keywords(items: list[str]) -> list[str]:
    deduped: list[str] = []
    for item in items:
        token = str(item or '').strip()
        if token and token not in deduped:
            deduped.append(token)
    return deduped


def content_block_for_root(root: Path) -> dict[str, Any]:
    content: dict[str, Any] = {'main': 'SKILL.md'}
    for key in ('references', 'scripts', 'assets'):
        if (root / key).exists():
            content[key] = key
    return content


def build_resource_manifest(
    *,
    skill_root: Path,
    skill_id: str,
    slug: str,
    detail: dict[str, Any],
    version_payload: dict[str, Any],
    existing_manifest: dict[str, Any] | None = None,
    upstream_manifest: dict[str, Any] | None = None,
    upstream_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing_manifest = existing_manifest or {}
    upstream_manifest = upstream_manifest or {}
    upstream_meta = upstream_meta or {}
    base = dict(upstream_manifest or existing_manifest or {})

    skill_info = dict(detail.get('skill') or {})
    owner_info = dict(detail.get('owner') or {})
    version_info = dict(version_payload.get('version') or detail.get('latestVersion') or {})

    skill_text = (skill_root / 'SKILL.md').read_text(encoding='utf-8')
    frontmatter, body = split_frontmatter(skill_text)
    frontmatter_name = str(frontmatter.get('name') or '').strip()
    frontmatter_description = str(frontmatter.get('description') or '').strip()

    description = (
        str(base.get('description') or '').strip()
        or frontmatter_description
        or str(skill_info.get('summary') or '').strip()
        or first_paragraph(body)
        or f'Installed from ClawHub skill {slug}.'
    )
    display_name = str(skill_info.get('displayName') or '').strip()
    owner_handle = str(owner_info.get('handle') or '').strip() or None
    version = str(version_info.get('version') or upstream_meta.get('version') or '').strip()
    published_at = version_info.get('createdAt') or upstream_meta.get('publishedAt')

    keyword_sources = list(((base.get('trigger') or {}).get('keywords') or []))
    keyword_sources.extend([skill_id, slug, frontmatter_name, display_name])

    manifest = dict(base)
    manifest['schema_version'] = 1
    manifest['kind'] = 'skill'
    manifest['name'] = str(base.get('name') or skill_id).strip() or skill_id
    manifest['description'] = description
    manifest['trigger'] = {
        'keywords': dedupe_keywords([str(item) for item in keyword_sources if str(item or '').strip()]),
        'always': bool((base.get('trigger') or {}).get('always', False)),
    }
    manifest['requires'] = {
        'tools': list(((base.get('requires') or {}).get('tools') or [])),
        'bins': list(((base.get('requires') or {}).get('bins') or [])),
        'env': list(((base.get('requires') or {}).get('env') or [])),
    }
    manifest['content'] = content_block_for_root(skill_root)
    manifest['exposure'] = {
        'agent': True,
        'main_runtime': True,
    }
    manifest['source'] = {
        'type': 'clawhub',
        'site': SITE_URL,
        'api_base': API_BASE,
        'slug': slug,
        'url': build_public_skill_url(slug=slug, owner_handle=owner_handle),
        'detail_api': f'{API_BASE}/skills/{urllib.parse.quote(slug)}',
        'download_api': f'{API_BASE}/download',
        'owner_handle': owner_handle,
        'ref': version or None,
        'managed_by': 'clawhub-skill-manager',
    }
    manifest['current_version'] = {
        'version': version or None,
        'published_at': iso_from_millis(published_at),
        'installed_at': now_iso(),
        'summary': f'Installed from ClawHub {slug} version {version}.' if version else f'Installed from ClawHub {slug}.',
        'compare_rule': 'Compare local current_version.version (fallback _meta.json.version) with ClawHub /api/v1/skills/{slug} -> latestVersion.version.',
        'source_of_truth': 'Local resource.yaml current_version + _meta.json; remote ClawHub /api/v1/skills/{slug} and /api/v1/download.',
    }
    if upstream_meta:
        manifest['upstream_meta'] = upstream_meta
    return manifest


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding='utf-8')


def search_url(*, query: str, limit: int, highlighted_only: bool, non_suspicious_only: bool) -> str:
    params: dict[str, str] = {'q': query.strip(), 'limit': str(max(1, limit))}
    if highlighted_only:
        params['highlightedOnly'] = '1'
    if non_suspicious_only:
        params['nonSuspiciousOnly'] = '1'
    return f'{API_BASE}/search?{urllib.parse.urlencode(params)}'


def list_url(*, limit: int, sort: str, non_suspicious_only: bool) -> str:
    params: dict[str, str] = {'limit': str(max(1, limit)), 'sort': sort}
    if non_suspicious_only:
        params['nonSuspiciousOnly'] = '1'
    return f'{API_BASE}/skills?{urllib.parse.urlencode(params)}'


def detail_url(slug: str) -> str:
    return f'{API_BASE}/skills/{urllib.parse.quote(slug)}'


def version_url(slug: str, version: str) -> str:
    return f'{API_BASE}/skills/{urllib.parse.quote(slug)}/versions/{urllib.parse.quote(version)}'


def download_url(*, slug: str, version: str | None = None) -> str:
    params = {'slug': slug}
    if version:
        params['version'] = version
    return f'{API_BASE}/download?{urllib.parse.urlencode(params)}'


def fetch_search_results(*, query: str, limit: int, highlighted_only: bool, non_suspicious_only: bool) -> dict[str, Any]:
    if query.strip():
        payload = request_json(search_url(query=query, limit=limit, highlighted_only=highlighted_only, non_suspicious_only=non_suspicious_only))
        items = list(payload.get('results') or [])
        return {
            'ok': True,
            'action': 'search',
            'api': 'search',
            'query': query,
            'count': len(items),
            'items': items,
        }
    payload = request_json(list_url(limit=limit, sort='downloads', non_suspicious_only=non_suspicious_only))
    items = list(payload.get('items') or [])
    return {
        'ok': True,
        'action': 'search',
        'api': 'list',
        'query': '',
        'count': len(items),
        'items': items,
    }


def fetch_skill_detail(slug: str) -> dict[str, Any]:
    payload = request_json(detail_url(slug))
    payload['detailUrl'] = build_public_skill_url(
        slug=str(((payload.get('skill') or {}).get('slug') or slug)).strip() or slug,
        owner_handle=((payload.get('owner') or {}).get('handle')),
    )
    payload['apiUrl'] = detail_url(slug)
    return payload


def fetch_version_detail(slug: str, version: str) -> dict[str, Any]:
    payload = request_json(version_url(slug, version))
    payload['apiUrl'] = version_url(slug, version)
    return payload


def ensure_safe_to_install(detail: dict[str, Any], *, allow_suspicious: bool) -> None:
    moderation = dict(detail.get('moderation') or {})
    if moderation.get('isMalwareBlocked'):
        raise ClawHubSkillError('Refusing to install: ClawHub marked this skill as malware blocked.')
    if moderation.get('isSuspicious') and not allow_suspicious:
        raise ClawHubSkillError('Refusing to install suspicious skill without explicit allow_suspicious consent.')


def copy_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def backup_existing_tree(path: Path, *, temp_root: Path) -> str | None:
    if not path.exists():
        return None
    backup_dir = temp_root / 'backups' / f'{path.name}-{datetime.now(UTC).strftime(BACKUP_TIMESTAMP_FORMAT)}'
    backup_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(path, backup_dir)
    return str(backup_dir)


def install_from_clawhub(
    *,
    slug: str,
    skill_id: str | None,
    version: str | None,
    skills_dir: Path,
    temp_root: Path,
    force: bool,
    allow_suspicious: bool,
) -> dict[str, Any]:
    normalized_slug = str(slug or '').strip().lower()
    if not normalized_slug:
        raise ClawHubSkillError('slug is required.')
    local_skill_id = normalize_skill_id(skill_id or normalized_slug, fallback=normalized_slug)
    if local_skill_id == 'clawhub-skill-manager':
        raise ClawHubSkillError('Refusing to overwrite the manager skill itself.')

    skills_dir.mkdir(parents=True, exist_ok=True)
    temp_root.mkdir(parents=True, exist_ok=True)

    destination = skills_dir / local_skill_id
    existing_manifest = load_yaml_file(destination / 'resource.yaml') if destination.exists() else {}
    existing_source = dict(existing_manifest.get('source') or {})
    existing_is_clawhub = str(existing_source.get('type') or '').strip() == 'clawhub'
    if destination.exists() and not force and not existing_is_clawhub:
        raise ClawHubSkillError(
            f'Destination already exists and is not a managed ClawHub skill: {destination}. Use --force only if overwrite is explicitly intended.'
        )

    detail = fetch_skill_detail(normalized_slug)
    ensure_safe_to_install(detail, allow_suspicious=allow_suspicious)
    resolved_version = str(version or ((detail.get('latestVersion') or {}).get('version') or '')).strip()
    if not resolved_version:
        raise ClawHubSkillError(f'Unable to resolve version for slug: {normalized_slug}')

    version_payload = fetch_version_detail(normalized_slug, resolved_version)
    download_payload = request_bytes(
        download_url(slug=normalized_slug, version=resolved_version),
        timeout=ZIP_TIMEOUT_SECONDS,
        accept='application/zip, application/octet-stream;q=0.9, */*;q=0.1',
    )

    with tempfile.TemporaryDirectory(prefix='g3ku-clawhub-', dir=str(temp_root)) as tmp_dir:
        stage_root = Path(tmp_dir) / 'stage'
        safe_extract_zip(download_payload, stage_root)
        skill_md_path = stage_root / 'SKILL.md'
        if not skill_md_path.exists():
            raise ClawHubSkillError('Downloaded archive does not contain SKILL.md at zip root.')

        upstream_manifest = load_yaml_file(stage_root / 'resource.yaml') if (stage_root / 'resource.yaml').exists() else {}
        upstream_meta = read_json_file(stage_root / '_meta.json') if (stage_root / '_meta.json').exists() else {}
        manifest = build_resource_manifest(
            skill_root=stage_root,
            skill_id=local_skill_id,
            slug=normalized_slug,
            detail=detail,
            version_payload=version_payload,
            existing_manifest=existing_manifest,
            upstream_manifest=upstream_manifest,
            upstream_meta=upstream_meta,
        )
        write_manifest(stage_root / 'resource.yaml', manifest)

        backup_path = backup_existing_tree(destination, temp_root=temp_root) if destination.exists() else None
        copy_tree(stage_root, destination)

    owner = dict(detail.get('owner') or {})
    moderation = dict(detail.get('moderation') or {})
    return {
        'ok': True,
        'action': 'install',
        'skill_id': local_skill_id,
        'slug': normalized_slug,
        'version': resolved_version,
        'installed_path': str(destination),
        'manifest_path': str(destination / 'resource.yaml'),
        'backup_path': backup_path,
        'detail_url': build_public_skill_url(slug=normalized_slug, owner_handle=owner.get('handle')),
        'download_url': download_url(slug=normalized_slug, version=resolved_version),
        'owner': {
            'handle': owner.get('handle'),
            'displayName': owner.get('displayName'),
        },
        'moderation': moderation,
        'files': sum(1 for item in destination.rglob('*') if item.is_file()),
        'replaced_existing': bool(backup_path),
    }


def local_clawhub_record(*, skill_id: str, skills_dir: Path) -> dict[str, Any]:
    normalized_skill_id = normalize_skill_id(skill_id, fallback=skill_id)
    target = skills_dir / normalized_skill_id
    if not target.exists():
        raise ClawHubSkillError(f'Local skill not found: {target}')
    manifest = load_yaml_file(target / 'resource.yaml')
    source = dict(manifest.get('source') or {})
    if str(source.get('type') or '').strip() != 'clawhub':
        raise ClawHubSkillError(f'Local skill is not managed by ClawHub: {target}')
    current_version = dict(manifest.get('current_version') or {})
    upstream_meta = read_json_file(target / '_meta.json') if (target / '_meta.json').exists() else {}
    slug = str(source.get('slug') or normalized_skill_id).strip().lower()
    version = str(current_version.get('version') or upstream_meta.get('version') or '').strip()
    return {
        'skill_id': normalized_skill_id,
        'path': target,
        'manifest': manifest,
        'source': source,
        'slug': slug,
        'installed_version': version,
        'upstream_meta': upstream_meta,
    }


def status_for_local_skill(*, skill_id: str, skills_dir: Path) -> dict[str, Any]:
    local = local_clawhub_record(skill_id=skill_id, skills_dir=skills_dir)
    detail = fetch_skill_detail(local['slug'])
    latest_version = str(((detail.get('latestVersion') or {}).get('version') or '')).strip()
    owner = dict(detail.get('owner') or {})
    return {
        'ok': True,
        'action': 'status',
        'skill_id': local['skill_id'],
        'slug': local['slug'],
        'path': str(local['path']),
        'installed_version': local['installed_version'] or None,
        'latest_version': latest_version or None,
        'update_available': bool(latest_version and latest_version != local['installed_version']),
        'detail_url': build_public_skill_url(slug=local['slug'], owner_handle=owner.get('handle')),
        'owner_handle': owner.get('handle'),
        'moderation': detail.get('moderation') or {},
    }


def update_local_skill(
    *,
    skill_id: str,
    version: str | None,
    skills_dir: Path,
    temp_root: Path,
    force: bool,
    allow_suspicious: bool,
) -> dict[str, Any]:
    local = local_clawhub_record(skill_id=skill_id, skills_dir=skills_dir)
    detail = fetch_skill_detail(local['slug'])
    ensure_safe_to_install(detail, allow_suspicious=allow_suspicious)
    target_version = str(version or ((detail.get('latestVersion') or {}).get('version') or '')).strip()
    if not target_version:
        raise ClawHubSkillError(f'Unable to resolve target version for {local["slug"]}.')
    if target_version == local['installed_version'] and not force:
        return {
            'ok': True,
            'action': 'update',
            'skill_id': local['skill_id'],
            'slug': local['slug'],
            'previous_version': local['installed_version'] or None,
            'version': target_version,
            'updated': False,
            'message': 'Already at requested version.',
            'installed_path': str(local['path']),
        }
    result = install_from_clawhub(
        slug=local['slug'],
        skill_id=local['skill_id'],
        version=target_version,
        skills_dir=skills_dir,
        temp_root=temp_root,
        force=True,
        allow_suspicious=allow_suspicious,
    )
    result['action'] = 'update'
    result['previous_version'] = local['installed_version'] or None
    result['updated'] = True
    return result


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Search, install, and update ClawHub skills for G3KU.')
    parser.add_argument('--skills-dir', default=str(default_skills_dir()), help='Target skills directory. Defaults to <workspace>/skills.')
    parser.add_argument('--temp-root', default=str(default_temp_root()), help='Temporary directory used for staging and backups.')

    subparsers = parser.add_subparsers(dest='command', required=True)

    p_search = subparsers.add_parser('search', help='Search ClawHub skills or browse top skills when query is empty.')
    p_search.add_argument('--query', default='', help='Natural-language search query.')
    p_search.add_argument('--limit', type=int, default=8, help='Maximum number of results to return.')
    p_search.add_argument('--highlighted-only', action='store_true', help='Limit results to highlighted skills.')
    p_search.add_argument('--include-suspicious', action='store_true', help='Disable nonSuspiciousOnly filter.')

    p_inspect = subparsers.add_parser('inspect', help='Inspect a ClawHub skill by slug.')
    p_inspect.add_argument('--slug', required=True, help='ClawHub skill slug.')

    p_install = subparsers.add_parser('install', help='Install a ClawHub skill into the local skills directory.')
    p_install.add_argument('--slug', required=True, help='ClawHub skill slug.')
    p_install.add_argument('--skill-id', default='', help='Optional local skill id. Defaults to normalized slug.')
    p_install.add_argument('--version', default='', help='Optional version to install. Defaults to latestVersion.version.')
    p_install.add_argument('--force', action='store_true', help='Allow overwriting an existing destination after backup.')
    p_install.add_argument('--allow-suspicious', action='store_true', help='Allow install when ClawHub marks the skill as suspicious.')

    p_download = subparsers.add_parser('download', help='Alias of install.')
    p_download.add_argument('--slug', required=True, help='ClawHub skill slug.')
    p_download.add_argument('--skill-id', default='', help='Optional local skill id. Defaults to normalized slug.')
    p_download.add_argument('--version', default='', help='Optional version to install. Defaults to latestVersion.version.')
    p_download.add_argument('--force', action='store_true', help='Allow overwriting an existing destination after backup.')
    p_download.add_argument('--allow-suspicious', action='store_true', help='Allow install when ClawHub marks the skill as suspicious.')

    p_status = subparsers.add_parser('status', help='Check whether a managed local skill has updates available.')
    p_status.add_argument('--skill-id', required=True, help='Local skill id under the skills directory.')

    p_update = subparsers.add_parser('update', help='Update a managed local ClawHub skill.')
    p_update.add_argument('--skill-id', required=True, help='Local skill id under the skills directory.')
    p_update.add_argument('--version', default='', help='Optional target version. Defaults to latestVersion.version.')
    p_update.add_argument('--force', action='store_true', help='Reinstall even when the version is already current.')
    p_update.add_argument('--allow-suspicious', action='store_true', help='Allow update when ClawHub marks the skill as suspicious.')
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)

    skills_dir = Path(str(args.skills_dir)).resolve()
    temp_root = Path(str(args.temp_root)).resolve()

    try:
        if args.command == 'search':
            payload = fetch_search_results(
                query=str(args.query or ''),
                limit=max(1, int(args.limit or 8)),
                highlighted_only=bool(args.highlighted_only),
                non_suspicious_only=not bool(args.include_suspicious),
            )
        elif args.command == 'inspect':
            detail = fetch_skill_detail(str(args.slug or '').strip())
            payload = {
                'ok': True,
                'action': 'inspect',
                'slug': str(((detail.get('skill') or {}).get('slug') or args.slug)).strip(),
                'detail': detail,
            }
        elif args.command in {'install', 'download'}:
            payload = install_from_clawhub(
                slug=str(args.slug or '').strip(),
                skill_id=str(args.skill_id or '').strip() or None,
                version=str(args.version or '').strip() or None,
                skills_dir=skills_dir,
                temp_root=temp_root,
                force=bool(args.force),
                allow_suspicious=bool(args.allow_suspicious),
            )
            payload['action'] = args.command
        elif args.command == 'status':
            payload = status_for_local_skill(skill_id=str(args.skill_id or '').strip(), skills_dir=skills_dir)
        elif args.command == 'update':
            payload = update_local_skill(
                skill_id=str(args.skill_id or '').strip(),
                version=str(args.version or '').strip() or None,
                skills_dir=skills_dir,
                temp_root=temp_root,
                force=bool(args.force),
                allow_suspicious=bool(args.allow_suspicious),
            )
        else:
            raise ClawHubSkillError(f'Unsupported command: {args.command}')
    except ClawHubSkillError as exc:
        payload = {'ok': False, 'error': str(exc), 'command': args.command}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

from __future__ import annotations

import argparse
import email.utils
import hashlib
import json
import os
import random
import re
import shutil
import sys
import tempfile
import time
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
REQUEST_THROTTLE_SECONDS = float(os.environ.get('CLAWHUB_REQUEST_THROTTLE_SECONDS', '0.5') or '0.5')
CACHE_TTL_SECONDS = int(os.environ.get('CLAWHUB_CACHE_TTL_SECONDS', '300') or '300')
CACHE_MAX_AGE_SECONDS = int(os.environ.get('CLAWHUB_CACHE_MAX_AGE_SECONDS', '3600') or '3600')
MAX_RETRIES = int(os.environ.get('CLAWHUB_HTTP_MAX_RETRIES', '4') or '4')
BACKOFF_BASE_SECONDS = float(os.environ.get('CLAWHUB_BACKOFF_BASE_SECONDS', '1.0') or '1.0')
BACKOFF_MAX_SECONDS = float(os.environ.get('CLAWHUB_BACKOFF_MAX_SECONDS', '12.0') or '12.0')
RETRYABLE_HTTP_CODES = {429, 502, 503, 504}
CACHE_NAMESPACE_SEARCH = 'search'
CACHE_NAMESPACE_SKILL = 'skill'
_request_throttle_state = {'last_request_at': 0.0}


class ClawHubSkillError(RuntimeError):
    pass


def workspace_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_skills_dir() -> Path:
    return workspace_root() / 'skills'


def default_temp_root() -> Path:
    return workspace_root() / '.tmp' / 'clawhub-skill-manager'


def cache_root(temp_root: Path) -> Path:
    return temp_root / 'cache'


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_skill_id(raw: str) -> str:
    candidate = SAFE_SKILL_ID.sub('-', (raw or '').strip()).strip('-_.').lower()
    if not candidate:
        raise ClawHubSkillError('Unable to derive a safe local skill id. Provide --skill-id explicitly.')
    return candidate


def slug_to_skill_id(slug: str) -> str:
    slug = (slug or '').strip()
    if not slug:
        raise ClawHubSkillError('Slug must not be empty.')
    if '/' in slug:
        slug = slug.split('/')[-1]
    return normalize_skill_id(slug)


def coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 'yes', 'on'}
    return bool(value)


def strip_html(value: str) -> str:
    return re.sub(r'<[^>]+>', '', value or '').strip()


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = str(text or '').splitlines()
    if not lines or lines[0].strip() != '---':
        return {}, str(text or '')
    end_index = None
    for index in range(1, len(lines)):
        if lines[index].strip() == '---':
            end_index = index
            break
    if end_index is None:
        return {}, str(text or '')
    frontmatter: dict[str, str] = {}
    for raw_line in lines[1:end_index]:
        if ':' not in raw_line:
            continue
        key, value = raw_line.split(':', 1)
        frontmatter[key.strip()] = value.strip().strip('"').strip("'")
    body = '\n'.join(lines[end_index + 1 :])
    return frontmatter, body


def first_paragraph(body: str) -> str:
    lines: list[str] = []
    in_code_block = False
    for raw_line in str(body or '').splitlines():
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


def _iso_from_timestamp(value: Any) -> str:
    raw = value
    if raw in (None, ''):
        return ''
    try:
        numeric = float(raw)
    except (TypeError, ValueError):
        return str(raw or '').strip()
    if numeric > 1_000_000_000_000:
        numeric = numeric / 1000.0
    return datetime.fromtimestamp(numeric, tz=UTC).isoformat()


def build_resource_manifest(
    *,
    skill_root: Path,
    skill_id: str,
    slug: str,
    detail: dict[str, Any] | None,
    version_payload: dict[str, Any] | None,
    upstream_meta: dict[str, Any] | None,
) -> dict[str, Any]:
    root = Path(skill_root)
    detail_payload = dict(detail or {})
    skill_payload = dict(detail_payload.get('skill') or {})
    owner_payload = dict(detail_payload.get('owner') or {})
    latest_version_payload = dict(detail_payload.get('latestVersion') or {})
    version_info = dict((version_payload or {}).get('version') or {})
    upstream = dict(upstream_meta or {})

    skill_md_path = root / 'SKILL.md'
    frontmatter: dict[str, str] = {}
    body = ''
    if skill_md_path.is_file():
        frontmatter, body = split_frontmatter(skill_md_path.read_text(encoding='utf-8'))

    local_description = str(frontmatter.get('description') or '').strip() or first_paragraph(body)
    remote_description = strip_html(
        str(skill_payload.get('summary') or skill_payload.get('description') or '')
    )
    owner_handle = str(owner_payload.get('handle') or owner_payload.get('username') or '').strip()
    owner_display_name = str(owner_payload.get('displayName') or '').strip()
    published_at = (
        _iso_from_timestamp(version_info.get('createdAt'))
        or _iso_from_timestamp(latest_version_payload.get('createdAt'))
        or _iso_from_timestamp(upstream.get('publishedAt'))
    )
    version = (
        str(version_info.get('version') or '').strip()
        or str(latest_version_payload.get('version') or '').strip()
        or str(upstream.get('version') or '').strip()
    )

    content: dict[str, Any] = {'main': 'SKILL.md'}
    if (root / 'references').is_dir():
        content['references'] = 'references'

    source = {
        'type': 'clawhub',
        'slug': str(slug or '').strip(),
        'url': build_skill_url(str(slug or '').strip(), owner_handle or None),
        'detail_api': f"{API_BASE}/skills/{str(slug or '').strip()}",
    }
    if owner_handle:
        source['owner'] = owner_handle

    manifest: dict[str, Any] = {
        'schema_version': 1,
        'kind': 'skill',
        'name': str(skill_id or '').strip(),
        'display_name': str(skill_payload.get('displayName') or '').strip() or str(skill_id or '').strip(),
        'description': local_description or remote_description or f'Imported from ClawHub skill {skill_id}.',
        'content': content,
        'source': source,
        'current_version': {
            'version': version,
            'published_at': published_at,
            'installed_at': datetime.now(UTC).isoformat(),
        },
        'x_g3ku': {
            'clawhub': {
                'slug': str(slug or '').strip(),
                'owner': owner_handle,
                'owner_display_name': owner_display_name,
                'managed': True,
            }
        },
    }
    if not published_at:
        manifest['current_version'].pop('published_at', None)
    if not version:
        manifest['current_version'].pop('version', None)
    return manifest


def build_skill_url(slug: str, owner: str | None = None) -> str:
    slug = (slug or '').strip().strip('/')
    owner = (owner or '').strip().strip('/')
    if owner:
        return f'{SITE_URL}/{owner}/{slug}'
    return f'{SITE_URL}/skills/{slug}'


def throttle_requests() -> None:
    min_interval = max(0.0, REQUEST_THROTTLE_SECONDS)
    if min_interval <= 0:
        return
    now = time.monotonic()
    elapsed = now - _request_throttle_state['last_request_at']
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _request_throttle_state['last_request_at'] = time.monotonic()


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.isdigit():
        return max(0.0, float(text))
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    delta = (parsed - datetime.now(UTC)).total_seconds()
    return max(0.0, delta)


def compute_backoff_delay(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return min(max(0.0, retry_after), BACKOFF_MAX_SECONDS)
    exponential = BACKOFF_BASE_SECONDS * (2 ** max(0, attempt - 1))
    jitter = random.uniform(0.0, max(0.1, BACKOFF_BASE_SECONDS))
    return min(BACKOFF_MAX_SECONDS, exponential + jitter)


def cache_key(url: str) -> str:
    return hashlib.sha256(url.encode('utf-8')).hexdigest()


def cache_file_for(url: str, *, namespace: str, temp_root: Path) -> Path:
    return cache_root(temp_root) / namespace / f'{cache_key(url)}.json'


def read_cache(path: Path, *, ttl_seconds: int) -> bytes | None:
    if ttl_seconds <= 0 or not path.exists():
        return None
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None
    if age > ttl_seconds:
        return None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    data = payload.get('body_hex')
    if not isinstance(data, str):
        return None
    try:
        return bytes.fromhex(data)
    except ValueError:
        return None


def write_cache(path: Path, body: bytes) -> None:
    ensure_dir(path.parent)
    payload = {
        'stored_at': datetime.now(UTC).isoformat(),
        'body_hex': body.hex(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def build_rate_limit_hint(*, url: str, status_code: int | None, retry_after: float | None, attempts: int) -> str:
    parts = ['ClawHub API request could not complete within retry limits.']
    if status_code is not None:
        parts.append(f'Last HTTP status: {status_code}.')
    if retry_after is not None:
        parts.append(f'Server requested retry after about {retry_after:.1f}s.')
    parts.append(f'Attempted {attempts} time(s) for {url}.')
    parts.append('Please slow down repeated search/install/update requests, wait briefly, then retry.')
    return ' '.join(parts)


def request_bytes(
    url: str,
    *,
    timeout: int,
    accept: str,
    temp_root: Path | None = None,
    cache_namespace: str | None = None,
    cache_ttl_seconds: int = 0,
    allow_stale_on_error: bool = False,
) -> bytes:
    cache_path: Path | None = None
    cached_bytes: bytes | None = None
    if temp_root is not None and cache_namespace:
        cache_path = cache_file_for(url, namespace=cache_namespace, temp_root=temp_root)
        cached_bytes = read_cache(cache_path, ttl_seconds=cache_ttl_seconds)
        if cached_bytes is not None:
            return cached_bytes

    attempts = max(1, MAX_RETRIES)
    last_error: Exception | None = None
    stale_bytes: bytes | None = None
    if allow_stale_on_error and cache_path is not None:
        stale_bytes = read_cache(cache_path, ttl_seconds=max(cache_ttl_seconds, CACHE_MAX_AGE_SECONDS))

    for attempt in range(1, attempts + 1):
        throttle_requests()
        request = urllib.request.Request(
            url,
            headers={
                'User-Agent': USER_AGENT,
                'Accept': accept,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=max(1, int(timeout or 30))) as response:
                body = response.read()
                if cache_path is not None:
                    write_cache(cache_path, body)
                return body
        except urllib.error.HTTPError as exc:
            body = exc.read().decode('utf-8', errors='replace').strip()
            retry_after = parse_retry_after(exc.headers.get('Retry-After'))
            if exc.code in RETRYABLE_HTTP_CODES and attempt < attempts:
                time.sleep(compute_backoff_delay(attempt, retry_after))
                continue
            if allow_stale_on_error and stale_bytes is not None and exc.code in RETRYABLE_HTTP_CODES:
                return stale_bytes
            message = body or exc.reason or f'HTTP {exc.code}'
            if exc.code in RETRYABLE_HTTP_CODES:
                message = f'{message} | {build_rate_limit_hint(url=url, status_code=exc.code, retry_after=retry_after, attempts=attempt)}'
            raise ClawHubSkillError(f'HTTP {exc.code} for {url}: {message}') from exc
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(compute_backoff_delay(attempt, None))
                continue
            if allow_stale_on_error and stale_bytes is not None:
                return stale_bytes
            raise ClawHubSkillError(f'Request failed for {url}: {exc.reason}') from exc
        except TimeoutError as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(compute_backoff_delay(attempt, None))
                continue
            if allow_stale_on_error and stale_bytes is not None:
                return stale_bytes
            raise ClawHubSkillError(f'Request timed out for {url}.') from exc
    if allow_stale_on_error and stale_bytes is not None:
        return stale_bytes
    raise ClawHubSkillError(f'Request failed for {url}: {last_error!r}')


def request_json(
    url: str,
    *,
    timeout: int = JSON_TIMEOUT_SECONDS,
    temp_root: Path | None = None,
    cache_namespace: str | None = None,
    cache_ttl_seconds: int = 0,
    allow_stale_on_error: bool = False,
) -> Any:
    raw = request_bytes(
        url,
        timeout=timeout,
        accept='application/json',
        temp_root=temp_root,
        cache_namespace=cache_namespace,
        cache_ttl_seconds=cache_ttl_seconds,
        allow_stale_on_error=allow_stale_on_error,
    )
    try:
        return json.loads(raw.decode('utf-8'))
    except json.JSONDecodeError as exc:
        raise ClawHubSkillError(f'Invalid JSON returned by {url}: {exc}') from exc


def load_local_manifest(skill_dir: Path) -> dict[str, Any]:
    manifest_path = skill_dir / 'resource.yaml'
    if not manifest_path.is_file():
        raise ClawHubSkillError(f'Missing local manifest: {manifest_path}')
    data = yaml.safe_load(manifest_path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ClawHubSkillError(f'Invalid manifest format in {manifest_path}')
    return data


def extract_clawhub_meta(manifest: dict[str, Any]) -> dict[str, Any]:
    x_meta = manifest.get('x_g3ku')
    if not isinstance(x_meta, dict):
        return {}
    clawhub = x_meta.get('clawhub')
    if not isinstance(clawhub, dict):
        return {}
    return clawhub


def ensure_safe_status(skill_data: dict[str, Any], *, allow_suspicious: bool) -> None:
    moderation = skill_data.get('moderation') or {}
    is_blocked = coerce_bool(moderation.get('isMalwareBlocked'))
    is_suspicious = coerce_bool(moderation.get('isSuspicious'))
    if is_blocked:
        raise ClawHubSkillError('ClawHub reports this skill as malware-blocked. Refusing to install/update.')
    if is_suspicious and not allow_suspicious:
        raise ClawHubSkillError('ClawHub reports this skill as suspicious. Re-run with --allow-suspicious only if the user explicitly accepts the risk.')


def choose_version(skill_data: dict[str, Any], requested_version: str | None) -> dict[str, Any]:
    versions = skill_data.get('versions')
    if not isinstance(versions, list):
        versions = []
    requested = (requested_version or '').strip()
    if requested:
        for item in versions:
            if isinstance(item, dict) and str(item.get('version') or '').strip() == requested:
                return item
        raise ClawHubSkillError(f'Requested version {requested!r} was not found in ClawHub metadata.')

    latest = skill_data.get('latestVersion')
    if isinstance(latest, dict) and latest.get('version'):
        return latest
    if versions:
        for item in versions:
            if isinstance(item, dict) and item.get('version'):
                return item
    raise ClawHubSkillError('ClawHub metadata did not include an installable version.')


def extract_archive_root(members: list[str]) -> str:
    roots: set[str] = set()
    for name in members:
        cleaned = name.strip('/').replace('\\', '/')
        if not cleaned:
            continue
        roots.add(cleaned.split('/', 1)[0])
    if len(roots) == 1:
        return next(iter(roots))
    return ''


def make_local_manifest(*, upstream_name: str, description: str, skill_id: str, source_url: str, skill_page_url: str, slug: str, owner: str | None, version: str) -> dict[str, Any]:
    return {
        'schema_version': 1,
        'kind': 'skill',
        'name': skill_id,
        'description': description or f'Imported from ClawHub skill {upstream_name}.',
        'origin': {
            'type': 'clawhub',
            'url': source_url,
            'homepage': skill_page_url,
        },
        'content': {
            'main': 'SKILL.md',
        },
        'x_g3ku': {
            'clawhub': {
                'slug': slug,
                'owner': owner,
                'source_url': source_url,
                'page_url': skill_page_url,
                'installed_version': version,
                'upstream_name': upstream_name,
                'managed': True,
            }
        },
    }


def rewrite_manifest(path: Path, *, skill_data: dict[str, Any], version_data: dict[str, Any], skill_id: str) -> dict[str, Any]:
    page_url = build_skill_url(str(skill_data.get('slug') or ''), str(skill_data.get('user', {}).get('username') or '') or None)
    manifest = load_local_manifest(path.parent) if path.exists() else {}
    if not isinstance(manifest, dict):
        manifest = {}

    manifest['schema_version'] = manifest.get('schema_version') or 1
    manifest['kind'] = manifest.get('kind') or 'skill'
    manifest['name'] = skill_id
    description = strip_html(str(skill_data.get('shortDescription') or skill_data.get('description') or ''))
    manifest['description'] = description or manifest.get('description') or f'Imported from ClawHub skill {skill_data.get("name") or skill_id}.'

    content = manifest.get('content')
    if not isinstance(content, dict):
        content = {}
    content['main'] = content.get('main') or 'SKILL.md'
    manifest['content'] = content

    manifest['origin'] = {
        'type': 'clawhub',
        'url': str(version_data.get('sourceURL') or ''),
        'homepage': page_url,
    }

    x_meta = manifest.get('x_g3ku')
    if not isinstance(x_meta, dict):
        x_meta = {}
    x_meta['clawhub'] = {
        'slug': str(skill_data.get('slug') or ''),
        'owner': str(skill_data.get('user', {}).get('username') or ''),
        'source_url': str(version_data.get('sourceURL') or ''),
        'page_url': page_url,
        'installed_version': str(version_data.get('version') or ''),
        'upstream_name': str(skill_data.get('name') or skill_id),
        'managed': True,
    }
    manifest['x_g3ku'] = x_meta

    path.write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding='utf-8')
    return manifest


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


def skill_detail_url(slug: str) -> str:
    safe_slug = urllib.parse.quote((slug or '').strip(), safe='')
    return f'{API_BASE}/skills/{safe_slug}'


def fetch_search_results(*, query: str, limit: int, highlighted_only: bool, non_suspicious_only: bool, temp_root: Path) -> list[dict[str, Any]]:
    url = search_url(query=query, limit=limit, highlighted_only=highlighted_only, non_suspicious_only=non_suspicious_only) if query.strip() else list_url(limit=limit, sort='downloadCount', non_suspicious_only=non_suspicious_only)
    data = request_json(url, temp_root=temp_root, cache_namespace=CACHE_NAMESPACE_SEARCH, cache_ttl_seconds=CACHE_TTL_SECONDS, allow_stale_on_error=True)
    if isinstance(data, dict) and isinstance(data.get('results'), list):
        data = data.get('results')
    if not isinstance(data, list):
        raise ClawHubSkillError(f'Unexpected search response from {url}: expected list or object.results.')
    return [item for item in data if isinstance(item, dict)]


def fetch_skill_detail(slug: str, *, temp_root: Path) -> dict[str, Any]:
    url = skill_detail_url(slug)
    data = request_json(url, temp_root=temp_root, cache_namespace=CACHE_NAMESPACE_SKILL, cache_ttl_seconds=CACHE_TTL_SECONDS, allow_stale_on_error=True)
    if not isinstance(data, dict):
        raise ClawHubSkillError(f'Unexpected skill detail response from {url}: expected object.')
    return data


def stage_archive_download(url: str, *, temp_root: Path) -> Path:
    ensure_dir(temp_root)
    archive_dir = ensure_dir(temp_root / 'downloads')
    archive_path = archive_dir / f'{cache_key(url)}.zip'
    body = request_bytes(url, timeout=ZIP_TIMEOUT_SECONDS, accept='application/zip,application/octet-stream,*/*')
    archive_path.write_bytes(body)
    return archive_path


def unpack_archive(archive_path: Path, staging_dir: Path) -> Path:
    ensure_dir(staging_dir)
    with zipfile.ZipFile(archive_path) as zf:
        names = [name for name in zf.namelist() if not name.endswith('/')]
        if not names:
            raise ClawHubSkillError('Downloaded archive is empty.')
        zf.extractall(staging_dir)
    root_name = extract_archive_root(names)
    if root_name:
        candidate = staging_dir / root_name
        if candidate.exists() and candidate.is_dir():
            return candidate
    return staging_dir


def ensure_required_files(skill_root: Path) -> None:
    if not (skill_root / 'SKILL.md').is_file():
        raise ClawHubSkillError('Installed skill content is missing required file: SKILL.md')


def backup_existing(target_dir: Path, backup_root: Path) -> Path:
    ensure_dir(backup_root)
    stamp = datetime.now(UTC).strftime(BACKUP_TIMESTAMP_FORMAT)
    backup_dir = backup_root / f'{target_dir.name}-{stamp}'
    shutil.move(str(target_dir), str(backup_dir))
    return backup_dir


def install_from_clawhub(*, skill_data: dict[str, Any], version_data: dict[str, Any], skills_dir: Path, temp_root: Path, skill_id: str, force: bool) -> dict[str, Any]:
    source_url = str(version_data.get('sourceURL') or '').strip()
    if not source_url:
        raise ClawHubSkillError('Selected ClawHub version does not include a sourceURL.')
    staging_parent = Path(tempfile.mkdtemp(prefix='stage-', dir=str(ensure_dir(temp_root))))
    try:
        archive_path = stage_archive_download(source_url, temp_root=temp_root)
        extracted_root = unpack_archive(archive_path, staging_parent / 'unzipped')
        ensure_required_files(extracted_root)

        target_dir = ensure_dir(skills_dir) / skill_id
        backup_dir: Path | None = None
        if target_dir.exists():
            if not force:
                raise ClawHubSkillError(f'Target directory already exists: {target_dir}. Re-run with --force to replace it.')
            backup_dir = backup_existing(target_dir, temp_root / 'backups')

        shutil.copytree(extracted_root, target_dir, dirs_exist_ok=False)
        manifest_path = target_dir / 'resource.yaml'
        if manifest_path.exists():
            manifest = rewrite_manifest(manifest_path, skill_data=skill_data, version_data=version_data, skill_id=skill_id)
        else:
            page_url = build_skill_url(str(skill_data.get('slug') or ''), str(skill_data.get('user', {}).get('username') or '') or None)
            manifest = make_local_manifest(
                upstream_name=str(skill_data.get('name') or skill_id),
                description=strip_html(str(skill_data.get('shortDescription') or skill_data.get('description') or '')),
                skill_id=skill_id,
                source_url=source_url,
                skill_page_url=page_url,
                slug=str(skill_data.get('slug') or ''),
                owner=str(skill_data.get('user', {}).get('username') or '') or None,
                version=str(version_data.get('version') or ''),
            )
            manifest_path.write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding='utf-8')
        return {
            'skill_id': skill_id,
            'target_dir': str(target_dir),
            'backup_dir': str(backup_dir) if backup_dir else '',
            'installed_version': str(version_data.get('version') or ''),
            'manifest_path': str(manifest_path),
            'source_url': source_url,
            'page_url': build_skill_url(str(skill_data.get('slug') or ''), str(skill_data.get('user', {}).get('username') or '') or None),
        }
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)


def command_search(args: argparse.Namespace) -> dict[str, Any]:
    temp_root = Path(args.temp_root)
    items = fetch_search_results(
        query=args.query,
        limit=args.limit,
        highlighted_only=args.highlighted_only,
        non_suspicious_only=not args.include_suspicious,
        temp_root=temp_root,
    )
    results: list[dict[str, Any]] = []
    for item in items[: max(1, args.limit)]:
        moderation = item.get('moderation') or {}
        latest = item.get('latestVersion') or {}
        user = item.get('user') or {}
        results.append(
            {
                'slug': str(item.get('slug') or ''),
                'name': str(item.get('name') or ''),
                'owner': str(user.get('username') or ''),
                'description': strip_html(str(item.get('shortDescription') or item.get('description') or '')),
                'highlighted': coerce_bool(item.get('isHighlighted')),
                'download_count': int(item.get('downloadCount') or 0),
                'latest_version': str(latest.get('version') or ''),
                'updated_at': str(latest.get('updatedAt') or item.get('updatedAt') or ''),
                'suspicious': coerce_bool(moderation.get('isSuspicious')),
                'blocked': coerce_bool(moderation.get('isMalwareBlocked')),
                'url': build_skill_url(str(item.get('slug') or ''), str(user.get('username') or '') or None),
            }
        )
    return {
        'command': 'search',
        'query': args.query,
        'count': len(results),
        'results': results,
        'cache_dir': str(cache_root(temp_root)),
    }


def command_inspect(args: argparse.Namespace) -> dict[str, Any]:
    temp_root = Path(args.temp_root)
    skill = fetch_skill_detail(args.slug, temp_root=temp_root)
    versions = [item for item in (skill.get('versions') or []) if isinstance(item, dict)]
    latest = skill.get('latestVersion') or {}
    moderation = skill.get('moderation') or {}
    user = skill.get('user') or {}
    return {
        'command': 'inspect',
        'slug': str(skill.get('slug') or args.slug),
        'name': str(skill.get('name') or ''),
        'owner': str(user.get('username') or ''),
        'description': strip_html(str(skill.get('description') or skill.get('shortDescription') or '')),
        'latest_version': str(latest.get('version') or ''),
        'version_count': len(versions),
        'versions': [
            {
                'version': str(item.get('version') or ''),
                'created_at': str(item.get('createdAt') or ''),
                'source_url': str(item.get('sourceURL') or ''),
            }
            for item in versions
        ],
        'suspicious': coerce_bool(moderation.get('isSuspicious')),
        'blocked': coerce_bool(moderation.get('isMalwareBlocked')),
        'url': build_skill_url(str(skill.get('slug') or args.slug), str(user.get('username') or '') or None),
        'cache_dir': str(cache_root(temp_root)),
    }


def command_install(args: argparse.Namespace) -> dict[str, Any]:
    temp_root = Path(args.temp_root)
    skills_dir = Path(args.skills_dir)
    skill = fetch_skill_detail(args.slug, temp_root=temp_root)
    ensure_safe_status(skill, allow_suspicious=args.allow_suspicious)
    version_data = choose_version(skill, args.version)
    local_skill_id = normalize_skill_id(args.skill_id) if args.skill_id else slug_to_skill_id(str(skill.get('slug') or args.slug))
    install_info = install_from_clawhub(
        skill_data=skill,
        version_data=version_data,
        skills_dir=skills_dir,
        temp_root=temp_root,
        skill_id=local_skill_id,
        force=args.force,
    )
    return {
        'command': 'install',
        'slug': str(skill.get('slug') or args.slug),
        'requested_version': args.version,
        'installed_version': install_info['installed_version'],
        'skill_id': install_info['skill_id'],
        'target_dir': install_info['target_dir'],
        'backup_dir': install_info['backup_dir'],
        'manifest_path': install_info['manifest_path'],
        'source_url': install_info['source_url'],
        'page_url': install_info['page_url'],
        'cache_dir': str(cache_root(temp_root)),
    }


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    skills_dir = Path(args.skills_dir)
    temp_root = Path(args.temp_root)
    target_dir = skills_dir / args.skill_id
    manifest = load_local_manifest(target_dir)
    clawhub = extract_clawhub_meta(manifest)
    slug = str(clawhub.get('slug') or '').strip()
    if not slug:
        raise ClawHubSkillError(f'Local skill {args.skill_id!r} is not managed by ClawHub or is missing x_g3ku.clawhub.slug.')
    installed_version = str(clawhub.get('installed_version') or '').strip()
    skill = fetch_skill_detail(slug, temp_root=temp_root)
    latest = choose_version(skill, None)
    latest_version = str(latest.get('version') or '').strip()
    return {
        'command': 'status',
        'skill_id': args.skill_id,
        'slug': slug,
        'installed_version': installed_version,
        'latest_version': latest_version,
        'up_to_date': installed_version == latest_version and bool(installed_version),
        'page_url': str(clawhub.get('page_url') or build_skill_url(slug, str(clawhub.get('owner') or '') or None)),
        'cache_dir': str(cache_root(temp_root)),
    }


def command_update(args: argparse.Namespace) -> dict[str, Any]:
    skills_dir = Path(args.skills_dir)
    temp_root = Path(args.temp_root)
    target_dir = skills_dir / args.skill_id
    manifest = load_local_manifest(target_dir)
    clawhub = extract_clawhub_meta(manifest)
    slug = str(clawhub.get('slug') or '').strip()
    if not slug:
        raise ClawHubSkillError(f'Local skill {args.skill_id!r} is not managed by ClawHub or is missing x_g3ku.clawhub.slug.')

    installed_version = str(clawhub.get('installed_version') or '').strip()
    skill = fetch_skill_detail(slug, temp_root=temp_root)
    ensure_safe_status(skill, allow_suspicious=args.allow_suspicious)
    target_version_data = choose_version(skill, args.version)
    target_version = str(target_version_data.get('version') or '').strip()
    if installed_version and installed_version == target_version and not args.force:
        return {
            'command': 'update',
            'skill_id': args.skill_id,
            'slug': slug,
            'installed_version': installed_version,
            'target_version': target_version,
            'updated': False,
            'reason': 'already-current',
            'cache_dir': str(cache_root(temp_root)),
        }

    install_info = install_from_clawhub(
        skill_data=skill,
        version_data=target_version_data,
        skills_dir=skills_dir,
        temp_root=temp_root,
        skill_id=args.skill_id,
        force=True,
    )
    return {
        'command': 'update',
        'skill_id': args.skill_id,
        'slug': slug,
        'installed_version': installed_version,
        'target_version': install_info['installed_version'],
        'updated': True,
        'backup_dir': install_info['backup_dir'],
        'target_dir': install_info['target_dir'],
        'manifest_path': install_info['manifest_path'],
        'cache_dir': str(cache_root(temp_root)),
    }


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Search, inspect, install, and update ClawHub skills inside the current workspace.')
    parser.add_argument('--skills-dir', default=str(default_skills_dir()), help='Target skills directory. Defaults to <workspace>/skills.')
    parser.add_argument('--temp-root', default=str(default_temp_root()), help='Temporary directory used for staging, backups, and API cache.')

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
    try:
        if args.command == 'search':
            payload = command_search(args)
        elif args.command == 'inspect':
            payload = command_inspect(args)
        elif args.command in {'install', 'download'}:
            payload = command_install(args)
            payload['command'] = args.command
        elif args.command == 'status':
            payload = command_status(args)
        elif args.command == 'update':
            payload = command_update(args)
        else:
            raise ClawHubSkillError(f'Unsupported command: {args.command}')
    except ClawHubSkillError as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    payload['ok'] = True
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""Bounded GitHub archive ingestion. No repository code is executed."""
import hashlib
import json
import re
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import httpx

from .config import Settings
from .contracts import DomainError

SOURCE_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
IGNORE_DIRS = {".git", "node_modules", "vendor", "dist", "build", "out", "coverage", ".next", ".nuxt", ".cache", "__pycache__", ".venv", "venv", "target", ".idea", ".vscode", ".aws", ".ssh", "secrets"}
CONFIG_NAMES = {"package.json", "tsconfig.json", "jsconfig.json", "pnpm-workspace.yaml"}
SECRET_NAMES = {".npmrc", ".pypirc", "credentials", "credentials.json", "id_rsa", "id_ed25519", "service-account.json"}
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{24,})"),
    re.compile(r"(?i)(?:(?:api[_-]?key|secret|password|access[_-]?token)\s*[:=]\s*[\"'])([^\"'\r\n]{12,})(?:[\"'])"),
]


def normalize_url(value: str) -> tuple[str, str, str]:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or parsed.query or parsed.fragment:
        raise DomainError("INVALID_REPOSITORY_URL", "Enter a public repository URL such as https://github.com/owner/repository.")
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    match = re.fullmatch(r"/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]{1,100})", path)
    if not match or match.group(2) in {".", ".."}:
        raise DomainError("INVALID_REPOSITORY_URL", "Use a repository root URL; branch, file, credential, and custom-host URLs are not supported.")
    owner, name = match.groups()
    return owner, name, f"https://github.com/{owner}/{name}"


def github_metadata(url: str, settings: Settings) -> dict:
    owner, name, _ = normalize_url(url)
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Codebase-Navigator"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    try:
        with httpx.Client(timeout=30, follow_redirects=False, headers=headers) as client:
            response = client.get(f"https://api.github.com/repos/{owner}/{name}")
            if response.status_code == 404:
                raise DomainError("REPOSITORY_NOT_FOUND", "Repository was not found or is not publicly accessible.", 404)
            if response.status_code in {403, 429}:
                raise DomainError("GITHUB_RATE_LIMIT", "GitHub rate limited this request. Retry later or configure a server-side GitHub token.", 503)
            response.raise_for_status()
            repo = response.json()
            if repo.get("private") or repo.get("visibility", "public") != "public":
                raise DomainError("PRIVATE_REPOSITORY", "Only public GitHub repositories are supported.")
            canonical_owner, canonical_name, canonical_url = normalize_url(repo["html_url"])
            default_branch = repo["default_branch"]
            commit_response = client.get(f"https://api.github.com/repos/{canonical_owner}/{canonical_name}/commits/{default_branch}")
            commit_response.raise_for_status()
            sha = commit_response.json()["sha"]
            if not re.fullmatch(r"[a-f0-9]{40}", sha):
                raise DomainError("INVALID_GITHUB_RESPONSE", "GitHub returned an invalid commit identifier.", 502)
            return {"githubId": str(repo["id"]), "owner": canonical_owner, "name": canonical_name, "url": canonical_url, "defaultBranch": default_branch, "commitSha": sha}
    except httpx.HTTPError as error:
        raise DomainError("GITHUB_UNAVAILABLE", "Unable to fetch repository metadata from GitHub. Retry when GitHub is reachable.", 503) from error


def safe_archive_path(name: str) -> str | None:
    if "\\" in name or "\x00" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise DomainError("UNSAFE_ARCHIVE", "Archive contains an unsafe path.")
    parts = name.split("/")
    if any(part == ".." for part in parts):
        raise DomainError("UNSAFE_ARCHIVE", "Archive contains a parent-directory path.")
    parts = [part for part in parts if part not in {"", "."}]
    if len(parts) <= 1:
        return None
    return "/".join(parts[1:])


def eligible(path: str) -> bool:
    pure = PurePosixPath(path)
    lower = pure.name.lower()
    if any(part.lower() in IGNORE_DIRS for part in pure.parts):
        return False
    if lower.startswith(".env") or lower in SECRET_NAMES or lower.endswith((".pem", ".key", ".p12", ".pfx", ".map", ".min.js")):
        return False
    return pure.suffix.lower() in SOURCE_SUFFIXES or lower in CONFIG_NAMES or lower.startswith(("tsconfig.", "jsconfig.")) and lower.endswith(".json") or lower in {"readme.md", "architecture.md"}


def sanitize(content: str) -> tuple[str, bool]:
    redacted = False
    for pattern in SECRET_PATTERNS:
        def replace(match):
            nonlocal redacted
            redacted = True
            value = match.group(0)
            if match.lastindex:
                start, end = match.span(1)
                relative_start, relative_end = start - match.start(), end - match.start()
                return value[:relative_start] + "*" * (relative_end - relative_start) + value[relative_end:]
            return "".join(character if character in "\r\n" else "*" for character in value)
        content = pattern.sub(replace, content)
    return content, redacted


def make_file(path: str, raw: bytes, analysis_id: str):
    if b"\x00" in raw:
        return None
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    content, redacted = sanitize(content)
    suffix = PurePosixPath(path).suffix.lower()
    language = "typescript" if suffix in {".ts", ".tsx", ".mts", ".cts"} else "javascript" if suffix in SOURCE_SUFFIXES else "json" if suffix == ".json" else "markdown" if suffix == ".md" else "yaml"
    return {"id": f"file:{path}", "analysisId": analysis_id, "path": path, "language": language, "content": content, "lineCount": content.count("\n") + 1, "redacted": redacted, "contentHash": hashlib.sha256(content.encode()).hexdigest()}


def collect_archive(archive, analysis_id: str, settings: Settings, check_cancel=lambda: None):
    files, warnings, seen = [], [], set()
    expanded = retained = entries = 0
    try:
        with tarfile.open(fileobj=archive, mode="r|gz") as tar:
            for member in tar:
                check_cancel()
                entries += 1
                if entries > 50000:
                    raise DomainError("REPOSITORY_TOO_LARGE", "Archive exceeds the 50,000-entry limit.")
                path = safe_archive_path(member.name)
                if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                    raise DomainError("UNSAFE_ARCHIVE", "Archive contains links or special filesystem entries.")
                if member.isdir():
                    continue
                expanded += member.size
                if expanded > settings.max_expanded_bytes:
                    raise DomainError("REPOSITORY_TOO_LARGE", "Expanded archive exceeds the 500 MiB limit.")
                if not path or not eligible(path):
                    continue
                if path in seen:
                    raise DomainError("UNSAFE_ARCHIVE", "Archive contains duplicate file paths.")
                seen.add(path)
                if member.size > settings.max_file_bytes:
                    warnings.append(f"Skipped oversized file: {path}")
                    continue
                source = tar.extractfile(member)
                raw = source.read(settings.max_file_bytes + 1)
                file = make_file(path, raw, analysis_id)
                if file:
                    retained += len(raw)
                    files.append(file)
                    enforce_limits(files, retained, settings)
    except (tarfile.TarError, EOFError) as error:
        raise DomainError("INVALID_ARCHIVE", "Repository archive is invalid or incomplete.") from error
    return files, warnings


def enforce_limits(files, retained, settings):
    if len(files) > settings.max_files or retained > settings.max_text_bytes:
        raise DomainError("REPOSITORY_TOO_LARGE", "Repository exceeds the 2,000-file or 25 MiB retained-source limit. Analyze a smaller repository.")


def fetch_files(repo: dict, sha: str, analysis_id: str, settings: Settings, check_cancel=lambda: None):
    # codeload is a fixed GitHub host. Credentials are deliberately not forwarded.
    url = f"https://codeload.github.com/{repo['owner']}/{repo['name']}/tar.gz/{sha}"
    try:
        with tempfile.TemporaryFile() as archive, httpx.Client(timeout=httpx.Timeout(60, connect=20), follow_redirects=False) as client:
            with client.stream("GET", url, headers={"User-Agent": "Codebase-Navigator"}) as response:
                response.raise_for_status()
                length = 0
                for block in response.iter_bytes(65536):
                    check_cancel()
                    length += len(block)
                    if length > settings.max_archive_bytes:
                        raise DomainError("REPOSITORY_TOO_LARGE", "Compressed archive exceeds the 100 MiB limit.")
                    archive.write(block)
            archive.seek(0)
            return collect_archive(archive, analysis_id, settings, check_cancel)
    except httpx.HTTPError as error:
        raise DomainError("GITHUB_UNAVAILABLE", "Failed to download the pinned GitHub archive. Retry when GitHub is reachable.", 503) from error


def demo_files(directory: Path, analysis_id: str, settings: Settings):
    if not directory.is_dir():
        raise DomainError("DEMO_MISSING", "The bundled sample repository was not found.", 503)
    files, retained = [], 0
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise DomainError("UNSAFE_DEMO", "Sample repository contains a symbolic link.")
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        if not eligible(relative) or path.stat().st_size > settings.max_file_bytes:
            continue
        raw = path.read_bytes()
        file = make_file(relative, raw, analysis_id)
        if file:
            files.append(file)
            retained += len(raw)
            enforce_limits(files, retained, settings)
    return files, []


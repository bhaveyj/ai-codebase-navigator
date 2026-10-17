"""Bounded GitHub archive ingestion. No repository code is executed."""
import hashlib
import json
import re
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

import httpx

from .config import Settings
from .contracts import DomainError

SOURCE_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".py", ".pyi", ".java"}
IGNORE_DIRS = {".git", "node_modules", "vendor", "dist", "build", "out", "coverage", ".next", ".nuxt", ".cache", "__pycache__", ".venv", "venv", "target", ".idea", ".vscode", ".aws", ".ssh", "secrets"}
CONFIG_NAMES = {"package.json", "tsconfig.json", "jsconfig.json", "pnpm-workspace.yaml", "pyproject.toml", "requirements.txt", "pipfile", "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"}
SECRET_NAMES = {".npmrc", ".pypirc", "credentials", "credentials.json", "id_rsa", "id_ed25519", "service-account.json"}
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{24,})"),
    re.compile(r"(?i)(?:(?:api[_-]?key|secret|password|access[_-]?token)\s*[:=]\s*[\"'])([^\"'\r\n]{12,})(?:[\"'])"),
    re.compile(r"(?i)<(?:api[_-]?key|secret|password|token)>\s*([^<\r\n]{12,})\s*</(?:api[_-]?key|secret|password|token)>"),
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


def github_metadata(url: str, settings: Settings, ref: str | None = None) -> dict:
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
            revision = (ref or default_branch).strip()
            if not revision or any(ord(char) < 32 for char in revision):
                raise DomainError("INVALID_REVISION", "Enter a branch, tag, commit SHA, or PR #number.")
            pull = re.fullmatch(r"(?:PR\s*#?|#|pull/)([1-9][0-9]*)", revision, re.I)
            if pull:
                pull_response = client.get(f"https://api.github.com/repos/{canonical_owner}/{canonical_name}/pulls/{pull.group(1)}")
                if pull_response.status_code == 404:
                    raise DomainError("REVISION_NOT_FOUND", "That pull request was not found.", 404)
                pull_response.raise_for_status()
                revision = pull_response.json()["head"]["sha"]
            commit_response = client.get(f"https://api.github.com/repos/{canonical_owner}/{canonical_name}/commits/{quote(revision, safe='')}")
            if commit_response.status_code == 404 or commit_response.status_code == 422:
                raise DomainError("REVISION_NOT_FOUND", "That branch, tag, or commit was not found.", 404)
            commit_response.raise_for_status()
            sha = commit_response.json()["sha"]
            if not re.fullmatch(r"[a-f0-9]{40}", sha):
                raise DomainError("INVALID_GITHUB_RESPONSE", "GitHub returned an invalid commit identifier.", 502)
            return {"githubId": str(repo["id"]), "owner": canonical_owner, "name": canonical_name, "url": canonical_url, "defaultBranch": default_branch, "commitSha": sha}
    except httpx.HTTPError as error:
        raise DomainError("GITHUB_UNAVAILABLE", "Unable to fetch repository metadata from GitHub. Retry when GitHub is reachable.", 503) from error


def github_commits(url: str, branch: str, settings: Settings, page: int, per_page: int = 30) -> dict:
    """Read one page of public commit history without fetching repository contents."""
    owner, name, _ = normalize_url(url)
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Codebase-Navigator"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    try:
        with httpx.Client(timeout=30, follow_redirects=False, headers=headers) as client:
            response = client.get(f"https://api.github.com/repos/{owner}/{name}/commits", params={"sha": branch, "page": page, "per_page": per_page})
            if response.status_code == 404:
                raise DomainError("REPOSITORY_NOT_FOUND", "Repository commit history was not found or is not publicly accessible.", 404)
            if response.status_code in {403, 429}:
                raise DomainError("GITHUB_RATE_LIMIT", "GitHub rate limited commit history. Retry later or configure a server-side GitHub token.", 503)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, list):
                raise DomainError("INVALID_GITHUB_RESPONSE", "GitHub returned an invalid commit history.", 502)
            commits = []
            for item in result:
                sha = item.get("sha", "")
                if not re.fullmatch(r"[a-f0-9]{40}", sha):
                    raise DomainError("INVALID_GITHUB_RESPONSE", "GitHub returned an invalid commit identifier.", 502)
                commit = item.get("commit") or {}
                author = commit.get("author") or {}
                committer = commit.get("committer") or {}
                commits.append({"sha": sha, "message": str(commit.get("message") or "Untitled commit").splitlines()[0][:240],
                                "author": str(author.get("name") or committer.get("name") or "Unknown author")[:100],
                                "committedAt": str(committer.get("date") or author.get("date") or "")})
            return {"commits": commits, "page": page, "hasMore": 'rel="next"' in response.headers.get("link", ""), "branch": branch}
    except httpx.HTTPError as error:
        raise DomainError("GITHUB_UNAVAILABLE", "Unable to fetch commit history from GitHub. Retry when GitHub is reachable.", 503) from error


def _changed_file(item: dict) -> dict:
    if not isinstance(item, dict):
        raise DomainError("INVALID_GITHUB_RESPONSE", "GitHub returned invalid changed-file details.", 502)
    patch = item.get("patch")
    cleaned, redacted = sanitize(str(patch)[:12000]) if patch is not None else (None, False)
    return {"filename": str(item.get("filename") or "")[:2048],
            "previousFilename": str(item["previous_filename"])[:2048] if item.get("previous_filename") else None,
            "status": str(item.get("status") or "modified"),
            "additions": int(item.get("additions") or 0), "deletions": int(item.get("deletions") or 0),
            "patch": cleaned, "patchTruncated": bool(patch and len(patch) > 12000), "redacted": redacted}


def github_commit_changes(url: str, sha: str, settings: Settings, page: int = 1) -> dict:
    """Return a bounded page of the files changed by one public commit."""
    owner, name, _ = normalize_url(url)
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise DomainError("INVALID_REVISION", "Choose a commit from the repository history.")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Codebase-Navigator"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    try:
        with httpx.Client(timeout=30, follow_redirects=False, headers=headers) as client:
            response = client.get(f"https://api.github.com/repos/{owner}/{name}/commits/{sha}", params={"page": page, "per_page": 100})
            if response.status_code == 404:
                raise DomainError("REVISION_NOT_FOUND", "That commit was not found in this repository.", 404)
            if response.status_code in {403, 429}:
                raise DomainError("GITHUB_RATE_LIMIT", "GitHub rate limited commit details. Retry later or configure a server-side GitHub token.", 503)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get("sha") != sha or not isinstance(data.get("files"), list):
                raise DomainError("INVALID_GITHUB_RESPONSE", "GitHub returned invalid commit details.", 502)
            files = [_changed_file(item) for item in data["files"]]
            return {"sha": sha, "message": str((data.get("commit") or {}).get("message") or "Untitled commit").splitlines()[0][:240],
                    "url": f"https://github.com/{owner}/{name}/commit/{sha}", "files": files,
                    "page": page, "hasMore": 'rel="next"' in response.headers.get("link", "")}
    except httpx.HTTPError as error:
        raise DomainError("GITHUB_UNAVAILABLE", "Unable to fetch commit details from GitHub. Retry when GitHub is reachable.", 503) from error


def github_compare_commits(url: str, selected_sha: str, current_sha: str, settings: Settings) -> dict:
    """Compare two public GitHub revisions directly, without creating an analysis job."""
    owner, name, _ = normalize_url(url)
    if not all(re.fullmatch(r"[a-f0-9]{40}", sha) for sha in (selected_sha, current_sha)):
        raise DomainError("INVALID_REVISION", "Choose a commit from the repository history.")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Codebase-Navigator"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    try:
        with httpx.Client(timeout=30, follow_redirects=False, headers=headers) as client:
            base, head = selected_sha, current_sha
            for _ in range(2):
                endpoint = f"https://api.github.com/repos/{owner}/{name}/compare/{base}...{head}"
                response = client.get(endpoint, params={"page": 1, "per_page": 100})
                if response.status_code in {404, 422}:
                    raise DomainError("REVISION_NOT_FOUND", "These commits could not be compared in this repository.", 404)
                if response.status_code in {403, 429}:
                    raise DomainError("GITHUB_RATE_LIMIT", "GitHub rate limited commit comparison. Retry later or configure a server-side GitHub token.", 503)
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict) or not isinstance(data.get("files"), list):
                    raise DomainError("INVALID_GITHUB_RESPONSE", "GitHub returned an invalid commit comparison.", 502)
                if data.get("status") != "behind":
                    files = [_changed_file(item) for item in data["files"]]
                    return {"selectedSha": selected_sha, "currentSha": current_sha,
                            "baseSha": base, "headSha": head, "status": str(data.get("status") or "unknown"),
                            "files": files, "possiblyTruncated": len(files) >= 300,
                            "url": f"https://github.com/{owner}/{name}/compare/{base}...{head}"}
                base, head = head, base
            raise DomainError("INVALID_GITHUB_RESPONSE", "GitHub could not determine the comparison direction.", 502)
    except httpx.HTTPError as error:
        raise DomainError("GITHUB_UNAVAILABLE", "Unable to compare commits on GitHub. Retry when GitHub is reachable.", 503) from error


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
    return (pure.suffix.lower() in SOURCE_SUFFIXES or lower in CONFIG_NAMES
            or lower.startswith(("tsconfig.", "jsconfig.")) and lower.endswith(".json")
            or lower.startswith("requirements-") and lower.endswith(".txt")
            or lower in {"readme.md", "architecture.md"})


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
    language = ("typescript" if suffix in {".ts", ".tsx", ".mts", ".cts"} else
                "python" if suffix in {".py", ".pyi"} else "java" if suffix == ".java" else
                "javascript" if suffix in SOURCE_SUFFIXES else "json" if suffix == ".json" else
                "markdown" if suffix == ".md" else "xml" if suffix == ".xml" else
                "toml" if suffix == ".toml" or pure_name(path) == "pipfile" else
                "yaml" if suffix in {".yaml", ".yml"} else "text" if suffix == ".txt" else
                "kotlin" if suffix == ".kts" else "groovy")
    return {"id": f"file:{path}", "analysisId": analysis_id, "path": path, "language": language, "content": content, "lineCount": content.count("\n") + 1, "redacted": redacted, "contentHash": hashlib.sha256(content.encode()).hexdigest()}


def pure_name(path: str) -> str:
    return PurePosixPath(path).name.lower()


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


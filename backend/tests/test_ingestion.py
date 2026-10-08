import io
import tarfile

import pytest

from navigator.config import Settings
from navigator.contracts import DomainError
from navigator.ingestion import collect_archive, eligible, make_file, normalize_url, safe_archive_path, sanitize


@pytest.mark.parametrize("value", ["http://github.com/a/b", "https://github.com.evil/a/b", "https://user:pass@github.com/a/b", "https://github.com/a/b/tree/main", "https://github.com/a/b?token=secret", "https://127.0.0.1/a/b", "file:///etc/passwd", "https://github.com/a/.."])
def test_only_public_github_repository_roots(value):
    with pytest.raises(DomainError):
        normalize_url(value)


def test_normalizes_repository_suffix():
    assert normalize_url(" https://github.com/owner/repo.git/ ") == ("owner", "repo", "https://github.com/owner/repo")


@pytest.mark.parametrize("path", ["root/../outside.ts", "/root/a.ts", "C:/root/a.ts", "root\\a.ts", "root/a\0.ts"])
def test_archive_traversal_rejected(path):
    with pytest.raises(DomainError, match="Archive"):
        safe_archive_path(path)


@pytest.mark.parametrize("path", [".git/config", "node_modules/pkg/index.js", "dist/app.js", ".env.example", "secrets/a.ts", "src/key.pem", "src/bundle.min.js", "src/a.ts.map", ".aws/credentials", "logo.png", "src/a.py"])
def test_ignored_paths(path):
    assert not eligible(path)


def test_secret_redaction_preserves_source_lines():
    source = 'const password = "very-private-password";\nconst key = "AIza' + "a" * 35 + '";\n'
    redacted, changed = sanitize(source)
    assert changed and "very-private-password" not in redacted and "AIza" not in redacted
    assert source.count("\n") == redacted.count("\n")
    assert len(source) == len(redacted)
    assert make_file("src/a.ts", b"\x00binary", "a") is None


def archive(entries):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as tar:
        for name, content, kind in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(content)
            if kind == tarfile.SYMTYPE:
                member.linkname = "../../outside.ts"
            tar.addfile(member, io.BytesIO(content))
    result.seek(0)
    return result


def test_archive_is_never_extracted_and_limits_retained_text():
    config = Settings(_env_file=None, max_files=1)
    data = archive([("repo/src/a.ts", b"export const a=1", tarfile.REGTYPE), ("repo/node_modules/b.js", b"ignored", tarfile.REGTYPE)])
    files, _ = collect_archive(data, "a", config)
    assert [file["path"] for file in files] == ["src/a.ts"]
    data = archive([("repo/a.ts", b"a", tarfile.REGTYPE), ("repo/b.ts", b"b", tarfile.REGTYPE)])
    with pytest.raises(DomainError) as error:
        collect_archive(data, "a", config)
    assert error.value.code == "REPOSITORY_TOO_LARGE"


@pytest.mark.parametrize("entries", [
    [("repo/a.ts", b"", tarfile.SYMTYPE)],
    [("repo/a.ts", b"a", tarfile.REGTYPE), ("repo/a.ts", b"b", tarfile.REGTYPE)],
    [("repo/../a.ts", b"a", tarfile.REGTYPE)],
])
def test_links_duplicate_paths_and_traversal_rejected(entries):
    with pytest.raises(DomainError) as error:
        collect_archive(archive(entries), "a", Settings(_env_file=None))
    assert error.value.code == "UNSAFE_ARCHIVE"

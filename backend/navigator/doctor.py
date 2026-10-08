"""Safe setup checks: python -m navigator.doctor [--services] [--initialize]."""

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass

from .config import Settings, get_settings


@dataclass
class Check:
    name: str
    status: str
    message: str


def runtime_checks(settings: Settings) -> list[Check]:
    checks = [Check("python", "ok" if sys.version_info >= (3, 12) else "error", "Python 3.12 or newer is required.")]
    node_ready = False
    try:
        result = subprocess.run([settings.node_binary, "--version"], capture_output=True, text=True, timeout=10, check=True)
        match = re.fullmatch(r"v(\d+)\.\d+\.\d+\s*", result.stdout)
        node_ready = bool(match and int(match.group(1)) >= 24)
        checks.append(Check("node", "ok" if node_ready else "error", "Node.js 24 or newer is required; set NODE_BINARY to its executable."))
    except (OSError, subprocess.SubprocessError):
        checks.append(Check("node", "error", "Node runtime is unavailable; install Node.js 24 and set NODE_BINARY."))

    if not settings.analyzer.is_file():
        checks.append(Check("analyzer", "error", "Configured TypeScript analyzer entry point is missing."))
    elif node_ready:
        try:
            subprocess.run(
                [settings.node_binary, "--input-type=module", "-e", "await import('typescript');"],
                cwd=settings.analyzer.parent,
                capture_output=True,
                timeout=15,
                check=True,
            )
            checks.append(Check("analyzer", "ok", "TypeScript analyzer and its compiler dependency are available."))
        except (OSError, subprocess.SubprocessError):
            checks.append(Check("analyzer", "error", "TypeScript compiler dependency is unavailable; install the pnpm workspace dependencies."))
    else:
        checks.append(Check("analyzer", "skip", "Analyzer dependency check requires the configured Node.js runtime."))

    for name, configured in (("MONGODB_URI", bool(settings.mongodb_uri.strip())), ("GEMINI_API_KEY", bool(settings.gemini_api_key.strip()))):
        status = "ok" if configured else "error" if settings.mode == "production" else "skip"
        message = "Configured; credential values are never displayed." if configured else "Missing; add this value to the project .env for full Atlas/Gemini functionality."
        checks.append(Check(name, status, message))
    checks.append(Check("mode", "ok", "Full Atlas and background-worker mode." if settings.mode == "production" else "Local graph preview; Atlas retrieval and Gemini answers are disabled."))
    return checks


def service_checks(settings: Settings, initialize: bool = False) -> list[Check]:
    """Probe configured services without ever exposing provider exception details."""
    checks = []
    if settings.mongodb_uri.strip():
        from .storage import MongoStore, create_indexes

        store = None
        try:
            store = MongoStore(settings)
            store.client.admin.command("ping")
            # Exercise database read permission in addition to server reachability.
            store.db.list_collection_names()
            checks.append(Check("mongodb", "ok", "MongoDB is reachable and the database can be read."))
            if initialize:
                create_indexes(store, settings, search=True)
                checks.append(Check("atlas_indexes", "ok", "Collection and search indexes requested; search indexes may need time to become queryable."))
            else:
                checks.append(Check("atlas_indexes", "skip", "Run with --initialize to create required database and search indexes."))
        except Exception:
            checks.append(Check("mongodb", "error", "MongoDB check failed. Verify the URI, database permissions, Atlas network access, and search-index privileges if initializing."))
        finally:
            if store is not None:
                store.close()
    else:
        checks.append(Check("mongodb", "error" if settings.mode == "production" or initialize else "skip", "MONGODB_URI is required for the Atlas check."))

    if settings.mode == "production":
        try:
            import redis

            with redis.Redis.from_url(settings.redis_url, socket_timeout=5, socket_connect_timeout=5) as connection:
                connection.ping()
            checks.append(Check("redis", "ok", "Redis is reachable."))
        except Exception:
            checks.append(Check("redis", "error", "Redis check failed. Start Redis and verify REDIS_URL."))
    else:
        checks.append(Check("redis", "skip", "Local graph preview uses an in-process worker."))

    if settings.gemini_api_key.strip():
        from google import genai
        from google.genai import types

        client = None
        try:
            client = genai.Client(api_key=settings.gemini_api_key, http_options=types.HttpOptions(timeout=10000))
            client.models.get(model=settings.gemini_model)
            checks.append(Check("gemini_model", "ok", "Configured generation-model metadata is accessible."))
            client.models.get(model=settings.embedding_model)
            checks.append(Check("embedding_model", "ok", "Configured embedding-model metadata is accessible. No text or embeddings were generated."))
        except Exception:
            checks.append(Check("gemini", "error", "Gemini model check failed. Verify the key, configured model IDs, project access, and network connectivity."))
        finally:
            if client is not None:
                client.close()
    else:
        checks.append(Check("gemini", "error" if settings.mode == "production" else "skip", "GEMINI_API_KEY is required for the model metadata check."))
    return checks


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--services", action="store_true", help="Probe configured MongoDB, Redis, and Gemini model metadata without generating content.")
    parser.add_argument("--initialize", action="store_true", help="Also create MongoDB collections/indexes and request Atlas Search indexes (implies --services).")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable status without credential values.")
    args = parser.parse_args(argv)
    try:
        settings = get_settings()
    except Exception:
        checks = [Check("configuration", "error", "Configuration is invalid. Check the project .env field names and value types.")]
    else:
        checks = runtime_checks(settings)
        if args.services or args.initialize:
            checks.extend(service_checks(settings, initialize=args.initialize))
    if args.json:
        print(json.dumps({"checks": [asdict(check) for check in checks]}, indent=2))
    else:
        for check in checks:
            print(f"[{check.status.upper()}] {check.name}: {check.message}")
    return 1 if any(check.status == "error" for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())

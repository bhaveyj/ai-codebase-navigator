"""Run the local graph explorer with installed project dependencies.

Usage: .venv/Scripts/python.exe scripts/dev.py
       NODE_BINARY=/path/to/node uv run --project backend python scripts/dev.py
The --production option requires configured Atlas, Redis, and a separate Celery worker.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--production', action='store_true')
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    env = os.environ.copy()
    env['NAVIGATOR_MODE'] = 'production' if args.production else 'local'
    env.setdefault('NAVIGATOR_DATA_DIR', str(ROOT / '.navigator'))
    env.setdefault('NAVIGATOR_ANALYZER', str(ROOT / 'analyzers/typescript/src/cli.mjs'))
    env.setdefault('NAVIGATOR_DEMO_DIR', str(ROOT / 'tests/fixtures/shop'))
    node = env.get('NODE_BINARY') or shutil.which('node')
    if not node:
        raise SystemExit('Node.js is required. Install Node 24 or set NODE_BINARY.')
    env['NODE_BINARY'] = node
    vite = ROOT / 'apps/web/node_modules/vite/bin/vite.js'
    if not vite.exists():
        raise SystemExit('Install dependencies first: pnpm --dir apps/web install')
    print(f"Navigator: {env['NAVIGATOR_MODE']} mode. Open http://127.0.0.1:5173", flush=True)
    commands = [
        ([sys.executable, '-m', 'uvicorn', 'navigator.api.main:app', '--host', '127.0.0.1', '--port', '8000'], ROOT / 'backend'),
        ([node, str(vite), '--host', '127.0.0.1', '--port', '5173', '--strictPort'], ROOT / 'apps/web'),
    ]
    processes: list[subprocess.Popen] = []
    try:
        for command, cwd in commands:
            processes.append(subprocess.Popen(command, cwd=cwd, env=env))
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
        failed = next((process.returncode for process in processes if process.returncode), 1)
        raise SystemExit(failed)
    except KeyboardInterrupt:
        pass
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == '__main__':
    main()

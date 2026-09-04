#!/usr/bin/env python3
"""
SpiderPanel Python-only installer/launcher.

Start command:
    python install.py

This script is designed for normal Python hosting platforms where Docker,
apt/yum, systemd and root access are unavailable. It creates a local virtual
environment, installs requirements from PyPI, verifies the application, then
executes Uvicorn in the foreground.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PYTHON = VENV / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
PIP = VENV / ("Scripts" if os.name == "nt" else "bin") / ("pip.exe" if os.name == "nt" else "pip")
REQUIREMENTS = ROOT / "requirements.txt"
STAMP = VENV / ".spider_requirements.sha256"

DEFAULT_HOST = os.getenv("HOST", "0.0.0.0")
DEFAULT_PORT = os.getenv("PORT", "8080")


def log(message: str) -> None:
    print(f"[SpiderPanel] {message}", flush=True)


def fail(message: str, code: int = 1) -> None:
    print(f"[SpiderPanel][ERROR] {message}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def run(*args: str, env: dict[str, str] | None = None) -> None:
    log("$ " + " ".join(args))
    try:
        subprocess.run(list(args), cwd=ROOT, env=env, check=True)
    except FileNotFoundError as exc:
        fail(f"Command not found: {args[0]}")
    except subprocess.CalledProcessError as exc:
        fail(f"Command failed with exit code {exc.returncode}: {' '.join(args)}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_files() -> None:
    for path in (ROOT / "main.py", REQUIREMENTS):
        if not path.is_file():
            fail(f"Required file is missing: {path.name}")


def ensure_venv() -> None:
    if PYTHON.exists() and PIP.exists():
        return

    log(f"Creating local virtual environment: {VENV}")
    try:
        builder = venv.EnvBuilder(with_pip=True, clear=False, upgrade=False)
        builder.create(VENV)
    except Exception as exc:
        fail(
            "Could not create a local virtual environment. "
            "Make sure the hosting provider exposes Python venv/ensurepip. "
            f"Details: {exc}"
        )

    if not PYTHON.exists():
        fail(f"Virtual environment Python was not created: {PYTHON}")


def install_requirements() -> None:
    wanted = sha256_file(REQUIREMENTS)
    installed = STAMP.read_text(encoding="utf-8").strip() if STAMP.exists() else ""

    if installed == wanted:
        try:
            subprocess.run(
                [str(PYTHON), "-c", "import fastapi, uvicorn, httpx, websockets, aiofiles, qrcode, PIL, psutil, cryptography, socks"],
                cwd=ROOT,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log("Python dependencies are already installed.")
            return
        except subprocess.CalledProcessError:
            log("Dependency verification failed; reinstalling requirements.")

    log("Upgrading pip/setuptools/wheel...")
    run(str(PYTHON), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel")

    log("Installing requirements from PyPI...")
    # Use the normal PyPI index explicitly. This avoids the dependency-index
    # selection issue seen with uv on some hosted builders.
    try:
        run(
            str(PYTHON),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--index-url",
            "https://pypi.org/simple",
            "--only-binary=:all:",
            "-r",
            str(REQUIREMENTS),
        )
    except SystemExit:
        log("Binary-only install was not possible; retrying with normal pip resolution...")
        run(
            str(PYTHON),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--index-url",
            "https://pypi.org/simple",
            "-r",
            str(REQUIREMENTS),
        )

    STAMP.write_text(wanted + "\n", encoding="utf-8")
    log("Python dependencies installed successfully.")


def verify_app() -> None:
    log("Checking Python source files...")
    run(str(PYTHON), "-m", "py_compile", "main.py", "telegram_proxy.py", "relay_vless.py", "shared.py", "pages.py")

    code = "import fastapi, uvicorn, httpx, websockets, aiofiles, qrcode, PIL, psutil, cryptography, socks; print('dependency-check: OK')"
    run(str(PYTHON), "-c", code)


def start_server() -> None:
    host = os.getenv("HOST", DEFAULT_HOST)
    port = os.getenv("PORT", DEFAULT_PORT)

    try:
        int(port)
    except ValueError:
        fail(f"Invalid PORT value: {port!r}")

    # Keep the application process in the foreground so platforms such as
    # Railway/Render/Koyeb/PaaS can monitor it correctly.
    log(f"Starting SpiderPanel on {host}:{port}")
    log(f"Python: {platform.python_version()} ({platform.system()} {platform.machine()})")

    os.execv(
        str(PYTHON),
        [
            str(PYTHON),
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            host,
            "--port",
            str(port),
        ],
    )


def main() -> None:
    check_files()

    # Allow a quick administrative check without changing the default
    # one-command deployment behavior.
    if "--check" in sys.argv:
        ensure_venv()
        install_requirements()
        verify_app()
        log("Installation check completed successfully.")
        return

    ensure_venv()
    install_requirements()
    verify_app()
    start_server()


if __name__ == "__main__":
    main()

"""Configure Windows analysis and supervise the unchanged API plus media service."""
import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def configure(host, media_port, device):
    from src.service.video_analysis.platform_runtime import media_root, scene_draft_root

    python = Path(os.environ.get("SCENE_PYTHON", ""))
    if not python.is_file():
        raise RuntimeError("Set SCENE_PYTHON to capcut-analysis/python.exe, or use start.bat")
    public = f"http://{host}:{media_port}"
    internal = f"http://127.0.0.1:{media_port}"
    defaults = {
        "SCENE_MEDIA_ROOT": str(media_root()),
        "SCENE_SPLIT_ROOT": str(ROOT / "output" / "scene-clips"),
        "SCENE_DRAFT_CLIP_ROOT": str(scene_draft_root()),
        "SCENE_WHISPER_ROOT": str(ROOT / "models" / "whisper-windows"),
        "SCENE_WHISPER_MODEL_PATH": str(ROOT / "models" / "faster-whisper-large-v3"),
        "SCENE_WHISPER_DEVICE": device,
        "DRAFT_MEDIA_BASE_URL": internal + "/media",
        "SCRIPT_VIDEO_MEDIA_URL_BASE": public + "/media",
        "DRAFT_SCENE_CLIP_BASE_URL": internal + "/output/analysis-clips",
        "DRAFT_SCENE_CLIP_PUBLIC_BASE_URL": public + "/output/analysis-clips",
        "SCENE_ALLOWED_MEDIA_HOSTS": f"127.0.0.1:{media_port},{host}:{media_port}",
        "DRAFT_URL": f"http://{host}:30000/openapi/capcut-mate/v1/get_draft",
        "DOWNLOAD_URL": public + "/",
        "PYTHONUTF8": "1",
        "SCENE_SPLIT_TIMEOUT_SECONDS": "900",
        "SCENE_MEDIA_COMMAND_TIMEOUT_SECONDS": "840",
        "SCENE_TRANSCRIBE_TIMEOUT_SECONDS": "1800",
    }
    for name, value in defaults.items():
        os.environ.setdefault(name, value)
    prefix = python.resolve().parent
    os.environ["PATH"] = os.pathsep.join([
        str(prefix / "Library" / "bin"), str(prefix / "Scripts"),
        os.environ.get("PATH", ""),
    ])
    for name in ("SCENE_MEDIA_ROOT", "SCENE_SPLIT_ROOT", "SCENE_DRAFT_CLIP_ROOT", "SCENE_WHISPER_ROOT"):
        path = Path(os.environ[name])
        path.mkdir(parents=True, exist_ok=True)
        import tempfile
        with tempfile.TemporaryFile(dir=path):
            pass
    return str(python)


def check_port(port):
    with socket.socket() as sock:
        sock.bind(("0.0.0.0", port))


def wait_media(child, port):
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("Analysis media server exited during startup")
        try:
            opener.open(f"http://127.0.0.1:{port}/", timeout=1).close()
            return
        except HTTPError as exc:
            if exc.code == 404:
                return
            raise
        except (URLError, TimeoutError):
            time.sleep(0.2)
    raise RuntimeError("Analysis media server startup timed out")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--public-host", default="127.0.0.1")
    parser.add_argument("--media-port", type=int, default=30001)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--transcribe-sample", help="Optional inference self-test using a short sample")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This launcher is for Windows only; Docker deployment is unchanged")
    if not (1 <= args.media_port <= 65535) or args.media_port == 30000:
        parser.error("Use a media port between 1 and 65535, different from API port 30000")
    if not args.public_host or any(c in args.public_host for c in "/\\:@?# \t"):
        parser.error("--public-host must be an IPv4 address or hostname, without a scheme or port")
    os.chdir(ROOT)
    python = configure(args.public_host, args.media_port, args.device)
    from src.service.video_analysis.platform_runtime import WindowsWorkerJob
    # Import/Job support is checked before starting either service.
    job = WindowsWorkerJob()
    job.close()
    command = [python, str(Path(__file__).with_name("check_worker.py"))]
    if args.transcribe_sample:
        command.extend(["--transcribe-sample", args.transcribe_sample])
    subprocess.run(command, check=True)
    if args.check_only:
        return
    check_port(30000)
    check_port(args.media_port)
    print(f"API: http://{args.public_host}:30000", flush=True)
    print(f"Analysis media: http://{args.public_host}:{args.media_port}", flush=True)
    print("Basic preflight is not a GPU inference test. Use --transcribe-sample to verify large-v3.", flush=True)
    children = []
    try:
        children.append(subprocess.Popen([
            sys.executable, "-m", "uvicorn",
            "src.service.video_analysis.media_server:create_app", "--factory",
            "--host", "0.0.0.0", "--port", str(args.media_port),
        ]))
        wait_media(children[0], args.media_port)
        children.append(subprocess.Popen([sys.executable, str(ROOT / "main.py")]))
        while all(child.poll() is None for child in children):
            time.sleep(0.5)
        if any(child.poll() not in (None, 0) for child in children):
            raise RuntimeError("API or analysis media server exited with an error")
    except KeyboardInterrupt:
        pass
    finally:
        for child in reversed(children):
            if child.poll() is None:
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                               capture_output=True, check=False)
            child.wait()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Analysis startup failed: {exc}", file=sys.stderr)
        sys.exit(1)

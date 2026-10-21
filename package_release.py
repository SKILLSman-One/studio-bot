"""Build a source release using an explicit allowlist; never copy runtime secrets."""

import hashlib
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent
FILES = (
    "README.md",
    "START-HERE.md",
    "VERIFICATION.md",
    "requirements.in",
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "studio.toml",
    "compose.yaml",
    "Dockerfile",
    ".dockerignore",
    ".env.example",
    ".gitignore",
    "Install.cmd",
    "Setup.cmd",
    "Start.cmd",
    "Check.cmd",
    "RunCommand.cmd",
    "ChangePassword.cmd",
    "CheckGoogle.cmd",
    "Verify.cmd",
    "bootstrap.py",
    "Server.sh",
    "package_release.py",
)


def build(root=ROOT, target=None):
    target = target or root.parent / "Studio-Bot-MVP.zip"
    files = [root / name for name in FILES]
    files += sorted((root / "studio_bot").glob("*.py"))
    files += sorted((root / "tests").glob("*.py"))
    files += sorted((root / "assets" / "rooms").glob("*.png"))
    contents = {}
    for path in files:
        data = path.read_bytes()
        if not data:
            raise ValueError("Empty release file: " + path.name)
        if path.suffix == ".cmd":
            data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        elif path.suffix == ".sh":
            data = data.replace(b"\r\n", b"\n")
        contents[path.relative_to(root).as_posix()] = data
    manifest = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(contents.items())
    )
    contents["MANIFEST.sha256"] = manifest.encode("utf-8")
    temporary = target.with_suffix(".tmp")
    with ZipFile(temporary, "w", ZIP_DEFLATED) as bundle:
        for name, data in sorted(contents.items()):
            bundle.writestr("studio-bot/" + name, data)
    with ZipFile(temporary) as bundle:
        if bundle.testzip() is not None:
            raise ValueError("Corrupt ZIP")
    temporary.replace(target)
    print(f"{target.name}: {len(contents)} files, {target.stat().st_size} bytes")
    print("SHA256: " + hashlib.sha256(target.read_bytes()).hexdigest())
    return target


if __name__ == "__main__":
    build(target=Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None)

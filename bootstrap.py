"""Install and launch with the standard library; works before dependencies exist."""

import hashlib
import importlib.metadata
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MIN_PYTHON = (3, 11)


def environment_python(root=ROOT):
    return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def verify_bundle(root=ROOT):
    manifest = root / "MANIFEST.sha256"
    if not manifest.is_file():
        print("Нет MANIFEST.sha256. Для проверки архива распакуйте полный выпуск.")
        return False
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
        if not lines:
            print("MANIFEST.sha256 пуст. Распакуйте полный ZIP заново.")
            return False
        for line in lines:
            digest, name = line.split("  ", 1)
            path = (root / name).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                print("Проверка комплекта не пройдена: отсутствует файл из MANIFEST.sha256.")
                return False
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                print("Проверка комплекта не пройдена: файл отличается от MANIFEST.sha256.")
                return False
    except (OSError, ValueError):
        print("Не удалось прочитать комплект или MANIFEST.sha256. Распакуйте ZIP заново.")
        return False
    print("Комплект полный, контрольные суммы всех файлов совпадают.")
    return True


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if sys.version_info < MIN_PYTHON:
        print("Нужен Python 3.11 или новее. Устанавливать именно Python 3.13 не требуется.")
        print("Если используется старая .venv, переименуйте её и повторите установку.")
        return 1
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("Команды: install, verify, setup, check, check-google, run, password, health.")
        return 1
    python = environment_python()
    try:
        if args == ["verify"]:
            return 0 if verify_bundle() else 1
        if args == ["install"]:
            import venv

            if (ROOT / "MANIFEST.sha256").exists() and not verify_bundle():
                return 1
            if not python.is_file():
                venv.EnvBuilder(with_pip=True).create(ROOT / ".venv")
            for command in (
                [str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")],
                [str(python), "-m", "pip", "check"],
            ):
                if subprocess.run(command, cwd=ROOT).returncode:
                    print("Установка не завершена. Проверьте сообщение pip выше и доступ к сети.")
                    return 1
            for name in ("data", "secrets"):
                (ROOT / name).mkdir(exist_ok=True)
            target = ROOT / ".env"
            if not target.exists():
                shutil.copyfile(ROOT / ".env.example", target)
                if os.name != "nt":
                    target.chmod(0o600)
            print("Установлено. Дальше: Setup.cmd → ключ Google в data → Check.cmd → Start.cmd.")
            print("Linux: python3 bootstrap.py setup / check / run.")
            return 0
        # Use this project's environment, even when invoked with a global Python.
        if python.is_file() and Path(sys.executable).resolve() != python.resolve():
            return subprocess.run(
                [str(python), str(ROOT / "bootstrap.py"), *args], cwd=ROOT
            ).returncode
        missing = []
        for package in ("aiogram", "python-dotenv", "google-auth", "requests", "tzdata"):
            try:
                importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                missing.append(package)
        if missing:
            print("Не установлены зависимости: " + ", ".join(missing) + ".")
            print("Запустите Install.cmd или python3 bootstrap.py install.")
            return 1
        return subprocess.run([sys.executable, "-m", "studio_bot", *args], cwd=ROOT).returncode
    except KeyboardInterrupt:
        return 0
    except OSError:
        print(
            "Не удалось открыть файлы или Python. Распакуйте весь ZIP в доступную для записи папку."
        )
        print(
            "Если .venv перенесена с другого компьютера, переименуйте её и повторите Install.cmd."
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())

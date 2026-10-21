import os

from .config import UserError


class InstanceLock:
    def __init__(self, path):
        self.path, self.stream = path, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0, os.SEEK_END)
        if not self.stream.tell():
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise UserError("Бот уже запущен с этой базой. Остановите второй экземпляр.") from None
        return self

    def __exit__(self, *_):
        self.stream.close()

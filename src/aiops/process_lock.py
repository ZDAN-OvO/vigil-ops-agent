"""本机推进者锁。只有最后一个持有描述符的进程退出后，内核才释放锁。"""

import fcntl
import os
from pathlib import Path

LAB_LOCK_FD_ENV = "VIGIL_LAB_LOCK_FD"
_held: dict[Path, int] = {}


def acquire(runtime_path: Path, *, inherit_lab_lock: bool = False) -> int:
    path = runtime_path / "vigil.lock"
    if path in _held:
        return _held[path]

    inherited = os.environ.pop(LAB_LOCK_FD_ENV, None) if inherit_lab_lock else None
    if inherited is not None:
        fd = int(inherited)
        if not os.path.samestat(os.fstat(fd), path.stat()):
            raise ValueError("实验调查进程收到无效的推进者锁")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(fd, False)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise
    _held[path] = fd
    # 不执行 LOCK_UN 或 close。子进程共享同一打开文件描述，最后一份引用关闭时才解锁。
    return fd

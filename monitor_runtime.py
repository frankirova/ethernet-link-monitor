"""Private local process ownership and snapshot transport for the monitor."""
import fcntl
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import threading
import time


class RuntimeErrorLocal(ValueError):
    pass


def directory(path):
    path = Path(path).expanduser().absolute()
    try:
        for component in reversed((path, *path.parents)):
            try:
                info = component.lstat()
            except FileNotFoundError:
                try:
                    component.mkdir(mode=0o700)
                except FileExistsError:
                    pass  # Another starter created it; validate it exactly like an existing path.
                info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise RuntimeErrorLocal('runtime path contains an unsafe symlink or non-directory')
            if component != path and info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
                raise RuntimeErrorLocal('runtime path contains an unsafe writable parent')
            if component == path and (info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise RuntimeErrorLocal('runtime directory must be an owner-only directory')
    except OSError:
        raise RuntimeErrorLocal('cannot create or access runtime directory') from None
    return path


def private_file(path):
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            os.close(fd)
            raise RuntimeErrorLocal('unsafe runtime file')
        return fd
    except OSError:
        raise RuntimeErrorLocal('cannot safely open runtime file') from None


def request(path, command):
    sockpath = path / 'monitor.sock'
    try:
        info = sockpath.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeErrorLocal('unsafe runtime socket')
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(1)
            client.connect(str(sockpath))
            client.sendall((command + '\n').encode())
            data = b''
            while not data.endswith(b'\n') and len(data) < 1024 * 1024:
                chunk = client.recv(65536)
                if not chunk:
                    break
                data += chunk
            return json.loads(data)
    except RuntimeErrorLocal:
        raise
    except (OSError, ValueError):
        return None


def identity(args):
    return {'hosts': str(Path(args.hosts).expanduser().resolve()), 'demo': args.demo, 'interval': args.interval}


def check(response, args):
    if response and response.get('identity') != identity(args):
        raise RuntimeErrorLocal('monitor configuration conflicts; stop it and restart with the requested settings')
    return response


def start(path, args):
    launch = private_file(path / 'launch.lock')
    try:
        fcntl.flock(launch, fcntl.LOCK_EX)
        response = request(path, 'status')
        if response:
            return check(response, args)
        log = private_file(path / 'monitor.log')
        try:
            os.lseek(log, 0, os.SEEK_END)
            command = [sys.executable, str(Path(__file__).with_name('monitor.py')), '--_owner',
                       '--hosts', args.hosts, '--interval', str(args.interval), '--runtime-dir', str(path)]
            if args.demo:
                command.append('--demo')
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=True, close_fds=True)
        finally:
            os.close(log)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = request(path, 'status')
            if response:
                return check(response, args)
            if child.poll() is not None:
                break
            time.sleep(0.05)
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        raise RuntimeErrorLocal('monitor failed to start; check configuration and monitor.log')
    finally:
        os.close(launch)


def wait_stopped(path):
    fd = private_file(path / 'owner.lock')
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                time.sleep(0.05)
        raise RuntimeErrorLocal('monitor did not stop; check monitor.log')
    finally:
        os.close(fd)


class Owner:
    def __init__(self, path, args):
        self.path = path
        self.identity = identity(args)
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.snapshot = {'pid': os.getpid(), 'identity': self.identity, 'scans': 0,
                         'updated': 0, 'lines': ['Monitor starting; awaiting first scan'],
                         'records': [], 'notices': []}
        self.fd = private_file(path / 'owner.lock')
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            raise RuntimeErrorLocal('monitor already running') from None
        self.server = socket.socket(socket.AF_UNIX)
        sockpath = path / 'monitor.sock'
        try:
            if sockpath.exists() or sockpath.is_symlink():
                info = sockpath.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise RuntimeErrorLocal('unsafe runtime socket')
                sockpath.unlink()
            self.server.bind(str(sockpath))
            os.chmod(sockpath, 0o600)
            self.server.listen(16)
            self.server.settimeout(0.2)
        except Exception:
            self.server.close()
            os.close(self.fd)
            raise
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def update(self, lines, records=(), notices=()):
        with self.lock:
            self.snapshot.update(lines=lines, records=list(records), notices=list(notices),
                                 updated=time.time(), scans=self.snapshot['scans'] + 1)

    def serve(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with connection:
                try:
                    connection.settimeout(0.2)
                    command = connection.recv(32).decode().strip()
                    with self.lock:
                        data = json.dumps(self.snapshot).encode() + b'\n'
                    if command in ('status', 'stop'):
                        connection.sendall(data)
                        if command == 'stop':
                            self.stopped.set()
                except (OSError, UnicodeError):
                    pass

    def close(self):
        self.stopped.set()
        self.server.close()
        self.thread.join(timeout=1)
        try:
            (self.path / 'monitor.sock').unlink()
        except FileNotFoundError:
            pass
        os.close(self.fd)

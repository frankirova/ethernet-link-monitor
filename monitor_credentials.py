"""Load a small, literal-only credential file without exposing its contents."""
import os
from pathlib import Path
import re
import stat

MAX_BYTES = 65536
KEY = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')


def load(required):
    path = Path(__file__).with_name('.env')
    try:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('credential file must be a private owner-only regular file')
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError('credential file is too large')
        values = {}
        for line in data.decode('utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, separator, value = line.partition('=')
            key, value = key.strip(), value.strip()
            if not separator or not KEY.fullmatch(key):
                raise ValueError('invalid credential file syntax')
            if value.startswith(('"', "'")):
                if len(value) < 2 or value[-1] != value[0] or value[0] in value[1:-1]:
                    raise ValueError('invalid credential file syntax')
                value = value[1:-1]
            elif '"' in value or "'" in value:
                raise ValueError('invalid credential file syntax')
            values[key] = value
        for key in required:
            if key in values and key not in os.environ:
                os.environ[key] = values[key]
    except (OSError, UnicodeError):
        raise ValueError('cannot safely read credential file') from None

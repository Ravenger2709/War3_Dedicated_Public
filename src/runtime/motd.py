"""Snapshot the optional welcome text for GHost's existing MOTD reader."""
import os
from pathlib import Path
import stat

from .files import atomic_text


def default_lines(host_name):
    return [
        f'Welcome to {host_name}!',
        '!help - Show a summary of the available commands.',
        '!status - Show the map, players, presets, running matches and iCCup status.',
        '!vote - Start or join a vote to start early. The preset\'s minimum human-player requirement still applies.',
    ]


def prepare_motd(source, destination, host_name):
    """Use config/motd.txt when safe; missing or invalid input uses English help.

    GHost reads at most eight lines. Copy bounded text into private native state
    so a later File Manager edit cannot turn its reader into a blocking pipe.
    An empty regular file deliberately disables the welcome text.
    """
    lines=default_lines(host_name)
    path=Path(source).parent/'motd.txt'
    fd=None
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
        before=os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink!=1
                or before.st_size>4096):
            raise ValueError('MOTD_INPUT_REJECTED')
        raw=os.read(fd,4097)
        after=os.fstat(fd)
        if (len(raw)!=before.st_size or
                (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=
                (after.st_size,after.st_mtime_ns,after.st_ctime_ns)):
            raise ValueError('MOTD_INPUT_CHANGED')
        custom=raw.decode('utf-8').splitlines()
        if any(len(line.encode('utf-8'))>200 or any(ord(c)<32 or ord(c)==127 for c in line) for line in custom):
            raise ValueError('MOTD_INPUT_REJECTED')
        lines=custom[:8]
    except (OSError,ValueError,UnicodeError):
        pass  # Optional display text must not prevent the host from starting.
    finally:
        if fd is not None:os.close(fd)
    atomic_text(destination,'\n'.join(lines)+('\n' if lines else ''))
    return str(destination)

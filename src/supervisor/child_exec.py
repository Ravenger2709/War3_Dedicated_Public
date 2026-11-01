"""Linux parent-death handling before exec; never runs a shell."""
import ctypes
import os
import signal
import sys


def main():
    parent = int(sys.argv[1])
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), "prctl failed")
    if os.getppid() != parent:
        return 125
    os.execvp(sys.argv[2], sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())

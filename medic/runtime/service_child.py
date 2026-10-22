"""Start only after joining the supervisor-owned process tree and receiving stdin."""
import sys
from medic.runtime.service_process import ProcessJob


def main():
    job = ProcessJob(sys.argv[1])
    job.close()  # Only the parent retains an owning handle.
    if sys.stdin.readline() != 'start\n':
        return 3
    sys.argv = [sys.argv[0], *sys.argv[2:]]
    from medic.runtime.__main__ import main as execute
    return execute()


if __name__ == '__main__':
    raise SystemExit(main())

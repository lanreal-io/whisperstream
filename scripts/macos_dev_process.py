"""Exact process ownership for the development launcher, without port shutdown."""
from __future__ import annotations

import argparse

import psutil


def present(pid: int, created: float):
    try:
        process = psutil.Process(pid)
        if process.create_time() == created and process.is_running():
            return process
    except psutil.Error:
        pass
    return None


def owns_listener(pid: int, created: float, port: int) -> bool:
    process = present(pid, created)
    if process is None:
        return False
    try:
        identities = [(member.pid, member.create_time()) for member in [process, *process.children(recursive=True)]]
        for member_pid, birth in identities:
            member = present(member_pid, birth)
            if member is None:
                continue
            listening = any(
                connection.status == psutil.CONN_LISTEN and connection.laddr.port == port
                for connection in member.net_connections(kind="tcp")
            )
            if listening and present(member_pid, birth) is not None:
                return present(pid, created) is not None
        return False
    except psutil.Error:
        return False


def stop_owned(pid: int, created: float) -> None:
    process = present(pid, created)
    if process is None:
        return
    try:
        family = [(child.pid, child.create_time()) for child in process.children(recursive=True)]
    except psutil.NoSuchProcess:
        return
    # The parent gets a graceful signal first. Retain exact descendant identities
    # so orphaned workers can still be settled after the parent exits.
    family.append((pid, created))
    process.terminate()
    psutil.wait_procs([p for child, birth in family if (p := present(child, birth))], timeout=3)
    survivors = [p for child, birth in family if (p := present(child, birth))]
    for child in survivors:
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(survivors, timeout=2)
    for child, birth in family:
        if (remaining := present(child, birth)) is not None:
            try:
                remaining.kill()
            except psutil.NoSuchProcess:
                pass
    psutil.wait_procs([p for child, birth in family if (p := present(child, birth))], timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["identity", "ready", "stop"])
    parser.add_argument("pid", type=int)
    parser.add_argument("--created", type=float)
    parser.add_argument("--parent", type=int)
    parser.add_argument("--port", type=int, default=38420)
    args = parser.parse_args()
    if args.action == "identity":
        try:
            process = psutil.Process(args.pid)
            if args.parent is None or process.ppid() != args.parent:
                return 1
            print(process.create_time())
        except psutil.Error:
            return 1
        return 0
    if args.created is None:
        parser.error("--created is required for ready and stop")
    if args.action == "ready":
        return 0 if owns_listener(args.pid, args.created, args.port) else 1
    stop_owned(args.pid, args.created)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

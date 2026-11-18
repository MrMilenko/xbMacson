"""Find consoles on the local /24 by trying the XBDM port."""

import concurrent.futures
import socket


def local_subnet():
    """(local_ip, "a.b.c") for the interface that routes outward, or
    (None, None). No packet is sent: connecting a UDP socket only picks a
    route."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        a, b, c, _ = ip.split(".")
        return ip, f"{a}.{b}.{c}"
    except (OSError, ValueError):
        return None, None


def check_host(ip, port=731, timeout=1.5):
    """(ip, debug name) if XBDM answers at ip:port, else None."""
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
    except OSError:
        return None
    try:
        s.settimeout(timeout)
        greeting = s.recv(256).decode("latin-1", "replace").strip()
        if not greeting.startswith(("201", "200")):
            return None
        s.sendall(b"dbgname\r\n")
        resp = s.recv(256).decode("latin-1", "replace").strip()
        name = "unknown"
        if resp.startswith("200"):
            name = resp[4:].lstrip("- ").strip() or "unknown"
        try:
            s.sendall(b"bye\r\n")
        except OSError:
            pass
        return ip, name
    except OSError:
        return ip, "unknown"
    finally:
        s.close()


def scan(subnet=None, port=731, hosts=None, workers=64, timeout=1.5,
         found=None):
    """Probe ``subnet``.1-254 (or the given ``hosts``). Calls ``found`` for
    each hit as it arrives and returns all hits."""
    if hosts is None:
        if subnet is None:
            _, subnet = local_subnet()
            if subnet is None:
                raise OSError("could not determine the local subnet")
        hosts = [f"{subnet}.{i}" for i in range(1, 255)]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(check_host, h, port, timeout) for h in hosts]
        for fut in concurrent.futures.as_completed(futs):
            r = fut.result()
            if r:
                results.append(r)
                if found:
                    found(r)
    return results

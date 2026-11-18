"""A sampling profiler built on thread context reads.

Every tick reads the program counter of each chosen thread with
``getcontext``. Whether XBDM needs the thread suspended first is decided on
the first read (see Console.get_context). The cost is documented in
docs/profiling.md.
"""

import time

from .protocol import XbdmError


class Sample:
    __slots__ = ("t", "thread", "pc")

    def __init__(self, t, thread, pc):
        self.t = t
        self.thread = thread
        self.pc = pc


class ProfileResult:
    def __init__(self, samples, elapsed, requested_rate, threads, errors,
                 needs_suspend, latencies):
        self.samples = samples
        self.elapsed = elapsed
        self.requested_rate = requested_rate
        self.threads = threads
        self.errors = errors
        self.needs_suspend = needs_suspend
        self.latencies = latencies

    @property
    def achieved_rate(self):
        """Samples per second per thread actually taken."""
        if not self.elapsed or not self.threads:
            return 0.0
        return len(self.samples) / self.elapsed / len(self.threads)

    @property
    def mean_latency_ms(self):
        if not self.latencies:
            return 0.0
        return 1000.0 * sum(self.latencies) / len(self.latencies)

    def rank(self, symbolizer, by="function"):
        """[(name, count, percent)] most frequent first. ``by`` is
        "function", "module" or "address"."""
        counts = {}
        for s in self.samples:
            if by == "address":
                key = f"0x{s.pc:08x}"
            else:
                loc = symbolizer.locate(s.pc)
                if by == "module":
                    key = loc.module.name if loc.module else "?"
                elif loc.symbol:
                    key = loc.symbol.display
                elif loc.module:
                    key = f"[{loc.module.name}]"
                else:
                    key = f"0x{s.pc:08x}"
            counts[key] = counts.get(key, 0) + 1
        total = len(self.samples) or 1
        rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return [(k, c, 100.0 * c / total) for k, c in rows]


def sample(console, threads, rate=100.0, seconds=5.0, suspend="auto",
           clock=time.monotonic, sleep=time.sleep, stop=None):
    """Sample ``threads`` ``rate`` times per second for ``seconds``.

    Returns a ProfileResult. Threads that vanish are dropped. ``stop`` is an
    optional callable that ends the run early when it returns true.
    """
    threads = list(threads)
    interval = 1.0 / rate if rate > 0 else 0.0
    samples = []
    latencies = []
    errors = {}
    start = clock()
    end = start + seconds
    next_tick = start
    live = list(threads)
    while live and clock() < end:
        if stop is not None and stop():
            break
        for tid in list(live):
            t0 = clock()
            try:
                ctx = console.get_context(tid, suspend=suspend, parts="control")
            except XbdmError as e:
                errors[tid] = str(e)
                if e.code == 405:  # no such thread
                    live.remove(tid)
                continue
            latencies.append(clock() - t0)
            pc = ctx.get(console.profile.pc_reg)
            if pc is not None:
                samples.append(Sample(t0 - start, tid, pc))
        next_tick += interval
        delay = next_tick - clock()
        if delay > 0:
            sleep(delay)
        else:
            # Running behind: do not try to catch up with a burst.
            next_tick = clock()
    elapsed = clock() - start
    return ProfileResult(samples, elapsed, rate, threads, errors,
                         console.context_needs_suspend, latencies)

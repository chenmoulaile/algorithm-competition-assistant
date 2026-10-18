"""Algorithm Competition Assistant - 并行跑全部测试点

串行跑的时候，总耗时 ≈ 时限 × 测试点数：5 个点里有 2 个 TLE，就得干等 2×TL。
这里用一个小的 worker 池（默认 4）同时跑，墙钟时间大约降到 1/4。

设计要点：

- **每个测试点一个独立的 ProcessManager**：各自一个进程、各自的管道和 stderr
  临时文件，worker 之间不共享任何可变状态（串行路径里复用的那个实例在这里
  不能用）。
- 时限由我们自己看门：超时就 terminate，判定沿用主流程的口径
  （被我们杀掉 + 崩溃特征 → RE，否则 TLE）。
- 结果全部算完后再用 ``sublime.set_timeout`` 回主线程写 UI，工作线程不碰视图。

放在 core/ 是因为根级插件模块之间不允许互相 import（Package Control 审查规则）。
ProcessManager 由调用方以工厂函数传入，core 不反向依赖 Modules。
"""

import threading
import time

import sublime

from .cph_verdict import (get_verdict, get_verdict_by_code, find_crash_location,
                          looks_like_crash)
from .cph_gdb_trace import names_source as _names_source

#: Extra allowance on top of the time limit before we kill a run. It covers
#: only this loop's 10 ms poll step - the moment we *notice* the program is
#: over the limit, not how long it is allowed to run. The verdict itself is
#: made from the measured runtime against the plain limit, so a run is never
#: given more time here than a judge would give it.
_GRACE_SECONDS = 0.05


def _replay_crash(manager, source_file, stdin_text, time_limit_ms):
    """(file, line) from a gdb replay of the binary, or None. Never raises."""
    try:
        timeout = 5.0
        if time_limit_ms:
            timeout = min(5.0, max(2.0, float(time_limit_ms) / 1000.0))
        payload = stdin_text.encode('utf-8', 'replace') if isinstance(stdin_text, str) else b''
        return manager.locate_crash(source_file=source_file,
                                    stdin_bytes=payload, timeout=timeout)
    except Exception:
        return None


def _run_one(make_manager, input_text, expected, time_limit_ms, memory_limit_mb,
             float_tolerance, regard_pe_as_ac, judge=None):
    """Run one test in this thread. Never raises: errors become a result."""
    result = {
        'rtcode': None, 'stdout': '', 'stderr': '', 'runtime': 0,
        'memory': None, 'verdict': None, 'crash': None, 'error': None,
    }
    manager = None
    try:
        manager = make_manager()
        manager.set_separate_stderr(True)
        if time_limit_ms:
            manager.set_time_limit(time_limit_ms)
        if memory_limit_mb:
            manager.set_memory_limit(memory_limit_mb)

        # A file-IO problem reads its sample from a file, so the sample has to
        # be on disk before the program starts. For a stdin run this only
        # remembers the text; the manager is duck-typed here, so ask softly.
        set_io_input = getattr(manager, 'set_io_input', None)
        if set_io_input is not None:
            set_io_input(input_text)

        manager.run_file()
        # The program's own clock, taken from the manager the instant Popen
        # returned. Starting it before the spawn (as this path used to) charged
        # ~36 ms of process creation to every single test case, and made the
        # serial and the parallel path disagree about the same binary.
        started = getattr(manager, 'spawn_time', None) or time.time()
        if input_text:
            manager.insert(input_text)
            # EOF after the stored sample: a program that reads until the end
            # of stdin would otherwise hang and be reported TLE.
            manager.close_stdin()

        limit_s = float(time_limit_ms or 10000) / 1000.0
        deadline = started + limit_s + _GRACE_SECONDS
        killed = False
        while manager.is_stopped() is None:
            if time.time() > deadline:
                killed = True
                try:
                    manager.terminate()
                except Exception:
                    pass
                break
            time.sleep(0.01)

        # Measure the runtime the moment the process is gone - before
        # read()/get_stderr()/finish_memory_sampling(), which can take a
        # noticeable moment on Windows and used to be billed to the program.
        measured = getattr(manager, 'runtime_ms', None)
        runtime = measured() if measured is not None else None
        if runtime is None:
            runtime = int((time.time() - started) * 1000)
        collect = getattr(manager, 'collect_output', None)
        if collect is not None:
            # stdin runs: (pipe text, ''). File-IO runs: the file the program
            # wrote, and when that file never appeared a note saying so - which
            # is shown as the output rather than judging a blank answer, because
            # "you did not write add.out" is not the same as "wrong answer".
            stdout, io_note = collect()
            if io_note:
                stdout = '\n' + io_note + '\n'
        else:
            stdout = manager.read()
        stderr = manager.get_stderr()
        try:
            memory = manager.finish_memory_sampling()
        except Exception:
            memory = None
        rtcode = manager.is_stopped()

        result.update(rtcode=rtcode, stdout=stdout, stderr=stderr,
                      runtime=runtime, memory=memory)
        # The cap is enforced inside the manager (the panel's clamp is not
        # reachable from this thread). Say so, because "the answer stopped
        # half-way" otherwise reads as a wrong answer the program never gave.
        if getattr(manager, 'output_capped', False):
            result['capped'] = True

        if killed:
            # Same rule as the serial path: a crash signature means the
            # process died on its own and our kill was collateral. When
            # nothing was printed, ask gdb instead - on Windows an aborting
            # program is dead at t=0 but reaped ~4.5s later (WER), so its
            # assertion text never reaches us before the deadline.
            replayed = None
            if not looks_like_crash(stderr or stdout):
                replayed = _replay_crash(manager, getattr(manager, 'file', None),
                                         input_text, time_limit_ms)
            if replayed:
                result['crash'] = replayed
            if replayed or looks_like_crash(stderr or stdout):
                result['verdict'] = get_verdict('runtime_error')
            else:
                result['verdict'] = get_verdict('time_limit_exceed')
        else:
            result['verdict'] = get_verdict_by_code(
                rtcode=int(rtcode) if rtcode is not None else 0,
                runtime=runtime,
                time_limit_ms=time_limit_ms,
                memory_limit_mb=memory_limit_mb,
                stderr=stderr,
                stdout=stdout,
                expected_output=expected,
                ignore_error=True,
                regard_pe_as_ac=regard_pe_as_ac,
                memory_used_mb=memory,
                float_tolerance=float_tolerance,
            )

        if judge is not None:
            # Optional custom judging (a configured SPJ checker). It may
            # replace the verdict and add a message; failures inside it must
            # never lose the result we already have.
            try:
                replacement = judge(input_text, stdout, expected, result['verdict'])
                if replacement:
                    verdict, message = replacement
                    if verdict:
                        result['verdict'] = verdict
                    if message:
                        result['message'] = message
            except Exception as e:
                result['message'] = 'checker: %s: %s' % (type(e).__name__, e)

        if result['verdict']['name'] == 'RE':
            source_file = getattr(manager, 'file', None)
            location = find_crash_location(stderr or stdout, source_file)
            # A C++ abort names a libstdc++ header, not the user's line; one
            # gdb replay is what turns it into the frame they have to fix.
            if not _names_source(location, source_file):
                replayed = _replay_crash(manager, source_file, input_text,
                                         time_limit_ms)
                if replayed:
                    location = replayed
            result['crash'] = location
    except Exception as e:
        result['error'] = '%s: %s' % (type(e).__name__, e)
        result['verdict'] = get_verdict('unknown_error')
    finally:
        if manager is not None:
            try:
                manager.close_stderr()
            except Exception:
                pass
    return result


def run_batch(make_manager, cases, workers=4, time_limit_ms=None,
              memory_limit_mb=None, float_tolerance=0, regard_pe_as_ac=False,
              on_done=None, on_progress=None, judge=None,
              case_runner=None):
    """Judge every case in `cases` concurrently.

    `cases` is a list of (input_text, expected_output) pairs.
    `judge(input, output, expected, default_verdict)` may return
    `(verdict, message)` to override the computed verdict (used by a
    configured SPJ checker).
    `case_runner(index, input_text, expected)` replaces the default "run the
    program once" step entirely - that is how interactive problems reuse this
    pool (they need a dialogue instead of a single run).
    `on_done(results)` and `on_progress(done, total)` are called on the MAIN
    thread (via sublime.set_timeout), so they may touch views.
    Returns the number of worker threads started.
    """
    total = len(cases)
    if total == 0:
        if on_done:
            sublime.set_timeout(lambda: on_done([]), 0)
        return 0

    workers = max(1, min(int(workers or 1), total))
    results = [None] * total
    state = {'next': 0, 'done': 0}
    lock = threading.Lock()

    def report():
        with lock:
            state['done'] += 1
            done = state['done']
        if on_progress:
            sublime.set_timeout(lambda done=done: on_progress(done, total), 0)

    def worker():
        while True:
            with lock:
                if state['next'] >= total:
                    return
                index = state['next']
                state['next'] += 1
            input_text, expected = cases[index]
            if case_runner is not None:
                results[index] = case_runner(index, input_text, expected)
            else:
                results[index] = _run_one(
                    make_manager, input_text, expected, time_limit_ms,
                    memory_limit_mb, float_tolerance, regard_pe_as_ac, judge)
            report()

    def supervise():
        threads = []
        for _ in range(workers):
            thread = threading.Thread(target=worker)
            thread.daemon = True
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()
        if on_done:
            sublime.set_timeout(lambda: on_done(results), 0)

    controller = threading.Thread(target=supervise)
    controller.daemon = True
    controller.start()
    return workers

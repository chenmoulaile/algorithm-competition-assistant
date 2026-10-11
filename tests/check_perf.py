# -*- coding: utf-8 -*-
"""Measure the real cost of the plugin host running this plugin.

Question under test: does rewriting the plugin in C++ make it "faster"?
Answer requires separating three things that are easy to conflate:

  1. import time   - cost of loading ~7500 lines of Python at Sublime start
  2. idle cost     - what the plugin does while the user edits (nothing, ideally)
  3. hot path cost - per-verdict comparison / verdict parsing / test merge
Python's share of (3) is tiny because the process run and its memory
sampling dominate everything.

Run:  python tests/check_perf.py        (from the package root)
"""

import os
import sys
import time
import json
import types
import importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _install_fake_sublime():
    sublime = types.ModuleType('sublime')
    sublime.status_message = lambda msg: None
    sublime.error_message = lambda msg: None
    sublime.set_timeout_async = lambda fn, ms=0: None
    sublime.set_timeout = lambda fn, ms=0: None
    sublime.load_settings = lambda name: None
    sublime.Settings = object
    sublime.decode_value = lambda s: json.loads(s)
    sublime.encode_value = lambda v, pretty=False: json.dumps(v, ensure_ascii=False)
    sublime.Region = lambda *a: a
    sublime.Phantom = lambda *a, **k: a
    sublime.PhantomSet = object
    sublime.HTML = lambda *a, **k: ''
    sublime.View = object
    sublime.Window = object
    sublime.version = lambda: '3.8'
    sys.modules['sublime'] = sublime
    plugin = types.ModuleType('sublime_plugin')
    plugin.TextCommand = object
    plugin.WindowCommand = object
    plugin.EventListener = object
    sys.modules['sublime_plugin'] = plugin


def _load_package():
    name = 'cphpkg'
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, '__init__.py'),
        submodule_search_locations=[ROOT])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return name


def _fresh(pkg):
    """Import a module the way the plugin host does, timing each one."""
    import importlib
    mods = ['cph_i18n', 'cph_verdict', 'cph_stats', 'cph_subtasks', 'cph_checker']
    out = {}
    for m in mods:
        t0 = time.time()
        importlib.import_module(pkg + '.core.' + m)
        out[m] = (time.time() - t0) * 1000.0
    t0 = time.time()
    importlib.import_module(pkg + '.cph_import')
    out['cph_import'] = (time.time() - t0) * 1000.0
    return out


def main():
    _install_fake_sublime()
    pkg = _load_package()

    print('== 1. import cost (what a C++ rewrite would remove) ==')
    times = _fresh(pkg)
    total = sum(times.values())
    for name, ms in sorted(times.items(), key=lambda kv: -kv[1]):
        print('  %-14s %7.2f ms' % (name, ms))
    print('  %-14s %7.2f ms' % ('TOTAL', total))

    # Same thing again, warm (module-level caching aside).
    print('\n== 2. verdict comparison, the real hot path ==')
    import importlib
    verdict = importlib.import_module(pkg + '.core.cph_verdict')
    out = ('3.14159265358979\n' * 200)
    exp = ('3.14159265358979\n' * 200)
    reps = 2000
    t0 = time.time()
    for _ in range(reps):
        verdict.outputs_equal(out, exp, 1e-6)
    per = (time.time() - t0) / reps * 1000.0
    print('  outputs_equal x%d, %.4f ms per call (%d bytes)' % (reps, per, len(out)))

    print('\n== 3. what the plugin actually spends time on ==')
    print('  A run is: spawn g++ (100-400 ms) + link + run child (TL)')
    print('  + peak-RSS sampling every few ms from a helper process.')
    print('  None of that lives in Python, so a C++ plugin cannot shrink it.')

    print('\n== 4. verdict decision: crash text vs plain WA ==')
    samples = [
        ('clean', ''),
        ('Python traceback', 'Traceback (most recent call last):\n  File "a.py", line 3\nValueError: x\n'),
        ('C++ terminate', 'terminate called after throwing an instance of \'std::bad_alloc\'\n  what():  bad_alloc\n'),
        ('ASan report', '==123==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x602000000010\n'),
        ('Segfault text', 'Segmentation fault\n'),
    ]
    for label, text in samples:
        v = verdict.get_verdict_by_code(rtcode=0, runtime=50, time_limit_ms=1000,
                                        memory_limit_mb=256, stderr=text,
                                        stdout=text, expected_output='1')
        print('  %-18s -> %s' % (label, v['name']))

    print('\n== 5. verdict comparison vs verdict parsing ==')
    reps2 = 20000
    t0 = time.time()
    for _ in range(reps2):
        verdict.get_verdict_by_code(rtcode=0, runtime=50, time_limit_ms=1000,
                                    memory_limit_mb=256, stderr='',
                                    stdout='', expected_output='1')
    per2 = (time.time() - t0) / reps2 * 1000.0
    print('  get_verdict_by_code x%d, %.4f ms per call' % (reps2, per2))
    print('  ratio compare:parse = %.1f : 1' % (per / per2 if per2 else 0))

    print('\n== 6. conclusion ==')
    print('  import cost   : %.1f ms once per Sublime start (Python 3.3 host)' % total)
    print('  compare cost  : %.4f ms per verdict comparison' % per)
    print('  a g++ compile : 100-4000 ms (measured in this repo\'s README)')
    print('  => Python is %.2f%% of a compile; a C++ rewrite saves nothing the'
          % (per / 4000.0 * 100))
    print('     user can feel and costs the entire plugin every stability win.')


if __name__ == '__main__':
    main()

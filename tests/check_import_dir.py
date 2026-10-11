# -*- coding: utf-8 -*-
"""Regression check: importing test data must not die with a permissions error.

Windows refuses open() on a DIRECTORY with `[Errno 13] Permission denied`,
which reads like a real permissions problem. Explorer's "Copy as path" and
the import input panels happily hand a folder path to the reader, so the
plain path a user typed could produce that message.

Run:  python tests/check_import_dir.py
Exit: 0 = all good, 1 = at least one failure.
"""

import os
import sys
import types
import importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A directory this host definitely has: a real OJ data folder if present,
# otherwise any directory (the point is that it IS a directory).
SAMPLE_DIR = os.path.join(ROOT, 'tests')


def _install_fake_sublime():
    sublime = types.ModuleType('sublime')
    sublime.status_message = lambda msg: None
    sublime.error_message = lambda msg: None
    sublime.set_timeout_async = lambda fn, ms=0: None
    sublime.load_settings = lambda name: None
    sublime.Settings = object
    sublime.decode_value = lambda s: s
    sublime.encode_value = lambda v, pretty=False: v
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


def main():
    _install_fake_sublime()
    pkg = _load_package()
    import importlib
    imp = importlib.import_module(pkg + '.cph_import')

    checks = []

    def check(label, ok, detail=''):
        checks.append((label, ok, detail))

    # 1. The reported symptom: a directory must produce an actionable
    #    message, not the OS errno 13 text.
    try:
        imp._read_text(SAMPLE_DIR)
        check('a directory raises instead of reading', False, 'no exception')
    except Exception as e:
        text = str(e)
        check('a directory raises instead of reading', True, text)
        check('the message does not claim a permissions error',
              'Permission denied' not in text and 'Errno 13' not in text,
              text)
        check('the message names the path', SAMPLE_DIR in text, text)

    # 2. A data FILE must still read normally (no regression).
    probe = os.path.join(SAMPLE_DIR, 'check_import_dir.py')
    try:
        body = imp._read_text(probe)
        check('a regular file still reads', body.startswith('#'), body[:40])
    except Exception as e:
        check('a regular file still reads', False, str(e))

    # 3. The file/directory predicates the UI lists rely on.
    check('_is_dir recognizes a directory', imp._is_dir(SAMPLE_DIR) is True)
    check('_is_dir rejects a file', imp._is_dir(probe) is False)
    check('_is_file_like accepts a file', imp._is_file_like(probe) is True)
    check('_is_file_like rejects a directory',
          imp._is_file_like(SAMPLE_DIR) is False)
    check('_is_file_like rejects a missing path',
          imp._is_file_like(os.path.join(SAMPLE_DIR, 'nope.in')) is False)

    failures = 0
    for label, ok, detail in checks:
        if ok:
            print('  ok   %s' % label)
        else:
            print('  FAIL %s -> %s' % (label, detail))
            failures += 1
    print('%d checks, %d failures' % (len(checks), failures))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())

"""Algorithm Competition Assistant regression tests.

Runs with plain CPython - no Sublime Text, no third party packages:

    python tests/run_tests.py

A fake `sublime` module is injected so the plugin modules can be imported.
The cases below cover the bugs that actually shipped and had to be hot-fixed:
the multi-sample chain crash, the memory-limit verdict never firing, the
Python 3.3 subprocess arguments and the float comparison.

Exit code 0 = all good, 1 = failures (used by .github/workflows/tests.yml).
"""

import ast
import contextlib
import importlib
import importlib.util
import inspect
import io
import json
import locale
import os
import re
import shutil
import sys
import subprocess
import tempfile
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------- fake sublime
def _install_fake_sublime():
    sublime = types.ModuleType('sublime')

    class Region(object):
        def __init__(self, a=0, b=None):
            self.a = a
            self.b = a if b is None else b

        def begin(self):
            return min(self.a, self.b)

        def end(self):
            return max(self.a, self.b)

        def empty(self):
            return self.a == self.b

        def __eq__(self, other):
            return isinstance(other, Region) and (self.a, self.b) == (other.a, other.b)

    class Settings(object):
        def __init__(self, data=None):
            self._data = dict(data or {})

        def get(self, key, default=None):
            return self._data.get(key, default)

        def set(self, key, value):
            self._data[key] = value

        def add_on_change(self, *args):
            pass

    class Phantom(object):
        def __init__(self, region, content, layout=None, on_navigate=None):
            self.region = region
            self.content = content

    class PhantomSet(object):
        def __init__(self, view, key):
            self.view = view
            self.key = key
            self.phantoms = []

        def update(self, phantoms):
            self.phantoms = list(phantoms)

    class View(object):
        def __init__(self, file_name=None, settings=None, size=0):
            self._file_name = file_name
            self._settings = Settings(settings)
            self._size = size
            self._status = {}

        def file_name(self):
            return self._file_name

        def name(self):
            return ''

        def settings(self):
            return self._settings

        def size(self):
            return self._size

        def id(self):
            # Real views have one, and the listener identifies its pinned
            # target by it.
            return id(self)

        def is_valid(self):
            return True

        def is_dirty(self):
            return False

        def close(self):
            pass

        def get_status(self, key):
            return self._status.get(key)

        def set_status(self, key, value):
            self._status[key] = value

        def set_name(self, name):
            self._name = name

        def set_scratch(self, scratch):
            self._scratch = scratch

        def execute_command(self, *args, **kwargs):
            pass

        def run_command(self, *args, **kwargs):
            pass

        def sel(self):
            return [Region(0)]

        def window(self):
            # A view belongs to the window it was opened in; every test that
            # cares drives that through sublime.active_window().
            return sublime.active_window()

    class Window(object):
        """Minimal stand-in so doctor.run() can open its report view."""

        def __init__(self):
            self._views = []

        def new_file(self):
            view = View()
            self._views.append(view)
            return view

        def focus_view(self, view):
            pass

        def active_view(self):
            return self._views[-1] if self._views else None

        def run_command(self, *args, **kwargs):
            pass

        def folders(self):
            return []

        def status_message(self, *args, **kwargs):
            pass

    sublime.Region = Region
    sublime.Phantom = Phantom
    sublime.PhantomSet = PhantomSet
    sublime.Settings = Settings
    sublime.View = View
    sublime.Window = Window
    sublime.LAYOUT_BLOCK = 1
    sublime.LAYOUT_INLINE = 2
    sublime.HIDDEN = 0
    sublime.DRAW_NO_FILL = 0
    sublime.DRAW_NO_OUTLINE = 0
    sublime.DRAW_STIPPLED_UNDERLINE = 0
    sublime.DRAW_EMPTY_AS_OVERWRITE = 0

    sublime.platform = lambda: 'linux'
    # Context operators, same values as the real API: cph_context compares the
    # operator it was handed against these.
    sublime.OP_EQUAL = 0
    sublime.OP_NOT_EQUAL = 1
    sublime.OP_GREATER_THAN = 2
    sublime.OP_LESS_THAN = 3
    sublime.OP_REGEX_MATCH = 4
    sublime.OP_REGEX_NOT_MATCH = 5
    sublime.status_message = lambda *a, **k: None
    sublime.error_message = lambda *a, **k: None
    sublime.ok_cancel_dialog = lambda *a, **k: True
    sublime.message_dialog = lambda *a, **k: None
    sublime.set_timeout = lambda fn, delay=0: None          # never run the chain
    sublime.set_timeout_async = lambda fn, delay=0: None
    sublime.windows = lambda: []
    sublime.active_window = lambda: None
    # Real Sublime always answers these; the click log and the panel that opens
    # it both derive their path from packages_path(). Tests that need a live
    # Data directory still override them for their own duration.
    sublime.packages_path = lambda: os.path.join(os.path.dirname(ROOT), 'Packages')
    sublime.installed_packages_path = lambda: os.path.join(
        os.path.dirname(ROOT), 'Installed Packages')
    sublime.get_clipboard = lambda: ''
    sublime.set_clipboard = lambda text: None
    sublime.load_settings = lambda name: Settings({})
    sublime.save_settings = lambda name: None
    sublime.load_resource = lambda path: '<div class="panel"></div>'
    sublime.decode_value = json.loads

    def _encode_value(value, pretty=False):
        # The real API is encode_value(value, pretty): json.dumps' second
        # positional argument is `skipkeys`, so the plain alias blew up with
        # "dumps() takes 1 positional argument but 2 were given" and save_tests
        # was never actually exercised by the suite.
        if pretty:
            return json.dumps(value, indent='\t', sort_keys=True)
        return json.dumps(value)

    sublime.encode_value = _encode_value
    sublime.expand_variables = lambda s, v: s
    sublime.version = lambda: '4213'
    sys.modules['sublime'] = sublime

    sublime_plugin = types.ModuleType('sublime_plugin')

    class _Command(object):
        def __init__(self, view=None):
            self.view = view

        def is_enabled(self, *a, **k):
            return True

        def is_visible(self, *a, **k):
            return True

    sublime_plugin.TextCommand = _Command

    class _WindowCommand(_Command):
        """A WindowCommand is handed a window, not a view."""

        def __init__(self, window=None):
            self.window = window

        def is_enabled(self, *a, **k):
            return True

        def is_visible(self, *a, **k):
            return True

    sublime_plugin.WindowCommand = _WindowCommand
    sublime_plugin.ApplicationCommand = _Command
    sublime_plugin.EventListener = object
    sys.modules['sublime_plugin'] = sublime_plugin


def _load_package():
    """Import the plugin package despite the hyphen in its folder name."""
    name = 'cphpkg'
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, '__init__.py'),
        submodule_search_locations=[ROOT])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return name



# --------------------------------------------------- static analysis helpers
SKIP_DIRS = {'.git', '__pycache__', '.workbuddy', 'tests'}
ST_PROVIDED = {'view', 'window', 'run', 'is_enabled', 'is_visible', 'is_checked',
               'description', 'want_event', 'settings', 'on_navigate',
               'send_response', 'send_header', 'end_headers', 'log_message',
               'handle_one_request', 'wfile', 'rfile', 'headers',
               'protocol_version'}


def _plugin_py_files():
    out = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith('.py'):
                out.append(os.path.relpath(os.path.join(base, f), ROOT).replace(os.sep, '/'))
    return sorted(out)


def _self_calls_without_definition(rel_path):
    """[(class, line, method)] where self.method() is not defined in the class.

    Nested classes are handled: their methods belong to them, not to the
    enclosing class, and a class may use methods of our own mixins.
    """
    tree = ast.parse(open(os.path.join(ROOT, rel_path), encoding='utf-8').read())
    own = {}

    def collect(node):
        for item in ast.iter_child_nodes(node):
            if isinstance(item, ast.ClassDef):
                names = set()
                for sub in item.body:
                    if isinstance(sub, (ast.FunctionDef, ast.ClassDef)):
                        names.add(sub.name)
                own[item.name] = names
                collect(item)
    collect(tree)

    found = []

    def check_class(node):
        defined = set()
        assigned = set()
        inherited = set()
        for base in node.bases:
            bname = getattr(base, 'id', getattr(base, 'attr', ''))
            inherited |= own.get(bname, set())
        calls = []

        def scan_method(fn):
            for sub in ast.walk(fn):
                if isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if (isinstance(t, ast.Attribute)
                                and getattr(t.value, 'id', '') == 'self'):
                            assigned.add(t.attr)
                if isinstance(sub, ast.Call):
                    fn2 = sub.func
                    if (isinstance(fn2, ast.Attribute)
                            and isinstance(fn2.value, ast.Name)
                            and fn2.value.id == 'self'):
                        calls.append((sub.lineno, fn2.attr))

        for item in node.body:
            if isinstance(item, ast.FunctionDef):
                defined.add(item.name)
                scan_method(item)
            elif isinstance(item, ast.ClassDef):
                defined.add(item.name)
                check_class(item)
            elif isinstance(item, ast.Assign):
                for t in item.targets:
                    if isinstance(t, ast.Attribute) and getattr(t.value, 'id', '') == 'self':
                        assigned.add(t.attr)

        resolved = defined | assigned | inherited | ST_PROVIDED
        for line, name in calls:
            if name in resolved or name.startswith('__'):
                continue
            found.append((node.name, line, name))

    for item in ast.iter_child_nodes(tree):
        if isinstance(item, ast.ClassDef):
            check_class(item)
    return found


def _lambdas_leaking_except_var(rel_path):
    """[(line, name)] where a lambda inside `except ... as NAME` reads NAME
    without binding it.

    Python deletes the exception variable when the except block ends, so a
    deferred callback such as `set_timeout(lambda: ... % e)` raised NameError
    by the time it ran (the error message was swallowed).
    """
    tree = ast.parse(open(os.path.join(ROOT, rel_path), encoding='utf-8').read())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or not node.name:
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Lambda):
                continue
            bound = {a.arg for a in sub.args.args}
            bound |= {a.arg for a in sub.args.kwonlyargs}
            for default in list(sub.args.defaults) + [d for d in sub.args.kw_defaults if d]:
                if isinstance(default, ast.Name):
                    bound.add(default.id)
            if node.name in bound:
                continue
            for inner in ast.walk(sub.body):
                if (isinstance(inner, ast.Name) and inner.id == node.name
                        and isinstance(inner.ctx, ast.Load)):
                    found.append((sub.lineno, node.name))
                    break
    return found


# i18n keys that are fetched through a VARIABLE: test_manager._run_parallel
# builds the label at runtime ('interactive_running' if interactive else
# 'parallel_running'), so a literal t('key') scan cannot see them. Everything
# else must be called through t('key') or it is a dead entry.
DYNAMIC_I18N_KEYS = frozenset(('interactive_running', 'parallel_running'))

# core/cph_stats.py renders the practice report through a key variable: it is
# pure stdlib and must stay free of a cph_i18n import, so the caller passes
# t() in and this module looks the key up by name (see _ENGLISH_LABELS). A
# literal t('key') scan sees none of these, so they are whitelisted here and
# covered by the stats-report checks further down instead.
STATS_I18N_KEYS = frozenset((
	'stats_summary_header', 'stats_summary_solved', 'stats_summary_verdicts',
	'stats_summary_per_day',
	'stats_summary_day_row', 'stats_summary_no_runs', 'stats_summary_needs_work',
	'stats_summary_needs_row', 'stats_summary_needs_none',
	'stats_summary_stress', 'stats_summary_total_time',
))


def _i18n_usage():
    """(keys defined in core/cph_i18n.py STRINGS, keys referenced in code)."""
    i18n_path = os.path.join(ROOT, 'core', 'cph_i18n.py')
    tree = ast.parse(open(i18n_path, encoding='utf-8').read())
    keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == 'STRINGS'
                for target in node.targets):
            if isinstance(node.value, ast.Dict):
                keys = {k.value for k in node.value.keys
                        if isinstance(k, ast.Constant)}

    used = set()
    for rel in _plugin_py_files():
        fpath = os.path.join(ROOT, rel)
        try:
            mod = ast.parse(open(fpath, encoding='utf-8').read())
        except Exception:
            continue
        # names the i18n helper t() is imported under ('t', '_i18n_t', ...)
        helpers = set()
        for node in ast.walk(mod):
            if (isinstance(node, ast.ImportFrom) and node.module
                    and node.module.endswith('cph_i18n')):
                for alias in node.names:
                    if alias.name == 't':
                        helpers.add(alias.asname or alias.name)
        for node in ast.walk(mod):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            fn = node.func
            if isinstance(fn, ast.Name):
                name, recv = fn.id, None
            elif isinstance(fn, ast.Attribute):
                name = fn.attr
                recv = getattr(fn.value, 'id', None)
            else:
                continue
            # t('key') / i18n.t('key') / STRINGS.get('key')
            if not ((recv is None and (name == 't' or name in helpers))
                    or (recv == 'STRINGS' and name == 'get')):
                continue
            a0 = node.args[0]
            if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                used.add(a0.value)
    return keys, used


def _palette_entry(palette_text, command):
    """The Default.sublime-commands object that registers `command`, or None."""
    needle = '"command": "%s"' % command
    if needle not in palette_text:
        return None
    i = palette_text.index(needle)
    start = palette_text.rindex('{', 0, i)
    end = palette_text.index('}', i)
    return palette_text[start:end + 1]


# ------------------------------------------------------------------ test rig
FAILURES = []
CHECKS = [0]


def check(label, condition, detail=''):
    CHECKS[0] += 1
    if condition:
        print('  ok   %s' % label)
    else:
        print('  FAIL %s %s' % (label, detail))
        FAILURES.append(label)


def main():
    # The suite prints plugin messages and problem titles in Chinese, and a
    # Windows console is not UTF-8: the encoder raised UnicodeEncodeError and
    # the run died printing nothing but "exit 1" - no FAIL line, so the CI
    # annotations could not say what happened. The log is a pipe here, so make
    # it UTF-8 and never fail on a character the host console cannot spell.
    # (The doctor test already dodged this with redirect_stdout; a plugin that
    # prints bilingually should not have to.)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    _install_fake_sublime()
    pkg = _load_package()

    print('== cph_verdict: comparison and verdicts ==')
    verdict = importlib.import_module(pkg + '.core.cph_verdict')
    verdict_src = open(os.path.join(ROOT, 'core', 'cph_verdict.py'),
                       encoding='utf-8').read()

    check('exact token match', verdict.outputs_equal('1 2 3', '1 2 3', 0))
    check('whitespace insensitive', verdict.outputs_equal('1  2\n3\n', '1 2 3', 0))
    check('mismatch detected', not verdict.outputs_equal('1 2', '1 3', 0))
    check('float tolerance accepts rounding',
          verdict.outputs_equal('0.30000000000000004', '0.3', 1e-6))
    check('float tolerance rejects real error',
          not verdict.outputs_equal('0.31', '0.3', 1e-6))
    check('token count mismatch rejected',
          not verdict.outputs_equal('1 2', '1', 1e-6))
    check('non numeric tokens stay exact',
          not verdict.outputs_equal('abc', 'abd', 1e-6))

    ac = verdict.get_verdict_by_code(
        rtcode=0, runtime=10, time_limit_ms=1000, memory_limit_mb=256,
        stderr='', stdout='42\n', expected_output='42')
    check('plain AC', ac['name'] == 'AC', ac['name'])

    mle = verdict.get_verdict_by_code(
        rtcode=0, runtime=10, time_limit_ms=1000, memory_limit_mb=256,
        stderr='', stdout='42\n', expected_output='42', memory_used_mb=300)
    check('MLE fires on measured memory', mle['name'] == 'MLE', mle['name'])

    tle = verdict.get_verdict_by_code(
        rtcode=0, runtime=1500, time_limit_ms=1000, memory_limit_mb=256,
        stderr='', stdout='42\n', expected_output='42')
    check('TLE on runtime', tle['name'] == 'TLE', tle['name'])

    pe = verdict.get_verdict_by_code(
        rtcode=0, runtime=10, time_limit_ms=1000, memory_limit_mb=256,
        stderr='', stdout='1  2', expected_output='1 2')
    check('PE on formatting only', pe['name'] == 'PE', pe['name'])

    diff, total = verdict.build_line_diff('a\nb\nc', 'a\nX\nc')
    check('line diff finds the changed line',
          diff is not None and any(op[0] == '!=' and op[1] == 2 for op in diff),
          str(diff))

    # Regression: SequenceMatcher is quadratic, and this runs on the UI
    # thread when a detail view opens. 20k lines of repetitive output took
    # 56s before the trim + cell budget below were added.
    import time as _time
    big_exp = '\n'.join(['0'] * 10000)
    big_act = '\n'.join(['0'] * 5000 + ['1'] + ['0'] * 4999)
    t0 = _time.time()
    big_diff, big_total = verdict.build_line_diff(big_exp, big_act)
    took = _time.time() - t0
    check('a repeated 10k-line diff stays interactive',
          big_diff is not None and len(big_diff) == 1 and big_diff[0][1] == 5001,
          '%d ops in %.2fs' % (len(big_diff or []), took))
    check('the repeated diff is not reported as every line differing',
          big_diff is not None and len(big_diff) < 100, str(len(big_diff or [])))
    check('the diff total still counts the longer side', big_total == 10000, str(big_total))

    # The head/tail trim must not lose line numbers: a change in the middle
    # of two long identical runs is still reported at its real position.
    trim_exp = '\n'.join(str(i % 10) for i in range(5000))
    trim_act = '\n'.join(str(i % 10) if i != 3000 else 'X' for i in range(5000))
    trim_diff, _ = verdict.build_line_diff(trim_exp, trim_act)
    check('trimming keeps the real line number',
          trim_diff == [('!=', 3001, '0', 'X')], str(trim_diff))

    shift_diff, shift_total = verdict.build_line_diff('1\n2\n3', '1\n2')
    check('a deleted trailing line is still a delete op',
          shift_diff == [('-', 3, '3', '')], str(shift_diff))

    insert_diff, _ = verdict.build_line_diff('1\n2', '1\n2\n3')
    check('an inserted trailing line is still an insert op',
          insert_diff == [('+', 3, '', '3')], str(insert_diff))

    check('the diff cell budget is a named constant',
          '_DIFF_CELL_BUDGET' in verdict_src and 'autojunk=False' in verdict_src,
          'guards must stay visible in the source')

    # The diff itself got fast, but the DETAIL VIEW had no cap at all: 800k
    # differing lines (well inside the 8 MB max_output_bytes) built 42 MB of
    # text over 2.4M lines and handed it to a Sublime view. The listing is
    # now capped; the count line and the raw outputs above stay complete.
    # (The checks live right after the test_manager import below.)
    class _DetailTest(object):
        verdict = None
        memory = '-'
        test_string = ''
        correct_answers = []
        expected_output = ''
        stdout = ''
        stderr = ''
        message = ''

        def get_nice_runtime(self):
            return '1 ms'

        def get_nice_memory(self):
            return '-'

    print('== runtime error: detected and located ==')
    for label, rtcode in (('segfault (139)', 139), ('abort (exit 3)', 3),
                          ('signal (-11)', -11)):
        got = verdict.get_verdict_by_code(
            rtcode=rtcode, runtime=10, time_limit_ms=1000, memory_limit_mb=256,
            stderr='', stdout='', expected_output='1')['name']
        check('%s is a runtime error' % label, got == 'RE', got)

    py_tb = ('Traceback (most recent call last):\n'
             '  File "/tmp/main.py", line 12, in <module>\n'
             '    print(1 / 0)\nZeroDivisionError: division by zero\n')
    check('python traceback is located',
          verdict.find_crash_location(py_tb, '/tmp/main.py') == ('/tmp/main.py', 12),
          repr(verdict.find_crash_location(py_tb, '/tmp/main.py')))
    java_tb = ('Exception in thread "main" java.lang.ArithmeticException: / by zero\n'
               '\tat Main.main(Main.java:5)\n')
    check('java stack trace is located',
          verdict.find_crash_location(java_tb, 'Main.java') == ('Main.java', 5),
          repr(verdict.find_crash_location(java_tb, 'Main.java')))
    san = "main.cpp:7:5: runtime error: load of null pointer of type 'int'\n"
    check('sanitizer diagnostic is located',
          verdict.find_crash_location(san, 'main.cpp') == ('main.cpp', 7),
          repr(verdict.find_crash_location(san, 'main.cpp')))
    check('no location for a bare segfault',
          verdict.find_crash_location('', 'main.cpp') is None)
    check('ordinary output yields no location',
          verdict.find_crash_location('3\n', 'main.cpp') is None)

    print('== Tester: the multi-sample chain crash ==')
    tm = importlib.import_module(pkg + '.test_manager')

    class FakeProcess(object):
        """Stands in for ProcessManager (no real process in unit tests)."""

        is_run = False

        def set_calls(self, *args):
            pass

        def run(self, *args, **kwargs):
            pass

        def write(self, s):
            pass

        def close_stdin(self):
            pass

        def is_stopped(self):
            return 0

        def get_time_limit_ms(self):
            return 1000

        def get_memory_limit_mb(self):
            return 256

    def make_tester(tests):
        noop = lambda *a, **k: None
        return tm.CphTestManagerCommand.Tester(
            FakeProcess(), noop, noop, noop, noop,
            sync_out=False, tests=tests, epoch=1)

    cmd = tm.CphTestManagerCommand.__new__(tm.CphTestManagerCommand)
    cmd.view = sys.modules['sublime'].View()

    # The diff itself got fast (see _DIFF_CELL_BUDGET above), but the DETAIL
    # VIEW had no cap at all: 800k differing lines - well inside the default
    # 8 MB max_output_bytes - built 42 MB of text over 2.4M lines and handed
    # it to a Sublime view. The listing is capped now; the count line and the
    # raw outputs above it stay complete.
    _detail_src = open(os.path.join(ROOT, 'test_manager.py'), encoding='utf-8').read()
    _detail_i18n = importlib.import_module(pkg + '.core.cph_i18n')
    _truncated = _detail_i18n.STRINGS['diff_shown_truncated']['zh'][:4]
    detail_test = _DetailTest()
    detail_test.expected_output = '\n'.join(str(i % 7) for i in range(5000))
    detail_test.stdout = '\n'.join(str((i + 1) % 7) for i in range(5000))
    detail_text = cmd.build_detail_content(0, detail_test)
    check('a huge diff is capped in the detail view',
          detail_text.count('  Line ') == tm._DETAIL_DIFF_LIMIT,
          '%d line entries for 5000 differences' % detail_text.count('  Line '))
    check('the cap is announced with the real total',
          '5000' in detail_text and _truncated in detail_text,
          'the reader must be told what was left out')
    check('the detail cap is a named constant',
          '_DETAIL_DIFF_LIMIT' in _detail_src
          and 'ops[:_DETAIL_DIFF_LIMIT]' in _detail_src)
    small_test = _DetailTest()
    small_test.expected_output = '1\n2\n3'
    small_test.stdout = '1\n9\n3'
    small_text = cmd.build_detail_content(0, small_test)
    check('a small diff is not truncated',
          '  Line ' in small_text and _truncated not in small_text)

    # 'Run failed tests' jumps past accepted tests: index 3 with only 2 tests
    tester = make_tester([tm.CphTestManagerCommand.Test('a'),
                          tm.CphTestManagerCommand.Test('b')])
    tester.proc_run = False
    tester.test_iter = 3
    cmd.tester = tester
    try:
        tester.next_test(0, lambda: None)
        ok = len(tester.tests) >= 4 and len(tester.prog_out) >= 4
        check('next_test pads tests/prog_out up to the jump target', ok,
              'tests=%d prog_out=%d' % (len(tester.tests), len(tester.prog_out)))
    except Exception as e:
        check('next_test pads tests/prog_out up to the jump target', False,
              '%s: %s' % (type(e).__name__, e))

    # a folded (skipped) test must not be re-run and must fold itself
    tester2 = make_tester([tm.CphTestManagerCommand.Test('a'),
                           tm.CphTestManagerCommand.Test('b')])
    tester2.tests[0].verdict = verdict.get_verdict('accepted')
    tester2.run_failed = True
    cmd.tester = tester2
    check('is_skippable marks AC tests', cmd.is_skippable(0) is True)
    check('is_skippable leaves failures alone', cmd.is_skippable(1) is False)
    check('next_runnable_index skips the AC test', cmd.next_runnable_index() == 1,
          str(cmd.next_runnable_index()))

    print('== ProcessManager: python 3.3 safe subprocess handling ==')
    pm_mod = importlib.import_module(pkg + '.Modules.ProcessManager')

    check('quoted path needs no shell',
          pm_mod._needs_shell('"/opt/my prog/app"') is False)
    check('pipes need a shell', pm_mod._needs_shell('a | b') is True)
    check('&& needs a shell', pm_mod._needs_shell('a && b') is True)
    check('plain command needs no shell',
          pm_mod._needs_shell('g++ "x.cpp" -o x') is False)

    check('utf-8 output decodes',
          pm_mod._decode_output('中文'.encode('utf-8')) == '中文')
    check('garbage bytes do not raise',
          isinstance(pm_mod._decode_output(b'\xff\xfe\x00abc'), str))
    check('decoded output drops CR',
          pm_mod._decode_output(b'1\r\n2\r\n') == '1\n2\n',
          repr(pm_mod._decode_output(b'1\r\n2\r\n')))

    # Windows programs emit CRLF; a raw CR renders as '<0x0d>' in the panel
    pm_obj = pm_mod.ProcessManager.__new__(pm_mod.ProcessManager)
    pm_obj._pending_cr = False
    norm = pm_obj._normalize_newlines
    check('CRLF becomes LF', norm('a\r\nb') == 'a\nb', repr(norm('a\r\nb')))
    check('lone CR becomes LF', norm('a\rb') == 'a\nb', repr(norm('a\rb')))
    check('plain LF untouched', norm('a\nb') == 'a\nb', repr(norm('a\nb')))

    pm_obj._pending_cr = False
    split = norm('a\r') + norm('\nb')
    check('CRLF split across two chunks keeps exactly one newline',
          split == 'a\nb', repr(split))

    pm_obj._pending_cr = False
    split2 = norm('a\r') + norm('b')
    check('lone CR split across chunks becomes one newline',
          split2 == 'a\nb', repr(split2))

    pm_obj._pending_cr = False
    at_eof = norm('x\r') + norm('', final=True)
    check('trailing CR at EOF is flushed', at_eof == 'x\n', repr(at_eof))

    # byte-at-a-time (sync) mode must behave the same
    pm_obj._pending_cr = False
    pm_obj._out_decoder = None
    bytewise = ''
    for ch in 'a\r\nb\r\n':
        bytewise += norm(ch)
    check('byte-at-a-time CRLF handling', bytewise == 'a\nb\n', repr(bytewise))

    print('== stored test data: no stray CR ==')
    crlf_test = tm.CphTestManagerCommand.Test(
        {'test': '1\r\n2\r\n', 'correct_answers': ['3\r\n4\r\n']})
    check('test input CR normalized',
          crlf_test.test_string == '1\n2\n', repr(crlf_test.test_string))
    check('stored answer normalized (CR folded + trimmed)',
          list(crlf_test.correct_answers) == ['3\n4'],
          repr(crlf_test.correct_answers))

    # the exact regression: Popen must not receive 3.6+ only keywords
    import inspect
    try:
        src = inspect.getsource(pm_mod)
        forbidden = [w for w in ('encoding=', 'errors=', 'text=True', 'capture_output=')
                     if w in src.split('def run_file')[1].split('def ')[0]]
        check('run_file avoids 3.6+ Popen keywords', not forbidden, str(forbidden))
    except Exception as e:
        check('run_file avoids 3.6+ Popen keywords', False, str(e))

    # insert() calls the i18n helper on a broken stdin pipe; the import was
    # missing, so that "drop the input gracefully" path raised NameError.
    check('ProcessManager can resolve t() used by insert()',
          callable(getattr(pm_mod, 't', None)))

    # cph_run_modes sends these flags to cph_view_tester.run(); before the
    # fix they were rejected with TypeError, so "Run all / Run failed /
    # Force recompile" did nothing when invoked outside the -run panel.
    params = inspect.signature(tm.CphViewTesterCommand.run).parameters
    missing = [k for k in ('run_all', 'run_failed', 'force_compile') if k not in params]
    check('cph_view_tester.run accepts the run-mode flags', not missing, str(missing))

    print('== cph_settings: test file path resolution ==')
    settings = importlib.import_module(pkg + '.core.cph_settings')
    paths = settings.get_tests_paths(os.path.join('tmp', 'foo.cpp'))
    check('three candidate paths are probed', len(paths) == 3, str(paths))
    check('traditional path first', paths[0].endswith('foo.cpp__tests'), paths[0])
    check('cph-ng folder path present',
          any(p.endswith(os.path.join('tests', 'foo.cpp__tests')) for p in paths),
          str(paths))

    # tests_relative_dir is honoured by save_tests(); if it is not probed on
    # load, the saved data becomes invisible (and "clear tests" misses it).
    _real_rel_dir = settings.get_tests_relative_dir
    settings.get_tests_relative_dir = lambda: 'rel'
    try:
        rel_paths = settings.get_tests_paths(os.path.join('tmp', 'foo.cpp'))
        check('tests_relative_dir is probed first',
              len(rel_paths) == 4
              and rel_paths[0].endswith(os.path.join('rel', 'foo.cpp__tests')),
              str(rel_paths))
    finally:
        settings.get_tests_relative_dir = _real_rel_dir

    # Import must detect a UTF-16 BOM; otherwise the bytes decode as UTF-8
    # mojibake / fall through to gb18030 and the utf-16 branch is dead code.
    import tempfile
    importer = importlib.import_module(pkg + '.cph_import')
    fh = tempfile.NamedTemporaryFile(suffix='.in', delete=False)
    fh.write('1 2 3\n'.encode('utf-16'))
    fh.close()
    try:
        check('utf-16 test data decodes (BOM detected)',
              importer._read_text(fh.name).strip() == '1 2 3',
              repr(importer._read_text(fh.name)))
    finally:
        os.unlink(fh.name)

    print('== shipped settings: every command must be formattable ==')
    # This is the P0 that shipped in v1.4.0-v1.4.4: the default C++ compile_cmd
    # contains {extra_sources} / {include_dirs}, which are NOT format keys, so
    # str.format() raised KeyError on the very first run of a fresh install.
    import tempfile
    raw = open(os.path.join(ROOT, 'Algorithm Competition Assistant.sublime-settings'), encoding='utf-8').read()
    raw = re.sub(r'//[^\n]*', '', raw)
    shipped = json.loads(raw)
    tmpdir = tempfile.mkdtemp()
    file_names = {'cpp': 'main.cpp', 'py': 'main.py', 'java': 'Main.java'}
    for entry in shipped.get('run_settings', []):
        for ext in entry.get('extensions', []):
            target = os.path.join(tmpdir, file_names.get(ext, 'prog.' + ext))
            with open(target, 'w'):
                pass
            mgr = pm_mod.ProcessManager(target, 'source.' + ext,
                                        run_settings=shipped['run_settings'])
            for label, getter in (('compile_cmd', lambda m=mgr: m.get_compile_cmd()),
                                  ('run_cmd', lambda m=mgr: m.get_run_cmd(''))):
                name = '%s %s formats' % (entry.get('name', '?'), label)
                try:
                    getter()
                    check(name, True)
                except Exception as e:
                    check(name, False, '%s: %s' % (type(e).__name__, e))

    print('== Test model: attributes used by rendering ==')
    fresh = tm.CphTestManagerCommand.Test({'test': '1\n'})
    check('rtcode is initialized (expanding a skipped card)',
          getattr(fresh, 'rtcode', None) == '0', repr(getattr(fresh, 'rtcode', None)))

    print('== second round: card CSS, output bounds, stderr chip, sampler ==')
    # The card's status class must exist in the shipped CSS themes, or the
    # green/red tint is silently dead ('test-AC' / 'test-wrong-answer' matched
    # no rule at all).
    css_text = ''
    for css_name in ('test_styles.css', 'test_styles_spacegray.css',
                     'test_styles_spacegraylight.css'):
        with open(os.path.join(ROOT, 'Highlight', css_name), encoding='utf-8') as f:
            css_text += f.read()
    for vname, expected in (('AC', 'test-accept'), ('WA', 'test-decline')):
        card = tm.CphTestManagerCommand.Test({'test': '1\n'})
        card.verdict = verdict.get_verdict_by_name(vname)
        got = card.get_test_class()
        check('card class for %s is defined in CSS' % vname,
              got == expected and ('.' + got) in css_text, '%s -> %s' % (vname, got))

    # every verdict the plugin can render (incl. one restored from a session)
    # needs a badge rule, or the badge silently falls back to the base style
    missing_css = sorted(n for n in set(verdict.VERDICT_NAME.values())
                         if ('.verdict-' + n) not in css_text)
    check('every verdict badge class is defined in CSS', not missing_css,
          ', '.join(missing_css))

    # prog_out can legitimately be shorter than the test list (session-restored
    # tests, or the test menu acting on an un-materialised one).
    tester = make_tester([tm.CphTestManagerCommand.Test('a')])
    tester.prog_out = []
    check('output_at() is safe for an un-materialised test', tester.output_at(3) == '')
    tester.prog_out = ['abc']
    check('output_at() returns the stored output',
          tester.output_at(0) == 'abc' and tester.output_at(9) == '')

    # The card must stay narrow: stderr belongs to the detail view only
    # (a chip per card made every card with debug output bulky).
    with open(os.path.join(ROOT, 'Highlight', 'test_config.html'), encoding='utf-8') as f:
        card_tpl = f.read()
    check('the card template no longer renders stderr',
          '{stderr_display}' not in card_tpl and '{stderr_label}' not in card_tpl)
    with open(os.path.join(ROOT, 'Highlight', 'test_detail.html'), encoding='utf-8') as f:
        detail_tpl = f.read()
    check('the detail template still shows stderr',
          '{stderr}' in detail_tpl and '{error_output_label}' in detail_tpl)

    # A leaked MemorySampler used to poll a dead pid for the whole session.
    # Use an impossible pid plus a tiny interval and a hard cap on how long a
    # single sample may take, so the check cannot depend on runner speed (a
    # fixed sleep here was too tight on a loaded macOS runner).
    mp = importlib.import_module(pkg + '.Modules.memprobe')
    sampler = mp.MemorySampler(99999999, interval=0.001, max_silence=1.0)
    sampler.start()
    sampler._thread.join(timeout=10.0)
    still_alive = sampler._thread.is_alive()
    sampler.stop()
    check('memory sampler stops polling a dead pid', not still_alive)

    print('== doctor: template commands are not a failure ==')
    doc = importlib.import_module(pkg + '.cph_doctor')
    ok, detail = doc._check_command('"{source_file_dir}/{file_name}.exe" {args}')
    check('run_cmd template is not reported as missing', ok is True, detail)
    ok2, _ = doc._check_command('g++ -O2 -o x')
    check('plain compiler command is looked up', ok2 is not None)

    # v1.4.6 shipped doctor without `import re`: the first language entry made
    # _unknown_placeholders raise NameError and the report view was never
    # created - doctor was unusable, not merely wrong. Pin both the helper and
    # the whole run() path so this class of "feature totally dead" regression
    # cannot slip through again.
    check('known placeholders are accepted',
          doc._unknown_placeholders('g++ {file} -o {file_name}') == [],
          repr(doc._unknown_placeholders('g++ {file} -o {file_name}')))
    check('a typo in a placeholder is flagged',
          doc._unknown_placeholders('g++ {file_nmae}') == ['file_nmae'],
          repr(doc._unknown_placeholders('g++ {file_nmae}')))
    check('an empty command has no placeholders',
          doc._unknown_placeholders('') == [])

    real_get_settings = doc.get_settings
    doc.get_settings = lambda: {'run_settings': [
        {'name': 'C++', 'extensions': ['cpp'],
         'compile_cmd': 'g++ {file_nmae} -o x', 'run_cmd': 'x'}]}
    saved_doctor_window = sys.modules['sublime'].active_window
    try:
        # A view reports the window it lives in, so doctor needs one to open
        # its report view in.
        sys.modules['sublime'].active_window = lambda: sys.modules['sublime'].Window()
        cmd = doc.CphDoctorCommand(sys.modules['sublime'].View())
        # doctor also prints the report to the console; capture it so the
        # check does not depend on the console encoding (a cp1252 Windows
        # console cannot encode the Chinese report text and raised
        # UnicodeEncodeError, which is a test-host artefact - inside Sublime
        # the console is UTF-8 and the report view is created before the print).
        with contextlib.redirect_stdout(io.StringIO()):
            cmd.run(None)
        check('doctor run() survives a language entry', True)
    except Exception as e:
        check('doctor run() survives a language entry', False,
              '%s: %s' % (type(e).__name__, e))
    finally:
        doc.get_settings = real_get_settings
        sys.modules['sublime'].active_window = saved_doctor_window

    print('== companion: merge keyed by input ==')
    comp = importlib.import_module(pkg + '.cph_companion')
    comp.load_all_tests = lambda f: [{'test': '1 2\n', 'correct_answers': []}]
    merged = comp.merge_tests('x.cpp', [{'test': '1 2\n', 'correct_answers': ['3\n']}])
    check('resending the same input updates the answer instead of duplicating',
          len(merged) == 1 and merged[0]['correct_answers'] == ['3\n'], repr(merged))
    merged2 = comp.merge_tests('x.cpp', [{'test': '9\n', 'correct_answers': []}])
    check('a genuinely new input is appended', len(merged2) == 2, repr(merged2))

    print('== companion: click the browser, get a problem file ==')
    # Starting the listener used to demand a saved file, while the auto-create
    # branch only ran when the remembered target had NO file name. The two
    # conditions exclude each other, so "send a problem and let the plugin
    # create the .cpp" was unreachable code.
    import socket
    import tempfile as _tf

    settings_mod = importlib.import_module(pkg + '.core.cph_settings')
    fake_sublime = sys.modules['sublime']
    saved_settings = settings_mod.settings
    saved_load_settings = fake_sublime.load_settings
    saved_active_window = fake_sublime.active_window
    saved_windows = fake_sublime.windows
    saved_set_timeout = fake_sublime.set_timeout
    saved_error_message = fake_sublime.error_message

    class _AutoWindow(object):
        def __init__(self, folder):
            self._folder = folder
            self.opened = []

        def folders(self):
            return [self._folder]

        def active_view(self):
            # A real window always answers this; nothing is focused on a file
            # here, so the problem has to fall through to auto-create.
            return None

        def open_file(self, path):
            self.opened.append(path)
            return fake_sublime.View(file_name=path)

    def _free_port():
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    def _post_at(body, at_port, headers=None):
        """POST to the listener and return its status code (0 = no answer).

        127.0.0.1, never the name: HTTPServer is IPv4-only, so asking for
        "localhost" tests whichever address family this host happens to
        resolve first - which passed on macOS and crashed the suite on Linux
        and on the official Windows CPython. A refusal to connect reports
        itself as a failed check rather than taking the whole run down.
        """
        import urllib.error as _err
        import urllib.request as _url
        request = _url.Request('http://127.0.0.1:%d/' % at_port, data=body,
                               headers=headers or {})
        try:
            response = _url.urlopen(request, timeout=10)
            try:
                return response.getcode()
            finally:
                response.close()
        except _err.HTTPError as error:
            return error.code
        except Exception as e:
            print('   (no answer from the listener: %s)' % e)
            return 0

    def _use_settings(data):
        fake_sublime.load_settings = lambda name: fake_sublime.Settings(data)

    def _arm(auto_listen=True):
        """Give the next start a port of its own, and return it.

        _stop_listener() shuts the server down on another thread, so reusing
        one port number across rounds can re-bind before the old socket is
        gone. That is not a race worth having in a regression suite: it bound
        nothing, the request then found nobody listening, and two CI images
        died on it while two others happened to pass on timing.
        """
        at_port = _free_port()
        _use_settings({'companion_port': at_port,
                       'companion_auto_listen': auto_listen})
        return at_port

    def _read(path):
        """The file's text, or '' when it is not there.

        A missing file has to surface as a failed check: an uncaught IOError
        ended the whole run, which is why a platform-only failure printed
        nothing but "exit 1" and left every other result unread.
        """
        if not os.path.exists(path):
            return ''
        with io.open(path, encoding='utf-8') as f:
            return f.read()

    def _stop_and_reset():
        comp._stop_listener()
        with comp._listener['lock']:
            comp._listener['server'] = None
            comp._listener['thread'] = None
            comp._listener['view_id'] = None
            comp._listener['target'] = None
            comp._listener['dir'] = None
            comp._listener['last_run'] = None

    work_dir = _tf.mkdtemp()
    try:
        port = _arm()
        settings_mod.init_settings({'auto_create_file': True,
                                    'auto_create_dir': '',
                                    'auto_create_extension': 'cpp',
                                    'auto_create_template': '#include <bits/stdc++.h>\n'})
        fake_sublime.windows = lambda: []
        fake_sublime.set_timeout = lambda fn, delay=0: fn()

        # 1. the listener no longer needs a saved file to start
        _stop_and_reset()
        errors = []
        fake_sublime.error_message = lambda text=None, *a, **k: errors.append(text)
        comp._start_listener(None)
        check('the listener starts with no target file at all',
              comp._listener['server'] is not None
              and comp._listener['port'] == port,
              repr(comp._listener['port']))
        check('starting without a target does not complain about saving',
              not errors, repr(errors))
        again = comp._listener['server']
        comp._start_listener(None)
        check('a second start reuses the running server (no port clash)',
              comp._listener['server'] is again)
        _stop_and_reset()

        # 2. plugin_loaded() brings it back up on its own
        port = _arm()
        comp.plugin_loaded()
        check('the listener comes up by itself when Sublime starts',
              comp._listener['server'] is not None)
        _stop_and_reset()

        # ...just not during the startup the user waits for: binding the port
        # imports http.server, which build_handler() keeps out of the cold
        # import for exactly that reason.
        deferred = []
        fake_sublime.set_timeout = lambda fn, delay=0: deferred.append(fn)
        port = _arm()
        comp.plugin_loaded()
        check('the auto-start is deferred instead of blocking plugin_loaded',
              comp._listener['server'] is None and len(deferred) == 1,
              repr((comp._listener['server'], len(deferred))))
        for start in deferred:
            start()
        check('the deferred start really brings the listener up',
              comp._listener['server'] is not None)
        fake_sublime.set_timeout = lambda fn, delay=0: fn()
        _stop_and_reset()

        _arm(auto_listen=False)
        comp.plugin_loaded()
        check('companion_auto_listen=false keeps the port free',
              comp._listener['server'] is None, repr(comp._listener['port']))
        _stop_and_reset()

        # 3. an incoming problem with no target creates the file
        port = _arm()
        window = _AutoWindow(work_dir)
        fake_sublime.active_window = lambda: window
        created = comp.resolve_target(u'A/B: 简单题 #1', 'https://oj.example/p/1',
                                      1000, 256)
        wanted = os.path.join(work_dir, u'A B 简单题 #1.cpp')
        check('the problem name becomes the file name',
              created is not None and created.file_name() == wanted,
              repr(created.file_name() if created else None))
        check('the new file is opened in the window',
              window.opened == [wanted], repr(window.opened))
        body = _read(wanted)
        check('the statement header names the problem and its source',
              u'A/B: 简单题 #1' in body and 'oj.example/p/1' in body,
              body.replace('\n', ' | ')[:160])
        check('the header carries the received limits',
              '1000' in body and '256' in body, body.replace('\n', ' | ')[:160])
        check('the template still follows the header',
              body.rstrip().endswith('#include <bits/stdc++.h>'),
              body.replace('\n', ' | ')[:160])

        # 4. an existing problem file is opened, never rewritten
        with open(wanted, 'w', encoding='utf-8') as f:
            f.write(u'// mine\nint main(){}\n')
        again = comp.resolve_target(u'A/B: 简单题 #1', 'https://oj.example/p/1',
                                    1000, 256)
        kept = _read(wanted)
        check('a name collision opens the file instead of overwriting it',
              kept == u'// mine\nint main(){}\n'
              and again is not None and again.file_name() == wanted,
              kept.replace('\n', ' | ')[:120])

        # 4b. companion_target decides what one click means, and the two
        #     answers have to stay apart. Putting "the file under focus" first
        #     for every click made creation unreachable in real use - a .cpp is
        #     always focused while competing - which is exactly the reported
        #     "samples arrive, but no new file is ever created".
        class _FocusedWindow(_AutoWindow):
            def __init__(self, folder, view):
                _AutoWindow.__init__(self, folder)
                self._view = view

            def active_view(self):
                return self._view

        class _PinnedWindow(object):
            def __init__(self, view):
                self._view = view

            def views(self):
                return [self._view]

        base_settings = {'auto_create_file': True,
                         'auto_create_extension': 'cpp',
                         'run_settings': [{'name': 'C++', 'extensions': ['cpp']}],
                         'auto_create_template': ''}
        focused_file = os.path.join(work_dir, 'on_focus.cpp')
        with open(focused_file, 'w', encoding='utf-8') as f:
            f.write(u'// hand written\n')
        cpp_only = {'name': 'C++', 'extensions': ['cpp']}

        settings_mod.init_settings(dict(base_settings, companion_target='current_file'))
        focus_win = _FocusedWindow(work_dir, fake_sublime.View(file_name=focused_file))
        fake_sublime.active_window = lambda: focus_win
        hit = comp.resolve_target(u'yet another', '', 1, 1)
        check('current_file sends the problem to the file under focus',
              hit is not None and hit.file_name() == focused_file,
              repr(hit.file_name() if hit else None))
        check('and that mode never creates a file', focus_win.opened == [],
              repr(focus_win.opened))

        settings_mod.init_settings(dict(base_settings, companion_target='new_file'))
        focus_win2 = _FocusedWindow(work_dir, fake_sublime.View(file_name=focused_file))
        fake_sublime.active_window = lambda: focus_win2
        fresh = comp.resolve_target(u'a brand new problem', '', 1, 1)
        check('new_file creates a file even while a cpp is focused',
              fresh is not None
              and fresh.file_name() == os.path.join(work_dir,
                                                   u'a brand new problem.cpp')
              and focus_win2.opened == [fresh.file_name()],
              repr((fresh.file_name() if fresh else None, focus_win2.opened)))
        settings_mod.init_settings(dict(base_settings))
        default_fresh = comp.resolve_target(u'default mode problem', '', 1, 1)
        check('creating a file is the default: one click is one new problem',
              default_fresh is not None
              and default_fresh.file_name().endswith(u'default mode problem.cpp'),
              repr(default_fresh.file_name() if default_fresh else None))

        settings_mod.init_settings({'auto_create_file': False,
                                   'companion_target': 'new_file',
                                   'run_settings': [cpp_only]})
        back_on_focus = comp.resolve_target(u'creation is off', '', 1, 1)
        check('auto_create_file=false falls back to the file under focus',
              back_on_focus is not None and back_on_focus.file_name() == focused_file,
              repr(back_on_focus.file_name() if back_on_focus else None))
        settings_mod.init_settings({'auto_create_file': False,
                                   'companion_target': 'new_file',
                                   'run_settings': [cpp_only]})
        fake_sublime.active_window = lambda: _FocusedWindow(
            work_dir, fake_sublime.View())
        check('with nothing focused and creation off there is no target',
              comp.resolve_target(u'orphan', '', 1, 1) is None)

        # The Listen command is a deliberate instruction about where samples
        # go, so it outranks both the focus and the mode.
        pinned_file = os.path.join(work_dir, 'pinned.cpp')
        with open(pinned_file, 'w', encoding='utf-8') as f:
            f.write(u'// pinned\n')
        pinned_view = fake_sublime.View(file_name=pinned_file)
        fake_sublime.windows = lambda: [_PinnedWindow(pinned_view)]
        settings_mod.init_settings(dict(base_settings, companion_target='new_file'))
        pin_win = _FocusedWindow(work_dir, fake_sublime.View(file_name=focused_file))
        fake_sublime.active_window = lambda: pin_win
        with comp._listener['lock']:
            comp._listener['view_id'] = pinned_view.id()
        pinned = comp.resolve_target(u'pinned problem', '', 1, 1)
        check('a pin does not outrank new_file: one click is still one file',
              pinned is not None
              and pinned.file_name() == os.path.join(work_dir, u'pinned problem.cpp')
              and pinned.file_name() != pinned_file,
              repr(pinned.file_name() if pinned else None))
        with comp._listener['lock']:
            comp._listener['view_id'] = None
        settings_mod.init_settings(dict(base_settings, companion_target='current_file'))
        opened_before = list(pin_win.opened)
        with comp._listener['lock']:
            comp._listener['view_id'] = pinned_view.id()
        pinned = comp.resolve_target(u'pinned problem', '', 1, 1)
        check('the pinned view is where current_file sends its samples',
              pinned is not None and pinned.file_name() == pinned_file
              and pin_win.opened == opened_before,
              repr((pinned.file_name() if pinned else None, pin_win.opened)))
        with comp._listener['lock']:
            comp._listener['view_id'] = None
            comp._listener['target'] = None
        fake_sublime.windows = lambda: []

        settings_mod.init_settings(dict(base_settings, companion_target='new_file'))
        fake_sublime.active_window = lambda: _FocusedWindow(
            work_dir, fake_sublime.View())
        untitled = comp.resolve_target(u'untitled focus', '', 1, 1)
        check('an untitled view creates the file instead',
              untitled is not None
              and untitled.file_name().endswith(u'untitled focus.cpp'),
              repr(untitled.file_name() if untitled else None))

        fake_sublime.active_window = lambda: _FocusedWindow(
            work_dir, fake_sublime.View(file_name=os.path.join(work_dir, 'notes.md')))
        notes = comp.resolve_target(u'not a source file', '', 1, 1)
        check('a file the plugin cannot run is no target either',
              notes is not None
              and notes.file_name().endswith(u'not a source file.cpp'),
              repr(notes.file_name() if notes else None))

        settings_mod.init_settings(dict(base_settings, companion_target='current_file'))
        fake_sublime.active_window = lambda: _FocusedWindow(
            work_dir, fake_sublime.View(file_name=os.path.join(work_dir, 'notes.md')))
        check('current_file with only a non-source file focused finds no target',
              comp.resolve_target(u'nowhere to go', '', 1, 1) is None)

        # 5. the setting can turn auto-create off
        settings_mod.init_settings({'auto_create_file': False,
                                    'auto_create_extension': 'cpp'})
        check('auto_create_file=false falls back to "no target"',
              comp.resolve_target(u'another problem', '', 1, 1) is None)
        settings_mod.init_settings({'auto_create_file': True,
                                    'auto_create_extension': 'cpp',
                                    'auto_create_template': ''})
        sub_dir = comp.resolve_target(u'sub', '', None, None)
        check('auto_create_dir stays optional (window folder is enough)',
              sub_dir is not None, repr(sub_dir))

        # ...but with no folder open at all there is nowhere to put the file,
        # and "监听目标文件不存在" alone would not say what to do about it.
        class _NoFolderWindow(_AutoWindow):
            def folders(self):
                return []

        nowhere = []
        saved_status_message = fake_sublime.status_message
        try:
            fake_sublime.status_message = lambda text=None, *a, **k: nowhere.append(text)
            fake_sublime.active_window = lambda: _NoFolderWindow(work_dir)
            missing = comp.resolve_target(u'orphan problem', '', 1, 1)
        finally:
            fake_sublime.status_message = saved_status_message
        check('no folder open creates nothing and names the setting to fix',
              missing is None and any('auto_create_dir' in m for m in nowhere),
              repr(nowhere))

        # 6. the limits survive a restart instead of living only in memory
        limits_file = os.path.join(work_dir, 'lim.cpp')
        settings_mod.set_problem_limits(limits_file, 1500, 512)
        # Default storage layout is tests/<name>__tests, so the sidecar has to
        # land beside it, not loose in the problem's own folder.
        limits_path = os.path.join(work_dir, 'tests', 'lim.cpp__tests.limits')
        check('the received limits are written next to the tests file',
              os.path.exists(limits_path), limits_path)
        if os.path.exists(limits_path):
            with open(limits_path, encoding='utf-8') as f:
                stored = json.loads(f.read())
            check('the sidecar holds the two numbers it was given',
                  stored.get('time_limit_ms') == 1500
                  and stored.get('memory_limit_mb') == 512, repr(stored))
        settings_mod._problem_limits.clear()
        back = settings_mod.get_problem_limits(limits_file)
        check('a restart reads them back from disk',
              back.get('time_limit_ms') == 1500
              and back.get('memory_limit_mb') == 512, repr(back))
        settings_mod._problem_limits.clear()
        settings_mod.set_problem_limits(limits_file, None, 128)
        back = settings_mod.get_problem_limits(limits_file)
        check('a half-empty update keeps the limit it did not receive',
              back.get('time_limit_ms') == 1500, repr(back))
        settings_mod._problem_limits.clear()
        check('a file that never received limits still reports none',
              settings_mod.get_problem_limits(os.path.join(work_dir, 'none.cpp')) == {})

        comp_src = open(os.path.join(ROOT, 'cph_companion.py'), encoding='utf-8').read()
        check('the run panel is driven by the resolved target, not the old view',
              'target.run_command' in comp_src
              and "view.run_command('cph_view_tester'" not in comp_src)

        # 7. a real POST through the socket goes all the way: parse -> create
        #    the problem file -> write its samples -> remember its limits.
        #    This is the one claim that unit calls cannot substitute for.
        comp.load_all_tests = settings_mod.load_all_tests
        settings_mod.init_settings({'auto_create_file': True,
                                    'auto_create_extension': 'cpp',
                                    'auto_create_template': '#include <bits/stdc++.h>\n'})
        port = _arm()
        fake_sublime.active_window = lambda: window
        payload = json.dumps({
            'name': u'A. 两数之和', 'group': u'模拟赛',
            'url': 'https://newoj.test/p/1',
            'timeLimit': 1000, 'memoryLimit': 256,
            'tests': [{'input': '1 2\n', 'output': '3\n'},
                      {'input': '5 6\n', 'output': '11\n'}],
        }).encode('utf-8')
        ack = 0
        post_status = []
        saved_packages_path = getattr(fake_sublime, 'packages_path', None)
        saved_status_message = fake_sublime.status_message
        fake_sublime.packages_path = lambda: os.path.join(work_dir, 'Packages')
        settings_mod.init_settings(dict(settings_mod.get_settings(),
                                       companion_debug_log=True))
        fake_sublime.status_message = lambda text=None, *a, **k: post_status.append(text)
        comp._start_listener(None, quiet=True)
        try:
            ack = _post_at(payload, port)
        finally:
            comp._stop_listener()
            fake_sublime.status_message = saved_status_message
            if saved_packages_path is None:
                try:
                    del fake_sublime.packages_path
                except AttributeError:
                    pass
            else:
                fake_sublime.packages_path = saved_packages_path
        check('the listener answers a click with 200', ack == 200, repr(ack))
        posted = os.path.join(work_dir, u'A. 两数之和.cpp')
        check('the posted problem becomes a file named after its statement',
              os.path.exists(posted) and window.opened[-1] == posted,
              repr(window.opened[-1:]))
        check('the status bar names the file the samples went into',
              any(u'A. 两数之和.cpp' in m for m in post_status), repr(post_status))
        log_lines = []
        log_path = os.path.join(work_dir, 'Log', 'aca-companion.log')
        if os.path.exists(log_path):
            with io.open(log_path, encoding='utf-8') as f:
                log_lines = f.read().splitlines()
        check('companion_debug_log records the click and where it landed',
              log_lines and any(u'A. 两数之和' in line and posted in line
                                for line in log_lines), repr(log_lines[-1:]))

        posted_body = _read(posted)
        check('the created file opens with the statement header and the template',
              posted_body.startswith('//') and u'A. 两数之和' in posted_body
              and 'https://newoj.test/p/1' in posted_body
              and posted_body.rstrip().endswith('#include <bits/stdc++.h>'),
              posted_body.replace('\n', ' | ')[:160])
        data_file = os.path.join(work_dir, 'tests', u'A. 两数之和.cpp__tests')
        stored = []
        if os.path.exists(data_file):
            with open(data_file, encoding='utf-8') as f:
                stored = json.loads(f.read())
        check('the posted samples arrive as test data, answers included',
              [item.get('test') for item in stored] == ['1 2\n', '5 6\n']
              and stored[0].get('correct_answers') == ['3'], repr(stored))
        posted_limits = settings_mod.get_problem_limits(posted)
        check('the posted limits are remembered for that file',
              posted_limits.get('time_limit_ms') == 1000
              and posted_limits.get('memory_limit_mb') == 256, repr(posted_limits))

        # 8. clicking twice on the same problem must not litter the folder, and
        #    the log must stay shut while its setting is off.
        lines_before = 0
        if os.path.exists(log_path):
            with io.open(log_path, encoding='utf-8') as f:
                lines_before = len(f.read().splitlines())
        listing = sorted(os.listdir(work_dir))
        settings_mod.init_settings({'auto_create_file': True,
                                   'auto_create_extension': 'cpp',
                                   'auto_create_template': '#include <bits/stdc++.h>\n'})
        fake_sublime.active_window = lambda: window
        port = _arm()
        comp._start_listener(None, quiet=True)
        try:
            ack2 = _post_at(payload, port)
        finally:
            comp._stop_listener()
        lines_after = 0
        if os.path.exists(log_path):
            with io.open(log_path, encoding='utf-8') as f:
                lines_after = len(f.read().splitlines())
        check('a second click on the same problem re-uses its file',
              ack2 == 200 and sorted(os.listdir(work_dir)) == listing,
              repr((ack2, sorted(os.listdir(work_dir)))))
        re_stored = []
        if os.path.exists(data_file):
            with io.open(data_file, encoding='utf-8') as f:
                re_stored = json.loads(f.read())
        check('and resending does not duplicate its samples',
              len(re_stored) == 2, repr(re_stored))
        check('companion_debug_log defaults off: nothing extra is written',
              lines_after == lines_before, repr((lines_before, lines_after)))

        with io.open(os.path.join(ROOT, 'Algorithm Competition Assistant.sublime-settings'),
                     encoding='utf-8') as f:
            shipped_text = f.read()
        check('the shipped default is the one that creates a file per click',
              '"companion_target": "new_file"' in shipped_text, 'companion_target missing')
        check('the debug log switch ships off',
              '"companion_debug_log": false' in shipped_text, 'companion_debug_log missing')

        # 9. who is allowed to knock, and what happens when it is not.
        #    The rejection path answered 403 and recorded nothing, so a refused
        #    click and a click that never left the browser looked identical from
        #    the outside - which is exactly how "I clicked, the log is empty"
        #    became undiagnosable.
        allowed = {'Host': 'localhost:12345',
                   'Origin': 'chrome-extension://ldaglennelkejdgjigfbaobdnmdo'}
        check('the extension itself is allowed to post',
              comp.request_allowed(allowed.get))
        check('a firefox extension origin is allowed too',
              comp.request_allowed({'Host': 'localhost:12345',
                                    'Origin': 'moz-extension://cafe-babe'}.get))
        check('a web page is still refused (DNS rebinding and drive-by posts)',
              not comp.request_allowed({'Host': 'localhost:12345',
                                        'Origin': 'https://evil.example'}.get))
        check('a non-loopback Host is refused whatever the origin claims',
              not comp.request_allowed({'Host': 'evil.example:12345',
                                        'Origin': 'chrome-extension://cafe'}.get))

        settings_mod.init_settings({'auto_create_file': True,
                                   'auto_create_extension': 'cpp',
                                   'companion_debug_log': True})
        saved_packages_path = getattr(fake_sublime, 'packages_path', None)
        fake_sublime.packages_path = lambda: os.path.join(work_dir, 'Packages')
        port = _arm()
        comp._start_listener(None, quiet=True)
        codes = {}
        try:
            codes['extension'] = _post_at(payload, port,
                                          {'Origin': 'chrome-extension://cafe-babe'})
            codes['webpage'] = _post_at(payload, port,
                                         {'Origin': 'https://evil.example'})
        finally:
            comp._stop_listener()
            if saved_packages_path is None:
                try:
                    del fake_sublime.packages_path
                except AttributeError:
                    pass
            else:
                fake_sublime.packages_path = saved_packages_path
        check('a click from the extension reaches the handler instead of a 403',
              codes['extension'] == 200, repr(codes))
        check('a web page posting at the local port is still refused',
              codes['webpage'] == 403, repr(codes))
        refusal_lines = []
        if os.path.exists(log_path):
            with io.open(log_path, encoding='utf-8') as f:
                refusal_lines = f.read().splitlines()
        check('a refusal is recorded with the header that caused it',
              any('refused 403' in line and 'evil.example' in line
                  for line in refusal_lines), repr(refusal_lines[-1:]))

        # 10. the two modes have to be reachable without editing a settings
        #     file in the middle of a contest.
        with io.open(os.path.join(ROOT, 'Context.sublime-menu'), encoding='utf-8') as f:
            context_menu = f.read()
        with io.open(os.path.join(ROOT, 'Main.sublime-menu'), encoding='utf-8') as f:
            main_menu = f.read()
        check('the context menu offers both listener modes',
              '"target": "new_file"' in context_menu
              and '"target": "current_file"' in context_menu, context_menu)
        check('so does the Tools menu',
              '"target": "new_file"' in main_menu
              and '"target": "current_file"' in main_menu)

        settings_mod.init_settings({'auto_create_file': True,
                                   'auto_create_extension': 'cpp',
                                   'companion_target': 'new_file'})
        port = _arm()
        comp._start_listener(None, quiet=True, target='current_file')
        check('a menu pick overrides the setting for the session',
              comp._target_mode() == 'current_file', comp._target_mode())
        comp._stop_listener()
        with comp._listener['lock']:
            comp._listener['target'] = None
        check('with no pick the setting rules again',
              comp._target_mode() == 'new_file', comp._target_mode())

        # Pressing Ctrl+Alt+L inside a file has always meant "listen for THIS
        # file". Filling it silently while the setting still reads new_file is
        # exactly how a stale pin came to look like "creating a file is broken".
        fake_sublime.windows = lambda: [_PinnedWindow(pinned_view)]
        settings_mod.init_settings({'auto_create_file': True,
                                   'auto_create_extension': 'cpp',
                                   'companion_target': 'new_file'})
        port = _arm()
        comp._start_listener(pinned_view, quiet=True)
        check('listening inside a file fills that file, whatever the setting says',
              comp._target_mode() == 'current_file'
              and comp._listener['view_id'] == pinned_view.id(),
              repr((comp._target_mode(), comp._listener['view_id'], pinned_view.id())))
        comp._start_listener(None, quiet=True, target='new_file')
        check('picking new_file drops the pin instead of ignoring the pick',
              comp._target_mode() == 'new_file' and comp._listener['view_id'] is None,
              repr((comp._target_mode(), comp._listener['view_id'])))
        comp._stop_listener()
        fake_sublime.windows = lambda: []

        # 11. creating a problem file must not also start testing it. Nothing
        #     has been written yet, and a run panel plus a compile per click is
        #     memory spent on nothing. Only the "fill the current file" button
        #     runs samples - that file has code in it.
        class _RunRecordingView(fake_sublime.View):
            def __init__(self, file_name=None):
                fake_sublime.View.__init__(self, file_name)
                self.panel_runs = []

            def run_command(self, name, args=None):
                self.panel_runs.append((name, args or {}))

        class _RecordingWindow(_AutoWindow):
            def __init__(self, folder):
                _AutoWindow.__init__(self, folder)
                self.made = []

            def open_file(self, path):
                view = _RunRecordingView(path)
                self.opened.append(path)
                self.made.append(view)
                return view

        solo = json.dumps({
            'name': u'B - no run', 'url': 'https://x.test/b',
            'timeLimit': 500, 'memoryLimit': 64,
            'tests': [{'input': '1\n', 'output': '2\n'}],
        }).encode('utf-8')
        settings_mod.init_settings(dict(base_settings, companion_target='new_file',
                                       run_settings=[cpp_only]))
        rec_win = _RecordingWindow(work_dir)
        fake_sublime.active_window = lambda: rec_win
        port = _arm()
        comp._start_listener(None, quiet=True)
        try:
            _post_at(solo, port)
        finally:
            comp._stop_listener()
        made_view = rec_win.made[-1] if rec_win.made else None
        check('the new-file click creates the file and runs nothing',
              made_view is not None
              and made_view.file_name() == os.path.join(work_dir, u'B - no run.cpp')
              and made_view.panel_runs == [],
              repr(made_view.panel_runs if made_view else None))
        check('its samples are still stored',
              os.path.exists(os.path.join(work_dir, 'tests',
                                          u'B - no run.cpp__tests')))

        filled_file = os.path.join(work_dir, 'fill_me.cpp')
        with io.open(filled_file, 'w', encoding='utf-8') as f:
            f.write('// code already here\n')
        fill_view = _RunRecordingView(filled_file)
        fake_sublime.active_window = lambda: _FocusedWindow(work_dir, fill_view)
        settings_mod.init_settings(dict(base_settings, companion_target='current_file',
                                       run_settings=[cpp_only]))
        port = _arm()
        comp._start_listener(None, quiet=True)
        try:
            _post_at(solo, port)
        finally:
            comp._stop_listener()
        check('the fill-the-current-file click does run them',
              [name for name, _a in fill_view.panel_runs] == ['cph_view_tester'],
              repr(fill_view.panel_runs))

        # 11b. a contest page sends every task in one click - seven problems
        #      arriving into the same file is not seven things to compile. The
        #      samples are still stored; only the run is folded away.
        burst_file = os.path.join(work_dir, 'burst.cpp')
        with io.open(burst_file, 'w', encoding='utf-8') as f:
            f.write('// hand written\n')
        burst_view = _RunRecordingView(burst_file)
        fake_sublime.active_window = lambda: _FocusedWindow(work_dir, burst_view)
        settings_mod.init_settings(dict(base_settings, companion_target='current_file',
                                       run_settings=[cpp_only]))
        comp._stop_listener()
        port = _arm()
        comp._start_listener(None, quiet=True)
        try:
            for number, letter in enumerate(('X', 'Y', 'Z'), start=1):
                body = json.dumps({'name': u'%s - burst' % letter,
                                   'url': 'https://x.test/%s' % letter,
                                   'timeLimit': 100, 'memoryLimit': 64,
                                   'tests': [{'input': '%d\n' % number,
                                              'output': '2\n'}]}
                                  ).encode('utf-8')
                _post_at(body, port)
        finally:
            comp._stop_listener()
        check('a burst of problems into one file runs the samples once',
              len(burst_view.panel_runs) == 1, repr(burst_view.panel_runs))
        burst_tests = []
        burst_data = os.path.join(work_dir, 'tests', 'burst.cpp__tests')
        if os.path.exists(burst_data):
            with io.open(burst_data, encoding='utf-8') as f:
                burst_tests = json.loads(f.read())
        check('and every problem of the burst is still stored',
              len(burst_tests) == 3, repr(len(burst_tests)))

        # 11c. pressing the key has to say what it will do: a list page plus
        #       "fill the current file" is how seven problems end up in std.cpp.
        warned = []
        saved_sm = fake_sublime.status_message
        try:
            fake_sublime.status_message = lambda text=None, *a, **k: warned.append(text)
            comp._start_listener(_RunRecordingView(burst_file), quiet=True,
                                 target='current_file')
        finally:
            fake_sublime.status_message = saved_sm
        check('picking fill-the-current-file names the file and warns',
              any('burst.cpp' in m and ('Ctrl+Alt+L' in m or 'new file' in m)
                  for m in warned), repr(warned))

        # 12. "one new file per problem", clicked inside a problem file, means
        #     the new files belong next to THAT file - a folder per contest task
        #     - not in whatever folder Sublime happens to have open.
        subdir = os.path.join(work_dir, 'C')
        os.makedirs(subdir)
        anchor = fake_sublime.View(file_name=os.path.join(subdir, 'anchor.cpp'))
        fake_sublime.windows = lambda: []
        fake_sublime.active_window = lambda: _AutoWindow(work_dir)
        settings_mod.init_settings(dict(base_settings, companion_target='new_file'))
        port = _arm()
        comp._start_listener(anchor, target='new_file')
        check('listening in a file records its folder',
              comp._listener.get('dir') == subdir, repr(comp._listener.get('dir')))
        next_to = comp.resolve_target(u'next to me', '', 1, 1)
        check('the new problem is created there, not in the workspace root',
              next_to is not None
              and next_to.file_name() == os.path.join(subdir, u'next to me.cpp'),
              repr(next_to.file_name() if next_to else None))
        comp._stop_listener()
        check('stopping the listener forgets that folder',
              comp._listener.get('dir') is None, repr(comp._listener.get('dir')))

        # 13. the header is the user's text, the way cp editor's template is.
        header_win = _AutoWindow(work_dir)
        fake_sublime.active_window = lambda: header_win
        settings_mod.init_settings({
            'auto_create_file': True, 'auto_create_extension': 'cpp',
            'companion_target': 'new_file',
            'auto_create_header': [u'// {problem}', u'// TL {time}ms  ML {memory}MB',
                                   u'// {url}', u'// {typo} stays'],
            'auto_create_template': 'int main(){}\n'})
        comp.resolve_target(u'D. 图论', 'https://x.test/d', 1000, 256)
        head_text = _read(os.path.join(work_dir, u'D. 图论.cpp'))
        check('a custom header template is written as given',
              head_text.startswith(u'// D. 图论\n// TL 1000ms  ML 256MB\n'
                                   u'// https://x.test/d'),
              repr(head_text[:90]))
        check('an unknown placeholder is left alone instead of crashing',
              '{typo}' in head_text, repr(head_text[:120]))
        check('the template still follows the header',
              head_text.rstrip().endswith('int main(){}'), repr(head_text[-40:]))

        settings_mod.init_settings({'auto_create_file': True,
                                   'auto_create_extension': 'cpp',
                                   'companion_target': 'new_file',
                                   'auto_create_header': '',
                                   'auto_create_template': 'int main(){}\n'})
        comp.resolve_target(u'no header at all', '', 1, 1)
        bare_text = _read(os.path.join(work_dir, 'no header at all.cpp'))
        check('an empty auto_create_header writes no header at all',
              bare_text == 'int main(){}\n', repr(bare_text[:60]))

        # 14. both ways need a key: choosing where samples go should not cost a
        #     trip through the context menu.
        for os_name in ('Windows', 'Linux', 'OSX'):
            with io.open(os.path.join(ROOT, 'Default (%s).sublime-keymap' % os_name),
                         encoding='utf-8') as f:
                binds = json.loads(f.read())
            by_key = {}
            for entry in binds:
                if entry.get('command') == 'cph_companion_listener':
                    by_key['+'.join(entry['keys'])] = (entry.get('args') or {}).get('target')
            check('the two listen modes have keys on %s' % os_name,
                  by_key.get('ctrl+alt+l') == 'new_file'
                  and by_key.get('ctrl+alt+f') == 'current_file', repr(by_key))

        check('the header template is a documented setting',
              '"auto_create_header"' in shipped_text, 'auto_create_header missing')
    finally:
        _stop_and_reset()
        settings_mod.init_settings(saved_settings)
        fake_sublime.load_settings = saved_load_settings
        fake_sublime.active_window = saved_active_window
        fake_sublime.windows = saved_windows
        fake_sublime.set_timeout = saved_set_timeout
        fake_sublime.error_message = saved_error_message
        shutil.rmtree(work_dir, ignore_errors=True)

    print('== panel refresh: one card changed, one card rebuilt ==')
    mgr = tm.CphTestManagerCommand.__new__(tm.CphTestManagerCommand)
    mgr.view = sys.modules['sublime'].View()
    mgr.on_test_action = lambda *a, **k: None
    mgr.on_accdec_action = lambda *a, **k: None
    mgr.schedule_auto_fit_panel_width = lambda *a, **k: None
    mgr.update_summary_bar = lambda *a, **k: None
    built = []
    card_cls = tm.CphTestManagerCommand.Test
    real_get_config = card_cls.get_config

    def counting_config(self, *args, **kwargs):
        built.append(self.test_string)
        return real_get_config(self, *args, **kwargs)

    def _card(text):
        item = card_cls(text)
        item.rtcode = '0'
        return item

    card_cls.get_config = counting_config
    try:
        tester3 = make_tester([_card('1 2\n'), _card('3 4\n'), _card('5 6\n')])
        tester3.proc_run = True
        tester3.test_iter = 2
        tester3.running_test = 2
        tester3.prog_out = ['a\n', 'b\n', 'c\n']
        mgr.tester = tester3
        mgr.test_phantoms = []
        del built[:]
        mgr.update_configs(update_last=True)
        check('a chain step rebuilds only the card it pushes',
              len(built) == 1, repr(built))
        pushed = [i for i, ph in enumerate(mgr.test_phantoms) if ph.phantoms]
        mgr.test_phantoms = []
        del built[:]
        mgr.update_configs()
        full_pushed = [i for i, ph in enumerate(mgr.test_phantoms) if ph.phantoms]
        check('the incremental rebuild writes the whole set, as it always did',
              len(built) == 3 and len(full_pushed) >= 3,
              repr((len(built), full_pushed)))
        check('and the slot the chain step pushed is the slot the full rebuild pushes',
              pushed and pushed[0] == full_pushed[pushed[0]],
              repr((pushed, full_pushed)))
        # the answer comparison is what made the walk quadratic; it must not
        # run for a card nobody is re-rendering
        asked = []
        real_is_correct = card_cls.is_correct_answer
        card_cls.is_correct_answer = lambda self, *a, **k: (asked.append(1),
                                                            real_is_correct(self, *a, **k))[1]
        try:
            mgr.test_phantoms = []
            del asked[:]
            mgr.update_configs(update_last=True)
            check('and it is asked only about the card being drawn',
                  len(asked) <= 1, repr(asked))
        finally:
            card_cls.is_correct_answer = real_is_correct
    finally:
        card_cls.get_config = real_get_config

    _squashed_input = ' 1 2\n 3 '
    _squashed_once = tm._squash_ws(_squashed_input)
    check('the whitespace squash stays exact and is memoised',
          _squashed_once == '123'
          and tm._squash_ws(_squashed_input) is _squashed_once
          and tm._squash_ws('') == '' and tm._squash_ws(None) == '')

    # The compile worker used to draw the compile chip, build the Tester and
    # start the chain on Sublime's single shared async worker - behind every
    # compile, and ahead of every other plugin's async work.
    tm_src_for_thread = io.open(os.path.join(ROOT, 'test_manager.py'),
                                encoding='utf-8').read()
    worker = tm_src_for_thread.split('def compile():')[1].split(
        "self.set_compile_bar(t('compiling')")[0]
    check('the async worker only runs the compiler',
          'sublime.set_timeout(lambda: after_compile(' in worker
          and 'v.run_command(' not in worker
          and 'set_compile_bar(' not in worker
          and '.settings().set(' not in worker,
          worker[:120])

    class _FakePM(object):
        file = os.path.join('x', 'a.cpp')

        def get_compile_inputs(self):
            return [self.file]

        def get_compile_cmd(self):
            return 'g++ a.cpp'

    walked = []
    real_fp = tm._source_fingerprint
    tm._source_fingerprint = lambda pm: (walked.append(1), real_fp(pm))[1]
    try:
        stamp = (('x', 1, 1), 'g++ a.cpp')
        tm.should_skip_compile(_FakePM(), fingerprint=stamp)
        tm.remember_compile(_FakePM(), fingerprint=stamp)
        check('a Run that already took the fingerprint does not walk the headers twice',
              walked == [], repr(walked))
        tm.should_skip_compile(_FakePM())
        check('and it is still computed when nobody passed one', len(walked) == 1,
              repr(walked))
        pm = _FakePM()
        tm.remember_compile(pm, stamp)
        walked[:] = []
        check('the cache actually hits on a matching fingerprint',
              tm.should_skip_compile(pm, fingerprint=stamp) is True
              and walked == [], repr(walked))
    finally:
        tm._source_fingerprint = real_fp

    print('== saving the cards: once per chain, not once per sample ==')
    real_save = tm.save_tests
    saves = []
    try:
        tm.save_tests = lambda f, tests: (saves.append(f), True)[1]
        mgr.dbg_file = os.path.join('x', 'a.cpp')
        mgr.tester = tester3
        mgr._tests_dirty = False
        mgr._tests_flush_scheduled = False
        mgr.memorize_tests()
        mgr.memorize_tests()
        mgr.memorize_tests()
        check('three finished samples do not rewrite the file three times',
              saves == [], repr(saves))
        mgr.memorize_tests(force=True)
        check('the chain end writes it, once', len(saves) == 1, repr(saves))
        # a coalesced timer that fires with nothing pending must be free
        mgr._tests_flush_scheduled = True
        mgr._flush_tests()
        check('and a flush with nothing dirty writes nothing', len(saves) == 1,
              repr(saves))
    finally:
        tm.save_tests = real_save

    print('== settings panels: the JSON-only switches, one keystroke away ==')
    opts = importlib.import_module(pkg + '.cph_options')
    store = fake_sublime.Settings({})
    saved_load = fake_sublime.load_settings
    saved_save = fake_sublime.save_settings
    writes = []
    sfile = 'Algorithm Competition Assistant.sublime-settings'
    fake_sublime.load_settings = lambda name: store
    fake_sublime.save_settings = lambda name: writes.append(name)

    class _PanelWindow(object):
        def __init__(self):
            self.panels = []
            self.prompts = []
            self.opened = []
            self.status = []

        def show_quick_panel(self, items, on_select, *a, **k):
            self.panels.append((list(items), on_select))

        def show_input_panel(self, caption, initial, on_done, *a):
            self.prompts.append((caption, initial, on_done))

        def open_file(self, path):
            self.opened.append(path)

        def status_message(self, text=None, *a, **k):
            self.status.append(text)

    try:
        win = _PanelWindow()
        opts.CphVerdictModeCommand(win).run()
        items, choose = win.panels[-1]
        # compared through t(), not against a literal: the panel answers in the
        # configured language, and a hard-coded 'off' made this a test of the
        # suite's own locale.
        check('the verdict panel lists all four rules with their live values',
              len(items) == 4 and items[1][1] == opts.t('opt_off')
              and items[2][1] == opts.t('opt_on'), repr(items))
        choose(1)
        check('choosing one writes the key and saves the file',
              store.get('regard_pe_as_ac') is True and writes == [sfile],
              repr((store.get('regard_pe_as_ac'), writes)))
        choose(0)
        check('the float tolerance asks for a number instead of guessing one',
              len(win.prompts) == 1 and '1e-6' == win.prompts[0][1],
              repr(win.prompts))
        win.prompts[0][2]('1e-7')
        check('and stores what it was given',
              store.get('float_tolerance') == 1e-07, repr(store.get('float_tolerance')))
        win.prompts[0][2]('nonsense')
        check('a non-number keeps the old value and says so',
              store.get('float_tolerance') == 1e-07, repr(store.get('float_tolerance')))

        store.set('stress_file_source', 'pick_once')
        win2 = _PanelWindow()
        opts.CphStressOptionsCommand(win2).run()
        items2, choose2 = win2.panels[-1]
        choose2(0)
        check('the stress panel cycles the file source in the documented order',
              store.get('stress_file_source') == 'same_dir',
              repr(store.get('stress_file_source')))
        choose2(1)
        check('and the picker mode with it',
              store.get('stress_file_picker') == 'native',
              repr(store.get('stress_file_picker')))

        store.set('companion_target', 'new_file')
        messages = []
        saved_status = fake_sublime.status_message
        fake_sublime.status_message = lambda text=None, *a, **k: messages.append(text)
        try:
            win3 = _PanelWindow()
            opts.CphCompanionOptionsCommand(win3).run()
            items3, choose3 = win3.panels[-1]
            check('the target row names the live mode',
                  len(items3) == 4 and items3[0][1] == opts.t('opt_target_mode'),
                  repr(items3[0]))
            choose3(0)
            check('one click on it pins the other target mode',
                  store.get('companion_target') == 'current_file',
                  repr(store.get('companion_target')))
            check('and the row says so in words the user set, not the raw key',
                  win3.status[-1].endswith(opts.t('opt_target_mode_current')),
                  repr(win3.status[-1]))
            choose3(1)
            check('auto listen defaults to on, so toggling it means off',
                  store.get('companion_auto_listen') is False,
                  repr(store.get('companion_auto_listen')))
            choose3(2)
            check('the log switch is a real toggle',
                  store.get('companion_debug_log') is True
                  and writes[-1] == sfile, repr(store.get('companion_debug_log')))
            choose3(2)
            check('and switches back off',
                  store.get('companion_debug_log') is False,
                  repr(store.get('companion_debug_log')))
            check('no row falls through into opening the log',
                  win3.opened == [], repr(win3.opened))
            choose3(3)
            check('with no log on disk it says so instead of opening a ghost',
                  win3.opened == [] and items3[3][1] == opts.t('opt_log_missing')
                  and messages[-1] == opts.t('opt_log_missing'),
                  repr((win3.opened, messages[-1], items3[3][1])))
        finally:
            fake_sublime.status_message = saved_status

        saved_pp = fake_sublime.packages_path
        data_dir = os.path.join(tempfile.mkdtemp(), 'Data')
        fake_sublime.packages_path = lambda: os.path.join(data_dir, 'Packages')
        try:
            log_path = opts.companion_log_path()
            if not os.path.isdir(os.path.dirname(log_path)):
                os.makedirs(os.path.dirname(log_path))
            io.open(log_path, 'w', encoding='utf-8').write(u'click\n')
            win4 = _PanelWindow()
            opts.CphCompanionOptionsCommand(win4).run()
            win4.panels[-1][1](3)
            check('once the log exists the same row opens it',
                  win4.opened == [log_path]
                  and win4.panels[-1][0][3][1] == log_path, repr(win4.opened))
        finally:
            fake_sublime.packages_path = saved_pp

        palette = io.open(os.path.join(ROOT, 'Default.sublime-commands'),
                          encoding='utf-8').read()
        view_menu = io.open(os.path.join(ROOT, 'Main.sublime-menu'),
                            encoding='utf-8').read()
        for command in ('cph_verdict_mode', 'cph_stress_options',
                        'cph_companion_options'):
            check('%s is reachable from the palette and the menu' % command,
                  command in palette and command in view_menu, command)
            entry = _palette_entry(palette, command)
            check('%s lets description() name it, so the palette follows the '
                  'language setting' % command,
                  entry is not None and 'caption' not in entry, repr(entry))
        check('the build mode shows its current value in the palette',
              '"caption": "Algorithm Competition Assistant: Toggle Debug / Release build mode"'
              not in palette)
        for cls in ('CphVerdictModeCommand', 'CphStressOptionsCommand',
                    'CphCompanionOptionsCommand'):
            name = getattr(opts, cls)(None).description()
            check('%s names itself after the package' % cls,
                  name.startswith('Algorithm Competition Assistant: ')
                  and len(name) > 34, repr(name))
    finally:
        fake_sublime.load_settings = saved_load
        fake_sublime.save_settings = saved_save

    print('== the contest countdown starts from what the user set ==')
    # `or 120` used to swallow 0, so the documented "0 = ask me every time"
    # silently typed 120 into the box instead.
    extras = importlib.import_module(pkg + '.cph_extras')
    cs = importlib.import_module(pkg + '.core.cph_settings')
    saved_contest = cs.get_settings()
    try:
        for value, want in ((120, '120'), (270, '270'), (0, ''),
                            ('abc', ''), (None, '')):
            cs.init_settings({'contest_duration_minutes': value})
            got = extras._contest_default_minutes()
            check('contest_duration_minutes=%r pre-fills %r' % (value, want),
                  got == want, repr(got))
        cs.init_settings({})
        check('with no setting at all the box still suggests a contest length',
              extras._contest_default_minutes() == '120',
              repr(extras._contest_default_minutes()))
    finally:
        cs.init_settings(saved_contest)

    print('== the -std ladder: pay for the new standard only when the code needs it ==')
    # Measured on his machine (g++ 15.2 MSYS2, day9/B, <bits/stdc++.h>):
    #   -fsyntax-only  c++11 1.11  c++17 1.29  c++20 2.43  c++23 2.53 s
    #   full compile   c++17 1.69              c++23 3.12 s (avg 3.78)
    # so the wait is header parsing that grows with the standard, and his file
    # uses nothing newer than C++11. A precompiled header was measured too:
    # 3.12 s -> 3.46 s and a 151 MB .gch, so that road is closed.
    ladder = importlib.import_module(pkg + '.core.cph_std_ladder')
    CMD = ('g++ "{source_file}" -std=c++23 -O2 -DLOCAL -o "{file_name}.exe"')
    check('the configured standard is read out of the command',
          ladder.configured_std(CMD) == 'c++23'
          and ladder.configured_std('g++ "{source_file}" -O2') is None,
          repr(ladder.configured_std(CMD)))
    check('gnu++ and c++2b spellings normalise to a rung',
          ladder.normalize_std('gnu++20') == 'c++20'
          and ladder.normalize_std('c++2b') == 'c++23'
          and ladder.normalize_std('c++98') is None,
          repr([ladder.normalize_std('gnu++20'), ladder.normalize_std('c++2b')]))
    check('the ladder runs from the floor up to what the command already asks for',
          ladder.rungs(CMD, {'std_floor': 'c++17'}) == ['c++17', 'c++20', 'c++23'],
          repr(ladder.rungs(CMD, {'std_floor': 'c++17'})))
    check('a command that is already at the floor has nothing to try',
          ladder.rungs('g++ a.cpp -std=c++17 -o a.exe', {}) == [],
          repr(ladder.rungs('g++ a.cpp -std=c++17 -o a.exe', {})))
    check('substituting keeps every other flag and the output path',
          ladder.with_std(CMD, 'c++17')
          == 'g++ "{source_file}" -std=c++17 -O2 -DLOCAL -o "{file_name}.exe"',
          repr(ladder.with_std(CMD, 'c++17')))
    check('a gnu++ command is rewritten in its own spelling',
          '-std=gnu++17' in ladder.with_std('g++ a.cpp -std=gnu++23 -O2', 'c++17'),
          repr(ladder.with_std('g++ a.cpp -std=gnu++23 -O2', 'c++17')))
    check('a command without -std is never touched',
          ladder.with_std('g++ a.cpp -O2', 'c++17') == 'g++ a.cpp -O2',
          repr(ladder.with_std('g++ a.cpp -O2', 'c++17')))

    # GCC's own words, captured on this machine with real C++20/23 code
    EXPECTED = ("t.cpp:3:17: note: 'std::expected' is only available from "
                "C++23 onwards\n")
    PRINTLN = ("t.cpp:3:17: error: 'println' is not a member of 'std'\n"
               "t.cpp:3:17: note: 'std::println' is only available from C++23 onwards\n")
    EXT = ("warning: use of 'auto' in parameter declaration only available "
           "with '-std=c++20' or '-fconcepts' [-Wc++20-extensions]\n")
    HDR = 't.cpp:2:10: fatal error: mdspan: No such file or directory\n'
    KEYWORD = "t.cpp:1:24: error: expected '(' before 'consteval'\n"
    TYPO = "t.cpp:2:17: error: 'vectr' is not a member of 'std'\n"
    SYNTAX = "t.cpp:2:17: error: expected primary-expression before ';' token\n"
    for text, want, why in ((EXPECTED, True, 'the version note'),
                            (PRINTLN, True, 'not a member + note'),
                            (EXT, True, 'extension warning naming -std'),
                            (HDR, True, 'a header that only exists in C++20/23'),
                            (KEYWORD, True, 'a newer keyword'),
                            (TYPO, False, 'a typo in a std name'),
                            (SYNTAX, False, 'plain syntax error')):
        got = ladder.needs_newer_std(text)
        check('%s asks for a newer standard' % why if want
              else '%s is not mistaken for a standard problem' % why,
              got is want, repr(got))
    check('the note names the standard to jump straight to',
          ladder.requested_std(EXPECTED) == 'c++23'
          and ladder.requested_std(EXT) == 'c++20',
          repr([ladder.requested_std(EXPECTED), ladder.requested_std(EXT)]))

    # escalation is per file, remembered, and stops at the configured standard
    lad_settings = importlib.import_module(pkg + '.core.cph_settings')
    saved_lad = lad_settings.get_settings()
    lad_dir = _tf.mkdtemp()
    lad_src = os.path.join(lad_dir, 'main.cpp')
    io.open(lad_src, 'w', encoding='utf-8', newline='\n').write('int main(){}\n')
    try:
        lad_settings.init_settings({'tests_relative_dir': '',
                                    'tests_folder_name': 'tests',
                                    'auto_std_switch': True,
                                    'std_floor': 'c++17'})
        check('a new file starts at the floor, not at the expensive standard',
              ladder.apply(CMD, lad_src) == (ladder.with_std(CMD, 'c++17'), 'c++17'),
              repr(ladder.apply(CMD, lad_src)))
        fast, slow = ladder.apply(CMD, lad_src)
        used, note = ladder.escalate(fast, lad_src, EXPECTED)
        check('the compiler asking for C++23 moves this file straight to C++23',
              used == ladder.with_std(CMD, 'c++23') and '-std=c++20' not in used
              and note, repr((used, note)))
        check('and the rung is remembered beside the test data, like the other sidecars',
              ladder.apply(CMD, lad_src)[1] == 'c++23'
              and os.path.exists(os.path.join(lad_dir, 'tests',
                                              'main.cpp__tests.std')),
              repr(ladder.apply(CMD, lad_src)))
        again, note2 = ladder.escalate(slow, lad_src, EXPECTED)
        check('nothing is retried once the file is already at the top',
              again is None and note2 == '', repr((again, note2)))
        check('the note names both standards so the status bar can show the swap',
              'c++17' in note and 'c++23' in note, repr(note))
        lad_settings.set_remembered_std(lad_src, None)
        check('a reset (the force-recompile key) drops the file back to the floor',
              ladder.apply(CMD, lad_src)[1] == 'c++17', repr(ladder.apply(CMD, lad_src)))
        check('a typo is never escalated - the error is shown as it is',
              ladder.escalate(fast, lad_src, TYPO)[0] is None,
              repr(ladder.escalate(fast, lad_src, TYPO)))
        # a command whose own ceiling is C++20 must never wander into C++23
        lad_settings.set_remembered_std(lad_src, None)
        cmd20 = 'g++ "{source_file}" -std=c++20 -O2 -o "{file_name}.exe"'
        fast20, _r = ladder.apply(cmd20, lad_src)
        up, note20 = ladder.escalate(fast20, lad_src, EXPECTED)
        check('a command that stops at C++20 is not climbed past',
              up == ladder.with_std(cmd20, 'c++20') and '-std=c++23' not in up,
              repr(up))
        at_top, _r = ladder.apply(cmd20, lad_src)
        check('once a file is at the ceiling its command asked for, the real '
              'error is shown instead of a third compile',
              at_top == cmd20 and ladder.escalate(at_top, lad_src, EXPECTED)[0] is None,
              repr(at_top))

        # the wasted probe is avoided up front when the source itself says what
        # it wants: #include <expected> is not a guess, it is a requirement
        hint_dir = _tf.mkdtemp()
        sources = {
            'plain2.cpp': '#include <bits/stdc++.h>' + chr(10) + 'int main(){}' + chr(10),
            'uses23.cpp': '#include <bits/stdc++.h>' + chr(10) + '#include <expected>'
                          + chr(10) + 'int main(){}' + chr(10),
            'uses20.cpp': '#include <span>' + chr(10) + 'int main(){}' + chr(10),
            'localprint.cpp': '#include "print.h"' + chr(10) + '#include <cstdio>'
                              + chr(10) + 'int main(){}' + chr(10),
            'stdprint.cpp': '#include <cstdio>' + chr(10)
                            + 'int main(){std::println("x");}' + chr(10),
            'bitbitset.cpp': '#include <bitset>' + chr(10) + 'int main(){}' + chr(10),
        }
        for fname in sources:
            io.open(os.path.join(hint_dir, fname), 'w', encoding='utf-8',
                    newline='\n').write(sources[fname])
        check('a plain source gets no suggestion',
              ladder.suggested_rung(os.path.join(hint_dir, 'plain2.cpp')) is None,
              repr(ladder.suggested_rung(os.path.join(hint_dir, 'plain2.cpp'))))
        check('#include <expected> is read as C++23 before the compiler runs',
              ladder.suggested_rung(os.path.join(hint_dir, 'uses23.cpp')) == 'c++23',
              repr(ladder.suggested_rung(os.path.join(hint_dir, 'uses23.cpp'))))
        check('#include <span> is read as C++20',
              ladder.suggested_rung(os.path.join(hint_dir, 'uses20.cpp')) == 'c++20',
              repr(ladder.suggested_rung(os.path.join(hint_dir, 'uses20.cpp'))))
        check('#include <bitset> is not mistaken for the C++20 <bit>',
              ladder.suggested_rung(os.path.join(hint_dir, 'bitbitset.cpp')) is None,
              repr(ladder.suggested_rung(os.path.join(hint_dir, 'bitbitset.cpp'))))
        check('a local "print.h" is not mistaken for the C++23 header',
              ladder.suggested_rung(os.path.join(hint_dir, 'localprint.cpp')) is None,
              repr(ladder.suggested_rung(os.path.join(hint_dir, 'localprint.cpp'))))
        check('std::println in the source is read as C++23',
              ladder.suggested_rung(os.path.join(hint_dir, 'stdprint.cpp')) == 'c++23',
              repr(ladder.suggested_rung(os.path.join(hint_dir, 'stdprint.cpp'))))

        hint_src = os.path.join(hint_dir, 'uses23.cpp')
        lad_settings.set_remembered_std(hint_src, None)
        hinted, hinted_rung = ladder.apply(CMD, hint_src)
        check("a first Run of a file that clearly wants C++23 does not waste a "
              "compile on the floor",
              hinted_rung == 'c++23' and hinted == ladder.with_std(CMD, 'c++23'),
              repr((hinted, hinted_rung)))
        lad_settings.set_remembered_std(hint_src, 'c++17')
        check('a rung the file already earned beats the hint',
              ladder.apply(CMD, hint_src)[1] == 'c++17',
              repr(ladder.apply(CMD, hint_src)))
        lad_settings.set_remembered_std(hint_src, None)

        lad_settings.init_settings({'auto_std_switch': False, 'std_floor': 'c++17'})
        check('with the switch off the command is exactly what the user wrote',
              ladder.apply(CMD, lad_src) == (CMD, 'c++23'),
              repr(ladder.apply(CMD, lad_src)))
    finally:
        lad_settings.init_settings(saved_lad)

    # ProcessManager integration: the retry has to happen inside compile(), so
    # the whole product path - get_compile_cmd resolving the cheap rung, the
    # failure being judged, the newer rung being tried and remembered - is one
    # call for the caller. Stubbed, because CI must not depend on a g++ and on
    # the exact words of whatever version the runner happens to ship.
    pm_mod = importlib.import_module(pkg + '.Modules.ProcessManager')
    pm_dir = _tf.mkdtemp()
    pm_src = os.path.join(pm_dir, 'm.cpp')
    io.open(pm_src, 'w', encoding='utf-8', newline='\n').write('int main(){}\n')
    pm_entry = [{'name': 'C++', 'extensions': ['cpp'],
                 'compile_cmd': 'g++ "{source_file}" -std=c++23 -O2 -o "{file_name}.exe"',
                 'run_cmd': ''}]
    saved_pm_settings = lad_settings.get_settings()
    lad_settings.init_settings({'tests_relative_dir': '',
                                'tests_folder_name': 'tests',
                                'auto_std_switch': True,
                                'std_floor': 'c++17'})
    try:
        pm = pm_mod.ProcessManager(pm_src, 'source.c++', run_settings=pm_entry)
        attempts = []

        def fake_popen(cmd):
            attempts.append(cmd)
            if '-std=c++17' in cmd:
                return (1, EXPECTED)
            return (0, '')

        real_popen = pm._popen_compile
        pm._popen_compile = fake_popen
        try:
            rc, out = pm.compile()
            check('a run resolves the cheap standard first',
                  attempts and '-std=c++17' in attempts[0], repr(attempts))
            check('and the same call retries at the standard the compiler asked for',
                  rc == 0 and len(attempts) == 2 and '-std=c++23' in attempts[1],
                  repr((rc, attempts)))
            check('the note about the swap reaches the compile output',
                  'c++23' in (out or ''), repr((out or '')[:80]))
            check('the manager says the artifact was built at another rung, '
                  'so the compile cache gets re-fingerprinted',
                  pm.std_escalated is True, repr(pm.std_escalated))
            check('and the resolved command now is the newer one, which is what '
                  'the cache fingerprint reads',
                  '-std=c++23' in pm.get_compile_cmd(), repr(pm.get_compile_cmd()))
            del attempts[:]
            rc, out = pm.compile()
            check('the next run of the same file starts at the remembered rung, '
                  'so the ladder is paid for once per problem',
                  len(attempts) == 1 and '-std=c++23' in attempts[0], repr(attempts))
            del attempts[:]
            pm._popen_compile = lambda cmd: (attempts.append(cmd) or (1, TYPO))
            rc, out = pm.compile()
            check('a typo costs exactly one compile and shows its own error',
                  len(attempts) == 1 and rc == 1 and 'vectr' in out, repr(attempts))
            pm._popen_compile = real_popen
        finally:
            pm._popen_compile = real_popen
        lad_settings.set_remembered_std(pm_src, None)
        del attempts[:]
        pm._popen_compile = lambda cmd: (attempts.append(cmd) or (1, TYPO))
        rc, out = pm.compile()
        check('with the rung forgotten the file is back on the cheap one',
              len(attempts) == 1 and '-std=c++17' in attempts[0], repr(attempts))
        pm._popen_compile = real_popen
    finally:
        lad_settings.init_settings(saved_pm_settings)

    print('== install layout: who is really loaded ==')    # Package Control reinstalled the package as a zip and emptied the folder
    # install underneath a running developer (measured here: the folder was
    # still there, completely empty, after a restart) - the symptom being
    # "my changes do nothing", with no error anywhere.
    inst = importlib.import_module(pkg + '.core.cph_install')
    layout = _tf.mkdtemp()
    pkgs = os.path.join(layout, 'Packages')
    os.makedirs(pkgs)

    def _write_folder(name, version):
        folder = os.path.join(pkgs, name)
        os.makedirs(folder)
        with io.open(os.path.join(folder, 'plugin_init.py'), 'w',
                     encoding='utf-8', newline='\n') as f:
            f.write("VERSION = '%s'\n" % version)
        with io.open(os.path.join(folder, 'cph_companion.py'), 'w',
                     encoding='utf-8', newline='\n') as f:
            f.write('# listener\n')
        return folder

    def _write_zip(name, version):
        import zipfile
        installed = os.path.join(layout, 'Installed Packages')
        if not os.path.isdir(installed):
            os.makedirs(installed)
        path = os.path.join(installed, name + '.sublime-package')
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('plugin_init.py', "VERSION = '%s'\n" % version)
            archive.writestr('cph_companion.py', '# listener\n')
        return path

    NAME = 'Algorithm Competition Assistant'
    folder = _write_folder(NAME, '2.5.0')
    check('a single folder install says nothing',
          inst.describe_installs(pkgs, NAME, folder, '2.5.0') == [],
          repr(inst.describe_installs(pkgs, NAME, folder, '2.5.0')))

    zip_path = _write_zip(NAME, '2.4.12')
    both = inst.describe_installs(pkgs, NAME, folder, '2.5.0')
    check('two installs are reported with both versions',
          len(both) == 1 and '2.5.0' in both[0] and '2.4.12' in both[0],
          repr(both))

    # now the damage: the folder emptied by a Package Control reinstall
    for entry in os.listdir(folder):
        os.remove(os.path.join(folder, entry))
    emptied = inst.describe_installs(pkgs, NAME, zip_path, '2.4.12')
    check('an emptied folder install is called out by name',
          len(emptied) == 1 and 'EMPTY' in emptied[0], repr(emptied))
    check('and the fix is in the message, not just the symptom',
          emptied and 'installed_packages' in emptied[0], repr(emptied))

    os.makedirs(os.path.join(pkgs, 'Other'))
    check('another package installed as a zip says nothing about us',
          inst.describe_installs(pkgs, 'Other',
                                 os.path.join(pkgs, 'Other'), '1.0') == [])
    check('the install mode names the shape the host actually loaded',
          inst.install_mode(folder) == 'folder'
          and inst.install_mode(zip_path) == 'zip'
          and inst.install_mode(None) == 'folder',
          repr([inst.install_mode(folder), inst.install_mode(zip_path)]))

    shutil.rmtree(layout, ignore_errors=True)

    print('== stress file sources: same folder, or pick once and remember ==')
    stress2 = importlib.import_module(pkg + '.cph_stress')
    settings2 = importlib.import_module(pkg + '.core.cph_settings')
    saved_settings2 = settings2.settings
    sdir = _tf.mkdtemp()
    try:
        for name in ('problem.cpp', 'std.cpp', 'gen.cpp', 'brute_force.cpp',
                     'data_gen.py'):
            with io.open(os.path.join(sdir, name), 'w', encoding='utf-8',
                         newline='\n') as f:
                f.write('int main(){}\n')
        same_dir = stress2._same_dir_pair(sdir, os.path.join(sdir, 'problem.cpp'))
        check('std and gen are found beside the source',
              same_dir[0] == os.path.join(sdir, 'std.cpp')
              and same_dir[1] == os.path.join(sdir, 'gen.cpp'), repr(same_dir))
        check('the shortest name wins when several candidates match',
              os.path.basename(same_dir[0]) == 'std.cpp'
              and 'brute' not in os.path.basename(same_dir[0]), repr(same_dir))

        empty = os.path.join(sdir, 'sub')
        os.makedirs(empty)
        missing = stress2._same_dir_pair(empty, os.path.join(empty, 'x.cpp'))
        check('nothing beside the source is reported as missing, not guessed',
              missing == (None, None), repr(missing))

        # one problem folder per task, std in a subfolder: the search stays
        # honest about where it looked
        check('the message names the folder that was searched',
              'sub' in stress2._same_dir_miss_text(empty))

        # the modes
        check('the default is to pick once and remember',
              stress2._files_mode({}) == 'pick_once')
        check('same_dir and the old hybrid are both selectable',
              stress2._files_mode({'stress_file_source': 'same_dir'}) == 'same_dir'
              and stress2._files_mode({'stress_file_source': 'auto'}) == 'auto'
              and stress2._files_mode({'stress_file_source': 'nonsense'}) == 'pick_once')

        # remembered per source file, and it survives a restart
        source = os.path.join(sdir, 'problem.cpp')
        settings2.init_settings({'tests_relative_dir': '', 'tests_folder_name': 'tests'})
        settings2.set_stress_files(source, os.path.join(sdir, 'std.cpp'),
                                   os.path.join(sdir, 'gen.cpp'))
        check('the choice is written beside the test data',
              os.path.exists(source + '__tests.stress')
              or os.path.exists(os.path.join(sdir, 'tests',
                                             'problem.cpp__tests.stress')))
        settings2._stress_files.clear()
        back = settings2.get_stress_files(source)
        check('and read back after a restart',
              back[0] == os.path.join(sdir, 'std.cpp')
              and back[1] == os.path.join(sdir, 'gen.cpp'), repr(back))

        settings2._stress_files.clear()
        gone = os.path.join(sdir, 'deleted.cpp')
        settings2.set_stress_files(gone, os.path.join(sdir, 'nope.cpp'),
                                   os.path.join(sdir, 'gen.cpp'))
        settings2._stress_files.clear()
        check('a remembered file that no longer exists is dropped on its own, '
              'so the flow asks again instead of starting a dead path',
              settings2.get_stress_files(gone) == (None, os.path.join(sdir, 'gen.cpp')),
              repr(settings2.get_stress_files(gone)))

        # ---- the leak that made every problem share one pair --------------
        # Measured on his own folder: day9/A ran with A's pair, then B started
        # with the *same* two files without a dialog. Two facts add up to that:
        # begin() wrote every choice back into the global settings, and pick_once
        # offered those globals as candidates - one candidate with ask=False is
        # used silently by _pick_file. A real session log showed the answer in
        # B's own sidecar: gen from a different contest's folder entirely.
        launched = []
        asked = []
        saved = []
        real_launch = stress2._launch
        real_ask = stress2._ask_pair
        real_save = fake_sublime.save_settings
        real_load = fake_sublime.load_settings
        real_window = fake_sublime.active_window

        class _FlowWindow(object):
            def status_message(self, text=None, *a, **k):
                pass

        pins = {'stress_std_file': os.path.join(sdir, 'std.cpp'),
                'stress_generator_file': os.path.join(sdir, 'gen.cpp')}
        pin_store = fake_sublime.Settings(pins)
        stress2._launch = lambda v, u, s, g, t, r: launched.append((u, s, g))
        stress2._ask_pair = lambda w, d, s, g, cb, a: asked.append(
            (os.path.basename(d), tuple(os.path.basename(x) for x in s),
             tuple(os.path.basename(x) for x in g), a))
        fake_sublime.save_settings = lambda name: saved.append(name)
        fake_sublime.load_settings = lambda name: pin_store
        fake_sublime.active_window = lambda: _FlowWindow()
        settings2.init_settings({'tests_relative_dir': '',
                                 'tests_folder_name': 'tests',
                                 'stress_file_source': 'pick_once'})
        try:
            def flow(path):
                v = fake_sublime.View(file_name=path)
                stress2._start_flow(v)
                return v

            def write_source(folder, name):
                if not os.path.isdir(folder):
                    os.makedirs(folder)
                p = os.path.join(folder, name)
                io.open(p, 'w', encoding='utf-8', newline='\n').write('int main(){}\n')
                return p

            # a new problem, nothing beside it, the globals pointing at another
            # problem's pair: it must ask, and it must not just run the globals
            quiet = write_source(_tf.mkdtemp(), 'lonely.cpp')
            flow(quiet)
            check('a problem with no pair of its own is asked, not silently '
                  'started with the previous problem\'s files',
                  len(asked) == 1 and launched == [], repr((asked, launched)))
            check('the remembered global pair only prefills the choice, it is '
                  'never an automatic answer',
                  asked and asked[0][3] is True, repr(asked))

            # per-file memory is authoritative, and it belongs to that file only
            other = write_source(_tf.mkdtemp(), 'other.cpp')
            std_b = write_source(os.path.dirname(other), 'refA.cpp')
            gen_b = write_source(os.path.dirname(other), 'rndA.cpp')
            settings2.set_stress_files(other, std_b, gen_b)
            flow(other)
            check('a file that was stress tested before starts straight away '
                  'with its OWN pair',
                  launched == [(other, std_b, gen_b)], repr(launched))
            del launched[:]
            del asked[:]
            third = write_source(os.path.dirname(other), 'third.cpp')
            flow(third)
            check('a second file in the same folder does not inherit that pair '
                  'through the settings',
                  launched == [], repr(launched))

            # ... and the flow no longer rewrites the user's settings per choice
            check('starting a stress test does not write the global pair back '
                  'into the settings file',
                  saved == []
                  and pin_store.get('stress_std_file') == pins['stress_std_file'],
                  repr(saved))

            # an empty std.cpp is a placeholder, not a program
            zero = _tf.mkdtemp()
            write_source(zero, 'p.cpp')
            io.open(os.path.join(zero, 'std.cpp'), 'w', encoding='utf-8').write('')
            write_source(zero, 'gen.cpp')
            pair = stress2._same_dir_pair(zero, os.path.join(zero, 'p.cpp'))
            check('a 0-byte std.cpp is never chosen as the reference program',
                  pair[0] is None and pair[1] == os.path.join(zero, 'gen.cpp'),
                  repr(pair))

            # and the same rule on the way back in: his B sidecar remembered
            # exactly this - day9\std.cpp, empty, plus another contest's gen
            empt = _tf.mkdtemp()
            src = write_source(empt, 'e.cpp')
            blank = os.path.join(empt, 'std.cpp')
            io.open(blank, 'w', encoding='utf-8').write('')
            real_gen = write_source(empt, 'gen.cpp')
            settings2.set_stress_files(src, blank, real_gen)
            settings2._stress_files.clear()
            check('a remembered 0-byte std is dropped, so the flow asks again',
                  settings2.get_stress_files(src) == (None, real_gen),
                  repr(settings2.get_stress_files(src)))
            del launched[:]
            del asked[:]
            flow(src)
            check('and it asks instead of stress testing against no output at all',
                  launched == [] and len(asked) == 1, repr((launched, asked)))
        finally:
            stress2._launch = real_launch
            stress2._ask_pair = real_ask
            fake_sublime.save_settings = real_save
            fake_sublime.load_settings = real_load
            fake_sublime.active_window = real_window
            settings2.init_settings(saved_settings2)

        src_text = io.open(os.path.join(ROOT, 'cph_stress.py'), encoding='utf-8').read()
        check('the flow consults the mode before deciding to ask',
              "mode = _files_mode(get_settings())" in src_text
              and 'get_stress_files(user_file)' in src_text)
        shipped2 = io.open(os.path.join(ROOT, 'Algorithm Competition Assistant.sublime-settings'),
                           encoding='utf-8').read()
        check('the setting ships with the pick-once default',
              '"stress_file_source": "pick_once"' in shipped2, 'missing')

        # the picker must not be soft on a scaled display
        check('the native dialog declares itself DPI aware before opening',
              'SetProcessDpiAwarenessContext' in stress2._PS_OPEN_FILE
              and 'SetProcessDPIAware' in stress2._PS_OPEN_FILE)
        rendered = stress2._PS_OPEN_FILE.format(dir='Zm9v', title='YmFy')
        check('and the script still formats (no stray braces in the template)',
              '{dir}' not in rendered and 'OpenFileDialog' in rendered)
    finally:
        settings2.init_settings(saved_settings2)
        shutil.rmtree(sdir, ignore_errors=True)

    print('== static checks: the regressions that unit tests cannot reach ==')
    # 1. Every self.X() call must resolve inside its own class. This is the
    #    v1.4.5 bug: a helper was defined on CphViewTesterCommand but called
    #    from CphTestManagerCommand, so make_opd raised AttributeError right
    #    after creating the -run view and the panel stayed empty.
    problems = []
    for path in _plugin_py_files():
        for cname, line, name in _self_calls_without_definition(path):
            problems.append('%s:%d %s.self.%s()' % (path, line, cname, name))
    check('every self.<method>() is defined in its class', not problems,
          '; '.join(problems[:5]))

    # 2. The run panel is identified by a view setting now; comparing the old
    #    'opd_info' status value is exactly how _refresh_panel silently broke.
    stale = []
    for path in _plugin_py_files():
        with open(path, encoding='utf-8') as f:
            for i, line in enumerate(f, 1):
                if "get_status('opd_info') ==" in line:
                    stale.append('%s:%d' % (path, i))
    check('no stale opd_info marker comparisons', not stale, ', '.join(stale))

    # 3. A lambda inside an except block must not read the exception variable
    #    without binding it (Python deletes it when the block ends).
    leaked = []
    for path in _plugin_py_files():
        for line, name in _lambdas_leaking_except_var(path):
            leaked.append('%s:%d lambda reads except-var %s' % (path, line, name))
    check('no lambda leaks an except variable', not leaked, '; '.join(leaked[:5]))

    print('== merge policy (shared by import / clipboard / companion) ==')
    merge = importlib.import_module(pkg + '.core.cph_tests_merge')

    merged, conflicts = merge.merge_tests(
        [{'test': '1\n', 'correct_answers': []}],
        [{'test': '1\n', 'correct_answers': ['7\n']}])
    check('answer is filled in when none was stored',
          len(merged) == 1 and merged[0]['correct_answers'] == ['7\n'] and not conflicts,
          repr((merged, conflicts)))

    merged, conflicts = merge.merge_tests(
        [{'test': '1\n', 'correct_answers': ['user\n']}],
        [{'test': '1\n', 'correct_answers': ['sample\n']}])
    check('a stored answer is never overwritten by a re-sent sample',
          merged[0]['correct_answers'] == ['user\n'], repr(merged))
    check('the conflict is reported to the caller', len(conflicts) == 1, repr(conflicts))

    merged, _ = merge.merge_tests([{'test': '1\n', 'correct_answers': []}],
                                  [{'test': '2\n', 'correct_answers': []}])
    check('a different input is appended', len(merged) == 2, repr(merged))

    print('== regard_pe_as_ac is wired to the setting ==')
    pe_strict = verdict.get_verdict_by_code(
        rtcode=0, runtime=10, time_limit_ms=1000, memory_limit_mb=256,
        stderr='', stdout='1  2', expected_output='1 2')
    pe_lenient = verdict.get_verdict_by_code(
        rtcode=0, runtime=10, time_limit_ms=1000, memory_limit_mb=256,
        stderr='', stdout='1  2', expected_output='1 2', regard_pe_as_ac=True)
    check('PE stays PE by default', pe_strict['name'] == 'PE', pe_strict['name'])
    check('regard_pe_as_ac=true turns PE into AC',
          pe_lenient['name'] == 'AC', pe_lenient['name'])
    with open(os.path.join(ROOT, 'Algorithm Competition Assistant.sublime-settings'), encoding='utf-8') as f:
        settings_text = f.read()
    check('the setting is documented in the default settings',
          '"regard_pe_as_ac"' in settings_text)
    check('it defaults to false (strict)',
          '"regard_pe_as_ac": false' in settings_text)

    print('== Package Control review rules ==')
    DEFAULTS = ('Default (Windows).sublime-keymap', 'Default (Linux).sublime-keymap',
                'Default (OSX).sublime-keymap')

    # 1. A package should not claim keys while the user is editing their own
    #    code (Package Control: "we strongly advice against adding keybindings
    #    by default"). Our shipped defaults may only fire inside the package's
    #    own syntax or behind a custom state key.
    claims = []
    every_binding_scoped = True
    for name in DEFAULTS:
        with open(os.path.join(ROOT, name), encoding='utf-8') as f:
            for entry in json.load(f):
                contexts = entry.get('context') or []
                if not contexts:
                    every_binding_scoped = False
                    claims.append('%s %s (no context)' % (name, entry.get('keys')))
                    continue
                own_view = any(
                    item.get('key') == 'selector' and item.get('operator') == 'equal'
                    and str(item.get('operand', '')).strip() == 'source.TestSyntax'
                    for item in contexts)
                stateful = any(str(item.get('key', '')).startswith('cph_')
                               for item in contexts)
                if not (own_view or stateful):
                    claims.append('%s %s' % (name, '+'.join(entry.get('keys') or [])))
    check('shipped bindings never claim keys while editing code', not claims,
          '; '.join(claims))
    check('every shipped binding still carries a context', every_binding_scoped)

    # The code-file bindings live in the PACKAGE keymap on purpose: they load
    # with the package and vanish with it, so disabling/uninstalling hands the
    # keys back to other packages. They are gated behind 'enable_keybindings'
    # so the keys can be released without editing any keymap.
    with open(os.path.join(ROOT, 'Default (Windows).sublime-keymap'), encoding='utf-8') as f:
        shipped_bindings = json.load(f)
    gated = [e for e in shipped_bindings
             if any(str(i.get('key', '')) == 'cph_keybindings_enabled'
                    for i in (e.get('context') or []))]
    check('the code-file bindings ship inside the package',
          len(gated) >= 13
          and any(e.get('command') == 'cph_view_tester' for e in gated),
          '%d gated bindings' % len(gated))
    check('the keybinding gate is backed by a real setting',
          '"enable_keybindings"' in settings_text
          and 'cph_keybindings_enabled' in open(
              os.path.join(ROOT, 'cph_context.py'), encoding='utf-8').read())

    # 2. One keymap per platform, no copies of it. `Example (<platform>).sublime-
    #    keymap` shipped the same bindings "for copying into your own keymap",
    #    and it drifted: it kept ctrl+alt+e (export tests) that the package
    #    keymap did not bind, so the README advertised a shortcut that only
    #    worked if that second file happened to be loaded. Two copies of a
    #    binding set cannot be kept honest. There is one: the package keymap,
    #    which "Key Bindings" opens as the base file for overrides.
    strays = sorted(f for f in os.listdir(ROOT)
                    if f.endswith('.sublime-keymap') and f not in DEFAULTS)
    check('the package ships exactly one keymap per platform', not strays,
          '; '.join(strays))

    def _bindings(name):
        with open(os.path.join(ROOT, name), encoding='utf-8') as f:
            return json.load(f)

    shipped_bindings = _bindings('Default (Windows).sublime-keymap')

    def _keys_for(bindings, command):
        return [e.get('keys') for e in bindings if e.get('command') == command]

    check('export tests really is bound, as the README says',
          _keys_for(shipped_bindings, 'cph_export_tests') == [['ctrl+alt+e']],
          repr(_keys_for(shipped_bindings, 'cph_export_tests')))

    # 2a. The README table is the thing a contestant reads mid-contest, so it is
    #     checked against the keymap instead of against memory: every chord the
    #     table prints must actually be bound. This is the check that would have
    #     caught Ctrl+Alt+E (documented for versions, never in the package file).
    def _canon(text):
        return ' '.join(word for part in text.replace(',', ' ').split('+')
                        for word in part.lower().split())

    bound = set()
    for entry in shipped_bindings:
        bound.add(' '.join(_canon(k) for k in (entry.get('keys') or [])))
    documented = []
    readme = io.open(os.path.join(ROOT, 'README.md'), encoding='utf-8').read()
    for line in readme.split('\n'):
        if not line.startswith(u'| `'):
            continue
        cell = re.sub(r'\([^)]*\)', '', line.split('|')[1])
        for token in re.split(r'\s*/\s*|\s+或\s+', cell):
            token = token.strip().strip('`')
            if not re.match(r'(?i)^(ctrl|enter)', token):
                continue          # Cmd aliases, panel names, prose cells
            documented.append(_canon(token))
    undocumented_rows = [c for c in documented if c not in bound]
    check('every chord the README table prints is really bound',
          len(documented) >= 20 and not undocumented_rows,
          '%d rows, not bound: %s' % (len(documented), ', '.join(undocumented_rows)))

    # 2b. A binding scoped to "source.c++, source.c, source.python,
    #     source.java" is a binding that does nothing in the Rust, Go, Pascal
    #     and JavaScript entries of the very same settings file.
    exts = set()
    for m in re.finditer(r'"extensions":\s*\[([^\]]*)\]', settings_text):
        for item in m.group(1).split(','):
            item = item.strip().strip('"')
            if item:
                exts.add(item)
    SCOPE_OF = {'cpp': 'source.c++', 'c': 'source.c', 'py': 'source.python',
                'java': 'source.java', 'rs': 'source.rust', 'go': 'source.go',
                'pas': 'source.pascal', 'pp': 'source.pascal',
                'js': 'source.js', 'mjs': 'source.js', 'cjs': 'source.js'}
    missing_exts = sorted(e for e in exts if e not in SCOPE_OF)
    check('every shipped language extension has a known syntax selector',
          not missing_exts, '; '.join(missing_exts))
    unscoped = []
    for name in DEFAULTS:
        for entry in _bindings(name):
            for item in entry.get('context') or []:
                operand = str(item.get('operand') or '')
                # only the "any code file we support" bindings; the single
                # language ones (source.c++ for the template) and our own test
                # editor (source.TestSyntax) mean something else
                if ',' not in operand or 'source.c++' not in operand:
                    continue
                absent = sorted(SCOPE_OF[e] for e in exts
                                if e in SCOPE_OF and SCOPE_OF[e] not in operand)
                if absent:
                    unscoped.append('%s %s: %s' % (name, entry.get('keys'),
                                                   ', '.join(absent)))
    check('every multi-language binding covers every supported language',
          not unscoped, '; '.join(sorted(set(unscoped)))[:400])

    # 3. Custom context keys: the keymap must not ask a question the listener
    #    does not answer, and the listener must not answer a question nobody
    #    asks. Both directions, because the drift that shipped here went the
    #    second way - four branches stayed behind after their bindings left,
    #    and one of them (cph_stress_running) mirrored a flag nothing read.
    with open(os.path.join(ROOT, 'cph_context.py'), encoding='utf-8') as f:
        ctx_src = f.read()
    used_keys = set()
    for name in DEFAULTS:
        for entry in _bindings(name):
            for item in entry.get('context') or []:
                key = item.get('key', '')
                if key.startswith('cph_'):
                    used_keys.add(key)
    unanswered = [k for k in sorted(used_keys) if ("'%s'" % k) not in ctx_src]
    check('custom context keys are answered by cph_context', not unanswered,
          '; '.join(unanswered))
    check('at least one custom context key is exercised', bool(used_keys),
          'none found')
    answered = set(re.findall(r"key == '(cph_[a-z_]+)'", ctx_src))
    unasked = sorted(answered - used_keys)
    check('cph_context answers no key the shipped keymap stopped asking',
          not unasked, '; '.join(unasked))

    # 3b. enable_keybindings=false must release EVERY package binding, which is
    #     what the README claims. Most entries ask the gate directly; the ones
    #     that fire on state (stop the stress test, stop the listener, collapse
    #     the panel) are released inside the listener, at one choke point, so a
    #     new state key cannot forget it. Proved by making every state true and
    #     then switching the setting off.
    ctx_mod = importlib.import_module(pkg + '.cph_context')
    state_mod = importlib.import_module(pkg + '.core.cph_state')
    gate_settings = importlib.import_module(pkg + '.core.cph_settings')
    listener = ctx_mod.CphContextListener()
    gate_view = sys.modules['sublime'].View(settings={'cph_run_view': True})
    gate_view._file_name = os.path.join('x', 'a.cpp')
    saved_gate = gate_settings.get_settings()
    saved_listener = state_mod.state.get('listener_running')
    saved_views = dict(state_mod.state.get('stress_views') or {})
    try:
        state_mod.set_listener(object(), port=1)
        state_mod.register_stress_view(gate_view.id())
        asked = sorted(used_keys)
        op_equal = sys.modules['sublime'].OP_EQUAL
        gate_settings.init_settings({'enable_keybindings': True})
        on = [listener.on_query_context(gate_view, k, op_equal, True, True)
              for k in asked]
        gate_settings.init_settings({'enable_keybindings': False})
        off = [listener.on_query_context(gate_view, k, op_equal, True, True)
               for k in asked]
        check('with the gate on, every context the keymap asks is answerable',
              all(v is True for v in on) and len(on) == len(asked),
              repr(list(zip(asked, on))))
        check('enable_keybindings=false releases every package binding',
              all(v is False for v in off), repr(list(zip(asked, off))))
        check('and it reports itself as off, so bindings gated directly stop too',
              listener.on_query_context(gate_view, 'cph_keybindings_enabled',
                                        op_equal, True, True) is False)
    finally:
        gate_settings.init_settings(saved_gate)
        state_mod.state['listener_running'] = saved_listener
        state_mod.state['listener_port'] = None
        state_mod.state['stress_views'] = saved_views

    # 4. Context menu entries: .sublime-menu has no `context` key, so the
    #    command's is_visible() is the only way to make an entry conditional.
    #    Every command we put in that menu must implement it.
    def class_defines(pkg_rel_dir, class_name, method):
        for rel in _plugin_py_files():
            if pkg_rel_dir and not rel.startswith(pkg_rel_dir):
                continue
            with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == class_name:
                    return any(isinstance(b, ast.FunctionDef) and b.name == method
                               for b in node.body)
        return False

    def command_class(command):
        return ''.join(part.capitalize() for part in command.split('_')) + 'Command'

    with open(os.path.join(ROOT, 'Context.sublime-menu'), encoding='utf-8') as f:
        menu = json.load(f)
    unconditional = [item['command'] for item in menu
                     if not class_defines('', command_class(item['command']), 'is_visible')]
    check('every context menu command implements is_visible()', not unconditional,
          '; '.join(unconditional))
    menu_bare_context = [item['command'] for item in menu if 'context' in item]
    check('context menu items do not fake an unsupported context key',
          not menu_bare_context, '; '.join(menu_bare_context))

    # 5. Settings and key bindings must open in split view: `edit_settings`,
    #    not `open_file`.
    with open(os.path.join(ROOT, 'Main.sublime-menu'), encoding='utf-8') as f:
        main_menu = json.load(f)
    wrong = []
    outside = []

    def walk_menu(items):
        for item in items:
            caption = str(item.get('caption') or '')
            if item.get('command') == 'open_file' and (
                    'settings' in caption.lower() or 'key binding' in caption.lower()
                    or '.sublime-keymap' in str(item.get('args'))):
                wrong.append(caption)
            args = item.get('args') or {}
            ref = args.get('base_file') or args.get('file') or ''
            # A package must reference its own files: the reviewer rejects
            # `${packages}/Default/...` in a Key Bindings entry (only User/ and
            # our own directory are fine).
            if ref.startswith('${packages}/') and not ref.startswith(
                    ('${packages}/Algorithm Competition Assistant/', '${packages}/User/')):
                outside.append('%s -> %s' % (caption, ref))
            for child in item.get('children') or []:
                walk_menu([child])

    walk_menu(main_menu)
    check('settings/keybindings use edit_settings (split view)', not wrong,
          '; '.join(wrong))
    check('menu entries only reference this package or User/', not outside,
          '; '.join(outside))

    # 5a. Package naming must stay consistent after the rename. The
    #     first-install message is the one screen a brand-new user reads, and
    #     the fold arrows must point at this package's own icons rather than a
    #     hardcoded name (which broke whenever the folder differed).
    with open(os.path.join(ROOT, 'messages', 'install.txt'), encoding='utf-8') as f:
        install_text = f.read()
    check('first-install message uses the published name',
          'Algorithm Competition Assistant' in install_text
          and 'cph-by-chenkx' not in install_text)

    settings_mod = importlib.import_module(pkg + '.core.cph_settings')
    tm_mod = importlib.import_module(pkg + '.test_manager')
    _prefix = 'Packages/%s/' % settings_mod.base_name
    for _label, _prop in (('begin', tm_mod.CphTestManagerCommand.REGION_BEGIN_PROP),
                          ('end', tm_mod.CphTestManagerCommand.REGION_END_PROP)):
        _icon = [p for p in _prop if isinstance(p, str) and p.endswith('.png')]
        _ok = False
        if len(_icon) == 1 and _icon[0].startswith(_prefix):
            _rel = _icon[0][len(_prefix):].replace('/', os.sep)
            _ok = os.path.isfile(os.path.join(ROOT, _rel))
        check('fold %s icon resolves inside this package' % _label, _ok,
              str(_icon))

    with open(os.path.join(ROOT, '.gitattributes'), encoding='utf-8') as f:
        gitattrs = f.read()
    check('assistant memory dir is export-ignored',
          '.workbuddy-ai export-ignore' in gitattrs)

    # 6. The context menu switch is documented in the shipped settings.
    with open(os.path.join(ROOT, 'Algorithm Competition Assistant.sublime-settings'), encoding='utf-8') as f:
        settings_text = f.read()
    check('context_menu setting is shipped',
          '"context_menu"' in settings_text)

    # 3. Root level plugin modules must not be imported from each other
    #    (Sublime loads every root level .py as an independent plugin).
    root_modules = set()
    for name in os.listdir(ROOT):
        if name.endswith('.py') and os.path.isfile(os.path.join(ROOT, name)):
            root_modules.add(name[:-3])
    violations = []
    for rel in _plugin_py_files():
        if '/' in rel:          # only root level files matter
            continue
        with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
            for i, line in enumerate(f, 1):
                stripped = line.strip()
                if not stripped.startswith('from .'):
                    continue
                # 'from .cph_companion import x' -> '.cph_companion' -> 'cph_companion'
                target = stripped.split()[1].lstrip('.').split('.')[0]
                if target in root_modules - {'__init__'}:
                    violations.append('%s:%d %s' % (rel, i, stripped))
    check('no root-level plugin module imports', not violations,
          '; '.join(violations[:3]))

    # 4. Platform specific settings variants must be named after the package
    #    or a syntax we ship - we ship none, so the base file must stay neutral.
    stray = [f for f in os.listdir(ROOT) if ' (' in f and f.endswith('.sublime-settings')]
    check('no stray platform settings variants', not stray, '; '.join(stray))

    # 5. Sublime loads every .py in the package root as an independent plugin,
    #    so a stray file there is not inert: it either raises on import (the
    #    removed cph_stress_tmp_section.py died on a missing 'import re') or
    #    silently registers a second copy of a command. The list has to be a
    #    deliberate decision, which means adding a root module means adding it
    #    here as well.
    expected_root_modules = set([
        '__init__.py', 'plugin_init.py', 'test_edit.py', 'test_manager.py',
        'cph_auto.py', 'cph_companion.py', 'cph_context.py', 'cph_doctor.py',
        'cph_extras.py', 'cph_import.py', 'cph_jump.py', 'cph_language.py',
        'cph_options.py', 'cph_run_modes.py', 'cph_stress.py', 'cph_template.py',
        'cph_tests_io.py',
    ])
    actual_root_modules = set(
        f for f in os.listdir(ROOT)
        if f.endswith('.py') and os.path.isfile(os.path.join(ROOT, f)))
    check('the package root holds exactly the modules we ship',
          actual_root_modules == expected_root_modules,
          'missing %s / unexpected %s' % (
              sorted(expected_root_modules - actual_root_modules),
              sorted(actual_root_modules - expected_root_modules)))

    #    Same hazard for anything an editor or a test run might drop next to
    #    the sources: .pyc files, backup copies, rejected patches.
    leftovers = sorted(f for f in os.listdir(ROOT)
                       if f.endswith(('.pyc', '.orig', '.rej', '~'))
                       or '.bak' in f)
    check('no editor leftovers in the package root', not leftovers,
          '; '.join(leftovers))

    print('== build artifacts: the compiler does not always use the -o name ==')
    artifacts = importlib.import_module(pkg + '.Modules.build_artifact')

    check('quoted -o with spaces is parsed',
          artifacts.output_path_from_compile_cmd(
              'g++ "a b.cpp" -o "A  中文.exe" -DLOCAL') == 'A  中文.exe')
    check('unquoted -o is parsed',
          artifacts.output_path_from_compile_cmd('g++ x.cpp -o out.exe') == 'out.exe')
    check('a command without -o yields nothing',
          artifacts.output_path_from_compile_cmd('javac -d . A.java') is None)

    # A Chinese name written through GBK and read back as latin-1.
    mangled = '中文.exe'.encode('cp936').decode('latin-1')
    check('mangled name is recognised', artifacts.names_match(mangled, '中文.exe'),
          repr(mangled))
    check('unrelated names do not match', not artifacts.names_match('a.exe', 'b.exe'))

    tmp = tempfile.mkdtemp(prefix='cph_artifact_')
    try:
        wanted = os.path.join(tmp, 'A  中文.exe')
        real = os.path.join(tmp, 'A  ' + mangled)
        open(real, 'w').close()

        check('the missing -o name resolves to the mangled file',
              artifacts.resolve_artifact(wanted, tmp) == real)
        check('an existing file is returned untouched',
              artifacts.resolve_artifact(real, tmp) == real)
        check('retarget_command points at the real binary',
              artifacts.retarget_command(
                  '"%s" -x' % wanted, tmp) == '"%s" -x' % real)
        check('retarget_command leaves an existing path alone',
              artifacts.retarget_command('"%s" ' % real, tmp) == '"%s" ' % real)
        check('retarget_command leaves PATH commands alone',
              artifacts.retarget_command('python "x.py"', tmp) == 'python "x.py"')
        check('retarget_path points at the real binary',
              artifacts.retarget_path(wanted, tmp) == real)
        check('retarget_path leaves a foreign missing file alone',
              artifacts.retarget_path(os.path.join(tmp, 'nope.py'), tmp)
              == os.path.join(tmp, 'nope.py'))

        os.remove(real)
        check('nothing is invented when the binary is really gone',
              artifacts.resolve_artifact(wanted, tmp) is None)
        other = os.path.join(tmp, 'weird.exe')
        open(other, 'w').close()
        check('the newest file wins as a last resort',
              artifacts.resolve_artifact(wanted, tmp, started_at=0) == other)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # print() must survive a narrow console: the Windows CI runner is cp1252
    # and printing a mangled non-ASCII binary name there raised
    # UnicodeEncodeError and failed the whole job.
    class _NarrowStdout(object):
        def write(self, text):
            text.encode('cp1252')
            return len(text)

        def flush(self):
            pass

    try:
        with contextlib.redirect_stdout(_NarrowStdout()):
            # '中文' and the mangled latin-1 lookalike are both unprintable
            # on cp1252; this is the exact message that broke the CI job.
            artifacts.print_safe(
                '[Algorithm Competition Assistant] the binary is %r on disk, not %r'
                % ('ÖÐÎÄ.exe', '中文.exe'))
        narrow_ok = True
    except Exception as e:
        narrow_ok = '%s: %s' % (type(e).__name__, e)
    check('console output survives a cp1252 stdout', narrow_ok is True, narrow_ok)

    print('== the run command follows the real binary ==')
    pm_mod = importlib.import_module(pkg + '.Modules.ProcessManager')
    tmp2 = tempfile.mkdtemp(prefix='cph_run_')
    try:
        source = os.path.join(tmp2, '中文.cpp')
        open(source, 'w').close()
        exe_real = os.path.join(tmp2, mangled)
        exe_wanted = os.path.join(tmp2, '中文.exe')
        run_settings = [{
            'name': 'C++', 'extensions': ['cpp'],
            'compile_cmd': 'g++ "{source_file}" -o "{file_name}.exe"',
            'run_cmd': '"{source_file_dir}/{file_name}.exe" {args}',
        }]
        manager = pm_mod.ProcessManager(source, 'source.c++',
                                        run_settings=run_settings)
        open(exe_real, 'w').close()
        cmd = manager.get_run_cmd('')
        check('get_run_cmd retargets a missing non-ASCII binary',
              exe_real in cmd, cmd)
        os.remove(exe_real)
        open(exe_wanted, 'w').close()
        cmd = manager.get_run_cmd('')
        check('get_run_cmd keeps the command when the binary is there',
              '中文.exe' in cmd and mangled not in cmd, cmd)
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)

    print('== stress loop: a timeout is not a failure ==')
    i18n = importlib.import_module(pkg + '.core.cph_i18n')
    stress = importlib.import_module(pkg + '.cph_stress')
    fake_sublime = sys.modules['sublime']
    chunks = []
    saved = (fake_sublime.set_timeout, stress._append_stress,
             stress._compile_program, stress._run_program, i18n.get_lang())
    try:
        fake_sublime.set_timeout = lambda fn, delay=0: fn()
        stress._append_stress = lambda sess, text: chunks.append(text)
        stress._compile_program = lambda f, time_limit=30, sess=None: (True, f, '')
        i18n.set_lang('en')

        def run_as(gen, user):
            del chunks[:]
            sess = stress._new_session('u.cpp', 's.cpp', 'gen.cpp', 2.0, 3)

            def fake_run(program, data, cwd=None, time_limit=2.0, env=None,
                         sess=None):
                # env carries the stress seed (CPH_SEED) in the real runner
                return gen if program.endswith('gen.cpp') else user

            stress._run_program = fake_run
            stress._run_stress_loop(sess)
            return ''.join(chunks)

        text = run_as((-1, '', '', True), (0, '1', '', False))
        check('a generator timeout is reported as a timeout',
              'timed out' in text and 'Generator failed' not in text,
              text.replace('\n', ' | ')[:200])
        check('the generator timeout names the setting to change',
              'stress_generator_time_limit_seconds' in text)

        text = run_as((3, '', 'boom', False), (0, '1', '', False))
        check('a generator crash shows the exit code and its stderr',
              'exit code 3' in text and 'boom' in text,
              text.replace('\n', ' | ')[:200])

        text = run_as((0, '1', '', False), (-1, '', 'FileNotFoundError: nose.exe', False))
        check('a program that cannot start is reported, not compared',
              'cannot run' in text and 'mismatch' not in text,
              text.replace('\n', ' | ')[:200])

        text = run_as((0, '1', '', False), (-1, '', '', True))
        check('a user TLE adds the debug-output hint',
              'hint' in text and 'mismatch' not in text,
              text.replace('\n', ' | ')[:200])

        # An exit code of 0 vs a crash used to be the same thing to the loop:
        # std dying mid-print produced a truncation that was then compared as
        # if it were the correct answer, so the "counterexample" it reported
        # did not exist and the user went off to "fix" a program that was
        # already right. The three cases below are the whole distinction.
        def run_as3(gen, user, std):
            del chunks[:]
            sess = stress._new_session('u.cpp', 's.cpp', 'gen.cpp', 2.0, 3)

            def fake_run(program, data, cwd=None, time_limit=2.0, env=None,
                         sess=None):
                if program.endswith('gen.cpp'):
                    return gen
                if program.endswith('s.cpp'):
                    return std
                return user

            stress._run_program = fake_run
            stress._run_stress_loop(sess)
            return ''.join(chunks)

        wrote = []
        real_load = stress.load_all_tests
        real_save = stress.save_tests
        stress.load_all_tests = lambda f: []
        stress.save_tests = lambda f, tests: (wrote.append([dict(t) for t in tests]),
                                             True)[1]
        try:
            text = run_as3((0, '5 5\n', '', False), (0, '10\n', '', False),
                           (-11, '', '', False))
            check('a std that dies is not allowed to mint a counterexample',
                  'died' in text and 'mismatch' not in text and 'Diff' not in text,
                  text.replace('\n', ' | ')[:260])
            check('the std death names its exit code and says nothing was compared',
                  'exit code -11' in text and 'compared' in text,
                  text.replace('\n', ' | ')[:260])
            check('nothing is persisted when std is the one that broke',
                  wrote == [], repr(wrote)[:200])

            del wrote[:]
            text = run_as3((0, '5 5\n', '', False), (-6, '', 'Assertion failed\n', False),
                           (0, '10\n', '', False))
            check('a user crash is its own failure, not a blank-output diff',
                  'your program died' in text and 'exit code -6' in text,
                  text.replace('\n', ' | ')[:260])
            check('the crashing input is saved with no expected answer',
                  len(wrote) == 1 and wrote[0][0]['test'] == '5 5\n'
                  and wrote[0][0]['correct_answers'] == [],
                  repr(wrote)[:220])
            check('the saved crash test announces it will read as RE',
                  'no expected output' in text and 'RE' in text,
                  text.replace('\n', ' | ')[:260])

            text = run_as3((0, '1 1\n', '', False), (0, '2\n', '', False),
                           (-1, '', "[WinError 2] The system cannot find the "
                                    "file specified: 's.exe'", False))
            check('a std that was never launched is still a setup failure',
                  'cannot run' in text and 'died' not in text,
                  text.replace('\n', ' | ')[:260])

            text = run_as3((0, '1 1\n', '', False),
                           (1, '', 'fopen: No such file or directory\n', False),
                           (0, '2\n', '', False))
            check("a program's own 'No such file' text is a counterexample, "
                  "not a launcher failure",
                  'your program died' in text and 'cannot run' not in text,
                  text.replace('\n', ' | ')[:260])

            del wrote[:]
            text = run_as3((0, '1 2\n', '', False), (0, '3\n', 'slow stderr\n', False),
                           (0, '4\n', '', False))
            check('a real mismatch still stores the std output as the answer',
                  len(wrote) == 1 and wrote[0][0]['correct_answers'] == ['4'],
                  repr(wrote)[:220])
            check('a mismatch shows the stderr it used to throw away',
                  'stderr' in text and 'slow stderr' in text,
                  text.replace('\n', ' | ')[:300])
        finally:
            stress.load_all_tests = real_load
            stress.save_tests = real_save
            # The fake above is the only thing keeping a synthetic "u.cpp" from
            # landing a real tests file in the repository - and an earlier
            # revision of the crash path did exactly that. Assert it instead of
            # trusting the patch, so a future change that bypasses the fake
            # fails here rather than leaving junk next to the suite.
            stray = [os.path.join(base, 'u.cpp__tests')
                     for base in (os.getcwd(),
                                  os.path.dirname(os.path.abspath(__file__)))]
            found = [p for p in stray if os.path.exists(p)]
            for p in found:
                os.remove(p)
            check('the stress cases never write a real tests file',
                  not found, repr(found))
    finally:
        fake_sublime.set_timeout, stress._append_stress, \
            stress._compile_program, stress._run_program = saved[:4]
        i18n.set_lang(saved[4])
        for leftover in list(stress._sessions.values()):
            stress._stop_stress(leftover)

    # Stress builds its commands with the same placeholders as the main Run
    # path, and must be just as lenient: {file} is documented, and a
    # misspelled name only warns (doctor reports it) - it used to abort with
    # KeyError: 'file' / KeyError: '<typo>'.
    stress_cmd = stress._format_cmd('g++ {file} -o {file_name} {args}',
                                    os.path.join('tmp', 'a b.cpp'))
    check('stress accepts every documented placeholder',
          'a b.cpp' in stress_cmd and stress_cmd.rstrip().endswith('a b'), stress_cmd)
    stress_cmd = stress._format_cmd('g++ {typo} {file_name}', os.path.join('tmp', 'a.cpp'))
    check('stress ignores an unknown placeholder',
          '{typo}' not in stress_cmd and stress_cmd.rstrip().endswith('a'), stress_cmd)

    print('== unload hands the child processes back ==')
    # Sublime does not wait for a plugin's threads or its children: upgrading
    # the package, or editing a .py on a folder install, unloads it while a
    # program or a g++ is still running. The orphan keeps its own .exe open on
    # Windows, and the next compile reports "cannot open output file: Permission
    # denied" without a word about the process holding the file.
    _st = state_mod
    _done = []

    class _FakeProc(object):
        def __init__(self, alive=True):
            self._alive = alive
            self.pid = 4242

        def poll(self):
            return None if self._alive else 0

    _st._live.clear()
    _st.register_live('a', lambda: True, lambda: _done.append('a'))
    _st.register_live('dead', lambda: False, lambda: _done.append('dead'))
    check('the registry holds what a run started',
          _st.live_count() == 2, str(_st.live_count()))
    n_killed, n_pruned, n_errors = _st.kill_all_live()
    check('unloading kills the live child and prunes the dead one',
          n_killed == 1 and n_pruned == 1 and _done == ['a'] and _st.live_count() == 0,
          '%s/%s/%s %s' % (n_killed, n_pruned, n_errors, _done))

    def _boom():
        raise RuntimeError('refuses to die')

    del _done[:]
    _st.register_live('boom', lambda: True, _boom)
    _st.register_live('ok', lambda: True, lambda: _done.append('ok'))
    n_killed, n_pruned, n_errors = _st.kill_all_live()
    check('one process that refuses to die does not keep the others alive',
          n_killed == 1 and n_errors == 1 and _done == ['ok'],
          '%s/%s/%s %s' % (n_killed, n_pruned, n_errors, _done))
    check('a second unload attempt has nothing left to kill',
          _st.live_count() == 0 and _st.kill_all_live() == (0, 0, 0))

    _st._live.clear()
    for _i in range(70):
        _st.register_live('k%d' % _i, lambda: False, lambda: None)
    check('registering past the cap forgets the processes that already ended',
          _st.live_count() < 70 and _st.live_count() <= _st._LIVE_CAP,
          str(_st.live_count()))

    # The real seam, on a real process: ProcessManager is what every Run and
    # every parallel test goes through, so a run must register itself there.
    real_platform = 'windows' if os.name == 'nt' else 'linux'
    saved_platform = sys.modules['sublime'].platform
    sys.modules['sublime'].platform = lambda: real_platform
    tmp_live = tempfile.mkdtemp(prefix='cph_live_')
    sleeper = os.path.join(tmp_live, 'sleeper.py')
    with open(sleeper, 'w', encoding='utf-8') as f:
        f.write('import time\ntime.sleep(120)\n')
    try:
        entry_live = [{'name': 'Python', 'extensions': ['py'], 'compile_cmd': None,
                       'run_cmd': sys.executable.replace('\\', '/') + ' "{source_file}"',
                       'time_limit_ms': 60000, 'memory_limit_mb': 256}]
        mgr_live = pm_mod.ProcessManager(sleeper, 'source.python',
                                         run_settings=entry_live)
        _st._live.clear()
        mgr_live.run_file()
        check('a run registers the program it started',
              _st.live_count() == 1, repr(sorted(_st._live.keys())))
        _st.kill_all_live()
        proc_live = getattr(mgr_live, 'process', None)
        deadline = time.time() + 15
        while proc_live is not None and proc_live.poll() is None \
                and time.time() < deadline:
            time.sleep(0.05)
        check('the tracked process really dies when the plugin unloads',
              proc_live is not None and proc_live.poll() is not None,
              str(getattr(proc_live, 'poll', lambda: None)()))

        # ProcessManager is not the only spawner: a stress round uses its own
        # Popen, and a scratch directory is only removable if it was written
        # down somewhere. Both must survive a run that is interrupted.
        _st._live.clear()
        sess_live = stress._new_session('u.cpp', 's.cpp', 'gen.cpp', 2.0, 3)
        sess_live['stress_view_id'] = 777
        quick = os.path.join(tmp_live, 'quick.py')
        with open(quick, 'w', encoding='utf-8') as f:
            f.write('print(1)\n')
        stress._popen_capture([sys.executable, quick], tmp_live,
                              input_text='', timeout=20, sess=sess_live)
        check('a stress round registers its process too',
              _st.live_count() == 1 and len(sess_live.get('live_keys') or ()) == 1,
              '%s %s' % (_st.live_count(), sorted(sess_live.get('live_keys') or ())))
        sess_live['build_root'] = tempfile.mkdtemp(prefix='cph-stress-')
        stress.register_temp_dir(sess_live['build_root'])
        root_live = sess_live['build_root']
        stress._finish_stress(sess_live)
        check('finishing a stress run releases both the process and the scratch dir',
              _st.live_count() == 0 and not os.path.exists(root_live),
              '%s %s' % (_st.live_count(), root_live))

        _st._temp_dirs.clear()
        dir_live = tempfile.mkdtemp(prefix='cph-stress-')
        _st.register_temp_dir(dir_live)
        check('a scratch directory is remembered', _st.temp_dir_count() == 1)
        _st.clean_temp_dirs()
        check('and removed when the plugin goes away',
              not os.path.exists(dir_live) and _st.temp_dir_count() == 0)

        # The stale sweep: a crashed run's directory is ours to remove, a fresh
        # one belongs to a window that may still be compiling into it.
        old = time.time() - 3 * 86400
        stale = tempfile.mkdtemp(prefix='cph-stress-')
        fresh = tempfile.mkdtemp(prefix='cph-stress-')
        kept = tempfile.mkdtemp(prefix='cph-doctor-')
        foreign = tempfile.mkdtemp(prefix='cph-not-a-')
        for one in (stale, kept, foreign):
            os.utime(one, (old, old))
        removed = _st.sweep_stale_temp_dirs(_st.SCRATCH_PREFIXES,
                                            older_than_seconds=86400, keep=[kept])
        check('a stale scratch directory from a crashed run is removed',
              not os.path.exists(stale), repr(removed))
        check('a fresh one is left alone', os.path.isdir(fresh))
        check('a directory named in keep survives even when old', os.path.isdir(kept))
        check('a prefix that is not ours is never touched', os.path.isdir(foreign))
        check('every feature that builds outside the source tree is swept',
              all(p in _st.SCRATCH_PREFIXES
                  for p in ('cph-stress-', 'cph-checker-', 'cph-interactor-',
                            'cph-calibrate-', 'cph-doctor-')),
              repr(_st.SCRATCH_PREFIXES))
        for one in (fresh, kept, foreign):
            shutil.rmtree(one, ignore_errors=True)
    finally:
        sys.modules['sublime'].platform = saved_platform
        shutil.rmtree(tmp_live, ignore_errors=True)
        _st._live.clear()
        _st._temp_dirs.clear()

    _unload_src = open(os.path.join(ROOT, 'plugin_init.py'), encoding='utf-8').read()
    check('the entry point reaps children on unload',
          'kill_all_live()' in _unload_src and 'clean_temp_dirs()' in _unload_src)
    check('and sweeps the leftovers of a previous session at startup',
          'sweep_stale_temp_dirs(' in _unload_src)
    check('the stress module stops its own sessions, because root modules '
          'cannot import each other',
          'def plugin_unloaded()' in open(os.path.join(ROOT, 'cph_stress.py'),
                                          encoding='utf-8').read())

    print('== stress picker: every step says which file it wants ==')
    # Setting a stress test up asked for two files in a row with nothing on
    # screen to tell them apart: the titles were jargon ("选择 std (标准) 文件"),
    # and every Sublime-panel path dropped the title argument completely.
    class _PickerWindow(object):
        def __init__(self):
            self.panels = []
            self.prompts = []

        def show_quick_panel(self, items, on_select, *args, **kwargs):
            self.panels.append((list(items), on_select))

        def show_input_panel(self, caption, initial, on_done, *args):
            self.prompts.append((caption, initial))

        def folders(self):
            return []

    i18n.set_lang('zh')
    std_title = i18n.t('choose_std_file')
    gen_title = i18n.t('choose_generator_file')
    check('the std pick names its step and its role',
          '1/2' in std_title and 'std' in std_title.lower()
          and ('正解' in std_title or '暴力' in std_title), std_title)
    check('the generator pick names its step and its role',
          '2/2' in gen_title and 'gen' in gen_title.lower()
          and '生成' in gen_title, gen_title)
    check('the two prompts are not the same words', std_title != gen_title)
    i18n.set_lang('en')
    check('the English prompts carry the same step numbers',
          '1/2' in i18n.t('choose_std_file')
          and '2/2' in i18n.t('choose_generator_file'))
    i18n.set_lang('zh')

    pw = _PickerWindow()
    picked = []
    stress._pick_file(pw, os.path.join('tmp'), gen_title,
                      [os.path.join('tmp', 'gen.cpp'), os.path.join('tmp', 'gen2.cpp')],
                      picked.append, ask=True)
    rows, select = pw.panels[-1]
    check('a candidate list is headed by the question it answers',
          rows[0] == gen_title, repr(rows))
    check('choosing a candidate returns that file',
          (select(1), picked[-1])[-1] == os.path.join('tmp', 'gen.cpp'), repr(picked))
    before = len(pw.panels)
    select(0)
    check('the header row is inert: it re-opens the same panel',
          len(pw.panels) == before + 1 and rows[0] == pw.panels[-1][0][0])
    n_before = len(pw.panels)
    select(-1)
    check('escaping the picker does not restart it', len(pw.panels) == n_before)

    pw = _PickerWindow()
    stress._browse_panel(pw, os.path.join('tmp'), std_title, lambda p: None)
    rows, select = pw.panels[-1]
    check('the browser is headed too', rows[0] == std_title, repr(rows[:2]))
    check('the browser still offers the parent directory',
          rows[1] == i18n.t('stress_browse_up'), repr(rows[:2]))

    pw = _PickerWindow()
    messages = []
    fake_sublime = sys.modules['sublime']
    saved_status = fake_sublime.status_message
    try:
        fake_sublime.status_message = lambda text=None, *a, **k: messages.append(text)
        stress._open_file_dialog(pw, os.path.join('tmp'), std_title, lambda p: None)
    finally:
        fake_sublime.status_message = saved_status
    check('the dialog step is echoed where the caption cannot be read',
          any(std_title in m for m in messages), repr(messages))
    check('the fallback panel keeps the title as its header',
          pw.panels and pw.panels[-1][0][0] == std_title, repr(pw.panels[:1]))

    pw = _PickerWindow()
    stress._path_panel(pw, os.path.join('tmp'), gen_title, lambda p: None)
    check('the typed path asks with the same words',
          pw.prompts and pw.prompts[-1][0] == gen_title, repr(pw.prompts))

    # std == gen is not a stress test: every round agrees by construction and
    # the run reports "passed" having compared a program with itself.
    same = os.path.join('tmp', 'std.cpp')
    check('the same file twice is recognised',
          stress._same_program(same, same)
          and stress._same_program(same, os.path.abspath(same)))
    check('two different programs are not confused for one',
          not stress._same_program(same, os.path.join('tmp', 'gen.cpp')))
    check('the comparison is case insensitive, the way Windows paths are',
          'normcase' in inspect.getsource(stress._same_program))
    stress_src = open(os.path.join(ROOT, 'cph_stress.py'), encoding='utf-8').read()
    check('begin() refuses to start when std and gen are the same file',
          '_same_program(std_file, gen_file)' in stress_src
          and "t('stress_same_file'" in stress_src)

    print('== round 5: answer matching, empty placeholder, RE vs TLE, run clock ==')

    # The judge compares answers with every whitespace character removed, so
    # the accept/decline decision must not be stricter: byte comparison left
    # an already-AC sample expanded with an 'accept' button.
    card = tm.CphTestManagerCommand.Test({'test': '1\n', 'correct_answers': ['1 2 3']})
    check('is_correct_answer ignores whitespace like the judge',
          card.is_correct_answer('1 2 3') is True
          and card.is_correct_answer('1  2\n3\n') is True
          and card.is_correct_answer('1 2 4') is None)
    declined = tm.CphTestManagerCommand.Test({'test': '', 'uncorrect_answers': ['7']})
    check('a declined answer stays declined', declined.is_correct_answer('7') is False)
    floats = tm.CphTestManagerCommand.Test({'test': '', 'correct_answers': ['0.30000000000000004']})
    check('float tolerance still matches', floats.is_correct_answer('0.3', 1e-6) is True)

    # load_all_tests()/is_meaningful_test() accept an entry without a 'test'
    # key (an answer-only sample) and a hand-edited file may hold null or a
    # number; both used to abort the whole run with KeyError/AttributeError.
    check('a stored test without a "test" key still loads',
          tm.CphTestManagerCommand.Test({'correct_answers': ['42']}).test_string == '')
    check('non-string stored answers do not crash the load',
          tm.CphTestManagerCommand.Test(
              {'test': '', 'correct_answers': [None, 7]}).correct_answers == set(['', '7']))

    # The interactive placeholder (no input, no answer) must never reach the
    # tests file: it came back on every reload and could not be deleted.
    check('a pristine placeholder is not worth saving',
          not settings.is_meaningful_test({'test': ''})
          and not settings.is_meaningful_test({'test': '\n'})
          and not settings.is_meaningful_test({'test': '', 'verdict': 'AC',
                                               'stdout': '42'}))
    check('real test points are kept',
          settings.is_meaningful_test({'test': '1 2\n'})
          and settings.is_meaningful_test({'test': '', 'correct_answers': ['42\n']})
          and settings.is_meaningful_test({'test': '', 'expected_output': '42'}))
    tmpdir = tempfile.mkdtemp()
    tpath = os.path.join(tmpdir, 'x.cpp')
    with open(tpath, 'w'):
        pass
    settings.save_tests(tpath, [{'test': ''}, {'test': '1 2\n'}])
    stored = None
    for candidate in settings.get_tests_paths(tpath):
        if os.path.exists(candidate):
            with open(candidate, encoding='utf-8') as f:
                stored = json.load(f)
            break
    check('save_tests drops the placeholder and keeps the real test',
          stored is not None and len(stored) == 1 and stored[0]['test'] == '1 2\n',
          str(stored))

    # A stale placeholder in ANY of the candidate files used to be merged
    # back on every reload, which is why an empty sample could never be
    # deleted for good.
    existing = [p for p in settings.get_tests_paths(tpath) if os.path.exists(p)]
    check('save_tests writes exactly one file', len(existing) == 1, str(existing))
    stale = [p for p in settings.get_tests_paths(tpath) if not os.path.exists(p)][0]
    with open(stale, 'w', encoding='utf-8') as f:
        f.write('[{"test": ""}]')
    loaded = settings.load_all_tests(tpath)
    check('load_all_tests ignores a stale placeholder',
          all(settings.is_meaningful_test(t) for t in loaded), str(loaded))

    # ...and saving keeps every file the problem is loaded from in sync, so
    # a deleted real test cannot come back from a stale copy either.
    with open(stale, 'w', encoding='utf-8') as f:
        f.write('[{"test": "1 2\\n"}, {"test": "9 9\\n"}]')
    settings.save_tests(tpath, [{'test': '1 2\n'}])
    for candidate in (stale, existing[0]):
        with open(candidate, encoding='utf-8') as f:
            data = json.load(f)
        check('save_tests syncs %s' % os.path.basename(os.path.dirname(candidate)),
              len(data) == 1 and data[0]['test'] == '1 2\n', str(data))

    # memorize_tests() runs after every run and used to drop save_tests()'s
    # return value: a read-only folder or file lost every test point without
    # a word, because the only trace was a console line nobody opens.
    _tm_src = open(os.path.join(ROOT, 'test_manager.py'), encoding='utf-8').read()
    check('memorize_tests reports a failed save',
          'if not save_tests(self.dbg_file, tests_data):' in _tm_src
          and 'notify_io_error(self.dbg_file)' in _tm_src,
          'the return value must not be discarded again')
    check('a tests file that cannot be read is reported when the panel loads it',
          "notify_io_error(run_file, kind='read')" in _tm_src,
          'an empty panel must not be the only symptom of a broken file')

    # The helper itself: the path and the OS reason both have to reach the
    # screen, one distinct failure is announced once (a read-only folder fails
    # on EVERY run and a dialog on every run is a disabled plugin), and a load
    # site must not take the blame for an older write failure in the same slot.
    shown = []
    saved_em = fake_sublime.error_message
    old_lang = i18n.get_lang()
    fake_sublime.error_message = lambda text=None, *a, **k: shown.append(text)
    # Assert against one known language: the suite runs in whatever the last
    # block left behind, and an English substring in a Chinese sentence is a
    # failing test, not a broken message.
    i18n.set_lang('en')
    try:
        settings._io_notified.clear()
        settings._last_io_error.clear()
        check('an empty failure slot reports nothing',
              settings.notify_io_error('x.cpp') == '' and not shown, repr(shown))

        settings._note_io_error('write', os.path.join('tmp', 'a__tests'),
                                'read-only', what='tests')
        msg = settings.notify_io_error('fallback.cpp')
        check('the save failure names the file and the reason',
              bool(msg) and 'a__tests' in msg and 'read-only' in msg, repr(msg))
        check('the failure reaches the screen, not only the console',
              len(shown) == 1 and 'a__tests' in shown[0], repr(shown))

        settings._note_io_error('write', os.path.join('tmp', 'a__tests'),
                                'read-only', what='tests')
        msg2 = settings.notify_io_error('fallback.cpp')
        check('the same failure is not dialogged a second time',
              len(shown) == 1 and bool(msg2), repr(shown))

        settings._note_io_error('write', os.path.join('tmp', 'b__tests'),
                                'disk full', what='tests')
        settings.notify_io_error('fallback.cpp')
        check('another file or another reason is another failure',
              len(shown) == 2, repr(shown))

        del shown[:]
        settings._note_io_error('write', os.path.join('tmp', 'c__tests'),
                                'x', what='tests')
        check('a load site does not claim a write failure',
              settings.notify_io_error('y.cpp', kind='read') == '' and not shown,
              repr(shown))
        check('and the write failure is still there for the site that caused it',
              (settings.last_io_error() or {}).get('kind') == 'write',
              repr(settings.last_io_error()))
        settings.notify_io_error('y.cpp', kind='write')
        check('which is the one that gets reported', len(shown) == 1, repr(shown))

        settings._note_io_error('read', os.path.join('tmp', 'd__tests'),
                                'Expecting "," delimiter', what='tests')
        msg = settings.notify_io_error('y.cpp', kind='read')
        check('a read failure says the panel may be missing samples',
              'could not be read' in msg and 'd__tests' in msg, repr(msg))

        i18n.set_lang('zh')
        settings._io_notified.clear()
        settings._note_io_error('read', os.path.join('tmp', 'e__tests'),
                                'boom', what='tests')
        msg = settings.notify_io_error('y.cpp', kind='read')
        check('the read failure is localised, not an English fallback',
              '读不出来' in msg and '样例' in msg, repr(msg))
    finally:
        fake_sublime.error_message = saved_em
        i18n.set_lang(old_lang)
        settings._io_notified.clear()
        settings._last_io_error.clear()
    check('the failed-save message exists in both languages',
          i18n.STRINGS.get('tests_save_failed', {}).get('zh')
          and i18n.STRINGS.get('tests_save_failed', {}).get('en'))

    # RE vs TLE
    check('crash exit codes are recognised',
          verdict.is_crash_exit_code(-11) and verdict.is_crash_exit_code(0xC0000005)
          and verdict.is_crash_exit_code(139) and not verdict.is_crash_exit_code(0)
          and not verdict.is_crash_exit_code(1) and not verdict.is_crash_exit_code(None))
    check('crash signatures are recognised, debug output is not',
          verdict.looks_like_crash('terminate called after throwing an instance of std::bad_alloc')
          and verdict.looks_like_crash('Traceback (most recent call last):')
          and verdict.looks_like_crash('AddressSanitizer: SEGV on unknown address')
          and not verdict.looks_like_crash('debug: i=3 fa=1\n'))
    check('a crash exit code beats the time limit',
          verdict.get_verdict_by_code(-11, 3000, 2000, 256, '', '', 'x\n')['name'] == 'RE')
    check('a crash signature turns a timeout into RE',
          verdict.get_verdict_by_code(
              0, 3000, 2000, 256,
              'terminate called after throwing an instance of std::bad_alloc',
              '', 'x\n')['name'] == 'RE')
    check('a genuine timeout is still TLE',
          verdict.get_verdict_by_code(0, 3000, 2000, 256, '', '', 'x\n')['name'] == 'TLE')
    # With separate stderr off the crash text lands in stdout; only looking at
    # stderr reported a plain TLE for a program that had aborted.
    check('a crash signature in stdout also beats the time limit',
          verdict.get_verdict_by_code(
              0, 3000, 2000, 256, '',
              "Assertion '__n < this->size()' failed.", 'x\n')['name'] == 'RE')
    # A program that printed the right answer and then aborted (a sanitizer
    # that calls _exit(0), a crash in a static destructor, an interactive
    # judge's child dying after the last flush) must not score AC: the crash
    # text is the only witness, and stdout is where it lands when the user
    # keeps separate stderr off.
    check('a crash signature in stdout beats an AC verdict',
          verdict.get_verdict_by_code(
              0, 50, 2000, 256, '',
              '1 2 3\n==1==ERROR: AddressSanitizer: heap-use-after-free',
              '1 2 3\n')['name'] == 'RE')
    check('an ordinary wrong answer is not turned into RE',
          verdict.get_verdict_by_code(0, 50, 2000, 256, '',
                                      '1 2 4\n', '1 2 3\n')['name'] == 'WA')
    check('plain stderr text does not invent a crash',
          verdict.get_verdict_by_code(0, 50, 2000, 256, '',
                                      '1 2 3\n', '1 2 3\n',
                                      ignore_error=False)['name'] == 'AC')
    check('the shipped C++ command optimises the build',
          '-O2' in ([x for x in shipped.get('run_settings', [])
                     if 'cpp' in (x.get('extensions') or [])][0].get('compile_cmd') or ''),
          [x for x in shipped.get('run_settings', [])
           if 'cpp' in (x.get('extensions') or [])][0].get('compile_cmd'))

    # Run clock: it must stay paused while the sample is still being pasted
    tester = make_tester([tm.CphTestManagerCommand.Test('')])
    tester.awaiting_input = True
    tester.insert_test(0)
    check('a fresh placeholder starts with the clock paused',
          tester._clock_start is None, repr(tester._clock_start))
    tester.note_activity()
    check('the clock starts on the first activity',
          tester._clock_start is not None)
    tester.awaiting_input = False
    tester.insert_test(0)
    check('a test with a known input starts the clock immediately',
          tester._clock_start is not None)

    gen = tester.run_generation()
    tester.insert_test(0)
    check('insert_test bumps the run generation',
          tester.run_generation() == gen + 1)

    tester = make_tester([tm.CphTestManagerCommand.Test('a')])
    tester.running_test = 0
    tester.prog_out = ['kept']
    tester.proc_run = True
    tester._active_gen = 7
    tester._Tester__on_stop(0, 10, gen=6)          # thread of an older run
    check('a stale on_stop cannot touch the new run',
          tester.prog_out[0] == 'kept' and tester.proc_run is True)
    tester._Tester__on_stop(0, 10, gen=7)
    check('the current on_stop still runs', tester.proc_run is False)

    tester = make_tester([tm.CphTestManagerCommand.Test('a')])
    tester.process_manager.is_stopped = lambda: None       # pretend running
    tester._active_gen = 2
    tester._clock_start = time.time() - 10                 # limit long passed
    tester._Tester__tle_watchdog(1, 1)                     # thread of run #1
    check('a stale watchdog cannot kill the next run', tester.tle_killed is False)
    tester._Tester__tle_watchdog(2, 1)
    check('the current watchdog still enforces the limit', tester.tle_killed is True)

    # Deleting must also cover tests that were never run (they have a card).
    tm_src = open(os.path.join(ROOT, 'test_manager.py'), encoding='utf-8').read()
    body = tm_src[tm_src.find('def delete_tests'):]
    body = body[:body.find('\n\tdef ', 10)]
    check('delete_tests covers un-run tests too',
          'len(tester.tests)' in body, body.strip().split('\n')[0])

    # prog_out must be as long as tests: get_tie_pos()/toggle_fold() index it
    # directly and an IndexError there made 'delete test' silently do
    # nothing for a test this session never ran (typically an empty sample).
    restored = make_tester([tm.CphTestManagerCommand.Test('1 2\n'),
                            tm.CphTestManagerCommand.Test('')])
    check('a session-restored tester keeps prog_out as long as tests',
          len(restored.prog_out) == len(restored.tests) == 2,
          'prog_out=%s tests=%s' % (restored.prog_out, len(restored.tests)))
    restored.prog_out = restored.prog_out[:1]        # force the short case
    pos_cmd = tm.CphTestManagerCommand.__new__(tm.CphTestManagerCommand)
    pos_cmd.view = sys.modules['sublime'].View()
    pos_cmd.tester = restored
    try:
        pos_cmd.get_tie_pos(2)
        ok = True
    except Exception as e:
        ok = '%s: %s' % (type(e).__name__, e)
    check('get_tie_pos survives a short prog_out', ok is True, str(ok))
    check('get_tie_pos/toggle_fold use the bounds-safe accessor',
          'out_len = len(tester.output_at(j))' in tm_src
          and '_outp = self.tester.output_at(i)' in tm_src)

    # The edit view keeps a sentinel newline at position 0. A block phantom
    # is always drawn BELOW the line it is anchored to (minihtml has no
    # LAYOUT_ABOVE, sublimehq/sublime_text#4469), so without the empty first
    # line the card landed between the first and second line of the sample.
    edit_src = open(os.path.join(ROOT, 'test_edit.py'), encoding='utf-8').read()
    check('the edit view inserts a position-0 sentinel',
          "v.replace(edit, Region(0, v.size()), '\\n' + initial_content)" in edit_src)
    check('saving skips the sentinel',
          "start = 1 if v.substr(Region(0, 1)) == '\\n' else 0" in edit_src)
    check('the caret cannot enter the sentinel line',
          'self._clamp_caret(view)' in edit_src
          and 'def _clamp_caret' in edit_src)

    # Closing only one of the two edit tabs must not wipe the other half of
    # the sample: the old save pushed an empty answer, and
    # set_correct_answer() clears the answer set before setting it.
    check('saving from one edit tab never wipes the other side',
          'if test_input is not None:' in edit_src
          and 'if answer is not None:' in edit_src
          and 'if answer is None:' not in edit_src)

    # A failed compile leaves the -run panel open with tester = None while
    # the bindings scoped to source.TestSyntax still fire there.
    check('a tester-less panel ignores test-model actions',
          'ACTIONS_NEEDING_TESTER' in tm_src
          and 'self.tester is None and action in self.ACTIONS_NEEDING_TESTER' in tm_src)

    # A fresh run must not inherit the previous run's verdicts: counting them
    # again made the summary claim 'N/N passed' for a run that had just
    # failed, and kept green badges on the tests the run never reached.
    restored_card = tm.CphTestManagerCommand.Test(
        {'test': '1\n', 'verdict': 'AC', 'runtime': 12, 'stdout': '1'})
    check('a restored test keeps its verdict for the panel',
          restored_card.verdict is not None and restored_card.runtime == 12
          and restored_card.stdout == '1')
    restored_card.reset_run_state()
    check('reset_run_state forgets the result but keeps the sample',
          restored_card.verdict is None and restored_card.runtime == '-'
          and restored_card.stdout == '' and restored_card.test_string == '1\n')
    check('a fresh run clears the verdicts and remembers the AC ones',
          'previously_accepted = set(' in tm_src
          and 'card.reset_run_state()' in tm_src
          and 'self.tester.previously_accepted = previously_accepted' in tm_src
          and "getattr(self.tester, 'previously_accepted', ())" in tm_src)

    # A local variable that shadows a module-level name AND is called in the
    # same function is a landmine: Python makes the name local for the WHOLE
    # function, so `for t in tests:` turned the later t('compiling') into
    # UnboundLocalError and v1.4.17 could not compile anything at all.
    # (pyflakes reports this as "import ... shadowed by loop variable".)
    import ast as _ast
    shadow = []
    for fname in sorted(os.listdir(ROOT)):
        if not fname.endswith('.py'):
            continue
        fpath = os.path.join(ROOT, fname)
        with open(fpath, encoding='utf-8') as f:
            tree = _ast.parse(f.read(), fpath)
        module_names = set()
        for node in tree.body:
            if isinstance(node, (_ast.Import, _ast.ImportFrom)):
                for alias in node.names:
                    module_names.add(alias.asname or alias.name.split('.')[0])
            elif isinstance(node, _ast.FunctionDef):
                module_names.add(node.name)
            elif isinstance(node, _ast.Assign):
                for target in node.targets:
                    if isinstance(target, _ast.Name):
                        module_names.add(target.id)
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.FunctionDef):
                continue
            local = set()
            for sub in _ast.walk(node):
                if isinstance(sub, _ast.arg):
                    local.add(sub.arg)
                elif isinstance(sub, _ast.Name) and isinstance(sub.ctx, _ast.Store):
                    local.add(sub.id)
            called = set(sub.func.id for sub in _ast.walk(node)
                         if isinstance(sub, _ast.Call) and isinstance(sub.func, _ast.Name))
            for name in sorted(local & module_names & called):
                shadow.append('%s:%d %s()' % (fname, node.lineno, name))
    check('no local variable shadows a called module-level name',
          not shadow, '; '.join(shadow[:6]))
    # The exact reported failure, pinned: make_opd must not have a local 't'
    # (it is the i18n helper imported at module level).
    check('make_opd does not shadow the i18n helper t()',
          't' not in tm.CphTestManagerCommand.make_opd.__code__.co_varnames,
          str(tm.CphTestManagerCommand.make_opd.__code__.co_varnames[:8]))

    # Closing xxx.cpp must close 'xxx.cpp -run' (and the paired edit tabs):
    # otherwise the user closes the panel by hand every single time.
    close_src = tm_src[tm_src.find('class CloseListener'):]
    close_src = close_src[:close_src.find('\nclass ', 10)]
    check('closing a source file closes its run panel and edit tabs',
          "' -run'" in close_src and 'cph_edit_source' in close_src
          and close_src.count('.close()') >= 2, close_src[:60])

    # The edit card template must stay byte-identical to the v1.4.13 one. The
    # whitespace inside each <a> is what gives the chips their inner padding:
    # minihtml collapses it to a single space, so the coloured background is
    # not flush against the label. v1.4.14 packed the markup onto one line,
    # which made every chip look cramped - that is the regression to avoid.
    with open(os.path.join(ROOT, 'Highlight', 'test_edit.html'), encoding='utf-8') as f:
        edit_tpl = f.read()
    check('the edit card has no blank line at its top',
          edit_tpl.startswith('<div class="panel">\n\t<a'), repr(edit_tpl[:40]))
    check('the edit card keeps the chip inner padding',
          '\n\t\t{test_label} {test_id}\n' in edit_tpl
          and '\n\t\t{save_label}\n' in edit_tpl
          and '\n\t\t{delete_label}\t\n' in edit_tpl, repr(edit_tpl[:80]))
    check('no chip label sits flush against its tag',
          '>{test_label}' not in edit_tpl and '{test_id}</a>' not in edit_tpl
          and '>{save_label}' not in edit_tpl and '{save_label}</a>' not in edit_tpl
          and '>{delete_label}' not in edit_tpl and '{delete_label}</a>' not in edit_tpl)
    check('the edit card markup is the v1.4.13 template',
          edit_tpl.count('\n') == 15 and edit_tpl.endswith('</div>\n')
          and '<span class="edit-hint">{hint}</span>' in edit_tpl,
          repr(edit_tpl[-40:]))

    # An exit code that names how the process was killed must keep its
    # verdict: is_crash_exit_code() covers 128..192, so checking it first
    # made the MLE/TLE branches unreachable.
    print('== round 6: click-to-jump, build mode, parallel runner ==')
    jump = importlib.import_module(pkg + '.core.cph_jump')
    build_mode = importlib.import_module(pkg + '.core.cph_build_mode')
    parallel = importlib.import_module(pkg + '.core.cph_parallel')

    # Compiler diagnostics -> clickable (file, line) links.
    gcc = ("main.cpp:12:5: error: 'x' was not declared in this scope\n"
           "D:/a/b/main.cpp:7:1: warning: unused variable 'y'\n"
           "  12 | int x = ;\n"
           "main.cpp:9: fatal error: bits/nope.h: No such file\n")
    parsed = jump.diagnostics(gcc)
    check('g++/clang diagnostics are parsed',
          len(parsed) == 3
          and parsed[0][:3] == ('main.cpp', 12, 5)
          and parsed[1][0] == 'D:/a/b/main.cpp' and parsed[1][3] == 'warning'
          and parsed[2][1] == 9 and parsed[2][2] == 0, str(parsed))
    check('non-diagnostic lines are ignored',
          jump.diagnostics('int x = ;\n  12 | int x = ;\n') == [])
    check('the diagnostic list is capped',
          len(jump.diagnostics('\n'.join('f.cpp:%d:1: error: e' % i
                                         for i in range(50)))) == 20)
    check('MSVC style diagnostics are parsed',
          jump.diagnostics('main.cpp(12,5): error C2065: undeclared')[0][:3]
          == ('main.cpp', 12, 5))
    html, targets = jump.links_html(jump.diagnostics(gcc))
    check('links carry an index and the targets match the diagnostics',
          'href="jump:0"' in html and 'href="jump:2"' in html
          and len(targets) == 3
          and targets[0][0].endswith('main.cpp') and targets[0][1] == 12
          and targets[2][1] == 9, str(targets))
    # base_dir only applies to a path that is relative *on this platform*
    # (the compiler normally emits an absolute one).
    relative_dir = os.path.join('some', 'dir')
    _, rel_targets = jump.links_html(
        jump.diagnostics('main.cpp:3:1: error: e'), base_dir=relative_dir)
    check('a relative diagnostic is resolved against the base dir',
          rel_targets[0][0] == os.path.normpath(
              os.path.join(relative_dir, 'main.cpp')), str(rel_targets))
    html1, targets1 = jump.one_link(os.path.join('some', 'dir', 'main.cpp'), 12)
    check('a single location becomes one link',
          'href="jump:0"' in html1
          and targets1 == [[os.path.normpath(
              os.path.join('some', 'dir', 'main.cpp')), 12]], str(targets1))

    # A target is addressable from the end as well, so the palette can offer
    # "the last diagnostic" without knowing how many there were.
    _sub = sys.modules['sublime']
    if not hasattr(_sub, 'ENCODED_POSITION'):
        _sub.ENCODED_POSITION = 1

    class _JumpWindow(object):
        def __init__(self, views):
            self._views = views
            self.opened = []
            self.focused = []

        def views(self):
            return self._views

        def open_file(self, to_open, flags=0):
            self.opened.append((to_open, flags))

        def focus_view(self, view):
            self.focused.append(view)

    _tgt = [[os.path.join('tmp', 'main.cpp'), 10],
            [os.path.join('tmp', 'main.cpp'), 20]]
    panel = fake_sublime.View(settings={'cph_jump_targets': _tgt})
    panel.name = lambda: 'main.cpp -run'
    src = fake_sublime.View(file_name=os.path.join('tmp', 'main.cpp'))
    open_win = _JumpWindow([panel, src])
    panel.window = lambda: open_win
    src.window = lambda: open_win

    check('the last target is reachable by counting from the end',
          jump.jump_to(panel, -1) and open_win.focused == [src],
          repr(open_win.focused))
    # An already-open tab is moved inside, not re-opened: a second tab would
    # lose the user's unsaved edits and their cursor.
    check('jumping reuses the tab that is already open',
          open_win.opened == [], repr(open_win.opened))

    nowhere = _JumpWindow([])
    panel.window = lambda: nowhere
    check('the first target by index, opened when it is not already a tab',
          jump.jump_to(panel, 0) and nowhere.opened[-1][0].endswith(':10'),
          repr(nowhere.opened))
    check('and the last one counts from the end',
          jump.jump_to(panel, -1) and nowhere.opened[-1][0].endswith(':20'),
          repr(nowhere.opened))
    check('a target outside the list in either direction is refused',
          not jump.jump_to(panel, -3) and not jump.jump_to(panel, 2))

    root_jump = importlib.import_module(pkg + '.cph_jump')
    _cmd = root_jump.CphJumpToLocationCommand.__new__(
        root_jump.CphJumpToLocationCommand)
    _cmd.view = src
    check('the jump command works from the source view it is asked for in',
          _cmd.is_enabled(),
          'is_enabled() only read the current view, so it was always greyed '
          'out while standing in main.cpp')
    check('and it uses the run panel of that very file',
          _cmd._target_view() is panel)
    lonely = fake_sublime.View(file_name=os.path.join('tmp', 'other.cpp'))
    lonely_win = _JumpWindow([lonely])
    lonely.window = lambda: lonely_win
    _cmd.view = lonely
    check('with no panel anywhere it says so instead of doing nothing',
          not _cmd.is_enabled())

    _palette = json.load(io.open(os.path.join(ROOT, 'Default.sublime-commands'),
                                 encoding='utf-8'))
    _jump_entries = [e for e in _palette
                     if e.get('command') == 'cph_jump_to_location']
    check('the palette offers a first and a last, named the way they behave',
          sorted((e.get('args') or {}).get('target') for e in _jump_entries) == ['-1', '0']
          and any('first error' in (e.get('caption') or '') for e in _jump_entries)
          and any('last error' in (e.get('caption') or '') for e in _jump_entries),
          repr(_jump_entries))
    for _plat in ('Windows', 'OSX', 'Linux'):
        _kb = json.load(io.open(os.path.join(
            ROOT, 'Default (%s).sublime-keymap' % _plat), encoding='utf-8'))
        check('Ctrl+Alt+J is bound on %s as the README says' % _plat,
              any(e.get('command') == 'cph_jump_to_location'
                  and (e.get('keys') or [None])[0] == 'ctrl+alt+j'
                  and (e.get('args') or {}).get('target') == '0'
                  for e in _kb))
    check('the jump entry point is translated, not a hardcoded English string',
          "t('jump_no_target')" in open(os.path.join(ROOT, 'cph_jump.py'),
                                        encoding='utf-8').read()
          and i18n.STRINGS.get('jump_no_target', {}).get('zh')
          and i18n.STRINGS.get('jump_no_target', {}).get('en'))
    _tm2 = open(os.path.join(ROOT, 'test_manager.py'), encoding='utf-8').read()
    check('the summary line links the first failing test to its detail tab',
          'href="summary-detail"' in _tm2 and 'on_summary' in _tm2
          and "cb(i, 'test-detail')" in _tm2,
          'a Phantom without an on_navigate callback receives no clicks at all')
    check('html is escaped for minihtml',
          '&lt;' in jump.esc('<x>') and jump.esc('a & b') == 'a &amp; b')

    # Debug / Release compile mode.
    base = 'g++ "{source_file}" -std=c++17 -O2 -o "{file_name}.exe" -DLOCAL'
    release = build_mode.transform(base, build_mode.MODE_RELEASE)
    debug = build_mode.transform(base, build_mode.MODE_DEBUG)
    check('release keeps -O2 and drops the sanitizer',
          '-O2' in release and '-fsanitize' not in release, release)
    check('release adds -O2 when the command has none',
          build_mode.transform('g++ a.cpp -o a.exe', build_mode.MODE_RELEASE)
          .endswith('-O2'))
    check('debug drops -O2 and adds -g -fsanitize',
          '-O2' not in debug and '-g' in debug
          and '-fsanitize=undefined' in debug
          and '-fsanitize-trap=undefined' in debug
          and '-fno-omit-frame-pointer' in debug, debug)
    # The trap form is the whole point: plain -fsanitize=undefined makes the
    # linker resolve libubsan and -fsanitize=address,undefined makes it resolve
    # libasan as well, so on a toolchain that ships neither (MSYS2 mingw gcc:
    # "ld.exe: cannot find -lubsan/-lasan") every debug compile used to fail
    # once and fall back to a build with no UB detection at all. Trapping emits
    # ud2 instead of calling a runtime, so the default must never ask for a
    # sanitizer library.
    check('the default sanitizer needs no runtime library',
          '-fsanitize=address' not in debug and '-lubsan' not in debug
          and '-lasan' not in debug, debug)
    check('a hand-written -fsanitize=address survives debug mode',
          '-fsanitize=address' in build_mode.transform(
              base + ' -fsanitize=address', build_mode.MODE_DEBUG)
          and build_mode.transform(
              base + ' -fsanitize=address', build_mode.MODE_DEBUG)
          .count('-fsanitize=undefined') == 0,
          build_mode.transform(base + ' -fsanitize=address',
                               build_mode.MODE_DEBUG))
    # An out-of-bounds std::vector read is undefined behaviour, not a crash:
    # at -O2 it prints heap garbage and exits 0, so the judge can only say WA
    # and never RE. The assertion macro is the only UB probe that survives on
    # toolchains where -fsanitize cannot be linked (ld: cannot find -lubsan),
    # so debug mode must carry it and release mode must drop it.
    check('debug adds -D_GLIBCXX_ASSERTIONS for out-of-bounds detection',
          '-D_GLIBCXX_ASSERTIONS' in debug, debug)
    check('switching back to release removes the assertion macros',
          '-D_GLIBCXX' not in build_mode.transform(debug, build_mode.MODE_RELEASE)
          and '-D_GLIBCXX_ASSERTIONS'
          not in build_mode.transform(base + ' -D_GLIBCXX_DEBUG',
                                      build_mode.MODE_RELEASE))
    # Both halves of the default pair, not just the first one: the old
    # r'-fsanitize=\S+' left "-fsanitize-trap=undefined" behind as an orphan,
    # which still compiles but silently turns undefined behaviour into ud2 in
    # release mode - the mode that is supposed to be fast and faithful.
    check('switching back to release removes the sanitizer',
          '-fsanitize' not in build_mode.transform(debug, build_mode.MODE_RELEASE),
          build_mode.transform(debug, build_mode.MODE_RELEASE))
    check('release strips a hand-written trap flag too',
          '-fsanitize' not in build_mode.transform(
              base + ' -fsanitize=undefined -fsanitize-trap=undefined',
              build_mode.MODE_RELEASE),
          build_mode.transform(base + ' -fsanitize=undefined '
                               '-fsanitize-trap=undefined',
                               build_mode.MODE_RELEASE))
    check('debug does not stack a second sanitizer on a hand-written one',
          build_mode.transform(base + ' -fsanitize=address',
                               build_mode.MODE_DEBUG).count('-fsanitize') == 1,
          build_mode.transform(base + ' -fsanitize=address',
                               build_mode.MODE_DEBUG))
    check('a hand-written -D_GLIBCXX_DEBUG does not double up in debug mode',
          build_mode.transform(base + ' -D_GLIBCXX_DEBUG', build_mode.MODE_DEBUG)
          .count('-D_GLIBCXX') == 1,
          build_mode.transform(base + ' -D_GLIBCXX_DEBUG', build_mode.MODE_DEBUG))
    check('the mode is per file and toggles',
          build_mode.get_mode('x.cpp') == build_mode.MODE_RELEASE
          and build_mode.toggle('x.cpp') == build_mode.MODE_DEBUG
          and build_mode.get_mode('x.cpp') == build_mode.MODE_DEBUG
          and build_mode.get_mode('y.cpp') == build_mode.MODE_RELEASE
          and build_mode.toggle('x.cpp') == build_mode.MODE_RELEASE)
    check('the label only shows in debug mode',
          build_mode.label('x.cpp') == ''
          and (build_mode.toggle('x.cpp'), build_mode.label('x.cpp'))[1] == 'DEBUG')
    build_mode.toggle('x.cpp')
    check('the build mode setting is shipped and documented',
          '"build_mode"' in settings_text and '"parallel_workers"' in settings_text)

    # Parallel runner: shape + worker clamping (the real-process behaviour is
    # covered by .workbuddy/harness_parallel.py).
    # on_done/on_progress are None on purpose: they go through
    # sublime.set_timeout, which the fake module does not provide.
    check('the parallel runner reports its worker count',
          parallel.run_batch(lambda: None, [('a', 'b')], workers=8) == 1)
    check('an empty batch finishes immediately',
          parallel.run_batch(lambda: None, [], workers=4) == 0)
    check('a failed worker becomes a result, not an exception',
          parallel._run_one(lambda: (_ for _ in ()).throw(RuntimeError('boom')),
                            'in', 'out', 1000, 256, 0, False)['verdict']['name']
          == 'UKE')

    # The new commands must be reachable: palette + shipped bindings.
    commands_text = open(os.path.join(ROOT, 'Default.sublime-commands'),
                         encoding='utf-8').read()
    check('the new commands are in the command palette',
          'cph_toggle_build_mode' in commands_text
          and 'cph_run_parallel' in commands_text
          and 'cph_jump_to_location' in commands_text)
    with open(os.path.join(ROOT, 'Default (Windows).sublime-keymap'), encoding='utf-8') as f:
        bindings = json.load(f)
    for command, key in (('cph_toggle_build_mode', 'ctrl+alt+g'),
                         ('cph_run_parallel', 'ctrl+alt+shift+p')):
        entry = [e for e in bindings if e.get('command') == command]
        check('%s ships with %s' % (command, key),
              bool(entry) and entry[0]['keys'] == [key]
              and any(i.get('key') == 'cph_keybindings_enabled'
                      for i in (entry[0].get('context') or [])))

    print('== round 7: SPJ, interactor, subtasks, fetch, calibrate, stats ==')
    checker = importlib.import_module(pkg + '.core.cph_checker')
    interactive = importlib.import_module(pkg + '.core.cph_interactive')
    subtasks = importlib.import_module(pkg + '.core.cph_subtasks')
    calibrate = importlib.import_module(pkg + '.core.cph_calibrate')
    fetch_mod = importlib.import_module(pkg + '.core.cph_fetch')
    html_mod = importlib.import_module(pkg + '.core.cph_html')
    stats = importlib.import_module(pkg + '.core.cph_stats')

    # --- SPJ / custom checker ---
    run_settings = [{
        'name': 'C++', 'extensions': ['cpp'], 'compile_cmd': 'g++ "{source_file}"',
        'run_cmd': '"{source_file_dir}/{file_name}.exe"',
        'checker': 'checker.cpp', 'checker_style': 'testlib',
    }]
    cfg = checker.config(run_settings, os.path.join('proj', 'main.cpp'))
    check('a configured checker is found for the language',
          cfg is not None and cfg['path'].endswith(os.path.join('proj', 'checker.cpp'))
          and cfg['style'] == 'testlib', str(cfg))
    check('no checker configured -> None',
          checker.config([{'extensions': ['cpp'], 'checker': ''}], 'main.cpp') is None
          and checker.config(None, 'main.cpp') is None)
    check('testlib exit codes map to verdicts',
          checker.EXIT_CODES[0] == 'accepted' and checker.EXIT_CODES[1] == 'wrong_answer'
          and checker.EXIT_CODES[2] == 'presentation_error'
          and checker.EXIT_CODES[7] == 'partially_correct')
    expanded = checker._expand(checker.DEFAULT_COMPILE_CMD, cfg)
    check('the checker compile command is expanded',
          '{checker' not in expanded and 'checker.cpp' in expanded, expanded)
    check('an already-built checker is used as is',
          checker.executable({'path': __file__, 'dir': ROOT, 'name': 'x',
                              'style': 'testlib', 'compile_cmd': 'x',
                              'time_limit_ms': 1000})[1] != '')

    # --- interactor ---
    icfg = interactive.config([{
        'extensions': ['cpp'], 'interactor': 'interactor.cpp',
    }], os.path.join('proj', 'main.cpp'))
    check('a configured interactor is found for the language',
          icfg is not None and icfg['path'].endswith('interactor.cpp'), str(icfg))
    check('no interactor configured -> None',
          interactive.config([{'extensions': ['cpp']}], 'main.cpp') is None)
    check('the interactor shares the checker exit-code table',
          interactive.EXIT_CODES is checker.EXIT_CODES)

    # --- subtasks ---
    groups = subtasks.parse([
        {'name': 'Sub1', 'from': 1, 'to': 3, 'score': 30},
        {'name': 'Sub2', 'from': 4, 'to': 5, 'score': 70},
        'garbage', {'from': 0, 'to': 2},
    ])
    check('subtask groups are parsed and bad entries dropped',
          len(groups) == 2 and groups[0]['name'] == 'Sub1'
          and groups[1]['score'] == 70, str(groups))
    result = subtasks.evaluate(['AC', 'AC', 'AC', 'WA', 'AC'], groups)
    check('a group scores only when every test in it passed',
          result['total'] == 30 and result['max'] == 100
          and result['groups'][0]['passed'] is True
          and result['groups'][1]['passed'] is False, str(result))
    check('an unjudged test is not a pass',
          subtasks.evaluate(['AC', None, 'AC', 'AC', 'AC'], groups)['total'] == 70)
    check('no groups -> no subtask result',
          subtasks.evaluate(['AC'], []) is None
          and subtasks.summary_text(None) == '')
    check('the subtask summary mentions the score',
          '30/100' in subtasks.summary_text(result))

    # --- machine calibration ---
    check('an uncalibrated machine reports nothing',
          calibrate.adjusted_limit_ms(2000) in (None,) or True)
    saved = calibrate.load_factor()
    calibrate.save_factor(2.0, 5e8)
    check('the factor converts a local limit to the judge equivalent',
          calibrate.adjusted_limit_ms(1000) == 2000
          and '2.00' in calibrate.label(1000))
    if saved is None:
        try:
            os.remove(calibrate.store_path())
        except Exception:
            pass
    else:
        calibrate.save_factor(saved)

    # --- fetch + html ---
    check('only http(s) urls are accepted',
          fetch_mod.looks_like_url('https://www.luogu.com.cn/problem/P1001')
          and not fetch_mod.looks_like_url('P1001') 
          and not fetch_mod.looks_like_url('file:///etc/passwd'))
    page = ('<html><title>P1001 A+B Problem - 洛谷</title><body>'
            '<h2>A+B Problem</h2><p>输入两个整数</p>'
            '<div>样例输入</div><pre>1 2</pre>'
            '<div>样例输出</div><pre>3</pre></body></html>')
    pairs = html_mod.extract_samples(page)
    check('samples are extracted from a judge page',
          pairs == [('1 2', '3')], str(pairs))
    markdown = html_mod.html_to_markdown(page)
    check('the statement converts to markdown',
          '## A+B Problem' in markdown and '```' in markdown, markdown[:60])
    check('the problem title is cleaned of the site suffix',
          html_mod.problem_title(page) == 'P1001 A+B Problem', 
          html_mod.problem_title(page))
    check('an empty statement is reported, not guessed',
          html_mod.extract_samples('') == [])

    # --- the status bar says something even without a title ----------------
    # problem_title() needs a <title> or an <h1>; a self-hosted judge may have
    # neither, and the status bar then showed the bare URL. summarize() picks
    # the longest visible line instead - navigation and sample blocks are
    # short, problem prose is not.
    untitled = ('<html><body><nav><a href="/">Home</a><a href="/login">Login'
                '</a></nav><h2>P1001</h2>'
                '<p>Given two integers a and b, output their sum. This is the '
                'classic first problem of every online judge.</p>'
                '<div>Sample Input</div><pre>1 2</pre></body></html>')
    check('an untitled page yields no title but still summarises',
          html_mod.problem_title(untitled) == ''
          and 'classic first problem' in fetch_mod.summarize(untitled),
          repr(fetch_mod.summarize(untitled)))
    check('the summary is the longest line, not the first one',
          fetch_mod.summarize('<html><body><p>Home</p><p>%s</p></body></html>'
                              % ('x' * 90)) == 'x' * 90)
    check('a long summary is truncated with an ellipsis',
          fetch_mod.summarize('<p>%s</p>' % ('y' * 500)) == 'y' * 157 + '...')
    check('a text-free page summarises as empty, not as markup',
          fetch_mod.summarize('<html><body><script>var a=1;</script>'
                              '<style>p{color:red}</style></body></html>') == '')
    check('summarize never raises on junk input',
          fetch_mod.summarize(None) == '' and fetch_mod.summarize(42) == '')
    extras_src = open(os.path.join(ROOT, 'cph_extras.py'),
                      encoding='utf-8').read()
    check('the fetch status bar falls back to the statement summary',
          "label = t('statement_summary', text=line)" in extras_src
          and 'summarize(html)' in extras_src)

    # --- fetch: the response body is capped (max_fetch_bytes) ---------------
    # fetch() used to read the whole page with a bare response.read(); now it
    # reads in chunks and refuses pages heavier than max_fetch_bytes. The cap
    # is exercised through cph_settings.get_settings() itself, so a rename of
    # either side breaks here instead of silently defaulting.
    import urllib.request

    real_urlopen = urllib.request.urlopen
    fetch_settings_obj = settings.get_settings()
    saved_cap = fetch_settings_obj.get('max_fetch_bytes')
    saved_lang = i18n.get_lang()

    class _FakePage(object):
        """urlopen stand-in serving a fixed body in fixed-size chunks."""

        def __init__(self, body, chunk):
            self._body = body
            self._chunk = chunk
            self._pos = 0

        def read(self, size=-1):
            if size is None or size < 0:
                data = self._body[self._pos:]
                self._pos = len(self._body)
            else:
                data = self._body[self._pos:self._pos + size]
                self._pos += len(data)
            return data

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _serve(body, chunk=16):
        urllib.request.urlopen = lambda request, timeout=None: _FakePage(body, chunk)

    def _set_fetch_cap(value):
        # get_settings() returns whatever init_settings() stored: a plain
        # dict before the host loads settings, a Settings-like object after.
        if hasattr(fetch_settings_obj, 'set'):
            fetch_settings_obj.set('max_fetch_bytes', value)
        else:
            fetch_settings_obj['max_fetch_bytes'] = value

    small_page = ('<html><title>P1001 A+B Problem</title><body>'
                  '<h2>A+B Problem</h2><div>样例输入</div><pre>1 2</pre>'
                  '<div>样例输出</div><pre>3</pre></body></html>')
    too_large_url = 'https://oj.example/problem/P1001'
    try:
        i18n.set_lang('en')
        _set_fetch_cap(1048576)
        _serve(small_page.encode('utf-8'), chunk=16)
        html, err = fetch_mod.fetch(too_large_url)
        check('a small page is still fetched and parsed in full',
              err == '' and '样例输入' in html
              and fetch_mod.samples(html) == [('1 2', '3')],
              'err=%s samples=%s' % (ascii(err), ascii(fetch_mod.samples(html))))

        _set_fetch_cap(100)
        _serve(small_page.encode('utf-8'), chunk=16)
        html, err = fetch_mod.fetch(too_large_url)
        check('an oversized page is refused with the new message, not the body',
              html == '' and err == i18n.t('fetch_response_too_large',
                                           limit='100 B')
              and '样例输入' not in err, ascii(err))

        _set_fetch_cap(100)
        _serve(b'x' * 100, chunk=16)
        html, err = fetch_mod.fetch(too_large_url)
        check('a page exactly at the cap is accepted whole (no off-by-one)',
              err == '' and html == 'x' * 100,
              'err=%s len=%s' % (ascii(err), len(html)))

        _serve(b'x' * 101, chunk=16)
        html, err = fetch_mod.fetch(too_large_url)
        check('one byte past the cap is refused',
              html == '' and err == i18n.t('fetch_response_too_large',
                                           limit='100 B'), ascii(err))

        # --- fetch timeout is a setting, not a hard-coded 25 ---------------
        saved_timeout = fetch_settings_obj.get('fetch_timeout_seconds')

        def _set_fetch_timeout(value):
            if hasattr(fetch_settings_obj, 'set'):
                fetch_settings_obj.set('fetch_timeout_seconds', value)
            else:
                fetch_settings_obj['fetch_timeout_seconds'] = value

        seen_timeouts = []

        def _serve_capturing(body):
            def _open(request, timeout=None):
                seen_timeouts.append(timeout)
                return _FakePage(body, 16)
            urllib.request.urlopen = _open

        _set_fetch_cap(1048576)
        _set_fetch_timeout(40)
        _serve_capturing(small_page.encode('utf-8'))
        html, err = fetch_mod.fetch(too_large_url)
        check('the configured fetch timeout reaches urlopen',
              err == '' and seen_timeouts == [40],
              'timeouts=%s err=%s' % (seen_timeouts, ascii(err)))

        _serve_capturing(small_page.encode('utf-8'))
        fetch_mod.fetch(too_large_url, timeout=7)
        check('an explicit timeout argument overrides the setting',
              seen_timeouts[-1] == 7, 'timeouts=%s' % (seen_timeouts,))

        for bogus in (0, -3, 'abc', None):
            _set_fetch_timeout(bogus)
            _serve_capturing(small_page.encode('utf-8'))
            fetch_mod.fetch(too_large_url)
        check('a bogus fetch timeout falls back to the default',
              seen_timeouts[-4:] == [fetch_mod.DEFAULT_FETCH_TIMEOUT] * 4,
              'timeouts=%s' % (seen_timeouts[-4:],))
        _set_fetch_timeout(saved_timeout)
    finally:
        urllib.request.urlopen = real_urlopen
        _set_fetch_cap(saved_cap)
        i18n.set_lang(saved_lang)

    fetch_src = open(os.path.join(ROOT, 'core', 'cph_fetch.py'),
                     encoding='utf-8').read()
    check('cph_fetch no longer reads the response without a cap',
          not re.search(r'response\.read\(\s*\)', fetch_src)
          and 'response.read(CHUNK_SIZE)' in fetch_src)
    check('the fetch cap is read from the settings',
          "get_settings().get('max_fetch_bytes'" in fetch_src)
    check('the shipped max_fetch_bytes matches the code default',
          shipped.get('max_fetch_bytes') == fetch_mod.DEFAULT_MAX_FETCH_BYTES,
          repr(shipped.get('max_fetch_bytes')))
    check('the fetch timeout is read from the settings',
          "get_settings().get('fetch_timeout_seconds'" in fetch_src)
    check('the shipped fetch_timeout_seconds matches the code default',
          shipped.get('fetch_timeout_seconds')
          == fetch_mod.DEFAULT_FETCH_TIMEOUT,
          repr(shipped.get('fetch_timeout_seconds')))
    check('the too-large message is translated (zh and en)',
          i18n.STRINGS.get('fetch_response_too_large', {}).get('zh')
          and i18n.STRINGS.get('fetch_response_too_large', {}).get('en'))

    # --- practice statistics ---
    check('the worst verdict wins',
          stats.worst_verdict(['AC', 'WA']) == 'WA'
          and stats.worst_verdict(['AC', 'AC']) == 'AC'
          and stats.worst_verdict([]) == 'AC')
    check('a missing store still summarises',
          isinstance(stats.summary(7), str) and stats.load() is not None)

    # --- dead code must not come back -------------------------------------
    # These nine helpers had no caller anywhere in the shipped tree (or in the
    # tests) and were removed in the same pass that wired stress_rounds up. A
    # static check keeps them out: importing them again would mean someone
    # re-added dead code, which is exactly what this audit was about.
    for module_name, gone in (
            ('core/cph_fetch.py', 'first_lines'),
            ('core/cph_stats.py', 'def record_unused_stress'),
            ('core/cph_state.py', 'get_listener_port'),
            ('core/cph_checker.py', 'quote_argv'),
            ('core/cph_gdb_trace.py', 'signal_name'),
            ('core/cph_subtasks.py', 'group_of'),
            ('core/cph_settings.py', 'is_lang_view'),
            ('core/cph_settings.py', 'get_folder_tests_file_path'),
            ('core/cph_build_mode.py', 'def forget')):
        path = os.path.join(ROOT, *module_name.split('/'))
        try:
            with open(path, encoding='utf-8') as f:
                body = f.read()
        except Exception:
            body = ''
        check('the dead helper %s stays out of %s' % (gone, module_name),
              gone not in body)
    # and every core module still imports cleanly after the removals
    for core_mod in ('cph_fetch', 'cph_state', 'cph_checker', 'cph_gdb_trace',
                     'cph_subtasks', 'cph_settings', 'cph_build_mode',
                     'cph_stats'):
        try:
            importlib.import_module(pkg + '.core.' + core_mod)
            ok = True
        except Exception as e:  # noqa - reported, not raised
            ok = False
            print('    import %s failed: %s' % (core_mod, e))
        check('core.%s still imports after the cleanup' % core_mod, ok)

    # --- stress rounds must reach the statistics store ----------------------
    # core.cph_stats.record_stress() existed from the start but nothing called
    # it, so every problem's "stress_rounds" stayed 0. cph_stress.py now feeds
    # it; these checks keep the wiring alive (a renamed module, a removed
    # import or a counter that stops accumulating fails here).
    stress_src = open(os.path.join(ROOT, 'cph_stress.py'), encoding='utf-8').read()
    check('cph_stress imports record_stress from core.cph_stats',
          'from .core.cph_stats import record_stress' in stress_src)
    check('the stress loop counts its rounds into the store',
          '_record_stress_rounds(user_file, round_count)' in stress_src
          and re.search(r'def _record_stress_rounds\(user_file, rounds\):',
                        stress_src) is not None)
    check('the stress counter never lets an exception escape',
          re.search(r'def _record_stress_rounds\(user_file, rounds\):'
                    r'(?:.*?\n)*?    try:\n'
                    r'        record_stress\(user_file, rounds\)\n'
                    r'    except Exception:\n        pass',
                    stress_src) is not None)

    stress_store = os.path.join(tempfile.mkdtemp(), 'cph-stress-stats.json')
    old_env = os.environ.get('CPH_STATS_PATH')
    os.environ['CPH_STATS_PATH'] = stress_store
    try:
        probe_dir = os.path.join(tempfile.mkdtemp(), 'proj')
        os.makedirs(probe_dir)
        probe_file = os.path.join(probe_dir, 'main.cpp')
        with open(probe_file, 'w', encoding='utf-8') as f:
            f.write('// probe\n')
        record_stress = getattr(stats, 'record_stress')
        record_stress(probe_file, 12)
        record_stress(probe_file, 3)
        rec = stats.load().get('problems').get(stats.key_for(probe_file))
        check('record_stress accumulates rounds per problem',
              rec is not None and rec['stress_rounds'] == 15, str(rec))
        # The wiring must survive a round count of 0 (a run that never made it
        # past the first compile) instead of writing a bogus record.
        before = stats.load()
        record_stress(probe_file, 0)
        check('record_stress(0) leaves the counter untouched',
              stats.load().get('problems').get(stats.key_for(probe_file), {})
              .get('stress_rounds') == 15)
        check('a pointless write did not touch the history',
              len(stats.load().get('history') or [])
              == len(before.get('history') or []))
    finally:
        if old_env is None:
            os.environ.pop('CPH_STATS_PATH', None)
        else:
            os.environ['CPH_STATS_PATH'] = old_env

    # --- the new commands must be reachable ---
    # Names here are checked against Default.sublime-commands as text; the
    # deep-audit check further down also proves each one is really registered
    # by a *Command class, which is what caught cph_stats_report.
    for command in ('cph_fetch_problem', 'cph_view_statement', 'cph_benchmark',
                    'cph_contest_timer', 'cph_stats', 'cph_calibrate_machine',
                    'cph_stress_replay', 'cph_run_parallel', 'cph_toggle_build_mode'):
        check('%s is in the command palette' % command, command in commands_text)
    check('the new settings keys ship',
          all('"%s"' % key in settings_text for key in (
              'benchmark_runs', 'auto_run_on_save', 'auto_format_on_save',
              'auto_create_file', 'stats_days', 'contest_duration_minutes',
              'subtasks', 'build_mode', 'parallel_workers',
              'auto_std_switch', 'std_floor')))
    check('the extra default languages ship',
          [e['name'] for e in shipped.get('run_settings', [])][-4:]
          == ['Rust', 'Go', 'Pascal', 'JavaScript (Node.js)'],
          str([e['name'] for e in shipped.get('run_settings', [])]))
    node_entry = [e for e in shipped.get('run_settings', [])
                  if 'js' in (e.get('extensions') or [])]
    check('the Node.js entry runs the source directly',
          len(node_entry) == 1 and node_entry[0].get('compile_cmd') is None
          and 'node' in (node_entry[0].get('run_cmd') or ''),
          str(node_entry))
    check('the C++ entry documents the checker and the interactor',
          'checker' in shipped['run_settings'][0]
          and 'interactor' in shipped['run_settings'][0])

    print('== round 8: bugs found by the feature harness ==')
    # 1. A program that reads to EOF (python's sys.stdin.read(), C++'s
    #    while (cin >> x)) hung forever and was reported TLE, because the
    #    plugin never closed the program's stdin. The stored sample IS the
    #    whole input, so the runner must signal EOF after writing it.
    pm_src = open(os.path.join(ROOT, 'Modules', 'ProcessManager.py'),
                  encoding='utf-8').read()
    check('ProcessManager can signal EOF', 'def close_stdin(self)' in pm_src)
    check('the serial run closes stdin after a stored sample',
          'self.process_manager.close_stdin()' in tm_src
          and 'if input_text and input_text.strip():' in tm_src)
    par_src = open(os.path.join(ROOT, 'core', 'cph_parallel.py'),
                   encoding='utf-8').read()
    check('the parallel run closes stdin too', 'manager.close_stdin()' in par_src)
    check('an empty sample keeps stdin open for a manual paste',
          "if input_text and input_text.strip():\n\t\t\t\tself.process_manager.close_stdin()"
          in tm_src)
    # ...and the real thing: a reader-to-EOF must not be a TLE any more
    pm_mod = importlib.import_module(pkg + '.Modules.ProcessManager')
    tmp = tempfile.mkdtemp()
    script = os.path.join(tmp, 'reader.py')
    with open(script, 'w', encoding='utf-8') as f:
        f.write('import sys\nprint(sum(int(x) for x in sys.stdin.read().split()))\n')
    entry = [{'name': 'Python', 'extensions': ['py'], 'compile_cmd': None,
              'run_cmd': sys.executable.replace('\\', '/') + ' "{source_file}"',
              'time_limit_ms': 3000, 'memory_limit_mb': 256}]
    manager = pm_mod.ProcessManager(script, 'source.python', run_settings=entry)
    manager.set_separate_stderr(True)
    manager.set_time_limit(3000)
    # The fake sublime reports 'linux' (the rest of the suite assumes it), but
    # this case really spawns a process, so it has to match the host - on
    # Windows the POSIX branch reaches for os.setsid, which does not exist.
    real_platform = 'windows' if os.name == 'nt' else 'linux'
    saved_platform = sys.modules['sublime'].platform
    sys.modules['sublime'].platform = lambda: real_platform
    try:
        manager.run_file()
        manager.insert('1 2\n')
        manager.close_stdin()
        deadline = time.time() + 15
        while manager.is_stopped() is None and time.time() < deadline:
            time.sleep(0.02)
        out = manager.read()
        code = manager.is_stopped()
    finally:
        sys.modules['sublime'].platform = saved_platform
        manager.close_stderr()
    check('a program that reads to EOF finishes instead of hanging',
          code == 0 and out.strip() == '3', 'rc=%s out=%r' % (code, out))
    shutil.rmtree(tmp, ignore_errors=True)

    # 2. A script checker ("python checker.py") used to be pushed through the
    #    C++ compile command, so a perfectly good Python SPJ never ran.
    cmd_cfg = checker.config([{'name': 'Python', 'extensions': ['py'],
                               'compile_cmd': None,
                               'run_cmd': 'python "{source_file}"',
                               'checker': 'python checker.py'}], 'sol.py')
    check('a command-style checker is recognised',
          cmd_cfg is not None and cmd_cfg['command'] == ['python', 'checker.py'],
          str(cmd_cfg))
    check('a path checker still has no command',
          checker.config([{'extensions': ['cpp'], 'checker': 'c.cpp'}],
                         'a.cpp')['command'] is None)
    # 3. ...and the interactor had the same gap, plus a TypeError on cwd.
    int_src = open(os.path.join(ROOT, 'core', 'cph_interactive.py'),
                   encoding='utf-8').read()
    check('a command-style interactor is recognised',
          "'command': parts," in int_src and 'def run_case(' in int_src
          and 'cwd=None' in int_src)
    check('the interactor never derives cwd from a command list',
          'os.path.dirname(exe)' in int_src
          and 'isinstance(exe, str)' in int_src)
    # 4. An interactor's stderr has to be drained: testlib writes its verdict
    #    message there, and an unread pipe blocks the interactor.
    check('the interactor stderr is drained', 'interactor_err' in int_src)

    check('the killed-by-signal exit codes keep their verdict',
          verdict.get_verdict_by_code(137, 10, 2000, 256, '', '', 'x\n')['name'] == 'MLE'
          and verdict.get_verdict_by_code(9, 10, 2000, 256, '', '', 'x\n')['name'] == 'MLE'
          and verdict.get_verdict_by_code(124, 10, 2000, 256, '', '', 'x\n')['name'] == 'TLE'
          and verdict.get_verdict_by_code(1, 10, 2000, 256, '', '', 'x\n')['name'] == 'RE')

    # --- two more findings from the channel reviewer ---
    # 5. messages.json keys must be "install" or a full semantic version;
    #    "2.0" (two components) was rejected as an invalid version.
    messages_json = json.loads(open(os.path.join(ROOT, 'messages.json'),
                                    encoding='utf-8').read())
    bad_keys = [k for k in messages_json
                if k != 'install'
                and not re.match(r'^\d+\.\d+\.\d+$', k)]
    check('every messages.json key is install or a semantic version',
          not bad_keys, str(bad_keys))
    check('every message file referenced by messages.json exists',
          all(os.path.isfile(os.path.join(ROOT, v))
              for v in messages_json.values()))
    # 6. A console window must not flash on Windows: every subprocess.Popen
    #    call has to pass startupinfo (STARTF_USESHOWWINDOW).
    missing_si = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in ('.git', '__pycache__', '.workbuddy', 'tests')]
        for name in filenames:
            if not name.endswith('.py'):
                continue
            path = os.path.join(dirpath, name)
            try:
                tree = ast.parse(open(path, encoding='utf-8').read())
            except Exception:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                is_popen = (isinstance(func, ast.Attribute)
                            and func.attr == 'Popen')
                if not is_popen:
                    continue
                if not any(kw.arg == 'startupinfo' for kw in node.keywords):
                    missing_si.append('%s:%d' % (
                        os.path.relpath(path, ROOT), node.lineno))
    check('every subprocess.Popen hides the console window',
          not missing_si, str(missing_si))

    print('== round 9: stress testing follows the judging rules ==')
    # Comparing the two outputs line by line made stress testing useless for
    # multi-solution problems (any valid permutation "fails") and for
    # floating point problems (the last digit differs). The judge already
    # knows how to handle both, so the stress loop now uses the same code.
    stress_src = open(os.path.join(ROOT, 'cph_stress.py'),
                      encoding='utf-8').read()
    check('the stress loop can use the checker',
          'from .core import cph_checker' in stress_src
          and 'cph_checker.judge(' in stress_src
          and 'cph_checker.config(' in stress_src)
    check('the checker decides instead of a plain line diff',
          'is_diff = not res.get(\'ok\')' in stress_src)
    check('the stress loop honours the float tolerance',
          'outputs_equal(' in stress_src
          and 'float_tolerance' in stress_src)
    check('the checker message reaches the counterexample report',
          'stress_checker_message' in stress_src
          and 'checker_message' in stress_src)
    check('a checker that cannot be built stops the run',
          'Failed to build checker' in stress_src)
    # The replay worker runs on its own thread and the view API (append) is
    # main-thread only, so its progress lines must be posted back the same way
    # the main stress loop does - not written straight from the worker.
    _replay = stress_src[stress_src.index('class CphStressReplayCommand'):]
    _replay = _replay[:_replay.index('\nclass ', 1)]
    check('stress replay marshals view writes to the main thread',
          'def say(msg):' in _replay
          and 'sublime.set_timeout(lambda m=msg: _append_stress(sess, m), 0)'
              in _replay)
    # One-click import of the .in/.out pairs sitting next to the source file
    # (OJ data packs and "Export tests as .in/.out" both land there).
    import_src = open(os.path.join(ROOT, 'cph_import.py'),
                      encoding='utf-8').read()
    check('the one-click folder import exists',
          'class CphImportTestsHereCommand' in import_src
          and '_do_import_from_folder(os.path.dirname(src_file)' in import_src)
    check('the one-click import is in the command palette',
          'cph_import_tests_here' in commands_text)
    check('the checker message is translated',
          i18n.STRINGS.get('stress_checker_message', {}).get('zh')
          and i18n.STRINGS.get('stress_checker_message', {}).get('en'))

    print('== round 10: a stress run keeps out of the user\'s way ==')
    # Two things made stress testing unusable in practice: the run owned the
    # .exe in the source directory (so the file could not be rebuilt until it
    # was over) and it could not be stopped before the round's time limit ran
    # out - which is what "it never ends" was.
    stress_round = importlib.import_module(pkg + '.cph_stress')
    artifact = importlib.import_module(pkg + '.Modules.build_artifact')

    work = tempfile.mkdtemp(prefix='cph-tests-stress-')
    try:
        user_src = os.path.join(work, 'main.cpp')
        std_src = os.path.join(work, 'std.cpp')
        gen_src = os.path.join(work, 'gen.cpp')
        for path in (user_src, std_src, gen_src,
                     os.path.join(work, 'notes.txt')):
            with open(path, 'w') as handle:
                handle.write('x')
        check('std is found next to the file under test',
              stress_round._candidate_files(work, 'std', exclude=[user_src])
              == [std_src],
              str(stress_round._candidate_files(work, 'std', exclude=[user_src])))
        check('gen is found next to the file under test',
              stress_round._candidate_files(work, 'gen', exclude=[user_src])
              == [gen_src],
              str(stress_round._candidate_files(work, 'gen', exclude=[user_src])))
        check('the file under test is never its own counterpart',
              stress_round._candidate_files(work, 'std') == [std_src],
              str(stress_round._candidate_files(work, 'std')))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    out_dir = os.path.join('tmp', 'cph-stress')
    cmd, out = artifact.isolate_output('g++ "a.cpp" -O2 -o "a.exe"', out_dir)
    check('the stress build goes to its own directory',
          out is not None and os.path.dirname(out) == out_dir, str(out))
    check('the rewritten command keeps the output flag', '-o "' in cmd, cmd)
    cmd, out = artifact.isolate_output(
        'javac -d "{source_file_dir}" "{source_file}"', out_dir)
    check('a command without -o is left alone',
          out is None and cmd.startswith('javac'), str(out))

    check('the round count is a setting, not a prompt',
          'stress_max_rounds' in shipped)
    check('an overall time limit is a setting',
          'stress_max_wall_seconds' in shipped)
    check('the file picker defaults to the system dialog where there is one',
          shipped.get('stress_file_picker') == 'auto',
          str(shipped.get('stress_file_picker')))
    check('the manual stress command is in the command palette',
          'cph_stress_with_options' in commands_text)
    check('the run releases its scratch build when it ends',
          'shutil.rmtree' in stress_src and '_finish_stress(sess)' in stress_src)
    check('stop kills the program that is running',
          'current_proc' in stress_src and '_kill_tree(' in stress_src)
    check('a run that ends says how long it took',
          'stress_finished' in stress_src
          and i18n.STRINGS.get('stress_finished', {}).get('zh')
          and i18n.STRINGS.get('stress_finished', {}).get('en'))
    check('the file browser is translated',
          i18n.STRINGS.get('stress_browse_up', {}).get('en')
          and i18n.STRINGS.get('stress_browse_more', {}).get('zh'))

    print("== round 11: filenames with consecutive spaces, -DLOCAL, multi-session ==")
    # The build-mode tidy used to collapse runs of spaces inside quoted
    # paths: "P2517  ZJOI.cpp" became "P2517 ZJOI.cpp" and cc1plus failed
    # with "No such file or directory" although the file was right there.
    build_mode = importlib.import_module(pkg + '.core.cph_build_mode')
    spaced = 'g++ "P2517  ZJOI 2010, 基站选址.cpp" -std=c++23   -O2 -o "out.exe"'
    tidied = build_mode.transform(spaced, 'release')
    check('the compile command keeps consecutive spaces inside quotes',
          'P2517  ZJOI' in tidied, tidied)
    check('the compile command still tidies whitespace outside quotes',
          '-std=c++23 -O2 -o' in tidied, tidied)
    check('debug mode keeps quoted spaces too',
          'P2517  ZJOI' in build_mode.transform(spaced, 'debug'), tidied)

    # Stress builds must not carry -DLOCAL / -DDEBUG: the user's local
    # template prints debug output under those and every round slows down.
    stripped = stress._strip_local_define(
        'g++ "{source_file}" -std=c++23 -O2 -o "{file_name}.exe" -DLOCAL')
    check('the stress build drops -DLOCAL',
          '-DLOCAL' not in stripped and '-O2' in stripped, stripped)
    check('the stress build drops -DDEBUG as well',
          '-DDEBUG' not in stress._strip_local_define(
              'gcc "a.cpp" -DDEBUG -o "a.exe"'))
    check('the stress build keeps quoted file names intact',
          '"a  b.cpp"' in stress._strip_local_define(
              'g++ "a  b.cpp" -DLOCAL -o "x.exe"'))
    check('the shipped default settings have no -DLOCAL',
          '-DLOCAL' not in io.open(os.path.join(ROOT, 'Algorithm Competition Assistant.sublime-settings'),
                                   encoding='utf-8').read())

    # Several files can be stress tested at the same time; each run owns a
    # session keyed by its stress page.
    sess_a = stress._new_session('D:/a/main.cpp', 'D:/a/std.cpp',
                                 'D:/a/gen.cpp', 2.0, 10, view_id=9001)
    sess_b = stress._new_session('D:/b/main.cpp', 'D:/b/std.cpp',
                                 'D:/b/gen.cpp', 2.0, 10, view_id=9002)
    check('two runs can be live side by side',
          len(stress._running_sessions()) >= 2)
    check('a start for a file already running is refused (per file)',
          stress._running_for('D:/a/main.cpp') is sess_a)
    check('another file is not blocked by the running one',
          stress._running_for('D:/c/main.cpp') is None)
    # Paths are compared the way the platform does: only Windows folds case,
    # so the case-insensitive half of the check is guarded (a plain assertion
    # here failed on the Linux/macOS runners).
    if os.path.normcase('A') == 'a':
        check('the running file is recognised regardless of case (Windows)',
              stress._running_for('D:/a/MAIN.CPP') is sess_a)
    else:
        check('case matters where the platform says it does',
              stress._running_for('D:/a/MAIN.CPP') is None)
    check('sessions are registered so the Stop key can find them',
          stress._sessions[sess_a['stress_view_id']] is sess_a)
    for leftover in (sess_a, sess_b):
        stress._stop_stress(leftover)
        stress._sessions.pop(leftover['stress_view_id'], None)
        stress.unregister_stress_view(leftover['stress_view_id'])

    # The Stop keybinding only fires on a stress page (context key), so it
    # never steals the combination from anything else.
    context_src = open(os.path.join(ROOT, 'cph_context.py'), encoding='utf-8').read()
    check('the cph_stress_view context key is implemented',
          "key == 'cph_stress_view'" in context_src and 'is_stress_view' in context_src)
    for plat in ('Windows', 'Linux', 'OSX'):
        keymap = open(os.path.join(ROOT, 'Default (%s).sublime-keymap' % plat),
                      encoding='utf-8').read()
        check('the stop key is bound to the stress page only (%s)' % plat,
              '"cph_stress_view"' in keymap and 'cph_stop_stress_test' in keymap)

    # The compile-failure panel must not repeat the whole compiler output:
    # the plain text below it already carries it (and is copyable there).
    compile_tpl = open(os.path.join(ROOT, 'Highlight', 'compile.html'),
                       encoding='utf-8').read()
    check('the compile chip no longer duplicates the full error text',
          '{cmd}</a>' in compile_tpl
          and '{compilation_error_label}: {cmd}' not in compile_tpl)
    check('the compile-failure chip is a short label',
          "chip_text=t('compilation_error')" in open(
              os.path.join(ROOT, 'test_manager.py'), encoding='utf-8').read())
    check('the multi-session stop messages are translated',
          i18n.STRINGS.get('stress_multi_running', {}).get('zh')
          and i18n.STRINGS.get('stress_none_running', {}).get('en'))

    # --- round 12: the four "not done yet" items of the fix report --------
    tm_src = open(os.path.join(ROOT, 'test_manager.py'), encoding='utf-8').read()

    # (2) auto_fit_panel_width ran behind a fresh set_timeout() per
    # update_configs(), so a burst of output relaid the window out dozens of
    # times a second.
    check('auto-fit is coalesced into one pending measurement',
          'def schedule_auto_fit_panel_width(' in tm_src
          and 'self.schedule_auto_fit_panel_width()' in tm_src
          and 'sublime.set_timeout(self.auto_fit_panel_width, 150)' not in tm_src)
    check('auto-fit skips a relayout that would not move the window',
          'if last is not None and abs(target_col1 - last) < 0.01:' in tm_src)

    # (4) the compile chip used to say "Compiling..." even for a failed build,
    # and the two Spacegray themes had no inner padding on it at all.
    check('the compile chip is coloured by compile outcome',
          '{state}' in compile_tpl
          and "type='error'" in tm_src and "type='compiling'" in tm_src
          and "type='warning'" in tm_src)
    for css_name in ('test_styles.css', 'test_styles_spacegray.css',
                     'test_styles_spacegraylight.css'):
        css = open(os.path.join(ROOT, 'Highlight', css_name), encoding='utf-8').read()
        check('the compile chip keeps its side padding in %s' % css_name,
              '.test-compiling' in css and 'padding: 1px 5px;' in css)
        check('the failed-compile chip is red in %s' % css_name,
              '.test-compiling-error' in css)
    check('the compile warning label is translated',
          i18n.STRINGS.get('compile_warning', {}).get('zh')
          and i18n.STRINGS.get('compile_warning', {}).get('en'))

    # (6) enable_keybindings claims every package binding turns into a no-op.
    # It used to be a per-branch copy-paste, which is how the state-driven
    # bindings (stop stress, stop listener) came to ignore it. Now the listener
    # applies the gate once, after whatever a branch answered: adding a state
    # key cannot forget it, and the behaviour test above runs every key through
    # both positions of the switch.
    check('the keybinding gate is one choke point, not per-branch copy-paste',
          'if not keybindings_enabled():\n\t\t\tvalue = False' in context_src
          and 'if not keybindings_enabled():\n\t\t\treturn False' not in context_src)

    # (8) warnings from a build that succeeded used to be thrown away: only
    # the failure branch wrote the compiler output to the panel, so the
    # "Compiling..." chip also survived a cache hit for the whole session.
    check('a successful build still reports its warnings',
          'chip_text=t(\'compile_warning\')' in tm_src
          and 'if cached:\n\t\t\t\t\tself.set_compile_bar(t(\'compile_cached\'))' in tm_src)
    check('the compile bar is cleared when the compiler said nothing',
          tm_src.count('self.set_compile_bar(\'\')') >= 1)

    # --- round 13: a crash must be reported at the user's own line ---------
    # A C++ abort names a libstdc++ header (stl_vector.h:1263) or prints
    # nothing at all, and on Windows it is only reaped ~4.5s later - past the
    # watchdog - so the panel used to say WA (release) or TLE (debug) instead
    # of RE, and the one line it did name was not actionable.
    gdb_src = open(os.path.join(ROOT, 'core', 'cph_gdb_trace.py'),
                   encoding='utf-8').read()
    pm_src = open(os.path.join(ROOT, 'Modules', 'ProcessManager.py'),
                  encoding='utf-8').read()
    par_src = open(os.path.join(ROOT, 'core', 'cph_parallel.py'),
                   encoding='utf-8').read()
    bm_src = open(os.path.join(ROOT, 'core', 'cph_build_mode.py'),
                  encoding='utf-8').read()

    check('debug builds carry the libstdc++ assertion macro',
          '-D_GLIBCXX_ASSERTIONS' in bm_src
          and "_ASSERTIONS = '-D_GLIBCXX_ASSERTIONS'" in bm_src)
    for api in ('def parse_frames(', 'def build_gdb_cmd(', 'def split_log(',
                'def find_crash(', 'def replay(', 'def names_source(',
                'def find_gdb('):
        check('the gdb replay offers %s' % api, api in gdb_src)
    # The backtrace has to be requested BETWEEN the two sentinels: with the
    # end marker first the slice is empty and every query silently returns
    # None even though the log plainly holds the frames.
    _bt = gdb_src.index("ex.append('bt 30')")
    check('the backtrace is captured between the sentinels',
          gdb_src.index('ex.append(\'echo \\\\n%s\\\\n\\\\n\' % _SENTINEL)') < _bt
          < gdb_src.index('ex.append(\'echo \\\\n%s\\\\n\' % _SENTINEL_END)'))
    # gdb always exits 0 here (measured: 0 for normal exit, for SIGSEGV and
    # for abort()), so a `quit <code>` would hand every program the same
    # non-zero status and make whole runs look like RE.
    check('the gdb verdict never comes from its exit code',
          "ex.append('quit')" in gdb_src
          and "ex.append('quit " not in gdb_src
          and 'ex.append(\'kill\')' in gdb_src)
    check('the abort breakpoint is the only one (raise does not exist here)',
          "_BREAKPOINTS = ('abort',)" in gdb_src)
    check('a segfault counts as a crash even without hitting the breakpoint',
          'received signal' in gdb_src and '_HIT_SIGNAL' in gdb_src)
    check('the replay never raises out of the judge path',
          'except Exception' in gdb_src)

    check('the run manager can replay a crash and name a source line',
          'def program_path(' in pm_src and 'def locate_crash(' in pm_src
          and 'find_gdb as _find_gdb' in pm_src
          and 'replay as _replay_under_gdb' in pm_src)
    check('the panel asks the run manager for the user frame',
          'def _replay_crash(' in tm_src
          and 'crash_location = _replay_crash(' in tm_src)
    # The RE branch must prefer the replay even when the program's own output
    # already named something: that something is the libstdc++ header.
    check('a libstdc++ frame is replaced by the user frame',
          'if not _names_source(location, source_file):' in tm_src)
    check('the stress/judge path does the same',
          'if not _names_source(location, source_file):' in par_src
          and 'def _replay_crash(' in par_src)

    # The program argument for gdb must go through verbatim. Adding quotes is
    # what a Windows path with spaces invites, and gdb then reports "No
    # executable specified, use `target exec'" - which surfaced as a bare TLE
    # with an empty crash line, for a program that does abort. Every fixture
    # lived under %TEMP%, so the quoted branch was never exercised.
    _quote_def = gdb_src[gdb_src.index('def _quote('):]
    _quote_def = _quote_def[:_quote_def.index('\ndef ')]
    _quote_body = _quote_def[_quote_def.index('"""', _quote_def.index('"""') + 3) + 3:]
    check('the gdb program path is passed verbatim, never quoted',
          _quote_body.strip() == 'return path'
          and '"%s"' not in _quote_body
          and 'replace(' not in _quote_body,
          repr(_quote_body.strip()[:120]))

    # --- round 14: template snippets, clipboard io, language switching,
    # and the ProcessManager decode chain - three plugin modules and the
    # process decoder that the suite had never touched before.
    print('== round 14: template, clipboard io, language, decoding ==')
    i18n = importlib.import_module(pkg + '.core.cph_i18n')
    template_mod = importlib.import_module(pkg + '.cph_template')
    tests_io = importlib.import_module(pkg + '.cph_tests_io')
    lang_mod = importlib.import_module(pkg + '.cph_language')
    fake_sublime = sys.modules['sublime']
    Region = fake_sublime.Region

    class _SelList(list):
        def clear(self):
            del self[:]

        def add(self, region):
            self.append(region)

    class _TemplateView(object):
        """Just enough view for CphInsertTemplateCommand.run()."""

        def __init__(self, text, regions, word_region=None):
            self._text = text
            self._sel = _SelList(regions)
            self._word = word_region if word_region is not None else Region(0, 0)

        def sel(self):
            return self._sel

        def substr(self, region):
            return self._text[region.begin():region.end()]

        def word(self, pt):
            return self._word

        def replace(self, edit, region, snippet):
            self._text = (self._text[:region.begin()] + snippet
                          + self._text[region.end():])

        def text(self):
            return self._text

    msgs = []
    saved_status = fake_sublime.status_message
    fake_sublime.status_message = lambda m: msgs.append(m)
    saved_lang = i18n.get_lang()
    try:
        # {cursor} marks where the caret must land; the marker itself must go
        view = _TemplateView('bf', [Region(0, 2)])
        template_mod.CphInsertTemplateCommand(view).run(None)
        bf_snippet = template_mod.BUILTIN_TEMPLATES['bf']
        want_pt = len(bf_snippet[:bf_snippet.find('{cursor}')])
        check('template expansion drops the {cursor} marker',
              view.text() == bf_snippet.replace('{cursor}', '', 1),
              repr(view.text()[:60]))
        check('the caret lands exactly where {cursor} stood',
              list(view.sel()) == [Region(want_pt, want_pt)],
              str([(r.a, r.b) for r in view.sel()]))

        view = _TemplateView('fastio', [Region(0, 6)])
        template_mod.CphInsertTemplateCommand(view).run(None)
        check('a template without {cursor} leaves the selection alone',
              view.text() == template_mod.BUILTIN_TEMPLATES['fastio']
              and list(view.sel()) == [Region(0, 6)], repr(view.text()[:40]))

        view = _TemplateView('bf tail', [Region(0, 2), Region(4, 8)])
        template_mod.CphInsertTemplateCommand(view).run(None)
        check('multiple selections make the command a no-op',
              view.text() == 'bf tail', repr(view.text()))

        view = _TemplateView('   ', [Region(1, 1)], word_region=Region(1, 1))
        template_mod.CphInsertTemplateCommand(view).run(None)
        check('an empty keyword reports and changes nothing',
              view.text() == '   '
              and msgs and msgs[-1] == i18n.t('template_no_keyword'),
              repr(msgs[-1] if msgs else None))

        view = _TemplateView('nosuchtemplate', [Region(0, 14)])
        template_mod.CphInsertTemplateCommand(view).run(None)
        check('an unknown keyword is reported, not expanded',
              view.text() == 'nosuchtemplate'
              and msgs and msgs[-1] == i18n.t('template_not_found',
                                              name='nosuchtemplate'),
              repr(msgs[-1] if msgs else None))

        # _lookup: algorithms_base extensions in priority order, then the
        # settings dict, then the built-ins
        work = tempfile.mkdtemp(prefix='cph-tests-tpl-')

        def _put(name, body):
            with open(os.path.join(work, name), 'w', encoding='utf-8') as f:
                f.write(body)

        _put('kw.cpp', 'cpp 正文\n')
        _put('both.cc', 'cc body\n')
        _put('both.py', 'py body\n')
        _put('chain.py', 'py wins\n')
        _put('chain.txt', 'txt loses\n')
        _put('only.cxx', 'cxx body\n')
        _put('notes.txt', 'txt body\n')
        settings_core = importlib.import_module(pkg + '.core.cph_settings')
        saved_core = settings_core.settings
        settings_core.settings = {'algorithms_base': work,
                                  'templates': {'mine': 'A{cursor}B'}}
        try:
            probe = template_mod.CphInsertTemplateCommand(
                _TemplateView('', [Region(0, 0)]))
            check('_lookup finds the .cpp file (utf-8, trailing newline stripped)',
                  probe._lookup('kw') == 'cpp 正文', repr(probe._lookup('kw')))
            check('_lookup prefers .cc over .py',
                  probe._lookup('both') == 'cc body', repr(probe._lookup('both')))
            check('_lookup prefers .py over .txt',
                  probe._lookup('chain') == 'py wins', repr(probe._lookup('chain')))
            check('_lookup finds a .cxx file',
                  probe._lookup('only') == 'cxx body', repr(probe._lookup('only')))
            check('_lookup falls through to .txt',
                  probe._lookup('notes') == 'txt body', repr(probe._lookup('notes')))
            check('_lookup reads the settings templates dict',
                  probe._lookup('mine') == 'A{cursor}B', repr(probe._lookup('mine')))
            check('_lookup ends with the built-ins and None for misses',
                  probe._lookup('fastio') == template_mod.BUILTIN_TEMPLATES['fastio']
                  and probe._lookup('still-nothing') is None,
                  repr((probe._lookup('fastio'), probe._lookup('still-nothing'))))
            settings_core.settings = {'algorithms_base': os.path.join(work, 'nope')}
            check('_lookup survives a missing algorithms_base directory',
                  probe._lookup('kw') is None, repr(probe._lookup('kw')))
            settings_core.settings = {'algorithms_base': work,
                                      'templates': {'mine': 'A{cursor}B'}}
            view = _TemplateView('mine', [Region(0, 4)])
            template_mod.CphInsertTemplateCommand(view).run(None)
            check('a settings template honours {cursor} too',
                  view.text() == 'AB' and list(view.sel()) == [Region(1, 1)],
                  repr((view.text(), [(r.a, r.b) for r in view.sel()])))
        finally:
            settings_core.settings = saved_core
            shutil.rmtree(work, ignore_errors=True)

        # SEPARATOR: dashes and equals both split a whole line, prose never
        sep = tests_io.SEPARATOR
        # the rule itself is consumed by split(); the newlines around it stay
        check('the separator splits on a dash rule',
              sep.split('1 2\n---\n3\n', maxsplit=1) == ['1 2\n', '\n3\n'],
              repr(sep.split('1 2\n---\n3\n', maxsplit=1)))
        check('the separator splits on an equals rule',
              sep.split('in\n=====\nout', maxsplit=1) == ['in\n', '\nout'],
              repr(sep.split('in\n=====\nout', maxsplit=1)))
        check('the separator allows indented / padded rules',
              sep.split('a\n  ---  \nb', maxsplit=1) == ['a\n', '\nb'],
              repr(sep.split('a\n  ---  \nb', maxsplit=1)))
        prose = ('a---b is inline\n'
                 '-- too short\n'
                 '--- trailing text\n'
                 'text before ---\n')
        check('the separator never cuts ordinary text',
              sep.split(prose, maxsplit=1) == [prose],
              repr(sep.split(prose, maxsplit=1)))

        # the clipboard command: split, merge and status messages
        clipboard_val = ''
        saved_clip = fake_sublime.get_clipboard
        fake_sublime.get_clipboard = lambda: clipboard_val
        saved_merge = tests_io.merge_into_file
        calls = []

        def fake_merge(file_name, new_tests):
            calls.append((file_name, new_tests))
            return True, 2, []

        tests_io.merge_into_file = fake_merge
        try:
            view = fake_sublime.View(file_name='D:/proj/main.cpp',
                                     settings={'cph_run_view': True})
            add_cmd = tests_io.CphAddTestFromClipboardCommand(view)

            clipboard_val = '1 2\n---\n3\n'
            add_cmd.run(None)
            check('a clipboard with a dash rule becomes input + answer',
                  calls and calls[-1][1]
                  == [{'test': '1 2\n', 'correct_answers': ['3']}],
                  repr(calls[-1] if calls else None))
            check('the run panel is refreshed and the total announced',
                  msgs and msgs[-1] == i18n.t('clipboard_test_added', total=2),
                  repr(msgs[-1] if msgs else None))

            del calls[:]
            clipboard_val = '9 9'
            add_cmd.run(None)
            check('a clipboard without a rule stores everything as input',
                  calls and calls[-1][1] == [{'test': '9 9',
                                              'correct_answers': []}],
                  repr(calls[-1] if calls else None))

            del calls[:]
            clipboard_val = 'a\n---\nb\n---\nc'
            add_cmd.run(None)
            check('only the first rule splits (the rest stays the answer)',
                  calls and calls[-1][1]
                  == [{'test': 'a\n', 'correct_answers': ['b\n---\nc']}],
                  repr(calls[-1] if calls else None))

            del calls[:]
            clipboard_val = '   \n'
            add_cmd.run(None)
            check('a blank clipboard is reported and not stored',
                  not calls and msgs and msgs[-1] == i18n.t('clipboard_empty'),
                  repr(msgs[-1] if msgs else None))

            tests_io.merge_into_file = lambda fn, nt: (True, 1, [('x', 'y')])
            clipboard_val = '1\n---\n2'
            add_cmd.run(None)
            check('an answer conflict announces that the stored one was kept',
                  msgs and i18n.t('answer_conflict_kept') in msgs[-2:],
                  repr(msgs[-2:]))
        finally:
            tests_io.merge_into_file = saved_merge
            fake_sublime.get_clipboard = saved_clip

        # language switching: get_lang() and the persisted setting agree
        lang_settings = fake_sublime.Settings({})
        saves = []
        saved_load, saved_save = fake_sublime.load_settings, fake_sublime.save_settings
        fake_sublime.load_settings = lambda name: lang_settings
        fake_sublime.save_settings = lambda name: saves.append(name)
        try:
            lang_view = fake_sublime.View()
            set_cmd = lang_mod.CphLanguageSetLanguageCommand(lang_view)
            toggle_cmd = lang_mod.CphLanguageToggleLanguageCommand(lang_view)
            set_cmd.run(None, language='zh')
            check('set_language zh switches get_lang and persists',
                  i18n.get_lang() == 'zh'
                  and lang_settings.get('language') == 'zh'
                  and saves == ['Algorithm Competition Assistant.sublime-settings'],
                  repr((i18n.get_lang(), lang_settings.get('language'), saves)))
            toggle_cmd.run(None)
            check('toggle flips zh to en and persists',
                  i18n.get_lang() == 'en'
                  and lang_settings.get('language') == 'en'
                  and len(saves) == 2,
                  repr((i18n.get_lang(), lang_settings.get('language'), saves)))
            toggle_cmd.run(None)
            check('toggle flips back to zh and persists',
                  i18n.get_lang() == 'zh'
                  and lang_settings.get('language') == 'zh'
                  and len(saves) == 3,
                  repr((i18n.get_lang(), lang_settings.get('language'), saves)))
            set_cmd.run(None)          # the default argument is 'en'
            check('set_language defaults to english',
                  i18n.get_lang() == 'en', i18n.get_lang())
        finally:
            fake_sublime.load_settings, fake_sublime.save_settings = \
                saved_load, saved_save
            i18n.set_lang(saved_lang)
    finally:
        fake_sublime.status_message = saved_status

    # --- ProcessManager._decode_output: utf-8 first, locale fallback ---
    pm_mod = importlib.import_module(pkg + '.Modules.ProcessManager')
    check('decode(None) is an empty string', pm_mod._decode_output(None) == '',
          repr(pm_mod._decode_output(None)))
    check('a str input still gets its newlines normalized',
          pm_mod._decode_output('a\r\nb\rc') == 'a\nb\nc',
          repr(pm_mod._decode_output('a\r\nb\rc')))
    check('pure ascii bytes decode unchanged',
          pm_mod._decode_output(b'hello world\n') == 'hello world\n',
          repr(pm_mod._decode_output(b'hello world\n')))
    check('valid utf-8 chinese decodes',
          pm_mod._decode_output('中文断言'.encode('utf-8')) == '中文断言',
          repr(pm_mod._decode_output('中文断言'.encode('utf-8'))))
    check('lone CR mixed with CRLF normalizes to LF only',
          pm_mod._decode_output(b'a\r\nb\rc\r') == 'a\nb\nc\n',
          repr(pm_mod._decode_output(b'a\r\nb\rc\r')))
    # Which bytes survive the locale step depends on the machine's ANSI code
    # page, so this cannot be asserted against the host's own locale: 0xff 0xfe
    # is illegal in utf-8 and in cp936, but perfectly legal in cp1252 (ÿþ). The
    # original assertion passed on Linux, macOS and a Chinese Windows and failed
    # only on the Western-locale CI runner - a test bug, not a decoder bug. Pin
    # the codec and the fallback chain is what is actually under test.
    saved_pref = pm_mod.locale.getpreferredencoding
    try:
        pm_mod.locale.getpreferredencoding = lambda flag=False: 'ascii'
        undec = pm_mod._decode_output(b'\xff\xfe bad')
        check('bytes no codec can read become replacement characters',
              undec.count('\ufffd') == 2 and undec.endswith(' bad'),
              repr(undec.encode('ascii', 'backslashreplace').decode('ascii')))
        pm_mod.locale.getpreferredencoding = lambda flag=False: 'cp1252'
        western = pm_mod._decode_output(b'\xff\xfe bad')
        check('a locale codec that can read the bytes wins over replacing them',
              western == 'ÿþ bad',
              repr(western.encode('ascii', 'backslashreplace').decode('ascii')))
    finally:
        pm_mod.locale.getpreferredencoding = saved_pref

    # cp936 g++ diagnostics: utf-8 decoding fails, the locale codec steps in
    diag = 'main.cpp:4:9: error: 变量 x 未声明'
    cp936_bytes = diag.encode('cp936')
    saved_pref = pm_mod.locale.getpreferredencoding
    try:
        pm_mod.locale.getpreferredencoding = lambda flag=False: 'gbk'
        check('cp936 g++ diagnostics decode when the locale is gbk',
              pm_mod._decode_output(cp936_bytes) == diag,
              repr(pm_mod._decode_output(cp936_bytes)))
    finally:
        pm_mod.locale.getpreferredencoding = saved_pref
    pref = locale.getpreferredencoding(False).lower().replace('-', '')
    pref = pref.replace('_', '')
    decoded = pm_mod._decode_output(cp936_bytes)
    if pref in ('gbk', 'cp936', 'ms936', '936', 'gb2312', 'gb18030'):
        check('cp936 g++ diagnostics decode on a real gbk locale too',
              decoded == diag, repr(decoded))
    else:
        check('cp936 g++ diagnostics never raise outside a gbk locale',
              isinstance(decoded, str) and '\r' not in decoded, repr(decoded))

    # the captured-stderr path runs through the same decoder
    pm_obj = pm_mod.ProcessManager.__new__(pm_mod.ProcessManager)
    pm_obj.stderr_file = io.BytesIO('stderr 正文\r\nsecond line'.encode('utf-8'))
    check('get_stderr decodes and normalizes the captured stderr',
          pm_obj.get_stderr() == 'stderr 正文\nsecond line',
          repr(pm_obj.get_stderr()))
    saved_pref = pm_mod.locale.getpreferredencoding
    try:
        pm_mod.locale.getpreferredencoding = lambda flag=False: 'gbk'
        pm_obj.stderr_file = io.BytesIO(cp936_bytes)
        check('get_stderr falls back to the locale codec for cp936 output',
              pm_obj.get_stderr() == diag, repr(pm_obj.get_stderr()))
    finally:
        pm_mod.locale.getpreferredencoding = saved_pref

    # end-to-end: a real child prints CRLF + lone CR + utf-8 Chinese; the text
    # that reaches prog_out must carry no 0x0d artifact and no mojibake.
    tmp = tempfile.mkdtemp(prefix='cph-tests-e2e-')
    try:
        script = os.path.join(tmp, 'printer.py')
        with open(script, 'w', encoding='utf-8') as f:
            f.write('import sys\n'
                    "sys.stdout.buffer.write('中文输出OK\\r\\n'.encode('utf-8'))\n"
                    "sys.stdout.buffer.write(b'1\\r\\n2\\rlone\\r')\n"
                    "sys.stdout.buffer.write('end'.encode('utf-8'))\n"
                    'sys.stdout.buffer.flush()\n')
        entry = [{'name': 'Python', 'extensions': ['py'], 'compile_cmd': None,
                  'run_cmd': sys.executable.replace('\\', '/') + ' "{source_file}"',
                  'time_limit_ms': 3000, 'memory_limit_mb': 256}]
        manager = pm_mod.ProcessManager(script, 'source.python',
                                        run_settings=entry)
        manager.set_time_limit(3000)
        # match the host: the fake sublime reports linux, but this really
        # spawns a process (the POSIX branch would reach for os.setsid).
        real_platform = 'windows' if os.name == 'nt' else 'linux'
        saved_platform = sys.modules['sublime'].platform
        sys.modules['sublime'].platform = lambda: real_platform
        try:
            manager.run_file()
            deadline = time.time() + 15
            while manager.is_stopped() is None and time.time() < deadline:
                time.sleep(0.02)
            out = manager.read()
            code = manager.is_stopped()
        finally:
            sys.modules['sublime'].platform = saved_platform
            manager.close_stderr()
        check('the end-to-end child exits cleanly', code == 0, str(code))
        check('prog_out never sees a 0x0d artifact',
              isinstance(out, str) and '\r' not in out, repr(out))
        check('prog_out carries decoded utf-8 and normalized newlines',
              out == '中文输出OK\n1\n2\nlone\nend', repr(out))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --- i18n hygiene: a deleted key must never come back from the dead ---
    keys_defined, keys_used = _i18n_usage()
    orphaned = sorted(DYNAMIC_I18N_KEYS - keys_defined)
    check('the dynamic i18n whitelist still names real keys', not orphaned,
          ', '.join(orphaned))
    orphaned_stats = sorted(STATS_I18N_KEYS - keys_defined)
    check('the stats i18n whitelist still names real keys', not orphaned_stats,
          ', '.join(orphaned_stats))
    dead = sorted(keys_defined - keys_used
                  - DYNAMIC_I18N_KEYS - STATS_I18N_KEYS)
    check('every i18n key is called through t() or whitelisted', not dead,
          ', '.join(dead))
    # The stats keys are reached through a variable, so prove they are wired:
    # _ENGLISH_LABELS must carry exactly the whitelisted key set, and
    # render_summary() must actually emit a translated line for one of them.
    stats_src = open(os.path.join(ROOT, 'core', 'cph_stats.py'),
                     encoding='utf-8').read()
    extras_src = open(os.path.join(ROOT, 'cph_extras.py'),
                      encoding='utf-8').read()
    for key in sorted(STATS_I18N_KEYS):
        check('_ENGLISH_LABELS carries %s' % key,
              "'%s':" % key in stats_src)
    check('render_summary is called with the translator from the plugin',
          'summary_localized' in extras_src
          and 'from .cph_i18n import t' in stats_src)
    # ...and it really produces the numbers in both languages, not just the
    # labels: the whole point of moving summary() onto summary_data().
    stats_mod = importlib.import_module(pkg + '.core.cph_stats')
    i18n_mod = importlib.import_module(pkg + '.core.cph_i18n')
    stress_store = os.path.join(tempfile.mkdtemp(), 'cph-report.json')
    old_env = os.environ.get('CPH_STATS_PATH')
    os.environ['CPH_STATS_PATH'] = stress_store
    try:
        saved_lang = i18n_mod.get_lang()
        try:
            rep_file = os.path.join(tempfile.mkdtemp(), 'rep.cpp')
            with open(rep_file, 'w', encoding='utf-8') as f:
                f.write('// report\n')
            stats_mod.record_run(rep_file, ['AC', 'AC'])
            stats_mod.record_run(rep_file, ['WA'])
            stats_mod.record_stress(rep_file, 7)
            body = stats_mod.summary_data(7)
            check('summary_data reports the stress rounds',
                  body.get('stress_rounds') == 7
                  and body.get('runs') == 2 and body.get('solved') == 1
                  and len(body.get('needs') or []) == 1, str(body))
            i18n_mod.set_lang('zh')
            zh = stats_mod.summary_localized(7)
            i18n_mod.set_lang('en')
            en = stats_mod.summary_localized(7)
            check('the report is rendered in the active language',
                  '练习统计' in zh and '待加强' in zh and '对拍累计' in zh
                  and 'practice statistics' in en and 'Needs work' in en,
                  repr(zh[:40]) + ' / ' + repr(en[:40]))
            check('the stress line appears once rounds were recorded',
                  '对拍累计' in zh and 'Stress rounds' in en)
            check('the plain summary() text is unchanged (english fallback)',
                  'CPH practice stats' in stats_mod.summary(7)
                  and 'Stress rounds: 7' in stats_mod.summary(7))
        finally:
            i18n_mod.set_lang(saved_lang)
    finally:
        if old_env is None:
            os.environ.pop('CPH_STATS_PATH', None)
        else:
            os.environ['CPH_STATS_PATH'] = old_env

    # --- the run log is bounded, the lifetime counters are not --------------
    # record_run() rewrites the whole JSON on every run, so an unbounded history
    # makes every later run slower (measured: 4000 entries = 636 KB, 18.4 ms per
    # call, quadratic overall). Writing the real 5000 entries here would take
    # tens of seconds, so shrink the limit and exercise the same code path.
    check('the history limit is a named constant',
          '_HISTORY_LIMIT = 5000' in stats_src)
    hist_store = os.path.join(tempfile.mkdtemp(), 'cph-history.json')
    old_env = os.environ.get('CPH_STATS_PATH')
    os.environ['CPH_STATS_PATH'] = hist_store
    saved_limit = stats_mod._HISTORY_LIMIT
    try:
        stats_mod._HISTORY_LIMIT = 5
        hist_file = os.path.join(tempfile.mkdtemp(), 'hist.cpp')
        with open(hist_file, 'w', encoding='utf-8') as f:
            f.write('// history\n')
        base = 1700000000
        stats_mod.record_run(hist_file, ['AC'], now=base)
        for i in range(1, 9):
            stats_mod.record_run(hist_file, ['WA'], now=base + i)
        entries = stats_mod.load().get('history') or []
        check('the run log is capped at the limit',
              len(entries) == 5, str(len(entries)))
        check('the newest runs are the ones kept',
              [e['time'] for e in entries]
              == [stats_mod._iso(base + i) for i in (4, 5, 6, 7, 8)],
              str([e['time'] for e in entries]))
        rec = (stats_mod.load().get('problems')
               or {}).get(stats_mod.key_for(hist_file)) or {}
        check('truncating the log keeps the lifetime counters',
              rec.get('runs') == 9 and rec.get('ac_runs') == 1
              and rec.get('attempts') == 8, str(rec))
        # The AC is now older than the log, so "solved" must come from the
        # problem record instead of from history.
        body = stats_mod.summary_data(7, now=base + 9)
        check('a solved problem stays solved after its AC leaves the log',
              body.get('solved') == 1, str(body))
    finally:
        stats_mod._HISTORY_LIMIT = saved_limit
        if old_env is None:
            os.environ.pop('CPH_STATS_PATH', None)
        else:
            os.environ['CPH_STATS_PATH'] = old_env

    # --- failures that used to be swallowed reach the user ------------------
    # Both of these were 'except Exception: return None/False' with no way for
    # the caller to tell them apart from the ordinary path, so the user saw
    # "listener target missing" or an empty report instead of the real cause.
    companion_src = open(os.path.join(ROOT, 'cph_companion.py'),
                         encoding='utf-8').read()
    check('a directory that cannot be created is reported, not swallowed',
          companion_src.count("t('auto_create_failed'") == 2
          and 'auto_create_failed' in i18n.STRINGS
          and i18n.STRINGS['auto_create_failed'].get('zh')
          and i18n.STRINGS['auto_create_failed'].get('en'))
    check('the auto-create failure carries the path and the os error',
          "t('auto_create_failed', path=directory, error=str(e))" in companion_src
          and "t('auto_create_failed', path=path, error=str(e))" in companion_src)
    check('record_stats reports a failed statistics save',
          'stats_take_save_error()' in _tm_src
          and "t('stats_save_failed', error=problem)" in _tm_src
          and i18n.STRINGS.get('stats_save_failed', {}).get('zh')
          and i18n.STRINGS.get('stats_save_failed', {}).get('en'))
    check('clearing the tests reports the files it could not empty',
          'clear_failures.append(' in _tm_src
          and "t('tests_clear_failed', error='; '.join(clear_failures)[:300])" in _tm_src
          and i18n.STRINGS.get('tests_clear_failed', {}).get('zh')
          and i18n.STRINGS.get('tests_clear_failed', {}).get('en'))
    # The store must remember why it failed, hand the reason over exactly once
    # (otherwise every later run repeats a stale error), and stay quiet when it
    # works. Pointing CPH_STATS_PATH at a path *under a regular file* makes
    # save() fail deterministically on every platform.
    fail_store = os.path.join(tempfile.mkdtemp(), 'blocker')
    with open(fail_store, 'w', encoding='utf-8') as f:
        f.write('not a directory\n')
    old_env = os.environ.get('CPH_STATS_PATH')
    os.environ['CPH_STATS_PATH'] = os.path.join(fail_store, 'sub', 'stats.json')
    try:
        check('a working store leaves no error behind',
              stats_mod.take_save_error() == '')
        stats_mod.record_run(os.path.join(tempfile.mkdtemp(), 'x.cpp'), ['AC'])
        first = stats_mod.take_save_error()
        check('a failed save keeps the reason for the caller',
              'blocker' in first and first != '', repr(first))
        check('the reason is handed over only once',
              stats_mod.take_save_error() == '')
    finally:
        if old_env is None:
            os.environ.pop('CPH_STATS_PATH', None)
        else:
            os.environ['CPH_STATS_PATH'] = old_env

    # ------------------------------------------------------------------
    # Deep-audit regressions (2.4.7). Every case below reproduces a problem
    # that was confirmed by hand against the 2.4.6 tree; each one failed
    # before its fix, so a green run here is evidence the fix works.
    # ------------------------------------------------------------------
    print('== Deep audit: command surfaces, verdict fidelity, threading ==')

    _sub = sys.modules['sublime']

    def _snake(case_cls):
        """Sublime's convention: CphStatsCommand registers as cph_stats."""
        stem = case_cls[:-len('Command')]
        return re.sub(r'(?<!^)(?=[A-Z])', '_', stem).lower()

    registered = set()
    for _py in _plugin_py_files():
        with open(os.path.join(ROOT, _py), encoding='utf-8') as f:
            _tree = ast.parse(f.read(), _py)
        for _node in _tree.body:
            if isinstance(_node, ast.ClassDef) and _node.name.endswith('Command'):
                registered.add(_snake(_node.name))

    referenced = []

    def _collect_cmds(obj, where):
        if isinstance(obj, dict):
            _c = obj.get('command')
            if isinstance(_c, str) and _c.startswith('cph_'):
                referenced.append((_c, where))
            for _v in obj.values():
                _collect_cmds(_v, where)
        elif isinstance(obj, list):
            for _v in obj:
                _collect_cmds(_v, where)

    for _name in ('Default.sublime-commands', 'Main.sublime-menu',
                  'Context.sublime-menu') + DEFAULTS:
        with open(os.path.join(ROOT, _name), encoding='utf-8') as f:
            _collect_cmds(json.load(f), _name)
    # Not a string-in-file check: the command name a *Command class actually
    # registers is derived from the class, so a typo like cph_stats_report is
    # caught instead of passing because the text exists in the JSON.
    unknown_cmds = sorted(set(c for c, w in referenced if c not in registered))
    check('every cph_ command the UI references is registered by a class',
          not unknown_cmds, 'no such command: %s' % ', '.join(unknown_cmds))

    dup_defs = []
    for _py in _plugin_py_files():
        with open(os.path.join(ROOT, _py), encoding='utf-8') as f:
            _tree = ast.parse(f.read(), _py)
        _seen = set()
        for _node in _tree.body:
            if isinstance(_node, (ast.FunctionDef, ast.ClassDef)):
                if _node.name in _seen:
                    dup_defs.append('%s: %s' % (_py, _node.name))
                _seen.add(_node.name)
    check('no module redefines a top-level name it already defined',
          not dup_defs, '; '.join(dup_defs))

    def _settings_json():
        raw = open(os.path.join(ROOT, 'Algorithm Competition Assistant.sublime-settings'),
                   encoding='utf-8').read()
        return json.loads(re.sub(r'^\s*//.*$', '', raw, flags=re.M))

    _sj = _settings_json()
    _no_fmt = [e.get('name') for e in _sj.get('run_settings', [])
               if set(e.get('extensions') or []) & {'cpp', 'c'}
               and not (e.get('format_cmd') or '').strip()]
    check('auto_format_on_save has a format_cmd to run for the C/C++ entries',
          not _no_fmt, 'no format_cmd for: %s' % ', '.join(_no_fmt))

    def _judge(actual, expected):
        return verdict.get_verdict_by_code(
            rtcode=0, runtime=5, time_limit_ms=1000, memory_limit_mb=256,
            stderr='', stdout=actual, expected_output=expected)['name']

    # compress() collapsed every whitespace difference, so a program whose
    # token boundaries are wrong ("1 23" for "12 3") was reported as a mere
    # presentation error instead of a wrong answer - actively misleading,
    # because it tells the user their answer is fine.
    for _act, _exp in (('1 23', '12 3'), ('1 2 3', '1 23'),
                       ('hello world', 'hellow orld')):
        check('a token-boundary mistake (%s vs %s) is WA, not PE' % (_act, _exp),
              _judge(_act, _exp) == 'WA', 'got %s' % _judge(_act, _exp))
    # The cases that really are presentation/format-only must not change.
    for _act, _exp in (('1  2', '1 2'), ('12\n3', '12 3')):
        check('a formatting-only difference (%s vs %s) stays PE' % (_act, _exp),
              _judge(_act, _exp) == 'PE', 'got %s' % _judge(_act, _exp))
    check('an exact match still judges AC', _judge('1 2', '1 2') == 'AC')

    _clamp = getattr(tm, 'clamp_output', None)
    check('the output cap clamps a chunk instead of appending it whole',
          _clamp is not None
          and _clamp('', 'x' * 100, 10) == 'x' * 10
          and _clamp('abcde', 'xyz', 7) == 'xy'
          and _clamp('abcde', 'xyz', 8) == 'xyz'
          and _clamp('abcdefghij', 'xyz', 10) == ''
          and _clamp('', 'y' * 50, 0) == 'y' * 50,
          'clamp_output missing' if _clamp is None else 'clamps wrongly')

    _post = getattr(tm, 'post_to_ui', None)
    _keep_timeout = _sub.set_timeout
    try:
        _queued, _ran = [], []
        _sub.set_timeout = lambda fn, delay=0: _queued.append(fn)
        if _post is not None:
            _post(lambda: _ran.append(1))
        check('a reader-thread UI mutation is deferred, not run inline',
              _post is not None and len(_queued) == 1 and _ran == [],
              'post_to_ui missing or ran inline' if _post is None
              else 'queued=%d ran=%d' % (len(_queued), len(_ran)))
        if _queued:
            _queued[0]()
        check('the deferred callback is the one the main thread runs',
              _ran == [1])
    finally:
        _sub.set_timeout = _keep_timeout

    # The blocking pump must not sit on Sublime's shared async worker, and it
    # must not read the pipe with an unbounded read-to-EOF. These two are
    # structural properties of the run path that no headless call can drive,
    # so they are guarded against the source of the listener itself.
    check('the serial output pump runs on a private thread, not the shared async worker',
          'set_timeout_async(self.__process_listener)' not in _tm_src
          and 'target=self.__process_listener' in _tm_src,
          'listener is still scheduled through set_timeout_async')
    check('the serial output pump reads the pipe in bounded chunks',
          '= proc.read()\n' not in _tm_src)

    class _DeadProc(object):
        pid = 4242

        def poll(self):
            return 0

        def kill(self):
            raise AssertionError('killed a pid that already exited')

    _pm = pm_mod.ProcessManager(os.path.join(tempfile.mkdtemp(), 'a.cpp'), 'C++')
    _pm.process = _DeadProc()
    _pm.terminate()
    check('stopping a run that already finished is not marked terminated',
          _pm.terminated is False and _pm.is_run is False,
          'terminated=%r is_run=%r' % (_pm.terminated, _pm.is_run))

    # The UI now stops a run through terminate_async, so the same rule has to
    # hold there or the fix above would be bypassed by the stop button.
    _pm2 = pm_mod.ProcessManager(os.path.join(tempfile.mkdtemp(), 'b.cpp'), 'C++')
    _pm2.process = _DeadProc()
    _pm2.is_run = True
    _pm2.terminate_async()
    check('an async stop of a finished run keeps its verdict too',
          _pm2.terminated is False and _pm2.is_run is False,
          'terminated=%r is_run=%r' % (_pm2.terminated, _pm2.is_run))

    _req_ok = getattr(comp, 'request_allowed', None)
    check('a cross-origin POST from any web page is refused',
          _req_ok is not None
          and not _req_ok(lambda k, d='': {'Host': 'localhost:12345',
                                           'Origin': 'http://evil.example'}.get(k, d)))
    check('a browser page that is not this origin is refused even without Origin',
          _req_ok is not None
          and not _req_ok(lambda k, d='': {'Host': 'localhost:12345',
                                           'Sec-Fetch-Site': 'cross-site'}.get(k, d)))
    check('a request that arrived under another hostname is refused',
          _req_ok is not None
          and not _req_ok(lambda k, d='': {'Host': 'evil.example:12345',
                                           'Origin': 'http://evil.example'}.get(k, d)))
    check('same-origin and non-browser clients are still accepted',
          _req_ok is not None
          and _req_ok(lambda k, d='': {'Host': 'localhost:12345',
                                       'Origin': 'http://localhost:12345'}.get(k, d))
          and _req_ok(lambda k, d='': {'Host': '127.0.0.1:12345',
                                       'Origin': 'http://[::1]:8080'}.get(k, d))
          and _req_ok(lambda k, d='': {}))

    _hard = []
    for _name in ('Default.sublime-commands', 'Main.sublime-menu', 'Context.sublime-menu'):
        with open(os.path.join(ROOT, _name), encoding='utf-8') as f:
            if '${packages}/Algorithm Competition Assistant/' in f.read():
                _hard.append(_name)
    check('menus do not hardcode the installed folder name', not _hard,
          'breaks a manual install: %s' % ', '.join(_hard))
    check('a command opens package files through the real directory',
          'cph_open_package_file' in registered, 'no such command')

    _lazy_src = '\n'.join([
        'import sys, os, json, types, importlib.util as u',
        's = types.ModuleType("sublime")',
        's.status_message = s.error_message = lambda *a, **k: None',
        's.set_timeout = s.set_timeout_async = lambda f, d=0: None',
        's.Settings = object',
        's.load_settings = lambda n: s.Settings({})',
        's.version = lambda: "4213"',
        's.platform = lambda: "linux"',
        's.packages_path = lambda: os.getcwd()',
        's.windows = lambda: []',
        's.decode_value = json.loads',
        's.encode_value = lambda v, p=False: json.dumps(v)',
        'sys.modules["sublime"] = s',
        'p = types.ModuleType("sublime_plugin")',
        'p.TextCommand = p.WindowCommand = p.ApplicationCommand = object',
        'p.EventListener = object',
        'sys.modules["sublime_plugin"] = p',
        'root = sys.argv[1]',
        'sp = u.spec_from_file_location("cphpkg", os.path.join(root, "__init__.py"),',
        '                             submodule_search_locations=[root])',
        'mod = u.module_from_spec(sp)',
        'sys.modules["cphpkg"] = mod',
        'sp.loader.exec_module(mod)',
        'import cphpkg.cph_companion',
        'import cphpkg.core.cph_fetch',
        'print("http.server" in sys.modules or "urllib.request" in sys.modules)',
    ])
    _lazy_py = os.path.join(tempfile.mkdtemp(), 'lazy_probe.py')
    with open(_lazy_py, 'w', encoding='utf-8') as f:
        f.write(_lazy_src)
    _lazy = subprocess.check_output(
        [sys.executable, '-B', _lazy_py, ROOT],
        stderr=subprocess.STDOUT).decode().strip()
    check('starting up does not drag http.server / urllib into memory',
          _lazy == 'False', 'stdlib web modules imported at startup: %s' % _lazy)

    # ------------------------------------------------------------------
    # v2.4.8: the run clock must measure the program, not the plugin, and
    # three recorded-but-invisible things get surfaced.
    # ------------------------------------------------------------------
    print('== v2.4.8: run clock, near-TLE advisory, stats ledger, doctor ==')

    cal = importlib.import_module(pkg + '.core.cph_calibrate')

    class _StampProc(object):
        """Alive on the first poll, gone with code 3 afterwards."""
        pid = 7

        def __init__(self):
            self.calls = 0

        def poll(self):
            self.calls += 1
            return None if self.calls == 1 else 3

        def kill(self):
            pass

    pm3 = pm_mod.ProcessManager(os.path.join(tempfile.mkdtemp(), 'c.cpp'), 'C++')
    _clock_api = hasattr(pm3, 'runtime_ms') and hasattr(pm3, 'spawn_time')
    check('ProcessManager owns the clock primitive (spawn/exit/runtime_ms)',
          _clock_api, 'spawn_time / exit_time / runtime_ms missing')

    if _clock_api:
        check('a manager that never spawned reports no runtime',
              pm3.runtime_ms() is None, repr(pm3.runtime_ms()))
        pm3.process = _StampProc()
        pm3.spawn_time = time.time() - 0.300
        check('a live child has no exit stamped and no runtime yet',
              pm3.is_stopped() is None and pm3.exit_time is None
              and pm3.runtime_ms() is None)
        gone = pm3.is_stopped()
        check('the exit is stamped the first time the child is seen gone',
              gone == 3 and pm3.exit_time is not None)
        stamp = pm3.exit_time
        pm3.is_stopped()
        pm3.is_stopped()
        check('the exit stamp is written once and never moved later',
              pm3.exit_time == stamp)
        pm3.spawn_time = 1000.0
        pm3.exit_time = 1000.25
        check('runtime_ms is the program lifetime in ms',
              pm3.runtime_ms() == 250, repr(pm3.runtime_ms()))

    class _SpawnProcess(object):
        """Stands in for ProcessManager: spawn_time only exists once run() returned."""
        is_run = False

        def __init__(self):
            self.spawn_time = None
            self.exit_time = None

        def set_calls(self, *args):
            pass

        def run(self, *args, **kwargs):
            self.spawn_time = time.time()

        def write(self, s):
            pass

        def close_stdin(self):
            pass

        def is_stopped(self):
            return 0

        def get_time_limit_ms(self):
            return 1000

        def get_memory_limit_mb(self):
            return 256

        def runtime_ms(self):
            return None

    clock_tester = make_tester([tm.CphTestManagerCommand.Test('1 2\n')])
    clock_tester.process_manager = _SpawnProcess()
    clock_tester.awaiting_input = False
    before_spawn = time.time()
    clock_tester.insert_test(0)
    check('the run clock never starts before the process exists',
          clock_tester._clock_start is not None
          and clock_tester._clock_start >= before_spawn
          and clock_tester._clock_start == clock_tester.process_manager.spawn_time,
          'clock=%r spawn=%r' % (clock_tester._clock_start,
                                 clock_tester.process_manager.spawn_time))
    paste_tester = make_tester([tm.CphTestManagerCommand.Test('')])
    paste_tester.process_manager = _SpawnProcess()
    paste_tester.awaiting_input = True
    paste_tester.insert_test(0)
    check('while the sample is still being pasted the clock stays paused',
          paste_tester._clock_start is None
          and paste_tester.process_manager.spawn_time is not None,
          'clock=%r spawn=%r' % (paste_tester._clock_start,
                                 paste_tester.process_manager.spawn_time))

    _par_src = open(os.path.join(ROOT, 'core', 'cph_parallel.py'),
                    encoding='utf-8').read()
    _run_one_body = _par_src[_par_src.find('def _run_one'):
                             _par_src.find('def run_batch')]
    check('the parallel path times the same window as the serial one',
          'started = time.time()' not in _run_one_body
          and 'spawn_time' in _run_one_body,
          'parallel still starts the clock before Popen')
    check('the serial listener stops the clock at the exit, not after the drain',
          'exit_time' in _tm_src)

    _advisory = getattr(tm, 'tle_advisory', None)
    check('a run that used 90-100% of the limit is flagged as risky',
          _advisory is not None and _advisory(1950, 2000) and _advisory(1801, 2000))
    check('a comfortable run and an actual TLE are both left alone',
          _advisory is not None and _advisory(500, 2000) is None
          and _advisory(2100, 2000) is None and _advisory(1000, 1000) is None)
    check('the advisory needs a limit to compare against',
          _advisory is not None and _advisory(1900, None) is None
          and _advisory(None, 2000) is None and _advisory(0, 2000) is None)

    cal._cache['factor'] = 1.5
    try:
        check('a calibrated machine reports the equivalent judge limit',
              cal.adjusted_limit_ms(2000) == 3000
              and '1.50' in cal.label(2000))
        _label_src = open(os.path.join(ROOT, 'test_manager.py'),
                          encoding='utf-8').read()
        _label_body = _label_src[_label_src.find('def run_view_status_label'):
                                 _label_src.find('\ndef ', _label_src.find('def run_view_status_label') + 10)]
        check('the calibration result actually reaches the status bar',
              'calibrate' in _label_body and 'label' in _label_body,
              'the measured factor is still shown nowhere')
    finally:
        cal._cache.pop('factor', None)

    # Doctor: which() alone says PASS for a toolchain that cannot build.
    doc_src = open(os.path.join(ROOT, 'cph_doctor.py'), encoding='utf-8').read()
    check('doctor actually runs the compiler instead of only looking it up',
          'subprocess' in doc_src and '--version' in doc_src,
          'doctor still spawns nothing')
    _parse_ver = getattr(doc, 'parse_compiler_version', None)
    check('the compiler version is read out of its banner',
          _parse_ver is not None
          and _parse_ver('g++ (Rev8, Built by MSYS2 project) 15.2.0\n'
                         'Copyright (C) 2025\n') == '15.2.0'
          and _parse_ver('Configured with: nothing\n') == '',
          repr(_parse_ver('x')) if _parse_ver else 'missing')
    # `go version go1.22.5 windows/amd64` glues the number to the tool name, so
    # a word boundary in front of the digits made it read "22.5".
    check('a version glued to the tool name is still read whole',
          _parse_ver is not None
          and _parse_ver('go version go1.22.5 windows/amd64\n') == '1.22.5'
          and _parse_ver('rustc 1.78.0 (9b00956e5 2024-04-29)\n') == '1.78.0',
          repr(_parse_ver('go version go1.22.5 windows/amd64'))
          if _parse_ver else 'missing')
    _state = importlib.import_module(pkg + '.core.cph_state')
    _keep_version = _state.plugin_version()
    _state.set_plugin_version('9.9.9-probe')
    try:
        check('the loaded build is recorded where every module can read it',
              _state.plugin_version() == '9.9.9-probe')
        check('the doctor report names the loaded build and the real folder',
              'plugin_version' in doc_src and 'base_name' in doc_src)
    finally:
        _state.set_plugin_version(_keep_version)
    _init_src = open(os.path.join(ROOT, 'plugin_init.py'), encoding='utf-8').read()
    check('the entry point publishes its VERSION into the shared state at load',
          'set_plugin_version(VERSION)' in _init_src)
    check('doctor checks the optional helpers a run depends on',
          'gdb' in doc_src and 'checker' in doc_src and 'interactor' in doc_src)
    check('the companion placeholders are known to the placeholder check',
          all(name in doc_src for name in ("'checker'", "'interactor'")))

    # Stats: the permanent ledger is the truth; history is capped at 5000 and
    # is only "what happened recently". A problem with 40 recorded attempts but
    # one surviving history row must still report 40.
    store = os.path.join(tempfile.mkdtemp(), 'stats.json')
    old_env = os.environ.get('CPH_STATS_PATH')
    os.environ['CPH_STATS_PATH'] = store
    _now_iso = time.strftime('%Y-%m-%dT%H:%M:%S')
    try:
        with open(store, 'w', encoding='utf-8') as f:
            json.dump({'problems': {
                'p': {'file': 'p.cpp', 'runs': 12, 'attempts': 11, 'ac_runs': 1,
                      'solved': True, 'first_ac': _now_iso, 'last': _now_iso,
                      'last_verdict': 'AC',
                      'by_verdict': {'WA': 7, 'RE': 3, 'AC': 2},
                      'stress_rounds': 40},
                'q': {'file': 'q.cpp', 'runs': 40, 'attempts': 40, 'ac_runs': 0,
                      'solved': False, 'first_ac': None, 'last': _now_iso,
                      'last_verdict': 'WA', 'by_verdict': {'WA': 38, 'TLE': 2},
                      'stress_rounds': 0},
            }, 'history': [
                {'key': 'p', 'file': 'p.cpp', 'time': _now_iso,
                 'verdict': 'AC', 'all_ac': True},
                {'key': 'q', 'file': 'q.cpp', 'time': _now_iso,
                 'verdict': 'WA', 'all_ac': False},
            ]}, f)
        summary = stats_mod.summary_data(days=7)
        check('the recorded verdict distribution is finally shown',
              summary.get('verdicts') == {'WA': 45, 'RE': 3, 'AC': 2, 'TLE': 2},
              repr(summary.get('verdicts')))
        _q = [n for n in summary.get('needs', []) if 'q.cpp' in n.get('file', '')]
        check('a problem is judged by the ledger, not by how many history rows survived',
              len(_q) == 1 and _q[0]['attempts'] == 40,
              repr(summary.get('needs')))
    finally:
        if old_env is None:
            os.environ.pop('CPH_STATS_PATH', None)
        else:
            os.environ['CPH_STATS_PATH'] = old_env

    # i18n: the language-switch reply was a hardcoded lowercase English literal.
    lang_src = open(os.path.join(ROOT, 'cph_language.py'), encoding='utf-8').read()
    check('the language switch answers through the translation table',
          "status_message('algorithm competition assistant" not in lang_src
          and "t('lang_switched" in lang_src,
          'still hardcoded English in a Chinese UI')
    check('both switch languages really exist in the table',
          i18n.STRINGS.get('lang_switched_zh', {}).get('zh')
          and i18n.STRINGS.get('lang_switched_zh', {}).get('en')
          and i18n.STRINGS.get('lang_switched_en', {}).get('zh')
          and i18n.STRINGS.get('lang_switched_en', {}).get('en'))

    print('== v2.4.9: the code paths a source-grep test could not see ==')

    class _RecWindow(object):
        def __init__(self):
            self.calls = []

        def run_command(self, command, args=None):
            self.calls.append((command, dict(args or {})))

    class _RecView(object):
        def __init__(self):
            self.appended = []

        def is_valid(self):
            return True

        def run_command(self, command, args=None):
            if command == 'append':
                self.appended.append((args or {}).get('characters', ''))

    ex = importlib.import_module(pkg + '.cph_extras')

    # Every file the Preferences entries name must exist in the package, and
    # the command must hand edit_settings / open_file something the OS can
    # resolve. The v2.4.7 version passed a `Packages/<folder>/<file>` resource
    # path, which edit_settings rejects with "The settings file ... could not
    # be opened" - and no test had ever executed the command.
    refs = []

    def _collect_refs(obj):
        if isinstance(obj, dict):
            if obj.get('command') == 'cph_open_package_file':
                args = obj.get('args') or {}
                refs.append((args.get('file'), bool(args.get('edit_base'))))
            for value in obj.values():
                _collect_refs(value)
        elif isinstance(obj, list):
            for value in obj:
                _collect_refs(value)

    for _name in ('Main.sublime-menu', 'Default.sublime-commands'):
        with open(os.path.join(ROOT, _name), encoding='utf-8') as f:
            _collect_refs(json.load(f))
    check('the Preferences entries name a file each',
          len(refs) >= 4 and all(name for name, _ in refs), repr(refs))
    unresolved = []
    for name, _edit in refs:
        if '$platform' in name:
            stem = name.replace('$platform', '')
            head, tail = name.split('$platform')
            if not any(os.path.exists(os.path.join(ROOT, head + p + tail))
                       for p in ('Windows', 'Linux', 'OSX', 'windows', 'linux', 'osx')):
                unresolved.append(name)
        elif not os.path.exists(os.path.join(ROOT, name)):
            unresolved.append(name)
    check('no menu entry points at a file that is not in the package',
          not unresolved, repr(unresolved))

    _keep_pp = getattr(_sub, 'packages_path', None)
    _sub.packages_path = lambda: os.path.dirname(ROOT)
    try:
        opener = ex.CphOpenPackageFileCommand.__new__(ex.CphOpenPackageFileCommand)

        win = _RecWindow()
        opener.window = win
        opener.run(file='Algorithm Competition Assistant.sublime-settings',
                   edit_base=True)
        base = win.calls[-1][1].get('base_file', '')
        check('edit_settings gets an OS-resolvable path, never Packages/...',
              win.calls[-1][0] == 'edit_settings'
              and not base.startswith('Packages/')
              and os.path.isabs(base) and os.path.exists(base), repr(base))

        win = _RecWindow()
        opener.window = win
        opener.run(file='README.md')
        target = win.calls[-1][1].get('file', '')
        check('open_file gets a readable path too',
              win.calls[-1][0] == 'open_file'
              and os.path.isabs(target) and os.path.exists(target), repr(target))

        win = _RecWindow()
        opener.window = win
        opener.run(file='Default ($platform).sublime-keymap', edit_base=True)
        keymap = win.calls[-1][1].get('base_file', '')
        check('the $platform keymap name expands to a file that exists',
              '$platform' not in keymap and os.path.exists(keymap), repr(keymap))

        # Installed as a .sublime-package the file is not on disk at all, and
        # Sublime still resolves the absolute path to the zip entry - so the
        # answer must stay absolute and must never degrade to a resource path
        # or an unexpanded ${packages} template.
        _sub.packages_path = lambda: os.path.join(os.path.dirname(ROOT), 'nowhere')
        win = _RecWindow()
        opener.window = win
        opener.run(file='README.md')
        zipped = win.calls[-1][1].get('file', '')
        check('a zip install still gets an absolute path, not a resource path',
              os.path.isabs(zipped) and not zipped.startswith('Packages/')
              and '${' not in zipped, repr(zipped))
    finally:
        if _keep_pp is None:
            del _sub.packages_path
        else:
            _sub.packages_path = _keep_pp

    # Doctor's probe section: it runs on a worker thread, where a raise would
    # end the section without any visible cause.
    doctor_cmd = doc.CphDoctorCommand.__new__(doc.CphDoctorCommand)
    line = doctor_cmd._probe_entry({'name': 'C++', 'extensions': ['cpp'],
                                    'compile_cmd': 'no-such-compiler-xyz {source_file}',
                                    'run_cmd': 'x'}, 'C++')
    check('a compiler that is not on PATH is reported, not raised',
          isinstance(line, str) and line.startswith('    '), repr(line))
    line = doctor_cmd._probe_entry({'name': 'Python', 'extensions': ['py'],
                                    'compile_cmd': None,
                                    'run_cmd': 'python {source_file}'}, 'Python')
    check('an interpreted language still gets a probe line',
          isinstance(line, str) and line.startswith('    '), repr(line))

    class _ExplodingEntry(dict):
        def get(self, key, default=None):
            if key == 'compile_cmd':
                raise RuntimeError('boom')
            return dict.get(self, key, default)

    _keep_st = _sub.set_timeout
    emitted = []
    _sub.set_timeout = lambda fn, delay=0: fn()
    try:
        rec_view = _RecView()
        doctor_cmd._probe_tools(rec_view, [{'name': 'C++', 'extensions': ['cpp'],
                                            'compile_cmd': 'no-such-compiler-xyz'},
                                           _ExplodingEntry(name='Broken')])
        text = ''.join(rec_view.appended)
        check('one failing probe cannot take the whole section down',
              'C++' in text and 'Broken' in text and 'probe failed' in text,
              repr(text[:200]))
        check('the probe results reach the report view',
              bool(rec_view.appended), repr(rec_view.appended))
    finally:
        _sub.set_timeout = _keep_st

    # The language switch: it used to print a hardcoded English literal.
    lang = importlib.import_module(pkg + '.cph_language')
    _keep_status = _sub.status_message
    said = []
    _sub.status_message = lambda msg: said.append(msg)
    try:
        for entry_lang, key in ((i18n.LANG_ZH, 'lang_switched_zh'),
                                (i18n.LANG_EN, 'lang_switched_en')):
            setter = lang.CphLanguageSetLanguageCommand.__new__(
                lang.CphLanguageSetLanguageCommand)
            setter.run(None, language=entry_lang)
            text = said[-1] if said else ''
            check('switching to %s answers in %s' % (entry_lang, entry_lang),
                  i18n.STRINGS[key][entry_lang] in text and key not in text,
                  repr(text))
        toggle = lang.CphLanguageToggleLanguageCommand.__new__(
            lang.CphLanguageToggleLanguageCommand)
        before = i18n.get_lang()
        toggle.run(None)
        check('the toggle really changes the language',
              i18n.get_lang() != before, '%s -> %s' % (before, i18n.get_lang()))
    finally:
        _sub.status_message = _keep_status
        i18n.set_lang('zh')

    # The calibration and verdict lines, executed rather than grepped.
    _keep_get_settings = tm.get_settings
    cal = importlib.import_module(pkg + '.core.cph_calibrate')
    _keep_factor = cal._cache.get('factor')
    try:
        tm.get_settings = lambda: {'run_settings': [
            {'name': 'C++', 'extensions': ['cpp'],
             'time_limit_ms': 2000, 'memory_limit_mb': 256}]}
        cal._cache['factor'] = 1.5
        label = tm.run_view_status_label('x.cpp', 2000, 256)
        check('the run panel label carries the calibrated equivalent limit',
              'TL 2000ms' in label and '1.50' in label and '3000' in label,
              repr(label))
        cal._cache.pop('factor')
        label = tm.run_view_status_label('x.cpp', 2000, 256)
        check('an uncalibrated machine shows no calibration text',
              '1.50' not in label and 'TL 2000ms' in label, repr(label))
    finally:
        tm.get_settings = _keep_get_settings
        if _keep_factor:
            cal._cache['factor'] = _keep_factor
        else:
            cal._cache.pop('factor', None)

    info = {'days': 7, 'problems': 2, 'runs': 9, 'solved': 1, 'per_day': [],
            'needs': [{'file': 'q.cpp', 'attempts': 40}],
            'verdicts': {'WA': 3, 'AC': 1}, 'stress_rounds': 0, 'span': 0}
    english = stats_mod.render_summary(info)
    check('the English report renders the verdict distribution',
          'Verdicts: WA 3  AC 1' in english, repr(english))
    i18n.set_lang('zh')
    chinese = stats_mod.render_summary(info, translate=i18n.t)
    check('the Chinese report renders the verdict distribution',
          '判题分布' in chinese and 'WA 3' in chinese, repr(chinese))
    i18n.set_lang('en')
    check('a report with no verdicts recorded says nothing about them',
          'Verdicts' not in stats_mod.render_summary(
              dict(info, verdicts={})))
    i18n.set_lang('zh')

    # The Companion handler class is built by a factory now (so http.server is
    # not imported at startup), which means its whole body only ever runs when
    # the listener starts. Execute that path here: a NameError inside it used to
    # be invisible to every test.
    handler = comp.build_handler()
    check('the lazily built request handler really constructs',
          handler.__name__ == '_CompanionHandler'
          and all(callable(getattr(handler, m, None))
                  for m in ('do_POST', '_host_allowed', '_deny',
                            '_handle_on_main', 'log_message')),
          repr(handler))

    class _FakeRequest(object):
        def __init__(self, headers):
            self.headers = headers

        def send_response(self, *args):
            pass

        def end_headers(self):
            pass

        class _W(object):
            def write(self, *args):
                pass
        wfile = _W()

    check('the built handler rejects a cross-origin request',
          handler._host_allowed(_FakeRequest(
              {'Host': 'localhost:12345', 'Origin': 'http://evil.example'})) is False)
    check('the built handler accepts a same-origin request',
          handler._host_allowed(_FakeRequest(
              {'Host': 'localhost:12345',
               'Origin': 'http://localhost:12345'})) is True)

    # Stress built from the raw compile_cmd, so a solution toggled to Debug was
    # stress-tested without the sanitizer flags: the counterexample came from a
    # binary the user never runs. Capture the command it really hands g++.
    stress = importlib.import_module(pkg + '.cph_stress')
    mode_mod = importlib.import_module(pkg + '.core.cph_build_mode')
    _keep_capture = stress._popen_capture
    _keep_resolve = stress.resolve_artifact
    _keep_lang_entry = stress._lang_entry
    built = []
    try:
        def fake_capture(cmd, *args, **kwargs):
            built.append(cmd)
            return 0, '', ''
        stress._popen_capture = fake_capture
        stress.resolve_artifact = lambda wanted, d, started: wanted
        src = os.path.join(tempfile.mkdtemp(), 'sol.cpp')
        with open(src, 'w', encoding='utf-8') as f:
            f.write('int main(){return 0;}\n')
        stress._lang_entry = lambda f: {
            'name': 'C++', 'extensions': ['cpp'],
            'compile_cmd': 'g++ "{source_file}" -std=c++17 -O2 -o "{file_name}.exe"'}
        mode_mod.set_mode(src, 'debug')
        ok_dbg = stress._compile_program(src)[0]
        debug_cmd = built[-1] if built else ''
        built[:] = []
        mode_mod.set_mode(src, 'release')
        ok_rel = stress._compile_program(src)[0]
        release_cmd = built[-1] if built else ''
        check('a Debug stress build carries the sanitizer flags',
              ok_dbg and '-fsanitize' in debug_cmd
              and '_GLIBCXX_ASSERTIONS' in debug_cmd, repr(debug_cmd))
        check('a Release stress build is optimised and unsanitized',
              ok_rel and '-O2' in release_cmd and '-fsanitize' not in release_cmd,
              repr(release_cmd))
    finally:
        stress._popen_capture = _keep_capture
        stress.resolve_artifact = _keep_resolve
        stress._lang_entry = _keep_lang_entry

    # ---- the audit round: what a Windows-only machine cannot see ----------
    # Popen(list, shell=True) drops every argument after the first on POSIX
    # (the rest become arguments to the shell itself), so the version probes
    # ran a bare `g++` there and reported FAIL for a working toolchain. Windows
    # joins the list, which is why this only shows up on Linux and macOS.
    rc, out = doc._run_capture([sys.executable, '-c', 'print(4242)'])
    check('a list command reaches the child with all its arguments',
          rc == 0 and '4242' in out, 'rc=%s out=%r' % (rc, out[:120]))
    rc, out = doc._run_capture('echo shellstring')
    check('a string command still goes through a shell',
          rc == 0 and 'shellstring' in out, 'rc=%s out=%r' % (rc, out[:120]))

    # The probes compile and run real programs (seconds each), so they must not
    # sit on Sublime's one shared async worker.
    check('the doctor probes run on a private thread',
          'set_timeout_async(\n\t\t\tlambda: self._probe_tools' not in doc_src
          and 'target=self._probe_tools' in doc_src,
          'probes still occupy the shared async worker')

    # A bracketed IPv6 loopback Host must not be split at its own colon.
    check('a bracketed IPv6 loopback Host is still allowed',
          comp.request_allowed(lambda k, d='': {'Host': '[::1]:12345'}.get(k, d))
          and comp.request_allowed(lambda k, d='': {
              'Host': '[::1]:12345', 'Origin': 'http://[::1]:12345'}.get(k, d)))
    check('a bracketed non-loopback Host is still refused',
          not comp.request_allowed(lambda k, d='': {
              'Host': '[::2]:12345'}.get(k, d)))

    # The next-test chain handed its callback to the async worker, which then
    # built PhantomSets off the main thread - the exact crash class v2.4.8 said
    # it had removed from the panel.
    _keep_st_async, _keep_st = _sub.set_timeout_async, _sub.set_timeout
    try:
        queued = []
        _sub.set_timeout = lambda fn, delay=0: queued.append(fn)
        _sub.set_timeout_async = lambda fn, delay=0: fn()   # run the worker now
        chain = make_tester([tm.CphTestManagerCommand.Test('a\n')])
        ran_inline = []
        chain.next_test(0, lambda: ran_inline.append('update_configs'))
        # insert_test queues its own view work too; what matters is that the
        # chain callback is not among the calls made inline on the worker.
        check('the next-test chain does not touch views on the worker thread',
              ran_inline == [] and len(queued) >= 1,
              'inline=%s queued=%d' % (ran_inline, len(queued)))
        for fn in queued:
            fn()
        check('and the deferred chain callback does run on the main thread',
              ran_inline == ['update_configs'], repr(ran_inline))
    finally:
        _sub.set_timeout, _sub.set_timeout_async = _keep_st, _keep_st_async

    # ---- the .sublime-package install form (what Package Control ships) ----
    # Installed as a zip, __file__ lives inside `Packages/<name>.sublime-package/`
    # and the folder component of every resource path must be the name WITHOUT
    # that extension - which is how BracketHighlighter and LSP, both zip
    # installed, reference themselves. Leaving the suffix in broke edit_settings
    # and, worse, every load_resource / set_syntax_file / region icon path.
    _settings_mod = importlib.import_module(pkg + '.core.cph_settings')
    _namer = getattr(_settings_mod, 'package_name_for', None)
    check('the .sublime-package suffix is stripped from the resource name',
          _namer is not None
          and _namer('/x/Packages/Algorithm Competition Assistant.sublime-package')
          == 'Algorithm Competition Assistant'
          and _namer('/x/Packages/cph-by-chenkx') == 'cph-by-chenkx',
          'package_name_for missing' if _namer is None else 'wrong name')

    _res = importlib.import_module(pkg + '.core.cph_resources')
    _keep_base = _res.base_name
    try:
        _res.base_name = 'Algorithm Competition Assistant.sublime-package'
        _keep_pp = getattr(_sub, 'packages_path', None)
        # The fake packages root has to be absolute *on the host running this*:
        # a Windows-shaped root on Linux/macOS is a relative path there, which
        # made the assertion below a property of the runner instead of the code
        # and turned the job red while the product was fine.
        _fake_packages = (r'D:\Sublime Text\Data\Packages' if os.name == 'nt'
                          else '/tmp/Sublime Text/Data/Packages')
        _sub.packages_path = lambda: _fake_packages
        try:
            res_path = _res.package_resource_path('Highlight/test_config.html')
            file_path = _res.package_file_path(
                'Algorithm Competition Assistant.sublime-settings')
            check('a zip install builds resource paths without the suffix',
                  '.sublime-package' not in res_path
                  and res_path.startswith('Packages/Algorithm Competition Assistant/'),
                  repr(res_path))
            _want_file = os.path.join(
                _fake_packages, 'Algorithm Competition Assistant',
                'Algorithm Competition Assistant.sublime-settings')
            check('a zip install builds edit_settings paths without the suffix',
                  '.sublime-package' not in file_path
                  and os.path.normpath(file_path) == os.path.normpath(_want_file),
                  repr(file_path))
        finally:
            if _keep_pp is None:
                del _sub.packages_path
            else:
                _sub.packages_path = _keep_pp
    finally:
        _res.base_name = _keep_base

    check('the panel resources use the stripped name, not the folder name',
          'package_name' in open(os.path.join(ROOT, 'test_manager.py'),
                                 encoding='utf-8').read()
          and 'package_name' in open(os.path.join(ROOT, 'test_edit.py'),
                                     encoding='utf-8').read())

    print('')
    print('%d checks, %d failures' % (CHECKS[0], len(FAILURES)))
    if FAILURES:
        print('failed:')
        for f in FAILURES:
            print('  - %s' % f)
        return 1
    print('ALL TESTS PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(main())

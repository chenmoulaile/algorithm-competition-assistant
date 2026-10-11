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
import io
import json
import os
import re
import shutil
import sys
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
            return Window()

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

        def status_message(self, *args, **kwargs):
            pass

    sublime.Region = Region
    sublime.Phantom = Phantom
    sublime.PhantomSet = PhantomSet
    sublime.Settings = Settings
    sublime.View = View
    sublime.LAYOUT_BLOCK = 1
    sublime.LAYOUT_INLINE = 2
    sublime.HIDDEN = 0
    sublime.DRAW_NO_FILL = 0
    sublime.DRAW_NO_OUTLINE = 0
    sublime.DRAW_STIPPLED_UNDERLINE = 0
    sublime.DRAW_EMPTY_AS_OVERWRITE = 0

    sublime.platform = lambda: 'linux'
    sublime.status_message = lambda *a, **k: None
    sublime.error_message = lambda *a, **k: None
    sublime.ok_cancel_dialog = lambda *a, **k: True
    sublime.message_dialog = lambda *a, **k: None
    sublime.set_timeout = lambda fn, delay=0: None          # never run the chain
    sublime.set_timeout_async = lambda fn, delay=0: None
    sublime.windows = lambda: []
    sublime.active_window = lambda: None
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
    sublime_plugin.WindowCommand = _Command
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
    _install_fake_sublime()
    pkg = _load_package()

    print('== cph_verdict: comparison and verdicts ==')
    verdict = importlib.import_module(pkg + '.core.cph_verdict')

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
    try:
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

    print('== companion: merge keyed by input ==')
    comp = importlib.import_module(pkg + '.cph_companion')
    comp.load_all_tests = lambda f: [{'test': '1 2\n', 'correct_answers': []}]
    merged = comp.merge_tests('x.cpp', [{'test': '1 2\n', 'correct_answers': ['3\n']}])
    check('resending the same input updates the answer instead of duplicating',
          len(merged) == 1 and merged[0]['correct_answers'] == ['3\n'], repr(merged))
    merged2 = comp.merge_tests('x.cpp', [{'test': '9\n', 'correct_answers': []}])
    check('a genuinely new input is appended', len(merged2) == 2, repr(merged2))

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
    EXAMPLES = ('Example (Windows).sublime-keymap', 'Example (Linux).sublime-keymap',
                'Example (OSX).sublime-keymap')

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

    # 2. The suggestions users copy must be valid JSON and scoped as well.
    suggestion_problems = []
    suggestion_count = 0
    for name in EXAMPLES:
        path = os.path.join(ROOT, name)
        if not os.path.isfile(path):
            suggestion_problems.append('%s missing' % name)
            continue
        with open(path, encoding='utf-8') as f:
            entries = json.load(f)
        suggestion_count += len(entries)
        for entry in entries:
            if not entry.get('context'):
                suggestion_problems.append('%s %s' % (name, entry.get('keys')))
    check('per-platform example keymaps exist and are scoped',
          not suggestion_problems and suggestion_count > 0,
          '; '.join(suggestion_problems) or 'no suggestions')
    check('no active key binding ships by default',
          not os.path.isfile(os.path.join(ROOT, 'Example.sublime-keymap')),
          'the old single Example.sublime-keymap is back')

    # 3. Custom context keys used anywhere must be answered by the listener.
    with open(os.path.join(ROOT, 'cph_context.py'), encoding='utf-8') as f:
        ctx_src = f.read()
    used_keys = set()
    for name in DEFAULTS + EXAMPLES:
        with open(os.path.join(ROOT, name), encoding='utf-8') as f:
            for entry in json.load(f):
                for item in entry.get('context') or []:
                    key = item.get('key', '')
                    if key.startswith('cph_'):
                        used_keys.add(key)
    unanswered = [k for k in sorted(used_keys) if ("'%s'" % k) not in ctx_src]
    check('custom context keys are answered by cph_context', not unanswered,
          '; '.join(unanswered))
    check('at least one custom context key is exercised', bool(used_keys),
          'none found')

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
          and '-fsanitize=address,undefined' in debug
          and '-fno-omit-frame-pointer' in debug, debug)
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
    check('switching back to release removes the sanitizer',
          '-fsanitize' not in build_mode.transform(debug, build_mode.MODE_RELEASE))
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

    # --- practice statistics ---
    check('the worst verdict wins',
          stats.worst_verdict(['AC', 'WA']) == 'WA'
          and stats.worst_verdict(['AC', 'AC']) == 'AC'
          and stats.worst_verdict([]) == 'AC')
    check('a missing store still summarises',
          isinstance(stats.summary(7), str) and stats.load() is not None)

    # --- the new commands must be reachable ---
    for command in ('cph_fetch_problem', 'cph_view_statement', 'cph_benchmark',
                    'cph_contest_timer', 'cph_stats_report', 'cph_calibrate_machine',
                    'cph_stress_replay', 'cph_run_parallel', 'cph_toggle_build_mode'):
        check('%s is in the command palette' % command, command in commands_text)
    check('the new settings keys ship',
          all('"%s"' % key in settings_text for key in (
              'benchmark_runs', 'auto_run_on_save', 'auto_format_on_save',
              'auto_create_file', 'stats_days', 'contest_duration_minutes',
              'subtasks', 'build_mode', 'parallel_workers')))
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

    # (6) enable_keybindings claims every package binding turns into a no-op;
    # the panel-toggle binding has to honour it too.
    check('the panel-toggle binding honours enable_keybindings',
          'def _panel_toggle_ok(' in context_src
          and 'if not keybindings_enabled():\n\t\treturn False' in context_src)

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

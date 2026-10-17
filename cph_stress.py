"""
Algorithm Competition Assistant - 对拍 (Stress Test) 功能
"""

import sublime
import sublime_plugin
import os
import random
import re
import shlex
import subprocess
import tempfile
import threading
import time

from .core.cph_settings import (get_settings, load_all_tests, save_tests,
                                get_stress_files, set_stress_files,
                                PACKAGE_NAME, settings_file)
from .core.cph_stats import record_stress
from .core.cph_tests_merge import merge_tests
from .core.cph_state import (set_stress_running, register_stress_view,
                             unregister_stress_view)
from .core.cph_target import context_menu_enabled, visible as context_menu_visible
from .core.cph_i18n import t
from .core.cph_verdict import normalize_lines, outputs_equal
from .core.cph_build_mode import (get_mode as _build_mode,
                                  transform as _apply_build_mode)
from .core import cph_checker
from .Modules.ProcessManager import _LenientFormat
from .Modules.build_artifact import (output_path_from_compile_cmd,
                                     isolate_output, resolve_artifact,
                                     retarget_path, print_safe)


class _Aborted(Exception):
    """Raised when the user stops a stress test mid-round."""


# One session per stress run, keyed by the id of its output view. Several
# files can be stress tested at the same time; each gets its own page, its
# own scratch build directory and its own Stop.
_sessions = {}


def _sync_stress_state():
    """Mirror 'any session running' into the shared state (keybindings)."""
    set_stress_running(any(s['running'] for s in _sessions.values()))


def _new_session(user_file, std_file, gen_file, time_limit, max_rounds,
                 view_id=None):
    sess = {
        'running': True,
        'stop_requested': False,
        'std_file': std_file,
        'generator_file': gen_file,
        'user_file': user_file,
        'time_limit': time_limit,
        'max_rounds': max_rounds,
        'current_round': 0,
        'current_seed': None,
        # The program running right now, so Stop can kill it instead of
        # waiting out its time limit.
        'current_proc': None,
        # Scratch directory the stress binaries are built into.
        'build_root': None,
        'build_seq': 0,
        'last_diff': None,
        'last_counterexample': None,
        'stress_view_id': view_id,
    }
    if view_id is not None:
        _sessions[view_id] = sess
        register_stress_view(view_id, user_file=user_file)
    _sync_stress_state()
    return sess


def _running_for(user_file):
    """The session currently stress testing this exact file, or None."""
    key = os.path.normcase(user_file or '')
    for sess in _sessions.values():
        if sess['running'] and                 os.path.normcase(sess.get('user_file') or '') == key:
            return sess
    return None


def _running_sessions():
    return [s for s in _sessions.values() if s['running']]


def _session_for_view(view):
    """The session this view belongs to: its own page, or its source file."""
    sess = _sessions.get(view.id())
    if sess is not None:
        return sess
    file_name = view.file_name()
    if file_name:
        key = os.path.normcase(file_name)
        for sess in _sessions.values():
            if os.path.normcase(sess.get('user_file') or '') == key:
                return sess
    return None


#: Hints for the two programs a stress test needs, matched against the stem
#: of every source file next to the file under test.
_STD_HINTS = ('std', 'brute', 'bf', 'baoli', 'sol', 'standard', 'correct',
              'right', '暴力')
_GEN_HINTS = ('gen', 'data', 'maker', 'make', 'rand', 'random', 'generator',
              'shuju', '生成', '数据')

_FALLBACK_EXTS = ('cpp', 'cc', 'cxx', 'c', 'py', 'java', 'js', 'mjs', 'pas',
                  'go', 'rs')


def _source_extensions():
    """Every source extension the package knows how to run."""
    exts = set(_FALLBACK_EXTS)
    try:
        for entry in (get_settings().get('run_settings') or []):
            for ext in (entry.get('extensions') or []):
                exts.add(str(ext).lower().lstrip('.'))
    except Exception:
        pass
    return exts


def _candidate_files(directory, kind, exclude=()):
    """Files in `directory` that look like the std / generator program.

    Typing the path of std.cpp and gen.cpp by hand for every run was the
    slowest part of setting a stress test up, and both files almost always
    sit right next to the file under test.
    """
    hints = _STD_HINTS if kind == 'std' else _GEN_HINTS
    exts = _source_extensions()
    skip = set()
    for item in (exclude or ()):
        if item:
            skip.add(os.path.normcase(os.path.abspath(item)))
    hits = []
    try:
        names = sorted(os.listdir(directory))
    except Exception:
        return hits
    for name in names:
        full = os.path.join(directory, name)
        if not os.path.isfile(full):
            continue
        if os.path.splitext(name)[1].lower().lstrip('.') not in exts:
            continue
        if os.path.normcase(os.path.abspath(full)) in skip:
            continue
        stem = os.path.splitext(name)[0].lower()
        for hint in hints:
            if hint in stem:
                hits.append(full)
                break
    # `std.cpp` before `std_old.cpp`: the shortest matching name is the one
    # that was meant.
    hits.sort(key=lambda p: (len(os.path.basename(p)),
                             os.path.basename(p).lower()))
    return hits


def _remembered_file(settings, key):
    """A path from an earlier run, when it still exists."""
    path = settings.get(key) or ''
    if path and os.path.exists(path):
        return [path]
    return []


def _same_program(first, second):
    """True when both paths are the one and the same file.

    Windows answers the same file under a different drive letter case or
    spelling, so both sides go through normcase + abspath before comparing.
    """
    if not first or not second:
        return False
    try:
        return (os.path.normcase(os.path.abspath(first))
                == os.path.normcase(os.path.abspath(second)))
    except Exception:
        return False


#: How the two programs a stress test needs are found.
_FILES_SAME_DIR = 'same_dir'
_FILES_PICK_ONCE = 'pick_once'
_FILES_AUTO = 'auto'


def _files_mode(settings):
    """same_dir: only beside the source. pick_once: ask once per file, then
    remember. auto: the old search that also falls back to the global pair."""
    mode = str((settings or {}).get('stress_file_source', _FILES_PICK_ONCE)
               or '').strip().lower()
    return mode if mode in (_FILES_SAME_DIR, _FILES_PICK_ONCE, _FILES_AUTO) \
        else _FILES_PICK_ONCE


def _same_dir_pair(src_dir, user_file):
    """The std and the generator sitting next to the file under test.

    The first hit wins rather than "exactly one": _candidate_files sorts by
    shortest name, so `std.cpp` is preferred over `std_old.cpp` - the naming
    convention the whole package already relies on. Which two files were picked
    is printed in the status bar, so a folder with several candidates is not a
    silent choice.
    """
    std_hits = _candidate_files(src_dir, 'std', exclude=[user_file])
    gen_hits = _candidate_files(src_dir, 'gen', exclude=[user_file] + std_hits)
    return ((std_hits[0] if std_hits else None),
            (gen_hits[0] if gen_hits else None))


def _same_dir_miss_text(src_dir):
    return t('stress_same_dir_missing', dir=src_dir)


def _ask_pair(window, src_dir, std_hits, gen_hits, on_begin, ask):
    """Ask for std first and the generator second, with each step labelled.

    An empty candidate list is not a dead end: _pick_file then opens the
    system dialog, starting in the folder of the file under test.
    """
    def pick_gen(std_file):
        _pick_file(window, src_dir, t('choose_generator_file'),
                   [g for g in gen_hits
                    if os.path.normcase(g) != os.path.normcase(std_file)],
                   lambda gen_file: on_begin(std_file, gen_file), ask=ask)

    _pick_file(window, src_dir, t('choose_std_file'), std_hits, pick_gen, ask=ask)


_PS_OPEN_FILE = """
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Windows.Forms
# The dialog looked soft and low-resolution on a scaled display because
# powershell.exe is not DPI-aware: Windows renders the whole window at 100%
# and stretches the bitmap. Declaring per-monitor awareness before any window
# exists makes the common dialog draw at the panel's native resolution.
# Every step is optional - on a host without the compiler or the API the
# dialog still opens, just as soft as before.
Add-Type -Namespace 'Native' -Name 'Dpi' -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
[DllImport("shcore.dll")] public static extern int SetProcessDpiAwareness(int awareness);
[DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
'@
try {{ [void][Native.Dpi]::SetProcessDpiAwarenessContext([IntPtr](-4)) }} catch {{}}
try {{ [void][Native.Dpi]::SetProcessDpiAwareness(4) }} catch {{}}
try {{ [void][Native.Dpi]::SetProcessDPIAware() }} catch {{}}
$dir = [System.Text.Encoding]::UTF8.GetString(
    [System.Convert]::FromBase64String('{dir}'))
$title = [System.Text.Encoding]::UTF8.GetString(
    [System.Convert]::FromBase64String('{title}'))
$dlg = New-Object System.Windows.Forms.OpenFileDialog
if ($dir) {{ $dlg.InitialDirectory = $dir }}
$dlg.Title = $title
$dlg.CheckFileExists = $true
$dlg.Filter = 'Source files (*.cpp;*.cc;*.cxx;*.c;*.py;*.java;*.js)|*.cpp;*.cc;*.cxx;*.c;*.py;*.java;*.js|All files (*.*)|*.*'
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.WindowState = 'Minimized'
$owner.ShowInTaskbar = $false
$owner.Show()
$res = $dlg.ShowDialog($owner)
$owner.Dispose()
if ($res -eq [System.Windows.Forms.DialogResult]::OK) {{
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    Write-Output $dlg.FileName
}}
"""


def _native_pick(window, start_dir, title, on_done, on_fallback):
    """The system file dialog, opened in the source file's directory.

    Runs on a worker thread: the dialog can stay open for as long as the user
    likes, and Sublime must not freeze while it does.
    """
    if sublime.platform() != 'windows':
        return False
    import base64
    try:
        script = _PS_OPEN_FILE.format(
            dir=base64.b64encode(start_dir.encode('utf-8')).decode('ascii'),
            title=base64.b64encode(title.encode('utf-8')).decode('ascii'))
        encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    except Exception:
        return False
    argv = ['powershell', '-NoProfile', '-STA', '-EncodedCommand', encoded]

    def worker():
        picked = ''
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    startupinfo=_hidden_startupinfo())
            out, _err = proc.communicate()
            text = (out or b'').decode('utf-8', 'replace').strip()
            if text:
                picked = text.splitlines()[0].strip()
        except Exception:
            picked = ''

        def finish():
            if picked and os.path.exists(picked):
                on_done(picked)
            else:
                on_fallback()
        sublime.set_timeout(finish, 0)

    thread = threading.Thread(target=worker)
    thread.daemon = True
    thread.start()
    return True


def _titled_panel(window, title, items, on_select):
    """A quick panel that states which file it is asking for.

    Sublime gives a quick panel no header, and the stress flow opens two of
    them in a row (std, then the generator), so the list of file names looked
    identical either way. The question is put in as the first row: choosing it
    re-opens the same panel instead of picking anything.
    """
    rows = [title] + list(items)

    def select(index):
        if index < 0:
            return
        if index == 0:
            _titled_panel(window, title, items, on_select)
            return
        on_select(index - 1)

    window.show_quick_panel(rows, select)


def _browse_panel(window, directory, title, on_done):
    """Walk the file system with a quick panel, starting at `directory`."""
    exts = _source_extensions()

    def show(path):
        dirs = []
        files = []
        try:
            names = sorted(os.listdir(path))
        except Exception:
            names = []
        for name in names:
            full = os.path.join(path, name)
            if os.path.isdir(full):
                dirs.append(full)
            elif os.path.splitext(name)[1].lower().lstrip('.') in exts:
                files.append(full)
        entries = list(dirs) + list(files)
        items = [t('stress_browse_up')]
        items += [os.path.basename(d) + '/' for d in dirs]
        items += [os.path.basename(f) for f in files]

        def on_select(index):
            if index < 0 or index >= len(items):
                return
            target = None if index == 0 else entries[index - 1]
            if target is None:
                parent = os.path.dirname(path.rstrip(os.sep))
                show(parent or path)
            elif os.path.isdir(target):
                show(target)
            else:
                on_done(target)

        _titled_panel(window, title, items, on_select)

    show(directory)


def _path_panel(window, start_dir, title, on_done):
    """Type a path into a Sublime input panel, pre-filled with the directory.

    The panel opens holding the directory of the file under test (plus its
    trailing separator), so using a differently named std / generator - or
    rotating several - is a matter of typing the file name. Typing a folder
    name and pressing enter moves the panel into that folder; Esc cancels.
    """
    initial = start_dir or ''
    if initial:
        initial = os.path.abspath(initial)
        if os.path.basename(initial):
            # A drive root ("C:\") already ends with its separator.
            initial += os.sep

    def accept(value):
        path = (value or '').strip().strip('"').strip("'")
        if not path:
            # Nothing typed: reopen the panel instead of failing silently.
            _path_panel(window, start_dir, title, on_done)
            return
        if not os.path.isabs(path) and start_dir:
            path = os.path.join(start_dir, path)
        path = os.path.expanduser(path)
        try:
            path = os.path.abspath(path)
            is_dir = os.path.isdir(path)
            exists = os.path.exists(path)
        except Exception:
            is_dir = exists = False
        if is_dir:
            # Stepping into a typed folder: reopen the panel inside it.
            _path_panel(window, path, title, on_done)
        elif exists:
            on_done(path)
        else:
            sublime.error_message(t('file_not_found') + ': ' + path)
            # Reopen with the same starting point so a typo can be fixed
            # without going through the whole flow again.
            _path_panel(window, start_dir, title, on_done)

    # The callback must not be named `on_done`: that name would shadow the
    # parameter and turn the `on_done(path)` call into infinite recursion.
    window.show_input_panel(title, initial, accept, None, None)


def _typed_or_browse(window, start_dir, title, on_done):
    """The system dialog was cancelled (or there is none): offer both ways.

    The typed-path panel comes first: it starts at the source directory, so
    picking any file is one word of typing rather than a dialog hunt.
    """
    items = [t('stress_type_path'), t('stress_browse_more')]

    def on_select(index):
        if index == 0:
            _path_panel(window, start_dir, title, on_done)
        elif index == 1:
            _browse_panel(window, start_dir, title, on_done)

    _titled_panel(window, title, items, on_select)


def _open_file_dialog(window, start_dir, title, on_done):
    """Pick a file: the system dialog where there is one, a panel otherwise.

    Every path also reaches the typed-path panel, pre-filled with the source
    directory; "panel" skips the system dialog and starts there right away.
    """
    mode = str(get_settings().get('stress_file_picker', 'auto') or 'auto').lower()

    # A modal system dialog covers the Sublime window, so the status bar and
    # the console repeat what the caption asks. Two dialogs in a row used to
    # be indistinguishable from behind them.
    sublime.status_message('Algorithm Competition Assistant: ' + title)
    print('[Algorithm Competition Assistant] %s' % title)

    def fallback():
        _typed_or_browse(window, start_dir, title, on_done)

    if mode == 'panel':
        _path_panel(window, start_dir, title, on_done)
        return
    if mode in ('auto', 'native'):
        if _native_pick(window, start_dir, title, on_done, fallback):
            return
    fallback()


def _pick_file(window, start_dir, title, candidates, on_done, ask=False):
    """One file: an exact hit is used silently, otherwise ask.

    `ask` (the "with options" flow) shows the choice even for a single hit,
    with a typed-path entry for any file name the naming hints do not know.
    """
    if len(candidates) == 1 and not ask:
        on_done(candidates[0])
        return
    if candidates:
        items = [os.path.basename(c) for c in candidates]
        items.append(t('stress_type_path'))
        items.append(t('stress_browse_more'))

        def on_select(index):
            if index < 0 or index >= len(items):
                return
            if index < len(candidates):
                on_done(candidates[index])
            elif index == len(candidates):
                _path_panel(window, start_dir, title, on_done)
            else:
                _open_file_dialog(window, start_dir, title, on_done)
        _titled_panel(window, title, items, on_select)
        return
    _open_file_dialog(window, start_dir, title, on_done)


def _open_stress_view(view, user_file):
    """Open (or reuse) the stress page of this exact source file.

    The page is matched by its full name '<basename> -stress', so starting a
    stress test for another file opens another page instead of hijacking the
    one that is already open - several files can be tested at once.
    """
    window = view.window()
    wanted = os.path.basename(user_file) + ' -stress'
    stress_view = None
    for v in window.views():
        if v.name() == wanted:
            stress_view = v
            break

    if stress_view is None:
        stress_view = window.new_file()
        stress_view.set_name(wanted)
        stress_view.set_scratch(True)
        stress_view.run_command('set_setting', {'setting': 'word_wrap', 'value': True})
        stress_view.run_command('set_setting', {'setting': 'fold_buttons', 'value': False})
        stress_view.run_command('set_setting', {'setting': 'line_numbers', 'value': False})

    window.focus_view(stress_view)
    stress_view.run_command('select_all')
    stress_view.run_command('left_delete')
    return stress_view


def _launch(view, user_file, std_file, gen_file, time_limit, max_rounds):
    window = view.window()
    if window is None:
        return
    stress_view = _open_stress_view(view, user_file)
    sess = _new_session(user_file, std_file, gen_file, time_limit, max_rounds,
                        view_id=stress_view.id())

    sublime.status_message(t('stress_running'))

    worker = threading.Thread(target=_run_stress_loop, args=(sess,))
    worker.daemon = True
    worker.start()


class CphStartStressTestCommand(sublime_plugin.TextCommand):
    def is_visible(self, event=None, **kwargs):
        """Context menu: only offered where stress testing makes sense."""
        return context_menu_visible(self.view, event)

    def run(self, edit, choose=False):
        _start_flow(self.view, choose)


class CphStressWithOptionsCommand(sublime_plugin.TextCommand):
    """Stress test with the files and the limits chosen by hand.

    The plain command picks std/gen next to the file under test and uses the
    configured limits; this one always asks, for the times the guess is wrong
    or the round count has to change.
    """

    def is_visible(self, event=None, **kwargs):
        if event is not None and not context_menu_enabled():
            return False
        return True

    def run(self, edit):
        _start_flow(self.view, True)


def _start_flow(view, choose=False):
    user_file = view.file_name()
    if not user_file:
        # The stress page is a scratch view and it takes focus when a run
        # starts, so pressing the key again used to answer "save the file
        # first" instead of restarting the test of its own file.
        sess = _sessions.get(view.id())
        if sess and sess.get('user_file') and os.path.exists(sess['user_file']):
            user_file = sess['user_file']
    if not user_file:
        sublime.error_message(t('save_file_first'))
        return

    # Re-entrancy guard, per file: a second Start for the same file used to
    # silently overwrite the state of the loop that is still running. Other
    # files are free to start their own run next to it.
    if _running_for(user_file):
        sublime.status_message('Algorithm Competition Assistant: ' + t('stress_already_running'))
        return

    window = view.window()
    if window is None:
        return
    settings = sublime.load_settings(settings_file)
    src_dir = os.path.dirname(user_file)

    # Where std and the generator come from. `pick_once` asks the first time
    # for this file and remembers the answer beside its test data; `same_dir`
    # never asks and only accepts a pair found next to the source; `auto` is
    # the old search (folder, then what was used last time, then ask).
    mode = _files_mode(get_settings())
    src_dir = os.path.dirname(user_file)
    remembered = get_stress_files(user_file) if mode == _FILES_PICK_ONCE \
        else (None, None)

    def begin(std_file, gen_file):
        if not std_file or not os.path.exists(std_file):
            sublime.error_message(t('file_not_found') + ': ' + str(std_file))
            return
        if not gen_file or not os.path.exists(gen_file):
            sublime.error_message(t('file_not_found') + ': ' + str(gen_file))
            return
        if _same_program(std_file, gen_file):
            # Comparing a program with itself passes every round, which reads
            # like a clean result and means nothing.
            sublime.error_message(t('stress_same_file',
                                    file=os.path.basename(std_file)))
            _start_flow(view, True)
            return
        settings.set('stress_std_file', std_file)
        settings.set('stress_generator_file', gen_file)
        sublime.save_settings(settings_file)
        # per problem file: the next stress run of THIS file asks nothing,
        # while a different problem in the same folder keeps its own pair
        set_stress_files(user_file, std_file, gen_file)

        try:
            time_limit = float(get_settings().get('stress_time_limit_seconds', 2) or 2)
        except (TypeError, ValueError):
            time_limit = 2.0
        try:
            max_rounds = int(get_settings().get('stress_max_rounds', 1000) or 1000)
        except (TypeError, ValueError):
            max_rounds = 1000

        if choose:
            ask_limits(window, user_file, std_file, gen_file, time_limit,
                       max_rounds, view)
            return
        sublime.status_message('%s: %s / %s'
                               % (PACKAGE_NAME,
                                  os.path.basename(std_file),
                                  os.path.basename(gen_file)))
        _launch(view, user_file, std_file, gen_file, time_limit, max_rounds)

    if remembered[0] and remembered[1]:
        begin(remembered[0], remembered[1])
        return

    if mode == _FILES_SAME_DIR:
        std_file, gen_file = _same_dir_pair(src_dir, user_file)
        if std_file and gen_file:
            begin(std_file, gen_file)
            return
        # Nothing to fall back to without asking, and asking is exactly what
        # this mode refuses - so say where it looked, then let the user point
        # at the files instead of leaving a key press with no way forward.
        sublime.error_message(_same_dir_miss_text(src_dir))
        _ask_pair(window, src_dir, [], [], begin, True)
        return

    if mode == _FILES_PICK_ONCE:
        std_file, gen_file = _same_dir_pair(src_dir, user_file)
        if std_file and gen_file:
            begin(std_file, gen_file)
            return
        std_hits = [std_file] if std_file \
            else _remembered_file(settings, 'stress_std_file')
        gen_hits = [gen_file] if gen_file \
            else _remembered_file(settings, 'stress_generator_file')
        _ask_pair(window, src_dir, std_hits, gen_hits, begin, False)
        return

    # Look next to the file under test first: that is where the std and the
    # generator of *this* problem live. Only then fall back to what was used
    # last time, which may belong to a completely different problem.
    std_hits = _candidate_files(src_dir, 'std', exclude=[user_file])
    if not std_hits:
        std_hits = _remembered_file(settings, 'stress_std_file')
    gen_hits = _candidate_files(src_dir, 'gen', exclude=[user_file] + std_hits)
    if not gen_hits:
        gen_hits = _remembered_file(settings, 'stress_generator_file')

    if choose or len(std_hits) != 1 or len(gen_hits) != 1:
        _ask_pair(window, src_dir, std_hits, gen_hits, begin, choose)
        return

    begin(std_hits[0], gen_hits[0])


def _to_number(text, kind, default):
    try:
        return kind(text)
    except (TypeError, ValueError):
        return default


def ask_limits(window, user_file, std_file, gen_file, time_limit, max_rounds,
               view):
    def on_rounds(value):
        rounds = _to_number((value or '').strip(), int, max_rounds)
        _launch(view, user_file, std_file, gen_file, time_limit, rounds)

    def on_limit(value):
        limit = _to_number((value or '').strip(), float, time_limit)
        if limit <= 0:
            limit = time_limit
        window.show_input_panel(t('stress_max_rounds') + ':', str(max_rounds),
                                on_rounds, None, None)

    window.show_input_panel(t('stress_time_limit') + ' (seconds):',
                            str(time_limit), on_limit, None, None)


class CphStopStressTestCommand(sublime_plugin.TextCommand):
    def is_visible(self, event=None, **kwargs):
        """Only offer 'Stop stress test' while a stress test is running.

        This one is a state gate rather than a file-context gate, so it stays
        correctly hidden in the View menu and the command palette too.
        """
        if event is not None and not context_menu_enabled():
            return False
        return bool(_running_sessions())

    def run(self, edit):
        # The Stop key is bound to the stress page only, so on that page it
        # stops exactly the run the user is looking at.
        sess = _session_for_view(self.view)
        if sess is None:
            running = _running_sessions()
            if len(running) == 1:
                sess = running[0]
            elif running:
                sublime.status_message(
                    'Algorithm Competition Assistant: ' + t('stress_multi_running', n=len(running)))
                return
            else:
                sublime.status_message(
                    'Algorithm Competition Assistant: ' + t('stress_none_running'))
                return
        if sess['running']:
            sess['stop_requested'] = True
            proc = sess.get('current_proc')
            if proc is not None:
                # Kill the program that is running right now instead of
                # waiting for the round's time limit to expire: with a 10s
                # generator limit, "stop" looked like it did nothing.
                killer = threading.Thread(target=_kill_tree, args=(proc,))
                killer.daemon = True
                killer.start()
            sublime.status_message('Algorithm Competition Assistant: ' + t('process_terminated'))
        else:
            # This run is already over; clear anything it left behind so
            # starting works again.
            _finish_stress(sess)
            sublime.status_message(
                'Algorithm Competition Assistant: ' + t('stress_none_running'))


def _hidden_startupinfo():
    if sublime.platform() != 'windows':
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def _lang_entry(file):
    """run_settings entry matching this file, or None."""
    run_settings = sublime.load_settings(settings_file) \
        .get('run_settings') or []
    ext = os.path.splitext(file)[1][1:]
    for entry in run_settings:
        if ext in (entry.get('extensions') or []):
            return entry
    return None


def _format_cmd(template, file, drop_args=True):
    src_dir = os.path.dirname(file)
    base = os.path.splitext(os.path.basename(file))[0]
    cmd = template or ''
    # optional multi-file placeholders are meaningless for stress programs
    cmd = cmd.replace('{extra_sources}', '').replace('{include_dirs}', '')
    if drop_args:
        cmd = cmd.replace('{args}', '')
    # Exactly the placeholders ProcessManager.format_command() knows, and
    # just as lenient. {file} is a documented placeholder, and the main Run
    # path accepts it, while stress used to abort with KeyError: 'file'; a
    # misspelled name must only warn (doctor reports it) instead of killing
    # the run.
    values = _LenientFormat({
        'file': os.path.basename(file),
        'source_file': file,
        'source_file_dir': src_dir,
        'file_name': base,
        'args': '',
    })
    return cmd.format_map(values)


_QUOTED_OR_PLAIN = re.compile(r'("[^"]*"|\'[^\']*\')')
_LOCAL_DEFINE = re.compile(r'(^|\s)-D(?:LOCAL|DEBUG)(?=\s|$)')


def _strip_local_define(cmd):
    """Drop -DLOCAL (and -DDEBUG) from a stress build command.

    Those defines switch the user's own template into local judging mode:
    `#ifdef LOCAL` blocks read from files or print debug output. During a
    stress run that output goes into a pipe nobody reads and slows every
    single round, so the stress binaries are built without them. The main
    Run path keeps the user's command exactly as configured.
    """
    parts = _QUOTED_OR_PLAIN.split(cmd)
    for i in range(0, len(parts), 2):
        parts[i] = _LOCAL_DEFINE.sub(r'\1', parts[i])
    return ''.join(parts).strip()


def _compile_program(file, time_limit=30, sess=None):
    """Build a stress-test program using the SAME config as the main flow.

    Previously this was hardcoded to `g++ -std=c++11 -O2`, so the stress
    binary could differ from what the judge-like runner actually builds.

    Returns (ok, path, reason): `reason` is a short explanation for the
    panel when the build failed, instead of a bare "failed to compile".
    """
    entry = _lang_entry(file)
    if entry is None:
        print_safe('[Algorithm Competition Assistant] stress: no run_settings entry for %s' % file)
        return False, None, 'no run_settings entry for this file type'

    if not os.path.exists(file):
        # The compiler only says "No such file or directory" for this, which
        # looks like a broken path rather than an unsaved buffer.
        return False, None, 'the file is not on disk (save it first)'

    src_dir = os.path.dirname(file)
    base = os.path.splitext(os.path.basename(file))[0]
    template = entry.get('compile_cmd')
    if not template:
        # interpreted language (python/java handled by its own run_cmd)
        return True, file, ''

    cmd = _format_cmd(template, file)
    cmd = _strip_local_define(cmd)
    # The panel's Debug/Release switch has to be the last word here as well:
    # stress used to build from the raw command, so a solution toggled to Debug
    # was stress-tested without the sanitizer and the assertion flags. A
    # counterexample then came out of a binary the user never actually runs, and
    # the undefined behaviour that produced it stayed invisible in the panel.
    cmd = _apply_build_mode(cmd, _build_mode(file))
    # Build into a scratch directory: the same .exe is the one the user
    # rebuilds by hand, and running it for thousands of rounds made that
    # rebuild fail with "Permission denied" until the run was over.
    out_dir = _build_dir(sess)
    exe_path = None
    if out_dir:
        cmd, exe_path = isolate_output(cmd, out_dir)
    if not exe_path:
        exe_path = output_path_from_compile_cmd(cmd)
        if exe_path and not os.path.isabs(exe_path):
            exe_path = os.path.join(src_dir, exe_path)
    if not exe_path:
        exe_path = os.path.join(
            src_dir, base + ('.exe' if sublime.platform() == 'windows' else ''))
    artifact_dir = os.path.dirname(exe_path) or src_dir

    started_at = time.time()
    try:
        rc, out, err = _popen_capture(
            cmd, src_dir, shell=True, timeout=time_limit,
            abort=(lambda: sess['stop_requested']) if sess is not None else None,
            sess=sess)
        if rc != 0:
            print_safe('[Algorithm Competition Assistant] Compile error in %s:\n%s' % (file, err))
            detail = (err or out or '').strip().splitlines()
            return False, None, ('compiler exit code %s%s'
                                 % (rc, (': ' + detail[0][:200]) if detail else ''))
        # A successful compile that wrote nothing (or wrote a different name
        # than the one asked for) used to fail later as "cannot run"; check
        # now so the message points at the real problem.
        real = resolve_artifact(exe_path, artifact_dir, started_at)
        if real is None:
            print_safe('[Algorithm Competition Assistant] stress: %s compiled but %r is missing'
                       % (file, os.path.basename(exe_path)))
            return False, None, ('the compiler produced no %s'
                                 % os.path.basename(exe_path))
        if real != exe_path:
            print_safe('[Algorithm Competition Assistant] stress: binary of %s is %r on disk'
                       % (base, os.path.basename(real)))
        return True, real, ''
    except _Aborted:
        # Stopped while the compiler was running: say the run was stopped,
        # not that the program failed to build.
        raise
    except Exception as e:
        print_safe('[Algorithm Competition Assistant] Compile error: %s' % str(e))
        return False, None, str(e)[:200]


def _kill_tree(proc):
    """Kill a program and everything it started.

    With `shell=True` the direct child is cmd.exe, so a plain kill leaves the
    compiler running; it then keeps holding the .exe it was writing, which is
    what made a rebuild fail with "Permission denied" afterwards.
    """
    try:
        pid = proc.pid
    except Exception:
        return
    if pid is None:
        return
    if sublime.platform() == 'windows':
        try:
            killer = subprocess.Popen(
                ['taskkill', '/F', '/T', '/PID', str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                startupinfo=_hidden_startupinfo())
            killer.wait(timeout=3)
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def _drain(stream, sink):
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            sink.append(chunk)
    except Exception:
        pass


def _feed(proc, data):
    try:
        proc.stdin.write(data)
        proc.stdin.flush()
    except Exception:
        pass
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass


def _communicate_abortable(proc, data, timeout, abort):
    """Read a program's output while staying able to kill it early.

    Popen.communicate(timeout) cannot do this: it kills the process itself as
    soon as the timeout expires and cannot be resumed, so pressing Stop had
    to wait out the whole time limit of the round that was running. Draining
    the pipes on reader threads and polling the process keeps the decision
    here. Raises subprocess.TimeoutExpired / _Aborted.
    """
    out_parts = []
    err_parts = []
    threads = []
    if proc.stdout is not None:
        th = threading.Thread(target=_drain, args=(proc.stdout, out_parts))
        th.daemon = True
        th.start()
        threads.append(th)
    if proc.stderr is not None:
        th = threading.Thread(target=_drain, args=(proc.stderr, err_parts))
        th.daemon = True
        th.start()
        threads.append(th)
    feeder = None
    if data is not None and proc.stdin is not None:
        feeder = threading.Thread(target=_feed, args=(proc, data))
        feeder.daemon = True
        feeder.start()

    deadline = (time.time() + timeout) if timeout and timeout > 0 else None
    timed_out = False
    aborted = False
    while proc.poll() is None:
        if abort is not None and abort():
            aborted = True
            _kill_tree(proc)
            break
        if deadline is not None and time.time() >= deadline:
            timed_out = True
            _kill_tree(proc)
            break
        time.sleep(0.02)
    try:
        proc.wait()
    except Exception:
        pass
    for th in threads:
        th.join(timeout=2)
    if feeder is not None:
        feeder.join(timeout=2)
    if timed_out:
        raise subprocess.TimeoutExpired(getattr(proc, 'args', ''), timeout)
    if aborted:
        raise _Aborted()
    return b''.join(out_parts), b''.join(err_parts)


def _popen_capture(cmd, cwd, input_text=None, shell=False, timeout=30, env=None,
                   abort=None, sess=None):
    """Run a command and capture its output as text.

    Uses Popen instead of subprocess.run: Sublime's plugin host is Python
    3.3, which has no subprocess.run (3.5+) and no text/encoding arguments
    (3.6/3.7+). Raises subprocess.TimeoutExpired on timeout, _Aborted when
    `abort()` turns true while the command is still running.
    """
    if env:
        # Extra variables (the stress seed) on top of the inherited ones.
        merged = dict(os.environ)
        merged.update(env)
    else:
        merged = None
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        shell=shell,
        env=merged,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        startupinfo=_hidden_startupinfo()
    )
    if sess is not None:
        sess['current_proc'] = proc
    data = input_text.encode('utf-8', 'replace') if isinstance(input_text, str) else input_text
    try:
        out, err = _communicate_abortable(proc, data, timeout, abort)
    finally:
        if sess is not None:
            sess['current_proc'] = None
    return (proc.returncode,
            (out or b'').decode('utf-8', 'replace'),
            (err or b'').decode('utf-8', 'replace'))


def _program_argv(file):
    """Command line to run a program, taken from run_settings.run_cmd."""
    entry = _lang_entry(file)
    if entry is None:
        return None
    run_cmd = entry.get('run_cmd')
    if not run_cmd:
        return None
    cmd = _format_cmd(run_cmd, file)
    windows = (sublime.platform() == 'windows')
    try:
        parts = shlex.split(cmd, posix=not windows)
    except Exception:
        return [cmd]
    if windows:
        # posix=False keeps the surrounding quotes: strip them, otherwise
        # the path would be passed to CreateProcess with literal quotes.
        cleaned = []
        for p in parts:
            if len(p) > 1 and p[0] == '"' and p[-1] == '"':
                p = p[1:-1]
            cleaned.append(p)
        parts = cleaned
    if parts:
        # The compiler may have written the binary under a different name
        # (non-ASCII -o names go through the ANSI codepage on Windows), which
        # used to make every round fail with "cannot run".
        parts[0] = retarget_path(parts[0], os.path.dirname(file))
    return parts


def _run_program(program, input_data, cwd=None, time_limit=2.0, env=None,
                 sess=None):
    """Run a program with the run_settings command; program is a source file."""
    argv = _program_argv(program) if not isinstance(program, list) else program
    if not argv:
        return (-1, '', 'no run command configured', False)
    if cwd is None:
        cwd = os.path.dirname(program if isinstance(program, str) else argv[0])
    try:
        rc, out, err = _popen_capture(argv, cwd, input_text=input_data,
                                     timeout=time_limit, env=env,
                                     abort=(lambda: sess['stop_requested'])
                                     if sess is not None else None,
                                     sess=sess)
        return (rc, out, err, False)
    except subprocess.TimeoutExpired:
        return (-1, '', '', True)
    except _Aborted:
        # Stop was pressed: let the loop wind down instead of reporting this
        # round as a timeout.
        raise
    except Exception as e:
        return (-1, '', str(e), False)


def _build_dir(sess=None):
    """A fresh scratch directory for one stress binary."""
    root = sess.get('build_root') if sess is not None else None
    if not root:
        return None
    seq = (sess.get('build_seq') or 0) + 1
    sess['build_seq'] = seq
    path = os.path.join(root, str(seq))
    try:
        os.makedirs(path)
        return path
    except Exception:
        return None


def _program_command(exe, source_file):
    """argv for one stress program, or None to go through run_cmd.

    Compiled languages run the binary that was just built; interpreted ones
    (Python) have no binary and keep using their run command.
    """
    if exe and os.path.normcase(exe) != os.path.normcase(source_file):
        return [exe]
    return None


def _finish_stress(sess):
    """Release everything a run owned, whether it ended or was stopped."""
    root = sess.get('build_root')
    if root:
        try:
            import shutil
            shutil.rmtree(root, ignore_errors=True)
        except Exception:
            pass
    sess['build_root'] = None
    sess['build_seq'] = 0
    sess['current_proc'] = None
    sess['current_round'] = 0
    _stop_stress(sess)


def _record_stress_rounds(user_file, rounds):
    """Count a finished stress run into the practice statistics.

    ``core.cph_stats.record_stress`` exists since the very first stats schema
    but nothing ever called it, so the "stress_rounds" field of every problem
    stayed 0 and the statistics page under-reported how much work was done.
    The module is defensive by contract ("never raises"), and a broken stats
    file must not take a stress run down with it, so the call is wrapped once
    more here instead of trusting that.
    """
    if not rounds:
        return
    try:
        record_stress(user_file, rounds)
    except Exception:
        pass


def _run_stress_loop(sess):
    user_file = sess['user_file']
    std_file = sess['std_file']
    gen_file = sess['generator_file']
    time_limit = sess['time_limit']
    max_rounds = sess['max_rounds']
    wall_start = time.time()
    sess['build_seq'] = 0
    sess['build_root'] = None
    try:
        # Every stress binary gets its own scratch directory, so the .exe in
        # the source directory stays free for the user to rebuild.
        sess['build_root'] = tempfile.mkdtemp(prefix='cph-stress-')
    except Exception:
        sess['build_root'] = None
    try:
        sublime.set_timeout(
            lambda: _append_stress(sess, '[Algorithm Competition Assistant] Compiling programs...\n'), 0)

        ok1, user_exe, why1 = _compile_program(user_file, sess=sess)
        if not ok1:
            sublime.set_timeout(
                lambda w=why1: _append_stress(sess, 
                    '[Algorithm Competition Assistant] Failed to compile user program: '
                    + os.path.basename(user_file) + (': ' + w if w else '') + '\n'), 0)
            _stop_stress(sess)
            return

        ok2, std_exe, why2 = _compile_program(std_file, sess=sess)
        if not ok2:
            sublime.set_timeout(
                lambda w=why2: _append_stress(sess, 
                    '[Algorithm Competition Assistant] Failed to compile std: '
                    + os.path.basename(std_file) + (': ' + w if w else '') + '\n'), 0)
            _stop_stress(sess)
            return

        ok3, gen_exe, why3 = _compile_program(gen_file, sess=sess)
        if not ok3:
            sublime.set_timeout(
                lambda w=why3: _append_stress(sess, 
                    '[Algorithm Competition Assistant] Failed to compile generator: '
                    + os.path.basename(gen_file) + (': ' + w if w else '') + '\n'), 0)
            _stop_stress(sess)
            return

        # Run the binaries that were just built instead of looking the run
        # command up again every round, and keep the working directory of the
        # source file (programs often read files next to it).
        user_argv = _program_command(user_exe, user_file)
        std_argv = _program_command(std_exe, std_file)
        gen_argv = _program_command(gen_exe, gen_file)
        user_cwd = os.path.dirname(user_file) or None
        std_cwd = os.path.dirname(std_file) or None
        gen_cwd = os.path.dirname(gen_file) or None

        # The generator is allowed to take longer than the program under
        # test: producing a large sample is not the same as solving it.
        try:
            generator_limit = float(
                get_settings().get('stress_generator_time_limit_seconds', 10) or 10)
        except (TypeError, ValueError):
            generator_limit = 10.0
        if generator_limit <= 0:
            generator_limit = 10.0

        # Multi-solution problems: comparing the two outputs line by line
        # reports a difference on almost every round even though both are
        # valid. When a checker is configured it decides instead.
        checker_cfg = cph_checker.config(
            (get_settings().get('run_settings') or []), user_file, get_settings())
        checker_exe = None
        if checker_cfg is not None:
            exe, why = cph_checker.executable(checker_cfg)
            if not exe:
                sublime.set_timeout(
                    lambda w=why: _append_stress(sess, 
                        '[Algorithm Competition Assistant] Failed to build checker: %s\n' % w), 0)
                _stop_stress(sess)
                return
            checker_exe = exe

        try:
            float_tol = float(get_settings().get('float_tolerance', 0) or 0)
        except (TypeError, ValueError):
            float_tol = 0.0

        started_extra = ''
        if checker_exe is not None:
            started_extra += '  checker: %s (%s)\n' % (
                os.path.basename(str(checker_cfg.get('path') or '')),
                checker_cfg.get('style') or 'testlib')
        elif float_tol > 0:
            started_extra += '  float tolerance: %g\n' % float_tol

        sublime.set_timeout(
            lambda: _append_stress(sess, 
                '[Algorithm Competition Assistant] Stress test started\n'
                '  user: %s\n  std:  %s\n  gen:  %s\n'
                '  time limit: %ss/round, generator limit: %ss, max rounds: %d\n'
                '%s\n'
                % (os.path.basename(user_file), os.path.basename(std_file),
                   os.path.basename(gen_file), time_limit, generator_limit,
                   max_rounds, started_extra)
            ), 0)

        try:
            wall_limit = float(get_settings().get('stress_max_wall_seconds', 0) or 0)
        except (TypeError, ValueError):
            wall_limit = 0.0

        round_count = 0
        compared = 0
        gen_timeouts = 0
        tle_hint_shown = False
        for round_count in range(1, max_rounds + 1):
            if sess['stop_requested']:
                sublime.set_timeout(
                    lambda: _append_stress(sess, '\n[Algorithm Competition Assistant] ' + t('process_terminated') + ' at round %d\n' % round_count), 0)
                break
            if wall_limit > 0 and time.time() - wall_start >= wall_limit:
                # A run that is still going after this long is not going to
                # finish any time soon; say so instead of hanging.
                sublime.set_timeout(
                    lambda l=wall_limit: _append_stress(sess, 
                        '\n[Algorithm Competition Assistant] ' + t('stress_wall_limit', limit=l) + '\n'), 0)
                break
            if round_count == 1 or round_count % 10 == 0:
                # Without this there is no way to tell a slow run from a hung
                # one, which is what "it never ends" looked like.
                sublime.set_timeout(
                    lambda r=round_count: sublime.status_message(
                        'Algorithm Competition Assistant: ' + t('stress_round', round=r)
                        + ' / %d' % max_rounds), 0)

            sess['current_round'] = round_count
            # A fresh seed per round, handed to the generator through the
            # environment (CPH_SEED) so a counterexample can be replayed:
            # "found a counterexample" is only actionable when you can make
            # the generator produce exactly that test again.
            seed = random.randrange(1, 2147483647)
            sess['current_seed'] = seed

            # The generator gets its own, larger budget. Generating the test
            # is legitimately slower than solving it, and measuring it with
            # the program's limit made a correct setup stop at
            # "Generator failed at round 1" (a timeout was reported as a
            # failure because the TLE flag was dropped here).
            ret, inp, gen_err, gen_tle = _run_program(
                gen_argv or gen_file, '', cwd=gen_cwd,
                time_limit=generator_limit,
                env={'CPH_SEED': str(seed)}, sess=sess)
            if gen_tle:
                gen_timeouts += 1
                sublime.set_timeout(
                    lambda r=round_count, l=generator_limit: _append_stress(sess, 
                        '[Algorithm Competition Assistant] ' + t('stress_generator_tle', round=r, limit=l) + '\n'), 0)
                if gen_timeouts >= 3:
                    sublime.set_timeout(
                        lambda l=generator_limit: _append_stress(sess, 
                            '[Algorithm Competition Assistant] ' + t('stress_generator_limit_hint', limit=l) + '\n'), 0)
                    break
                continue
            if ret != 0:
                # Say *why*: exit code plus whatever the generator printed.
                sublime.set_timeout(
                    lambda r=round_count, rc=ret, e=gen_err: _append_stress(sess, 
                        '[Algorithm Competition Assistant] ' + t('stress_generator_failed', round=r, code=rc)
                        + ('\n  ' + e.strip()[:500] if e.strip() else '') + '\n'), 0)
                break

            ret1, user_out, user_err, tle1 = _run_program(
                user_argv or user_file, inp, cwd=user_cwd, time_limit=time_limit, sess=sess)
            if tle1:
                sublime.set_timeout(
                    lambda r=round_count: _append_stress(sess, 
                        '[Algorithm Competition Assistant] Round %d: user program TLE\n' % r), 0)
                if not tle_hint_shown:
                    # By far the most common cause on a correct solution: the
                    # compile command defines LOCAL, so a debug macro left
                    # inside the main loop prints O(n^2) of stderr. Saying it
                    # once saves a long hunt (and a stderr dump would be huge).
                    tle_hint_shown = True
                    sublime.set_timeout(
                        lambda: _append_stress(sess, 
                            '[Algorithm Competition Assistant] ' + t('stress_tle_hint') + '\n'), 0)
                continue
            if ret1 < 0 and user_err:
                # The program never started (missing binary, bad command):
                # comparing its empty output as a wrong answer hid this.
                sublime.set_timeout(
                    lambda e=user_err: _append_stress(sess, 
                        '[Algorithm Competition Assistant] ' + t('stress_cannot_run',
                                               program=os.path.basename(user_file),
                                               reason=e.strip()[:300]) + '\n'), 0)
                break

            ret2, std_out, std_err, tle2 = _run_program(
                std_argv or std_file, inp, cwd=std_cwd, time_limit=time_limit, sess=sess)
            if tle2:
                sublime.set_timeout(
                    lambda r=round_count: _append_stress(sess, 
                        '[Algorithm Competition Assistant] Round %d: std program TLE\n' % r), 0)
                continue
            if ret2 < 0 and std_err:
                sublime.set_timeout(
                    lambda e=std_err: _append_stress(sess, 
                        '[Algorithm Competition Assistant] ' + t('stress_cannot_run',
                                               program=os.path.basename(std_file),
                                               reason=e.strip()[:300]) + '\n'), 0)
                break

            compared += 1
            checker_msg = ''
            if checker_exe is not None:
                # Both answers can be valid: let the checker decide.
                res = cph_checker.judge(checker_cfg, checker_exe, inp,
                                        user_out, std_out)
                is_diff = not res.get('ok')
                checker_msg = (res.get('message') or '').strip()
            else:
                # Same comparison rules as the judge-like runner (whitespace
                # insensitive) plus the configured float tolerance, so a
                # floating point problem no longer "fails" on the last digit.
                is_diff = not outputs_equal(user_out, std_out, float_tol)

            if is_diff:
                sess['last_diff'] = {
                    'round': round_count,
                    'input': inp,
                    'user_output': user_out,
                    'std_output': std_out,
                    'checker_message': checker_msg,
                }
                sublime.set_timeout(
                    lambda m=checker_msg: _on_stress_failed(
                        round_count, inp, user_out, std_out, m, sess), 0)
                break

            if round_count <= 10 or round_count % 10 == 0:
                sublime.set_timeout(
                    lambda r=round_count: _append_stress(sess, 
                        t('stress_round', round=r) + ' ... OK\n'
                    ), 0)

        else:
            if compared:
                sublime.set_timeout(
                    lambda: _append_stress(sess, 
                        '\n[Algorithm Competition Assistant] ' + t('stress_passed', rounds=round_count) + '\n'
                    ), 0)
            else:
                # Every round hit `continue` (TLE), so nothing was ever
                # compared: the for/else used to still announce "passed".
                sublime.set_timeout(
                    lambda: _append_stress(sess, 
                        '\n[Algorithm Competition Assistant] ' + t('stress_all_timeout') + '\n'
                    ), 0)
        # Feed the practice statistics. `round_count` is the number of rounds
        # the loop actually attempted; a run that stopped at the wall limit or
        # on Stop is still work the user did, so it is counted too. The write
        # is best effort: statistics must never be able to fail a stress run.
        _record_stress_rounds(user_file, round_count)
        # "Passed: N rounds" alone does not say how much of the run actually
        # compared anything: a run where 90 of 100 rounds timed out prints the
        # same sentence as one where all 100 did real work. One summary line
        # makes the difference visible without reading the whole panel.
        sublime.set_timeout(
            lambda r=round_count, c=compared, gt=gen_timeouts: _append_stress(sess, 
                '[Algorithm Competition Assistant] ' + t('stress_summary', rounds=r, compared=c, timeouts=gt) + '\n'
            ), 0)
        # Always say the run is over: a panel that just stopped growing is
        # what "it never ends" looked like.
        elapsed = time.time() - wall_start
        sublime.set_timeout(
            lambda e=elapsed: _append_stress(sess, 
                '\n[Algorithm Competition Assistant] ' + t('stress_finished', seconds='%.1f' % e) + '\n'
            ), 0)
        sublime.set_timeout(
            lambda e=elapsed: sublime.status_message(
                'Algorithm Competition Assistant: ' + t('stress_finished', seconds='%.1f' % e)), 0)
    except _Aborted:
        sublime.set_timeout(
            lambda: _append_stress(sess, '\n[Algorithm Competition Assistant] ' + t('process_terminated') + '\n'), 0)
    except Exception as e:
        # Bind `e` as a default argument: Python deletes the except-variable
        # when the block ends, so a bare `lambda: ... % e` raised NameError
        # by the time the deferred callback actually ran (the message never
        # appeared and the real error was swallowed).
        sublime.set_timeout(
            lambda e=e: _append_stress(sess, '[Algorithm Competition Assistant] Error: %s\n' % str(e)), 0)
    finally:
        _finish_stress(sess)


def _stop_stress(sess):
    sess['running'] = False
    sess['stop_requested'] = False
    _sync_stress_state()


def _append_stress(sess, text):
    if sess is None:
        return
    view_id = sess.get('stress_view_id')
    if view_id is None:
        return
    for window in sublime.windows():
        for v in window.views():
            if v.id() == view_id:
                v.run_command('append', {'characters': text})
                v.show(v.size())
                return


def _insert_limit():
    """The stress_max_insert_bytes cap for __tests (bytes; -1 = no limit)."""
    try:
        return int(get_settings().get('stress_max_insert_bytes', 2000))
    except (TypeError, ValueError):
        return 2000


def _utf8_size(text):
    """The size of `text` in bytes, the unit of stress_max_insert_bytes."""
    try:
        return len(text.encode('utf-8'))
    except Exception:
        return len(text)


def _on_stress_failed(round_count, inp, user_out, std_out, checker_message='',
                       sess=None):
    # Keep everything needed to reproduce this counterexample later
    # ('Stress: replay last counterexample').
    sess['last_counterexample'] = {
        'seed': sess.get('current_seed'),
        'input': inp,
        'user_out': user_out,
        'std_out': std_out,
        'round': round_count,
    }

    text = '\n' + '=' * 60 + '\n'
    text += '[Algorithm Competition Assistant] ' + t('stress_failed', round=round_count) + '\n'
    text += '=' * 60 + '\n\n'
    seed = sess.get('current_seed')
    if seed is not None:
        # Without the seed the counterexample cannot be regenerated; say it
        # out loud so it can be replayed (or set CPH_SEED by hand).
        text += '[Algorithm Competition Assistant] ' + t('stress_seed', seed=seed) + '\n\n'
    if checker_message:
        # testlib's quitf message: the only explanation of *why* the two
        # outputs are not both valid.
        text += '[Algorithm Competition Assistant] ' + t('stress_checker_message',
                                       message=checker_message[:500]) + '\n\n'
    text += '[' + t('stress_input') + ']\n'
    text += inp + '\n'
    text += '\n[' + t('stress_user_output') + ']\n'
    text += user_out + '\n'
    text += '\n[' + t('stress_std_output') + ']\n'
    text += std_out + '\n'
    text += '\n[' + t('stress_diff') + ']\n'
    user_lines = user_out.splitlines()
    std_lines = std_out.splitlines()
    max_lines = max(len(user_lines), len(std_lines))
    for i in range(max_lines):
        u = user_lines[i] if i < len(user_lines) else ''
        s = std_lines[i] if i < len(std_lines) else ''
        if u != s:
            text += 'Line %d:\n' % (i + 1)
            text += '  user: %s\n' % u
            text += '  std:  %s\n' % s

    # Save the counterexample as a permanent test case: finding a
    # counterexample is only useful if it comes back as a regression test.
    # A huge multi-case sample is misery to debug inside __tests, though, so
    # stress_max_insert_bytes keeps oversized inputs out of the tests file
    # (they are still printed above, and their seed can still be replayed).
    try:
        if get_settings().get('stress_save_counterexample', True):
            user_file = sess.get('user_file')
            if user_file:
                limit = _insert_limit()
                size = _utf8_size(inp)
                if limit >= 0 and size > limit:
                    # Say why nothing was saved instead of failing silently.
                    text += '\n[Algorithm Competition Assistant] ' + \
                        t('stress_counterexample_too_big', size=size,
                          limit=limit) + '\n'
                else:
                    answer = std_out.strip()
                    merged, _conflicts = merge_tests(load_all_tests(user_file), [{
                        'test': inp,
                        'correct_answers': [answer] if answer else [],
                        # Keep the generator seed with the counterexample so
                        # 'Stress: replay last seed' can reproduce it exactly.
                        'seed': sess.get('current_seed')}])
                    if save_tests(user_file, merged):
                        text += '\n[Algorithm Competition Assistant] ' + \
                            t('stress_counterexample_added', total=len(merged)) + '\n'
    except Exception as e:
        # The counterexample was already printed above, so the user has seen
        # it. A failure to *persist* it is different: the message used to go
        # to print_safe() only, which lands in the Sublime console nobody
        # opens, so a silently-unsaved counterexample looked exactly like a
        # saved one. It now appears in the stress page as well.
        print_safe('[Algorithm Competition Assistant] failed to save counterexample: %s' % e)
        text += '\n[Algorithm Competition Assistant] ' + \
            t('stress_counterexample_save_failed', reason=str(e)[:200]) + '\n'

    _append_stress(sess, text)
    _stop_stress(sess)


class CphStressReplayCommand(sublime_plugin.TextCommand):
	"""Re-run the last counterexample from its recorded generator seed.

	The stress loop hands the generator a fresh seed every round (CPH_SEED) and
	stores it with the counterexample, so the exact failing test can be
	reproduced instead of hoping the random search hits it again.
	"""

	def run(self, edit):
		# Replay belongs to the page it was invoked on: with several stress
		# pages open, each keeps its own last counterexample.
		sess = _sessions.get(self.view.id())
		state = sess.get('last_counterexample') if sess else None
		if not state or state.get('seed') is None:
			sublime.status_message('[Algorithm Competition Assistant] ' + t('stress_no_counterexample'))
			return
		gen_file = sess.get('generator_file')
		user_file = sess.get('user_file')
		std_file = sess.get('std_file')
		if not (gen_file and user_file and std_file):
			sublime.status_message('[Algorithm Competition Assistant] ' + t('stress_no_counterexample'))
			return

		seed = state['seed']
		_append_stress(sess, '\n[Algorithm Competition Assistant] ' + t('stress_replaying', seed=seed) + '\n')

		def say(msg):
			# The worker below runs on its own thread, and the view API
			# (append) is main-thread only; post every line back the same way
			# the main stress loop reports progress. `m=msg` keeps the text
			# alive for the deferred call.
			sublime.set_timeout(lambda m=msg: _append_stress(sess, m), 0)

		def worker():
			try:
				ok, gen_exe, why = _compile_program(gen_file, sess=sess)
				if not ok:
					say('[Algorithm Competition Assistant] generator: %s\n' % (why or 'failed'))
					return
				rc, inp, err, tle = _run_program(
					gen_exe, '', time_limit=float(
						get_settings().get('stress_generator_time_limit_seconds', 10) or 10),
					env={'CPH_SEED': str(seed)}, sess=sess)
				if rc != 0 or tle:
					say('[Algorithm Competition Assistant] generator failed: rc=%s %s\n'
						% (rc, err[:200]))
					return
				say('[Algorithm Competition Assistant] regenerated %d bytes\n' % len(inp))
				ok1, user_exe, why1 = _compile_program(user_file, sess=sess)
				ok2, std_exe, why2 = _compile_program(std_file, sess=sess)
				if not (ok1 and ok2):
					say('[Algorithm Competition Assistant] compile failed: %s%s\n'
						% (why1 or '', why2 or ''))
					return
				limit = float(get_settings().get('stress_time_limit_seconds', 2) or 2)
				_rc1, user_out, _e1, tle1 = _run_program(user_exe, inp, time_limit=limit, sess=sess)
				_rc2, std_out, _e2, _tle2 = _run_program(std_exe, inp, time_limit=limit, sess=sess)
				if tle1:
					say('[Algorithm Competition Assistant] ' + t('stress_replay_tle') + '\n')
					return
				if normalize_lines(user_out) == normalize_lines(std_out):
					say('[Algorithm Competition Assistant] ' + t('stress_replay_same') + '\n')
				else:
					# _on_stress_failed writes to the view and saves a test, so
					# the whole call is handed to the main thread.
					sublime.set_timeout(
						lambda: _on_stress_failed(state.get('round') or 0, inp,
												  user_out, std_out, sess=sess), 0)
			except Exception as e:
				say('[Algorithm Competition Assistant] replay error: %s\n' % e)

		thread = threading.Thread(target=worker)
		thread.daemon = True
		thread.start()


class CphStressEvents(sublime_plugin.EventListener):
    """Keep sessions in sync with their pages.

    Closing a stress page stops its run (if any) and drops the session, so
    the Stop keybinding and the cph_stress_view context never point at a
    detached view.
    """

    def on_close(self, view):
        sess = _sessions.pop(view.id(), None)
        if sess is None:
            return
        unregister_stress_view(view.id())
        _sync_stress_state()
        if sess['running']:
            sess['stop_requested'] = True
            proc = sess.get('current_proc')
            if proc is not None:
                killer = threading.Thread(target=_kill_tree, args=(proc,))
                killer.daemon = True
                killer.start()

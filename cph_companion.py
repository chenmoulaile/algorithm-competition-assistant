"""
Algorithm Competition Assistant - Competitive Companion 持久监听器
接收来自浏览器扩展 Competitive Companion 的 POST 请求
参考: https://github.com/jmerle/competitive-companion
思路来自 FastOlympicCodingHook (https://github.com/DrSwad/FastOlympicCodingHook)

与旧版 (FastOlympicCodingHook 一次性会话) 的区别:
- 一次启动后持续监听 (cph-ng 风格), 浏览器扩展可反复点击发送样例
- 默认随 Sublime 启动自动监听 (companion_auto_listen), 不需要先打开某个文件
- 两种监听方式各有右键入口和快捷键 (companion_target):
  new_file     每题新建 <题目名>.cpp + 存样例 + 记限制, **到此为止, 不编译不跑样例**
               (代码还没写; 运行面板和每次编译都是白占内存)
  current_file 样例送进聚焦/固定的文件, 并立刻跑一遍 (那文件里已经有代码)
- 新建文件的目录优先取你点命令时那个 cpp 所在的文件夹, 其次聚焦文件, 再次工作区
- 头部注释可用 auto_create_header 完全自定义 ({problem} {url} {time} {memory});
  只写插件自己新建的文件, 已有源码仍然一个字都不插
- 再次执行监听命令只会切换目标文件, 不会重复起服务器/产生端口冲突
- 收到的样例与已有样例 **合并去重**（与导入流程语义一致），不再整体覆盖
- 收到的 TL/ML 按源文件持久化在测试数据旁边，重启后手动 Ctrl+Alt+B 也不丢
"""

import sublime
import sublime_plugin
import json
import os
import re
import threading
import time

from .core.cph_i18n import t
from .core.cph_settings import (get_settings, is_run_supported_ext, load_all_tests,
								save_tests, set_problem_limits, settings_file,
								notify_io_error, tests_lock)
from .core.cph_tests_merge import merge_tests as merge_stored_with
from .core.cph_state import set_listener


# Competitive Companion 官方字段单位: timeLimit = ms, memoryLimit = MB
MAX_BODY_BYTES = 10 * 1024 * 1024

_listener = {
    'server': None,      # HTTPServer instance
    'thread': None,      # serving thread
    'port': None,
    'view_id': None,     # target source view id
    'target': None,      # mode picked from the menu, for this session
    'dir': None,         # folder picked by listening inside a file
    'last_run': None,    # (file, monotonic) of the last auto-run
    'lock': threading.Lock(),
}

#: One click on a contest page is not one problem: an AtCoder task list sends
#: every task at once (seven POSTs measured here), so filling one file would
#: queue a compile and a run per problem, back to back. The samples are all
#: stored; only the repeat runs are folded away.
AUTO_RUN_DEBOUNCE_SECONDS = 2.0


def _should_run(file_name):
    """True unless this very file was auto-run by the same click a moment ago.

    Debounced per file, not globally: two files being filled are two
    intentions, seven problems landing in one file are one.
    """
    now = time.monotonic()
    last = _listener.get('last_run')
    if (last is not None and last[0] == file_name
            and now - last[1] < AUTO_RUN_DEBOUNCE_SECONDS):
        _debug_log('auto-run skipped for %s (another problem of the same click)'
                   % os.path.basename(file_name))
        return False
    _listener['last_run'] = (file_name, now)
    return True


def _find_view(view_id):
    if view_id is None:
        return None
    for window in sublime.windows():
        for view in window.views():
            if view.id() == view_id:
                return view
    return None


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


#: One line per click, for the cases the interface cannot show: which mode was
#: in effect, and which file (or which refusal) the problem ended up at.
DEBUG_LOG_NAME = 'aca-companion.log'


def _debug_log(message):
    """Append to <Data>/Log/aca-companion.log when companion_debug_log is on.

    Off by default on purpose: a background HTTP callback writing to the user's
    disk on every click is not a thing to do silently, and everything worth
    saying is already in the status bar.
    """
    try:
        if not (get_settings() or {}).get('companion_debug_log', False):
            return
        import datetime
        directory = os.path.join(os.path.dirname(sublime.packages_path()), 'Log')
        if not os.path.isdir(directory):
            os.makedirs(directory)
        stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(os.path.join(directory, DEBUG_LOG_NAME), 'a',
                  encoding='utf-8', newline='\n') as handle:
            handle.write('%s %s\n' % (stamp, message))
    except Exception as e:
        print('[Algorithm Competition Assistant] companion log not written: %s' % e)


def merge_tests(file_name, incoming):
    """Merge freshly received samples with the stored ones.

    Uses the shared policy (see core/cph_tests_merge.py): deduplicated by
    INPUT, and an answer is only filled in when none is stored yet - a
    re-sent sample must never overwrite an answer the user accepted by
    hand. Conflicting answers are reported through the status bar.
    """
    merged, conflicts = _merge_with_stored(file_name, incoming)
    if conflicts:
        sublime.status_message(t('answer_conflict_kept'))
    return merged


def _merge_with_stored(file_name, incoming):
    """Pure-data twin of merge_tests: (merged, conflicts), no UI."""
    return merge_stored_with(load_all_tests(file_name), incoming)


def remember_limits(file_name, time_limit_ms, memory_limit_mb):
    """Remember the problem limits for this session.

    Without this the TL/ML received from the browser only applied to the
    run it triggered and a later manual Ctrl+Alt+B lost them again.
    Kept in memory on purpose: writing the user's settings file from a
    background HTTP callback is not acceptable behaviour for a plugin.
    """
    set_problem_limits(file_name, time_limit_ms, memory_limit_mb)


#: Host names a companion request may arrive on. Anything else means the
#: caller resolved our port under a different name (DNS rebinding).
_LOOPBACK_HOSTS = ('localhost', '127.0.0.1', '::1', '[::1]')

#: Origins that are not a web page. A browser extension's own background
#: worker is where Competitive Companion posts from, and Chrome puts the
#: extension's id in Origin rather than the page's - refusing that refused the
#: extension itself, silently, with a 403 and no trace. An extension is code
#: already installed on this machine, not a remote attacker; the rebinding
#: defence is the Host check above.
_EXTENSION_SCHEMES = ('chrome-extension://', 'moz-extension://',
                      'safari-web-extension://', 'extension://')


def _origin_host(origin):
    """Host part of an http(s) origin.

    Parsed by hand instead of urllib.parse because this module is imported on
    every Sublime start and must not drag the urllib package in for it.
    """
    rest = origin.split('://', 1)[-1]
    rest = rest.split('/', 1)[0]
    if rest.startswith('['):        # IPv6 literal, e.g. [::1]:12345
        rest = rest[1:]
        return rest.split(']', 1)[0]
    return rest.split(':')[0]


def _is_extension_origin(origin):
    return origin.startswith(_EXTENSION_SCHEMES)


def _host_name(host):
    """The hostname of a Host header, port removed, brackets kept.

    A bracketed IPv6 literal cannot be split on ':': `[::1]:12345` would come
    out as `[`, and every loopback request would be refused - which is what the
    old code's '[::1]' entry was there to allow.
    """
    if host.startswith('['):
        name, _, _rest = host[1:].partition(']')
        return '[' + name + ']'
    return host.split(':')[0]


def request_allowed(get_header):
    """True when this request may drive the plugin.

    Checking Host alone only stopped DNS rebinding: a page on any other site
    that posts to http://localhost:12345 arrives with Host: localhost, so it
    could still merge attacker-chosen test data into the user's __tests file,
    rewrite the time and memory limits, create a source file and start a
    compile that executes it. Origin and Sec-Fetch-Site say which page sent
    the request, and a browser script cannot set either one.

    The extension's own origin counts as allowed: it is the only thing
    Competitive Companion ever sends, and blocking it made the plugin deaf
    while still answering 200-adjacent errors to the browser.

    Callers with no trace of a browser (curl, a local script, an OJ tool) are
    still accepted: this listener is a local automation endpoint.
    """
    host = (get_header('Host') or '').strip().lower()
    if host and _host_name(host) not in _LOOPBACK_HOSTS:
        return False

    origin = (get_header('Origin') or '').strip().lower()
    if origin:
        return (_origin_host(origin) in _LOOPBACK_HOSTS
                or _is_extension_origin(origin))

    fetch_site = (get_header('Sec-Fetch-Site') or '').strip().lower()
    if fetch_site:
        return fetch_site in ('same-origin', 'same-site', 'none')

    return True


def build_handler():
    """Build the request handler class, importing http.server on first use.

    `http.server` pulls in socket, http.client and email.parser: measured at
    ~145 ms of the package's cold import, paid by every Sublime start even
    though only the opt-in Companion listener ever uses it. So the class is
    created when the listener starts rather than when the module loads.
    """
    from http.server import BaseHTTPRequestHandler

    class _CompanionHandler(BaseHTTPRequestHandler):

        def log_message(self, format, *args):
            pass

        def _host_allowed(self):
            return request_allowed(self.headers.get)

        def _deny(self, code=403):
            try:
                self.send_response(code)
                self.end_headers()
                self.wfile.write(b'forbidden')
            except Exception:
                pass

        def do_POST(self):
            if not self._host_allowed():
                # Recorded, not just answered: a click the plugin refused and a
                # click that never left the browser were indistinguishable.
                _debug_log('refused 403 host=%s origin=%s fetch-site=%s'
                           % (self.headers.get('Host') or '-',
                              self.headers.get('Origin') or '-',
                              self.headers.get('Sec-Fetch-Site') or '-'))
                self._deny()
                return

            try:
                content_length = int(self.headers.get('Content-Length', 0))
            except (TypeError, ValueError):
                content_length = 0
            if content_length > MAX_BODY_BYTES:
                self._deny(413)
                return
            if content_length <= 0:
                # A negative Content-Length used to slip past the check above
                # and make rfile.read(-1) block until EOF, hanging the handler.
                self._deny(400)
                return

            try:
                raw = self.rfile.read(content_length) if content_length else b''
                data = json.loads(raw.decode('utf-8'))
            except Exception:
                _debug_log('refused 400: body is not json (%d bytes)'
                           % content_length)
                self._deny(400)
                return

            # Reply before the work: the extension only needs to know the
            # request arrived. 保持服务器运行, 不 shutdown —— 浏览器扩展可以反复点击
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'ok')
            except Exception:
                pass

            # Everything below used to run on the MAIN thread: merge_tests
            # reads up to four tests files and save_tests writes them back,
            # and a problem-list click sends N of these in a burst - the
            # editor froze for every one. Only two steps here need the main
            # thread (resolving the target view, and the UI feedback); the
            # rest is pure data + file work and stays on THIS thread.
            self._process_incoming(data)

        def _process_incoming(self, data):
            tests = data.get('tests', []) or []
            problem_name = data.get('name', '')
            time_limit_ms = _as_int(data.get('timeLimit'))
            memory_limit_mb = _as_int(data.get('memoryLimit'))

            incoming = []
            for test in tests:
                output = (test.get('output') or '').strip()
                incoming.append({
                    'test': test.get('input', ''),
                    'correct_answers': [output] if output else [],
                })

            # 1. Resolve the target on the main thread (it touches views and
            # may create a file). Bounded wait: a wedged UI must not wedge
            # the listener with it.
            holder = {}
            done = threading.Event()

            def _resolve():
                try:
                    holder['target'] = resolve_target(
                        problem_name, data.get('url') or '',
                        time_limit_ms, memory_limit_mb)
                except Exception as e:
                    holder['error'] = e
                finally:
                    done.set()

            try:
                sublime.set_timeout(_resolve, 0)
            except Exception as e:
                _debug_log('target resolve dispatch failed: %s' % e)
                return
            if not done.wait(10.0):
                _debug_log('target resolve timed out after 10s; POST dropped')
                return
            if 'error' in holder:
                print(t('error_handling_post', error=str(holder['error'])))
                return

            target = holder.get('target')
            if target is None or not target.file_name():
                _debug_log('incoming "%s" tests=%d mode=%s -> no target'
                           % (problem_name, len(incoming), _target_mode()))
                sublime.set_timeout(
                    lambda: sublime.status_message(t('listener_no_target')), 0)
                return

            file_name = target.file_name()
            _debug_log('incoming "%s" tests=%d tl=%s ml=%s mode=%s -> %s'
                       % (problem_name, len(incoming), time_limit_ms,
                          memory_limit_mb, _target_mode(), file_name))

            # 2. Merge + write HERE, on the HTTP thread. encode_value /
            # decode_value are pure converters and the data is plain
            # str/list/dict, so none of this touches view state.
            try:
                # Read+merge+write as one critical section: the run panel's
                # async save may be writing the same file at this instant.
                with tests_lock():
                    merged, conflicts = _merge_with_stored(file_name, incoming)
                    saved = save_tests(file_name, merged)
            except Exception as e:
                print(t('error_handling_post', error=str(e)))
                return
            if not saved:
                # A status_message flashes past while the browser has already
                # been answered with 200 "ok": the click looked accepted and
                # the samples were not on disk. notify_io_error names the
                # file and the OS reason (it must run on the main thread).
                payload = file_name

                def _report_io_error():
                    if not notify_io_error(payload):
                        sublime.status_message(t('import_save_failed'))
                sublime.set_timeout(_report_io_error, 0)
                return
            remember_limits(file_name, time_limit_ms, memory_limit_mb)
            print('[Algorithm Competition Assistant] %s' % t('new_test_file_path', path=file_name))

            count = len(incoming)
            tl = time_limit_ms if time_limit_ms is not None else '-'
            ml = memory_limit_mb if memory_limit_mb is not None else '-'

            # 3. UI feedback + optional auto-run, back on the main thread.
            # Auto-run only where it can mean something. `new_file` just
            # created an empty problem file: there is no code to test yet,
            # and a panel plus a compile for every click is memory spent on
            # nothing. `current_file` fills the file being written, and
            # running its samples right there is the point.
            # The panel belongs to the file that was resolved above: the
            # view the request arrived on may have been closed by
            # auto-create, and running a command on a closed view raised.
            def _ui():
                if conflicts:
                    sublime.status_message(t('answer_conflict_kept'))
                if _target_mode() == _TARGET_CURRENT and _should_run(file_name):
                    target.run_command('cph_view_tester', {
                        'action': 'make_opd',
                        'time_limit_ms': time_limit_ms,
                        'memory_limit_mb': memory_limit_mb,
                    })
                # Naming the destination is the one thing the user could not
                # tell: "已接收题目" alone does not say whether it landed in the
                # file he was looking at or in a newly created one.
                sublime.status_message(t('tests_received',
                                         name=problem_name or '?',
                                         count=count, time=tl, memory=ml,
                                         file=os.path.basename(file_name)))
            sublime.set_timeout(_ui, 0)

    return _CompanionHandler



def _sanitize_name(name):
    """Turn a problem name into a file name that Windows accepts."""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', ' ', name or '').strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned[:80].rstrip('. ')


#: Extensions whose comments start with '#'. The created file is opened
#: straight into an editor, so a marker the language does not know would be a
#: syntax error on the first line.
_HASH_COMMENTS = ('py', 'rb', 'sh', 'pl', 'r', 'jl')


def _flat(value):
    return re.sub(r'\s+', ' ', str(value if value is not None else '')).strip()


#: The words a custom header template may use.
HEADER_FIELDS = ('problem', 'url', 'time', 'memory')


def _custom_header(template, problem_name, url, time_limit_ms, memory_limit_mb):
    """The user's own header text, written as they typed it.

    `{problem} {url} {time} {memory}` are filled and anything else in braces is
    left alone rather than raising - a template is the user's, so its comment
    markers are too. Guessing the marker would rewrite what someone wrote for a
    language this plugin cannot see.
    """
    if isinstance(template, (list, tuple)):
        lines = [str(line) for line in template]
    else:
        lines = str(template).split('\n')
    values = {'problem': _flat(problem_name), 'url': _flat(url),
              'time': '' if time_limit_ms is None else str(time_limit_ms),
              'memory': '' if memory_limit_mb is None else str(memory_limit_mb)}
    out = []
    for line in lines:
        for key in HEADER_FIELDS:
            line = line.replace('{%s}' % key, values[key])
        out.append(line.rstrip())
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    return '\n'.join(out)


def _statement_header(problem_name, url, time_limit_ms, memory_limit_mb, ext):
    """The comment block written on top of a freshly created problem file.

    Only ever used for a file this listener creates itself; an existing source
    file is opened as it is. `auto_create_header` replaces this built-in text
    entirely - as null it is the language-aware default below, as "" there is
    no header at all.
    """
    template = (get_settings() or {}).get('auto_create_header')
    if template is not None:
        return _custom_header(template, problem_name, url,
                              time_limit_ms, memory_limit_mb)

    mark = '#' if ext in _HASH_COMMENTS else '//'
    lines = []
    if _flat(problem_name):
        lines.append('%s %s' % (mark, t('header_problem', value=_flat(problem_name))))
    if _flat(url):
        lines.append('%s %s' % (mark, t('header_source', value=_flat(url))))
    if time_limit_ms is not None:
        lines.append('%s %s' % (mark, t('header_time_limit', value=time_limit_ms)))
    if memory_limit_mb is not None:
        lines.append('%s %s' % (mark, t('header_memory_limit', value=memory_limit_mb)))
    return '\n'.join(lines)


def _base_dir(view):
    """The directory a relative auto_create_dir is resolved against.

    `view` is usually None now (nothing was open when the problem arrived), and
    a relative path must not silently be resolved against whatever the plugin
    host's working directory happens to be - the focused problem file's folder
    is what the setting means.
    """
    if view is not None and view.file_name():
        return os.path.dirname(view.file_name())
    focused = _focused_source_view()
    if focused is not None:
        return os.path.dirname(focused.file_name())
    return ''


def _auto_create_file(view, problem_name, url='', time_limit_ms=None,
                      memory_limit_mb=None):
    """Create '<dir>/<problem name>.<ext>' for an incoming problem.

    Returns the view for the new file, or None when auto-create is off or no
    usable directory could be found (in which case the caller reports the
    usual 'no target' message).

    `view` may be None: the listener no longer needs a saved file to start,
    and answering a browser click when nothing was opened first is the whole
    point of that.

    A directory that exists but cannot be written to is a different story:
    the caller cannot tell that apart from "no target", so the failure is
    reported here, where the path and the OS error are still known.
    """
    settings = get_settings() or {}
    if not settings.get('auto_create_file', True):
        return None
    name = _sanitize_name(problem_name)
    if not name:
        return None
    ext = (settings.get('auto_create_extension') or 'cpp').lstrip('.')

    window = (view.window() if view is not None else None) or sublime.active_window()
    base = _base_dir(view)

    directory = (settings.get('auto_create_dir') or '').strip()
    if directory and not os.path.isabs(directory):
        directory = os.path.join(base, directory) if base else directory
    if not directory:
        # Where the Listen command was clicked, then the file being worked on:
        # in a contest "here" is the folder of the task at hand, which beats
        # whatever folder Sublime happens to have open.
        directory = _listener.get('dir') or base
    if not directory:
        folders = window.folders() if window is not None else []
        directory = folders[0] if folders else ''
    if not directory:
        # Reported here for the same reason a write failure is: the caller
        # only sees "no target" and cannot tell that no folder was open.
        sublime.status_message(t('listener_no_directory'))
        return None
    try:
        if not os.path.isdir(directory):
            os.makedirs(directory)
    except Exception as e:
        sublime.status_message(t('auto_create_failed', path=directory, error=str(e)))
        return None

    path = os.path.join(directory, '%s.%s' % (name, ext))
    if os.path.exists(path):
        # Already there: just open it instead of overwriting anything.
        return window.open_file(path) if window is not None else None

    header = _statement_header(problem_name, url, time_limit_ms, memory_limit_mb, ext)
    skeleton = (settings.get('auto_create_template') or '').lstrip('\n')
    body = (header + '\n' if header else '') + skeleton
    try:
        with open(path, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(body)
    except Exception as e:
        sublime.status_message(t('auto_create_failed', path=path, error=str(e)))
        return None

    if window is None:
        return None
    new_view = window.open_file(path)
    # Close the empty scratch view the problem arrived in, but only when it
    # really is untouched: it may be the user's actual work in progress.
    try:
        if view is not None and not view.is_dirty() and view.size() == 0:
            view.close()
    except Exception:
        pass
    print('[Algorithm Competition Assistant] auto-created %s' % path)
    return new_view


def _focused_view():
    """Whatever view the user is looking at, file or not."""
    window = sublime.active_window()
    if window is None:
        return None
    return window.active_view()


def _focused_source_view():
    """The problem file under focus, when there is one.

    The run / stress / answer panels are scratch views without a file name,
    and a file the plugin cannot run is not a problem file either.
    """
    view = _focused_view()
    if view is None:
        return None
    file_name = view.file_name()
    if not file_name:
        return None
    ext = os.path.splitext(file_name)[1].lower().lstrip('.')
    if not is_run_supported_ext(ext):
        return None
    return view


#: What one click on the extension means. `new_file` is one file per problem,
#: the way cph and FastOlympicCodingHook do it; `current_file` only ever fills
#: the problem file already under focus and never creates anything.
_TARGET_NEW = 'new_file'
_TARGET_CURRENT = 'current_file'


def _target_mode():
    """The mode in effect: the menu pick for this session, else the setting.

    A pick from the context menu outranks `companion_target` without editing
    any settings file mid-contest, and it is only remembered until the
    listener stops - the file stays the place to change the default.
    """
    pinned = _listener.get('target')
    if pinned in (_TARGET_NEW, _TARGET_CURRENT):
        return pinned
    mode = str((get_settings() or {}).get('companion_target', _TARGET_NEW)
               or '').strip().lower()
    return mode if mode in (_TARGET_NEW, _TARGET_CURRENT) else _TARGET_NEW


def resolve_target(problem_name, url='', time_limit_ms=None, memory_limit_mb=None):
    """The view an incoming problem belongs to.

    `new_file` creates a file per problem and ignores the pinned view: the pin
    is a decision about *where samples go*, and letting it outrank the mode
    meant that anyone who had ever run "Listen to Competitive Companion" in a
    file - the documented way to use it, and the thing the log of a real
    session shows - silently stopped creating files for the rest of the
    session, which reads as "the new-file feature does not work at all".

    `current_file` fills the pinned view, or the problem file under focus, and
    never creates anything. Kept apart from the POST callback so the choice is
    testable on its own.
    """
    if _target_mode() == _TARGET_CURRENT:
        pinned = _find_view(_listener.get('view_id'))
        if pinned is not None and pinned.file_name():
            return pinned
        return _focused_source_view()

    arrival = _focused_view()
    if arrival is not None and arrival.file_name():
        arrival = None        # a real file is never closed to make room
    created = _auto_create_file(arrival, problem_name, url,
                                time_limit_ms, memory_limit_mb)
    if created is not None:
        return created
    # Creation is off (auto_create_file) or has nowhere to write: a focused
    # problem file still beats dropping the samples on the floor.
    return _focused_source_view()


def _start_listener(view=None, quiet=False, target=None):
    """Start the listener, or retarget the one that is already running.

    A saved file is no longer a precondition. It used to be, and that is what
    made "click the browser, get a new problem file" unreachable: the only
    target the listener could remember then always had a file name, so the
    auto-create branch could never run.

    `target` is what a context-menu pick passes (see _target_mode); leaving it
    None keeps whatever is already in effect.

    `quiet` is the startup path - a port somebody else already holds must not
    put a complaint in the status bar of every Sublime session.
    """
    file_name = view.file_name() if view is not None else None
    view_id = view.id() if view is not None else None
    label = file_name or t('listener_auto_target')

    def report(message):
        if quiet:
            print('[Algorithm Competition Assistant] %s' % message)
        else:
            sublime.status_message(message)

    with _listener['lock']:
        _listener['view_id'] = view_id
        if target in (_TARGET_NEW, _TARGET_CURRENT):
            _listener['target'] = target
            if target == _TARGET_NEW:
                # A pick of "one new file per problem" is the more recent
                # instruction: a pin left over from listening in some file
                # would otherwise keep filling that file and look like the pick
                # did nothing. The file clicked in is still what "here" means,
                # so its folder is where the new problems go.
                _listener['view_id'] = None
                _listener['dir'] = os.path.dirname(file_name) if file_name else None
                sublime.status_message(t('listener_mode_new'))
            else:
                # Say what the key just did. A contest page sends its whole
                # task list per click, and "all seven into this file" is not
                # what most people meant when they reached for it.
                sublime.status_message(t(
                    'listener_mode_current',
                    file=os.path.basename(file_name) if file_name
                    else t('listener_auto_target')))
        elif file_name:
            # Running the plain command inside a file has always meant
            # "listen for THIS file", so it fills that file for this session
            # instead of pinning it under a mode that says otherwise.
            _listener['target'] = _TARGET_CURRENT
        if _listener['server'] is not None:
            report(t('listener_retarget', file=label))
            return
        port = 12345
        try:
            port = int(sublime.load_settings(settings_file)
                       .get('companion_port', 12345) or 12345)
            from http.server import HTTPServer
            # Bind the loopback address, not the name: HTTPServer is IPv4, so
            # "localhost" is a resolution lottery - a host whose resolver puts
            # ::1 first would leave the listener on an address no browser can
            # reach, and the doctor's probe would then call the port free.
            server = HTTPServer(('127.0.0.1', port), build_handler())
        except Exception as e:
            # A failure is not routine output. With quiet=True (the startup
            # auto-listen) this used to go to the console only, so Sublime came
            # up *not listening* while the user reasonably believed the browser
            # extension was wired up - every click then did nothing, and the
            # symptom pointed at the extension.
            report(t('listener_port_error', port=port, error=str(e)))
            if quiet:
                sublime.status_message('[Algorithm Competition Assistant] ' + t(
                    'listener_start_failed_startup', port=port, error=str(e)[:120]))
            return
        _listener['server'] = server
        _listener['port'] = port
        set_listener(server, port, view_id)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        _listener['thread'] = thread
        thread.start()

    report(t('listener_started', file=label, port=port))
    if not quiet:
        print('[Algorithm Competition Assistant] ' + t('listener_started', file=label, port=port))


def _stop_listener():
    with _listener['lock']:
        server = _listener.get('server')
        if server is None:
            sublime.status_message(t('listener_not_running'))
            return
        _listener['server'] = None
        set_listener(None)
        _listener['thread'] = None
        _listener['port'] = None
        _listener['view_id'] = None
        _listener['target'] = None
        _listener['dir'] = None
        _listener['last_run'] = None
    threading.Thread(target=server.shutdown, daemon=True).start()
    try:
        server.server_close()
    except Exception:
        pass
    sublime.status_message(t('listener_stopped'))


from .core.cph_target import visible as context_menu_visible


class CphCompanionListenerCommand(sublime_plugin.TextCommand):
    """Listen to Competitive Companion: 启动/切换目标文件 (幂等, 可反复点击).

    Without a saved file this starts the listener with no target. The context
    menu passes "target" to pick what one click means for the rest of the
    session - "new_file" creates a file per problem, "current_file" only ever
    fills the file under focus; leaving it out keeps `companion_target`.
    """

    def is_visible(self, event=None, **kwargs):
        """Context menu: only offered where this package applies.

        A .sublime-menu item has no `context` key in Sublime Text, so the
        command decides (Default's own `open_context_url` does the same).
        """
        return context_menu_visible(self.view, event)

    def run(self, edit, target=None):
        _start_listener(self.view, target=target)


class CphCompanionStopListenerCommand(sublime_plugin.TextCommand):
    """停止 Competitive Companion 监听."""

    def run(self, edit):
        _stop_listener()


def plugin_loaded():
    """随 Sublime 启动自动监听: 装好就能点, 不需要先开文件.

    Sublime calls plugin_loaded() in every top-level module of a package, so
    the auto-start lives here instead of reaching this module from
    plugin_init.py - root-level modules are not allowed to import each other
    (see core/cph_state.py for why).

    Deferred, because binding the port pulls in http.server (socket,
    http.client, email.parser), which build_handler() deliberately keeps out
    of the package's cold import. Starting it after the window is up keeps
    that cost off the startup the user waits for.
    """
    try:
        if not sublime.load_settings(settings_file).get('companion_auto_listen', True):
            return
        sublime.set_timeout(lambda: _start_listener(None, quiet=True), 1200)
    except Exception as e:
        print('[Algorithm Competition Assistant] companion auto-listen failed: %s' % e)


def plugin_unloaded():
    """插件重载/卸载时关闭监听, 释放端口, 避免重启后端口占用."""
    try:
        with _listener['lock']:
            server = _listener.get('server')
            _listener['server'] = None
            _listener['thread'] = None
            _listener['port'] = None
    except Exception:
        return
    if server is not None:
        try:
            threading.Thread(target=server.shutdown, daemon=True).start()
            server.server_close()
        except Exception:
            pass

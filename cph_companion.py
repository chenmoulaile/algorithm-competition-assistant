"""
Algorithm Competition Assistant - Competitive Companion 持久监听器
接收来自浏览器扩展 Competitive Companion 的 POST 请求
参考: https://github.com/jmerle/competitive-companion
思路来自 FastOlympicCodingHook (https://github.com/DrSwad/FastOlympicCodingHook)

与旧版 (FastOlympicCodingHook 一次性会话) 的区别:
- 一次启动后持续监听 (cph-ng 风格), 浏览器扩展可反复点击发送样例
- 再次执行本命令只会切换目标文件, 不会重复起服务器/产生端口冲突
- 收到样例后通过 cph_view_tester 正常走 Run 流程, 不会向源代码文件插入任何文字
- 收到的样例与已有样例 **合并去重**（与导入流程语义一致），不再整体覆盖
- 收到的 TL/ML 按源文件持久化，之后手动 Ctrl+Alt+B 也不会丢
"""

import sublime
import sublime_plugin
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import re
import threading

from .core.cph_i18n import t
from .core.cph_settings import (get_settings, load_all_tests, save_tests,
								set_problem_limits, settings_file)
from .core.cph_tests_merge import merge_tests as merge_stored_with
from .core.cph_state import set_listener


# Competitive Companion 官方字段单位: timeLimit = ms, memoryLimit = MB
MAX_BODY_BYTES = 10 * 1024 * 1024

_listener = {
    'server': None,      # HTTPServer instance
    'thread': None,      # serving thread
    'port': None,
    'view_id': None,     # target source view id
    'lock': threading.Lock(),
}


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


def merge_tests(file_name, incoming):
    """Merge freshly received samples with the stored ones.

    Uses the shared policy (see core/cph_tests_merge.py): deduplicated by
    INPUT, and an answer is only filled in when none is stored yet - a
    re-sent sample must never overwrite an answer the user accepted by
    hand. Conflicting answers are reported through the status bar.
    """
    merged, conflicts = merge_stored_with(load_all_tests(file_name), incoming)
    if conflicts:
        sublime.status_message(t('answer_conflict_kept'))
    return merged


def remember_limits(file_name, time_limit_ms, memory_limit_mb):
    """Remember the problem limits for this session.

    Without this the TL/ML received from the browser only applied to the
    run it triggered and a later manual Ctrl+Alt+B lost them again.
    Kept in memory on purpose: writing the user's settings file from a
    background HTTP callback is not acceptable behaviour for a plugin.
    """
    set_problem_limits(file_name, time_limit_ms, memory_limit_mb)


class _CompanionHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def _host_allowed(self):
        """Block DNS-rebinding attempts from random web pages.

        A malicious page could otherwise POST to localhost:12345 and make
        the plugin write test files and compile/run code.
        """
        host = (self.headers.get('Host') or '').strip().lower()
        if not host:
            return True   # non-browser clients (curl/scripts) usually omit it
        port = _listener.get('port') or ''
        allowed = {
            'localhost', '127.0.0.1', '[::1]',
            'localhost:%s' % port, '127.0.0.1:%s' % port, '[::1]:%s' % port,
        }
        return host in allowed

    def _deny(self, code=403):
        try:
            self.send_response(code)
            self.end_headers()
            self.wfile.write(b'forbidden')
        except Exception:
            pass

    def do_POST(self):
        if not self._host_allowed():
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
            # A negative Content-Length used to slip past the check above and
            # make rfile.read(-1) block until EOF, hanging the handler.
            self._deny(400)
            return

        try:
            raw = self.rfile.read(content_length) if content_length else b''
            data = json.loads(raw.decode('utf-8'))
        except Exception:
            self._deny(400)
            return

        # Everything below touches the Sublime API -> main thread only
        sublime.set_timeout(lambda data=data: self._handle_on_main(data), 0)

        try:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'ok')
        except Exception:
            pass
        # 保持服务器运行, 不 shutdown —— 浏览器扩展可以反复点击

    def _handle_on_main(self, data):
        try:
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

            view = _find_view(_listener.get('view_id'))
            if view is not None and not view.file_name():
                # Auto-create: an untitled view is the normal state when a
                # problem arrives before any file was opened, and asking the
                # user to save first defeats the point of the listener.
                view = _auto_create_file(view, problem_name) or view
            if view is None or not view.file_name():
                sublime.status_message(t('listener_no_target'))
                return

            file_name = view.file_name()
            merged = merge_tests(file_name, incoming)
            if not save_tests(file_name, merged):
                sublime.status_message(t('import_save_failed'))
                return
            remember_limits(file_name, time_limit_ms, memory_limit_mb)
            print('[Algorithm Competition Assistant] %s' % t('new_test_file_path', path=file_name))

            count = len(incoming)
            tl = time_limit_ms if time_limit_ms is not None else '-'
            ml = memory_limit_mb if memory_limit_mb is not None else '-'

            view.run_command('cph_view_tester', {
                'action': 'make_opd',
                'time_limit_ms': time_limit_ms,
                'memory_limit_mb': memory_limit_mb,
            })
            sublime.status_message(t('tests_received',
                                     name=problem_name or '?',
                                     count=count, time=tl, memory=ml))
        except Exception as e:
            print(t('error_handling_post', error=str(e)))



def _sanitize_name(name):
    """Turn a problem name into a file name that Windows accepts."""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', ' ', name or '').strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned[:80].rstrip('. ')


def _auto_create_file(view, problem_name):
    """Create '<dir>/<problem name>.<ext>' for an incoming problem.

    Returns the view for the new file, or None when auto-create is off or no
    usable directory could be found (in which case the caller reports the
    usual 'no target' message).

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

    directory = (settings.get('auto_create_dir') or '').strip()
    if directory and not os.path.isabs(directory):
        base = os.path.dirname(view.file_name() or '') or ''
        directory = os.path.join(base, directory) if base else directory
    if not directory:
        window = view.window() or sublime.active_window()
        folders = window.folders() if window is not None else []
        if folders:
            directory = folders[0]
    if not directory:
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
        window = view.window() or sublime.active_window()
        return window.open_file(path) if window is not None else None

    skeleton = settings.get('auto_create_template') or ''
    try:
        with open(path, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(skeleton)
    except Exception as e:
        sublime.status_message(t('auto_create_failed', path=path, error=str(e)))
        return None

    window = view.window() or sublime.active_window()
    if window is None:
        return None
    new_view = window.open_file(path)
    # Close the empty scratch view the problem arrived in, but only when it
    # really is untouched: it may be the user's actual work in progress.
    try:
        if not view.is_dirty() and view.size() == 0:
            view.close()
    except Exception:
        pass
    print('[Algorithm Competition Assistant] auto-created %s' % path)
    return new_view


def _start_listener(view):
    file_name = view.file_name()
    if not file_name:
        sublime.status_message(t('listener_need_save'))
        return

    with _listener['lock']:
        _listener['view_id'] = view.id()
        if _listener['server'] is not None:
            sublime.status_message(t('listener_retarget', file=file_name))
            return
        port = 12345
        try:
            port = int(sublime.load_settings(settings_file)
                       .get('companion_port', 12345) or 12345)
            server = HTTPServer(('localhost', port), _CompanionHandler)
        except Exception as e:
            sublime.status_message(t('listener_port_error', port=port, error=str(e)))
            return
        _listener['server'] = server
        _listener['port'] = port
        set_listener(server, port, view.id())
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        _listener['thread'] = thread
        thread.start()

    sublime.status_message(t('listener_started', file=file_name, port=port))
    print('[Algorithm Competition Assistant] ' + t('listener_started', file=file_name, port=port))


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
    threading.Thread(target=server.shutdown, daemon=True).start()
    try:
        server.server_close()
    except Exception:
        pass
    sublime.status_message(t('listener_stopped'))


from .core.cph_target import visible as context_menu_visible


class CphCompanionListenerCommand(sublime_plugin.TextCommand):
    """Listen to Competitive Companion: 启动/切换目标文件 (幂等, 可反复点击)."""

    def is_visible(self, event=None, **kwargs):
        """Context menu: only offered where this package applies.

        A .sublime-menu item has no `context` key in Sublime Text, so the
        command decides (Default's own `open_context_url` does the same).
        """
        return context_menu_visible(self.view, event)

    def run(self, edit):
        _start_listener(self.view)


class CphCompanionStopListenerCommand(sublime_plugin.TextCommand):
    """停止 Competitive Companion 监听."""

    def run(self, edit):
        _stop_listener()


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

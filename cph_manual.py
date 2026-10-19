"""
Algorithm Competition Assistant - 手动交互终端

针对交互题（如 CF 1486/C2）最直接的测试方式：开一个类终端的 scratch 视图，
用户手动输入、程序实时回显输出。与 core/cph_interactive.py 的 interactor
自动对拍互补——那里是"程序 vs 交互器自动判定"，这里是"人当交互器"。

Root 模块互不 import（Package Control 规则），所以这里只依赖
Modules/ProcessManager 与 core/。
"""

import sublime
import sublime_plugin
import os
import threading

from .Modules.ProcessManager import ProcessManager
from .core.cph_i18n import t
from .core.cph_settings import get_settings, is_run_supported_ext

#: 每次从进程管道读取的字符数。read(bfsize) 会在拿到至少 1 字节后立刻返回，
#: 所以大缓冲既保证低延迟（prompt 不带换行也能马上上屏），又避免逐字节
#: 系统调用的慢路径（见 ProcessManager.read 的注释）。
_READ_SIZE = 65536

#: region 存储键，锚定"当前输入行"的起点
_MARKER_KEY = 'cph_term_input'

#: 终端视图 settings 标记，keymap 的 context 靠它生效
_TERMINAL_FLAG = 'cph_interactive_terminal'
#: 终端视图反向记录它属于哪个源文件
_TERMINAL_FILE = 'cph_interactive_terminal_file'

#: view.id() -> _Session。视图关闭时由 EventListener 清理。
_SESSIONS = {}
_LOCK = threading.Lock()


def _clean_newlines(s):
	"""CRLF / 孤立 CR 归一成 LF，避免视图里出现 <0x0d>。"""
	return s.replace('\r\n', '\n').replace('\r', '\n')


def _terminal_view_for(window, file):
	"""在当前窗口里找该源文件已有的终端视图，找不到返回 None。"""
	for v in window.views():
		if v.settings().get(_TERMINAL_FLAG) and \
				v.settings().get(_TERMINAL_FILE) == file:
			return v
	return None


def _sessions_for_file(file):
	"""该源文件所有仍存活（编译中/运行中）的 session。"""
	with _LOCK:
		return [s for s in list(_SESSIONS.values())
				if s.file == file and s.alive()]


def _session_for(view):
	"""终端视图对应的 session，可能为 None（旧会话已结束）。"""
	with _LOCK:
		return _SESSIONS.get(view.id())


def _forget(view):
	with _LOCK:
		_SESSIONS.pop(view.id(), None)


class _Session(object):
	"""一次"编译 + 运行 + 输出泵"的完整生命周期。"""

	def __init__(self, view, file):
		self.view = view
		self.file = file
		self.closed = False
		# ProcessManager.syntax 目前无人消费，只作占位：终端模式永远走 stdin。
		self.pm = ProcessManager(file, '', run_settings=get_settings()
								 .get('run_settings'))
		# 终端要的是 stderr 直接混进 stdout（用户要看见 cerr 调试信息），
		# 与测试面板的 ignore_stderr 语义相反，这里固定关掉。
		self.pm.set_separate_stderr(False)
		# 手动终端永远走 stdin，不管题目本身是不是文件 IO。
		self.pm.set_io_spec({'mode': 'stdin'})
		limit = get_settings().get('max_output_bytes', 0)
		try:
			limit = int(limit or 0)
		except (TypeError, ValueError):
			limit = 0
		self.pm.set_output_limit(limit)
		self.compiling = False
		self.running = False

	# -- 状态 ------------------------------------------------------------
	def alive(self):
		"""编译中或程序仍在跑；closed 之后不再算活。"""
		if self.closed:
			return False
		if self.compiling:
			return True
		return self.pm.is_stopped() is None

	def stop(self):
		self.closed = True
		self.compiling = False
		self.running = False
		self.pm.terminate_async()

	# -- 视图写入（只允许主线程调用，泵线程经 set_timeout 进来） ------
	def post_append(self, text):
		if self.closed or not text:
			return
		sublime.set_timeout(lambda: self._append_now(text), 0)

	def _append_now(self, text):
		if self.closed:
			return
		self.view.run_command('cph_manual_append', {'text': text})

	# -- 输入 ------------------------------------------------------------
	def pending_input(self):
		"""从输入锚点到视图末尾的文本（含上次输出后用户敲的内容）。"""
		r = self.view.get_regions(_MARKER_KEY)
		start = r[0].a if r else self.view.size()
		start = min(start, self.view.size())
		return _clean_newlines(self.view.substr(sublime.Region(start, self.view.size())))

	# -- 编译 + 运行（worker 线程） --------------------------------------
	def start(self):
		self.compiling = True
		thread = threading.Thread(target=self._worker)
		thread.daemon = True
		thread.start()

	def _worker(self):
		try:
			result = self.pm.compile()
		except Exception as e:
			result = (1, '[Algorithm Competition Assistant] compile failed: %s' % e)
		if self.closed:
			return
		sublime.set_timeout(lambda: self._after_compile(result), 0)

	def _after_compile(self, result):
		if self.closed:
			return
		self.compiling = False
		if result is None:
			# 解释类语言：没有编译命令，直接跑。
			self._launch()
			return
		rc, note = result
		if rc != 0:
			self.post_append(_clean_newlines(note or '') + '\n')
			sublime.status_message('[Algorithm Competition Assistant] %s'
								   % t('compile_failed_status'))
			return
		if (note or '').strip():
			fixed = _clean_newlines(note)
			if not fixed.endswith('\n'):
				fixed += '\n'
			self.post_append(fixed)
		self._launch()

	def _launch(self):
		if self.closed:
			return
		try:
			self.pm.run_file()
		except Exception as e:
			self.post_append('[Algorithm Competition Assistant] run failed: %s\n' % e)
			return
		self.running = True
		pump = threading.Thread(target=self._pump)
		pump.daemon = True
		pump.start()

	# -- 输出泵（worker 线程） --------------------------------------------
	def _pump(self):
		pm = self.pm
		while True:
			chunk = ''
			try:
				chunk = pm.read(_READ_SIZE)
			except Exception:
				chunk = ''
			if chunk:
				self.post_append(chunk)
			if pm.is_stopped() is not None:
				# 进程已退：把管道里剩余的字节排干再收尾。
				try:
					drain = pm.read()
				except Exception:
					drain = ''
				if drain:
					self.post_append(drain)
				pm.finish_memory_sampling()
				pm.is_run = False
				rc = pm.is_stopped()
				self.running = False
				if not self.closed:
					sublime.set_timeout(
						lambda rc=rc: self.post_append(
							'\n[%s %d]\n' % (t('manual_exit'), rc)), 0)
				return
			if self.closed:
				return


class CphManualTerminalCommand(sublime_plugin.TextCommand):
	"""在源文件视图上打开/聚焦/重启交互终端（Ctrl+Alt+Shift+X）。

	- 该文件的终端视图不存在：新建并启动一次编译+运行。
	- 终端还在编译或程序还在跑：只是聚焦，不打断。
	- 上一轮已经结束：复用同一个视图追加新一轮（不清屏，保留时间线）。
	"""

	def run(self, edit):
		view = self.view
		window = view.window()
		if window is None:
			return
		# 终端视图自身按快捷键 = 重启（见 keymap context）。
		if view.settings().get(_TERMINAL_FLAG):
			file = view.settings().get(_TERMINAL_FILE)
			if not file:
				return
			sess = _session_for(view)
			if sess and sess.alive():
				sublime.status_message('[Algorithm Competition Assistant] %s'
									   % t('manual_already_running'))
				return
			_session_for_file_restart(view, file)
			return
		file = view.file_name()
		if not file:
			sublime.status_message('[Algorithm Competition Assistant] %s'
								   % t('save_file_first'))
			return
		window.run_command('cph_manual_open', {'file': file})


class CphManualOpenCommand(sublime_plugin.WindowCommand):
	"""给命令面板/菜单用：显式指定要在交互终端里跑的文件。"""

	def run(self, file=None):
		window = self.window
		if file is None:
			view = window.active_view()
			file = view.file_name() if view else None
		if not file:
			sublime.status_message('[Algorithm Competition Assistant] %s'
								   % t('save_file_first'))
			return
		ext = os.path.splitext(file)[1][1:]
		if not is_run_supported_ext(ext):
			sublime.status_message('[Algorithm Competition Assistant] %s'
								   % t('unsupported_language', ext=ext))
			return
		# 现有终端还在编译/运行 -> 只聚焦，绝不悄悄杀掉正在交互的程序。
		term = _terminal_view_for(window, file)
		if term is not None:
			sess = _session_for(term)
			if sess and sess.alive():
				window.focus_view(term)
				return
		# run_cmd 没配置/不是字符串时 run_file 会把 None 交给 Popen 炸掉，
		# 提前拦下，给一条能看懂的话。
		probe = ProcessManager(file, '',
							   run_settings=get_settings().get('run_settings'))
		try:
			run_cmd = probe.get_run_cmd('')
		except Exception:
			run_cmd = None
		if not (isinstance(run_cmd, str) and run_cmd.strip()):
			sublime.status_message('[Algorithm Competition Assistant] %s'
								   % t('unsupported_language', ext=ext))
			return
		# 视图已经关了但进程可能还挂着 -> 收掉旧会话，防双进程。
		for old in _sessions_for_file(file):
			old.stop()
			_forget(old.view)
		term = window.new_file()
		term.set_scratch(True)
		term.set_name('%s -term' % os.path.basename(file))
		term.set_syntax_file('Packages/Text/Plain Text.tmLanguage')
		term.settings().set(_TERMINAL_FLAG, True)
		term.settings().set(_TERMINAL_FILE, file)
		term.settings().set('word_wrap', True)
		_start_session(term, file)


def _anchor(view):
	"""把输入锚点钉到视图末尾（regions API 不需要 edit 事务）。"""
	view.add_regions(_MARKER_KEY,
					 [sublime.Region(view.size(), view.size())],
					 flags=sublime.HIDDEN)


def _start_session(view, file):
	"""清掉旧锚点，从当前末尾开一个新输入锚点，然后起编译+运行线程。"""
	view.erase_regions(_MARKER_KEY)
	_anchor(view)
	sess = _Session(view, file)
	with _LOCK:
		_SESSIONS[view.id()] = sess
	sess.start()


def _session_for_file_restart(view, file):
	"""同一终端视图上开新一轮：保留旧输出，锚点移到当前末尾。"""
	_anchor(view)
	sess = _Session(view, file)
	with _LOCK:
		_SESSIONS[view.id()] = sess
	sess.start()


class CphManualAppendCommand(sublime_plugin.TextCommand):
	"""把一段程序输出插到输入锚点之前（输出永远出现在用户输入的上方）。"""

	def run(self, edit, text=''):
		if not text:
			return
		r = self.view.get_regions(_MARKER_KEY)
		point = r[0].a if r else self.view.size()
		point = min(point, self.view.size())
		self.view.insert(edit, point, text)
		# 锚点被插入顶走了，重新钉回末尾（用户输入始终在最后一行）。
		self.view.add_regions(_MARKER_KEY,
							  [sublime.Region(self.view.size(), self.view.size())],
							  flags=sublime.HIDDEN)
		self.view.show(self.view.size(), False)


class CphManualAnchorCommand(sublime_plugin.TextCommand):
	"""把输入锚点钉到视图末尾（开新会话时）。"""

	def run(self, edit):
		_anchor(self.view)


def _commit_send(view, edit, line):
	"""提交一次输入：锚点后的旧残留换成 line，光标钉回末尾。"""
	r = view.get_regions(_MARKER_KEY)
	start = r[0].a if r else view.size()
	start = min(start, view.size())
	if start < view.size():
		view.erase(edit, sublime.Region(start, view.size()))
	view.insert(edit, view.size(), line + '\n')
	view.add_regions(_MARKER_KEY,
					 [sublime.Region(view.size(), view.size())],
					 flags=sublime.HIDDEN)
	view.sel().clear()
	view.sel().add(sublime.Region(view.size()))
	view.show(view.size(), False)


def _terminal_session(view):
	"""当前终端视图仍活着的会话，否则 None。"""
	sess = _session_for(view)
	if sess and not sess.closed and sess.pm.is_stopped() is None:
		return sess
	return None


class CphManualSendLineCommand(sublime_plugin.TextCommand):
	"""keymap: enter -> 发送当前输入行（视图补换行 + 写 stdin）。"""

	def run(self, edit):
		view = self.view
		sess = _terminal_session(view)
		line = _pending(view)
		if sess:
			_commit_send(view, edit, line)
			sess.pm.insert(line + '\n')
		else:
			# 程序已经退出：回车只补换行，stdin 写入是空操作。
			_commit_send(view, edit, line)
			if _session_for(view) and _session_for(view).closed:
				sublime.status_message('[Algorithm Competition Assistant] %s'
									   % t('manual_program_ended'))


def _pending(view):
	r = view.get_regions(_MARKER_KEY)
	start = r[0].a if r else view.size()
	start = min(start, view.size())
	return _clean_newlines(view.substr(sublime.Region(start, view.size())))


class CphManualEofCommand(sublime_plugin.TextCommand):
	"""keymap: ctrl+d -> 提交未发送的输入行（保留回显），然后 EOF。"""

	def run(self, edit):
		view = self.view
		sess = _terminal_session(view)
		if not sess:
			return
		line = _pending(view)
		_commit_send(view, edit, line)
		if line:
			sess.pm.insert(line + '\n')
		sess.pm.close_stdin()


class CphManualStopCommand(sublime_plugin.TextCommand):
	"""keymap: ctrl+x -> 杀掉正在跑的程序（视图保留，可重启）。"""

	def run(self, edit):
		sess = _session_for(self.view)
		if sess and not sess.closed:
			sess.stop()
			sublime.status_message('[Algorithm Competition Assistant] %s'
								   % t('manual_stopped'))


class CphManualEvents(sublime_plugin.EventListener):
	"""终端视图关掉 = 进程一起带走；源文件视图不受影响。"""

	def on_close(self, view):
		if not view.settings().get(_TERMINAL_FLAG):
			return
		sess = _session_for(view)
		if sess:
			sess.stop()
		_forget(view)

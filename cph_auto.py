"""Algorithm Competition Assistant - 保存后自动运行 / 自动格式化

两个都是 CP Editor 的标配，但都会"抢方向盘"，所以**默认关闭**，由设置打开：

- ``auto_run_on_save``：保存支持的源文件后自动跑一次测试。带防抖，连续保存
  只在最后一次之后触发；正在跑的时候不会打断。
- ``auto_format_on_save``：保存后按语言的 ``format_cmd`` 格式化（clang-format /
  black / rustfmt ...），文件真的变了才 revert 视图。

用 ``on_post_save_async``：它不在主线程里跑重活，但里面所有触碰视图的操作都
回到 ``sublime.set_timeout``，遵守 ST 的线程约定。
"""

import os
import shlex
import subprocess
import threading
import time

import sublime
import sublime_plugin

from .core.cph_settings import get_settings, is_run_supported_ext
from .core.cph_i18n import t


def _hidden_startupinfo():
	"""STARTUPINFO that keeps a console window from flashing (Windows).

	Returns None elsewhere, so it can be passed to Popen unconditionally.
	"""
	try:
		import sublime
		windows = sublime.platform() == 'windows'
	except Exception:
		windows = os.name == 'nt'
	if not windows:
		return None
	try:
		info = subprocess.STARTUPINFO()
		info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
		return info
	except Exception:
		return None

#: file -> token; a newer save bumps the token so the older timer does nothing
_run_tokens = {}
_pending_locks = {}


def _settings():
	try:
		return get_settings() or {}
	except Exception:
		return {}


def _entry_for(settings, file_name):
	ext = os.path.splitext(file_name)[1][1:]
	for entry in (settings.get('run_settings') or []):
		if ext in (entry.get('extensions') or []):
			return entry
	return None


def _debounce_seconds(settings):
	try:
		return max(0.0, float(settings.get('auto_run_debounce_seconds', 0.8)))
	except (TypeError, ValueError):
		return 0.8


def _run_view_of(window, file_name):
	if window is None:
		return None
	target = os.path.basename(file_name) + ' -run'
	for view in window.views():
		if (view.name() or '') == target:
			return view
	return None


def _start_run(view, settings, file_name):
	"""Kick off a run the same way Ctrl+Alt+B does."""
	token = time.time()
	_run_tokens[file_name] = token

	def fire():
		# A later save (or a manual run) replaced this one.
		if _run_tokens.get(file_name) != token:
			return
		if not view.is_valid():
			return
		window = view.window()
		if window is None:
			return
		run_view = _run_view_of(window, file_name)
		try:
			if run_view is not None:
				run_view.run_command('cph_test_manager', {
					'action': 'make_opd', 'load_session': True,
					'run_file': file_name})
			else:
				view.run_command('cph_view_tester', {'action': 'make_opd'})
		except Exception as e:
			# The user saved expecting a run because auto_run_on_save is on.
			# A console line is not an answer to "why did nothing happen".
			print('[Algorithm Competition Assistant] auto run failed: %s' % e)
			sublime.status_message('[Algorithm Competition Assistant] ' + t(
				'auto_run_failed', reason=str(e)[:160]))

	sublime.set_timeout(fire, int(_debounce_seconds(settings) * 1000))


def _format_file(view, entry, file_name):
	"""Run the language's format_cmd and reload the buffer when it changed."""
	cmd = (entry.get('format_cmd') or '').strip()
	if not cmd:
		return
	expanded = (cmd.replace('{source_file}', '"%s"' % file_name)
				.replace('{source_file_dir}', '"%s"' % os.path.dirname(file_name))
				.replace('{file_name}', os.path.splitext(
					os.path.basename(file_name))[0]))
	before = ''
	try:
		with open(file_name, 'rb') as handle:
			before = handle.read()
	except Exception:
		pass

	def worker():
		try:
			subprocess.Popen(expanded, shell=True,
							 cwd=os.path.dirname(file_name) or None,
							 startupinfo=_hidden_startupinfo()).wait()
		except Exception as e:
			print('[Algorithm Competition Assistant] format failed: %s' % e)
			# auto_format_on_save is opt-in, so an empty format_cmd or a
			# clang-format missing from PATH otherwise looks like the plugin
			# quietly deciding not to format.
			sublime.status_message('[Algorithm Competition Assistant] ' + t(
				'format_failed', reason=str(e)[:160]))
			return
		after = ''
		try:
			with open(file_name, 'rb') as handle:
				after = handle.read()
		except Exception:
			return
		if after == before:
			return

		def reload_view():
			if view.is_valid():
				# The buffer is clean (we just saved), so reverting only
				# picks up the formatter's changes.
				view.run_command('revert')
		sublime.set_timeout(reload_view, 0)

	thread = threading.Thread(target=worker)
	thread.daemon = True
	thread.start()


class CphAutoSaveListener(sublime_plugin.EventListener):
	"""Auto-run / auto-format after a save (both off by default)."""

	def on_post_save_async(self, view):
		settings = _settings()
		auto_run = bool(settings.get('auto_run_on_save', False))
		auto_format = bool(settings.get('auto_format_on_save', False))
		if not (auto_run or auto_format):
			return
		file_name = view.file_name()
		if not file_name:
			return
		ext = os.path.splitext(file_name)[1][1:]
		if not ext or not is_run_supported_ext(ext):
			return
		entry = _entry_for(settings, file_name)
		if entry is None:
			return

		if auto_format:
			_format_file(view, entry, file_name)
		if auto_run:
			# Never interrupt a run that is already in progress.
			if view.settings().get('cph_run_view'):
				return
			window = view.window()
			run_view = _run_view_of(window, file_name)
			if run_view is not None and run_view.settings().get('process_status') == 'RUNNING':
				return
			_start_run(view, settings, file_name)

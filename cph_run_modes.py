"""
Algorithm Competition Assistant - 运行模式命令（跑完全部 / 只重跑失败 / 强制重新编译）

这三个模式以前只能靠改设置或手动一个个点，这里做成命令，方便挂到
命令面板与快捷键上。
"""

import os

import sublime
import sublime_plugin

from .core.cph_build_mode import MODE_DEBUG, MODE_RELEASE, get_mode, toggle as toggle_build_mode
from .core.cph_i18n import t

#: Palette captions are Python strings (a menu caption is static JSON and can
#: never follow the `language` setting), so they all start with the package
#: name. Same constant as cph_options.py - root modules may not import each
#: other, so it is repeated deliberately.
PREFIX = 'Algorithm Competition Assistant: '


def _is_run_view(view):
	"""The -run panel is a scratch view tagged with this setting."""
	return bool(view.settings().get('cph_run_view'))


def _paired_run_view(view, file_name):
	"""The '<name> -run' view of this source file, if it is open."""
	window = view.window()
	if window is None or not file_name:
		return None
	target = os.path.split(file_name)[-1] + ' -run'
	for other in window.views():
		if (other.name() or '') == target:
			return other
	return None


class _RunModeMixin(object):
	"""Dispatch a run with extra make_opd flags on the owning source view."""

	flags = {}

	def run(self, edit):
		view = self.view
		args = {'action': 'make_opd'}
		args.update(self.flags)

		if _is_run_view(view):
			# Invoked from inside the panel: reuse its session (source file,
			# limits, output mode) and just add the mode flag on top.
			args['load_session'] = True
			view.run_command('cph_test_manager', args)
			return

		if not view.file_name():
			sublime.status_message('[Algorithm Competition Assistant] ' + t('save_file_first'))
			return
		view.run_command('cph_view_tester', args)

	def is_enabled(self):
		return _is_run_view(self.view) or bool(self.view.file_name())


class CphRunAllTestsCommand(_RunModeMixin, sublime_plugin.TextCommand):
	"""Run every test, continuing past failures."""

	flags = {'run_all': True}


class CphRunFailedTestsCommand(_RunModeMixin, sublime_plugin.TextCommand):
	"""Re-run only the tests that are not AC yet (skips the accepted ones)."""

	flags = {'run_failed': True}


class CphRunForceRecompileCommand(_RunModeMixin, sublime_plugin.TextCommand):
	"""Run ignoring the compile cache (force a fresh compile)."""

	flags = {'force_compile': True}


class CphRunParallelCommand(_RunModeMixin, sublime_plugin.TextCommand):
	"""Judge every test at the same time with a small worker pool.

	Serial execution costs roughly the time limit times the number of tests
	when several of them time out; with the default 4 workers that drops to
	about a quarter. See core/cph_parallel and the `parallel_workers` setting.
	"""

	flags = {'parallel': True}


class CphToggleBuildModeCommand(sublime_plugin.TextCommand):
	"""Switch the compile command between release (-O2) and debug.

	Debug mode adds ``-g -fsanitize=undefined -fsanitize-trap=undefined
	-fno-omit-frame-pointer -D_GLIBCXX_ASSERTIONS`` and drops the
	optimisation, so a crash prints ``file:line`` and the detail view can jump
	straight to it. The trap form is deliberate: plain ``-fsanitize=undefined``
	needs libubsan at link time and ``-fsanitize=address,undefined`` needs both
	runtimes, so on toolchains that ship neither (MSYS2 mingw gcc: "ld.exe:
	cannot find -lubsan/-lasan") every debug build would fail once and fall
	back to a build with no UB detection. Trapping needs no runtime and still
	turns signed overflow and out-of-bounds array access into a non-zero exit
	code, and core/cph_gdb_trace.py recovers the line from the SIGILL. The
	assertion macro is what catches out-of-bounds ``std::vector`` access: that
	is undefined behaviour rather than a crash, so at ``-O2`` it prints garbage
	and exits 0 and the judge can only report WA. Release mode guarantees
	``-O2`` and removes the sanitizer and assertion flags again, so local
	timings mean something. The mode is remembered per source file in memory
	only - the user's settings file is never touched.
	"""

	def run(self, edit):
		view = self.view
		file_name = view.file_name()
		if not file_name:
			sublime.status_message('[Algorithm Competition Assistant] ' + t('save_file_first'))
			return
		mode = toggle_build_mode(file_name)
		if mode == MODE_DEBUG:
			detail = t('build_mode_debug_detail')
		else:
			detail = t('build_mode_release_detail')
		sublime.status_message('[Algorithm Competition Assistant] '
							   + t('build_mode_now', mode=mode, detail=detail))

		# The compile cache key includes the command, so the next run
		# recompiles automatically; just refresh the panel's status label.
		target = view if _is_run_view(view) else _paired_run_view(view, file_name)
		if target is not None:
			target.run_command('cph_test_manager', {'action': 'refresh_status'})

	def is_enabled(self):
		return _is_run_view(self.view) or bool(self.view.file_name())

	def description(self):
		file_name = self.view.file_name()
		mode = get_mode(file_name) if file_name else MODE_RELEASE
		return PREFIX + t('opt_build_mode', mode=mode)


class CphTestMenuCommand(sublime_plugin.TextCommand):
	"""Pick a test and run / inspect / edit it without the mouse."""

	def run(self, edit):
		view = self.view
		if _is_run_view(view):
			view.run_command('cph_test_manager', {'action': 'show_test_menu'})
			return
		if not view.file_name():
			sublime.status_message('[Algorithm Competition Assistant] ' + t('save_file_first'))
			return
		view.run_command('cph_view_tester', {'action': 'make_opd'})

	def is_enabled(self):
		return _is_run_view(self.view) or bool(self.view.file_name())

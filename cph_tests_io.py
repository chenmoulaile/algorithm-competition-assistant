"""
Algorithm Competition Assistant - 测试数据杂项 I/O

- 从剪贴板新增测试点（"输入 --- 输出" 或整段当输入）
- 复制某个测试点的 输入 / 预期输出 / 实际输出
- 把全部测试点导出成 1.in / 1.out 文件对（方便离线调试或分享）

以前这些都要手动开文件复制粘贴，是竞赛里高频但是零散的操作。
"""

import os
import re
import sublime
import sublime_plugin

from .core.cph_i18n import t
from .core.cph_settings import load_all_tests, notify_io_error
from .core.cph_tests_merge import merge_into_file


SEPARATOR = re.compile(r'^[ \t]*[-=]{3,}[ \t]*$', re.M)


def _resolve_source_file(view):
	"""Source file owning the tests, even when invoked from the -run panel."""
	if view.file_name():
		return view.file_name()
	name = view.name() or ''
	if name.endswith(' -run'):
		base = name[:-len(' -run')]
		for window in sublime.windows():
			for v in window.views():
				fn = v.file_name()
				if fn and os.path.basename(fn) == base:
					return fn
	return None


def _is_run_view(view):
	return bool(view.settings().get('cph_run_view'))


def _refresh_panel(view):
	"""Re-run make_opd on the paired -run view so the cards pick up the change."""
	# Must use the view marker: the old 'opd_info' status value now carries
	# the language/limits label, so comparing it silently disabled the
	# refresh when the command was run from the panel itself.
	if _is_run_view(view):
		view.run_command('cph_test_manager', {'action': 'make_opd', 'load_session': True})
		return
	window = view.window()
	if window is None or not view.file_name():
		return
	target = os.path.basename(view.file_name()) + ' -run'
	for v in window.views():
		if (v.name() or '') == target:
			v.run_command('cph_test_manager', {'action': 'make_opd', 'load_session': True})
			break


def _merge_into_tests(file_name, new_tests):
	"""Merge into the stored tests using the shared policy."""
	saved, total, conflicts = merge_into_file(file_name, new_tests)
	if conflicts:
		sublime.status_message(t('answer_conflict_kept'))
	return saved, total


class CphAddTestFromClipboardCommand(sublime_plugin.TextCommand):
	"""Add a test case from the clipboard."""

	def run(self, edit):
		view = self.view
		file_name = _resolve_source_file(view)
		if not file_name:
			sublime.status_message(t('save_file_first'))
			return

		try:
			content = sublime.get_clipboard()
		except Exception:
			content = ''
		if not content or not content.strip():
			sublime.status_message(t('clipboard_empty'))
			return

		parts = SEPARATOR.split(content, maxsplit=1)
		if len(parts) == 2:
			test_input, answer = parts[0], parts[1].strip()
		else:
			test_input, answer = content, ''

		new_test = {
			'test': test_input,
			'correct_answers': [answer] if answer else [],
		}
		ok, total = _merge_into_tests(file_name, [new_test])
		if not ok:
			# The clipboard content is already gone from the panel's point of
			# view; a flashing status_message was the only trace of a test
			# point that never reached the disk.
			notify_io_error(file_name)
			return
		sublime.status_message(t('clipboard_test_added', total=total))
		_refresh_panel(view)


class _CopyTestPartCommand(sublime_plugin.TextCommand):
	"""Base: copy one part of the selected test to the clipboard."""

	part_name = 'input'

	def run(self, edit):
		view = self.view

		# Inside the -run panel the live test model knows exactly which test
		# the cursor is in (and holds the actual output of the last run).
		if _is_run_view(view):
			view.run_command('cph_test_manager', {
				'action': 'copy_test_part', 'part': self.part_name})
			return

		# Otherwise fall back to the stored tests of the source file.
		file_name = _resolve_source_file(view)
		if not file_name:
			sublime.status_message(t('save_file_first'))
			return
		tests = load_all_tests(file_name) or []
		if not tests:
			sublime.status_message(t('no_tests'))
			return
		value = (self.part(tests[len(tests) - 1]) or '').rstrip('\n')
		if not value.strip():
			sublime.status_message(t('nothing_to_copy'))
			return
		sublime.set_clipboard(value)
		sublime.status_message(t('copied_test_part', id=len(tests)))

	def part(self, test):
		raise NotImplementedError

	def is_enabled(self):
		return _is_run_view(self.view) or bool(_resolve_source_file(self.view))


class CphCopyTestInputCommand(_CopyTestPartCommand):
	"""Copy the input of a test."""

	part_name = 'input'

	def part(self, test):
		return test.get('test', '')


class CphCopyTestExpectedCommand(_CopyTestPartCommand):
	"""Copy the expected answer of a test."""

	part_name = 'expected'

	def part(self, test):
		answers = test.get('correct_answers') or []
		return answers[0] if answers else ''


class CphCopyTestActualCommand(_CopyTestPartCommand):
	"""Copy the actual output of the last run (run panel only)."""

	part_name = 'actual'

	def part(self, test):
		return test.get('stdout', '')


class CphExportTestsCommand(sublime_plugin.TextCommand):
	"""Export all tests as 1.in / 1.out file pairs."""

	def run(self, edit):
		view = self.view
		file_name = _resolve_source_file(view)
		if not file_name:
			sublime.status_message(t('save_file_first'))
			return

		tests = load_all_tests(file_name) or []
		if not tests:
			sublime.status_message(t('no_tests'))
			return

		window = view.window()
		default_dir = os.path.join(os.path.dirname(file_name), 'tests_export')
		window.show_input_panel(
			t('export_to_dir') + ':', default_dir, self._export, None, None)

	def _export(self, target_dir):
		target_dir = (target_dir or '').strip()
		if not target_dir:
			return
		file_name = _resolve_source_file(self.view)
		tests = load_all_tests(file_name) or []
		try:
			if not os.path.isdir(target_dir):
				os.makedirs(target_dir)
			for i, test in enumerate(tests, 1):
				with open(os.path.join(target_dir, '%d.in' % i), 'w',
						  encoding='utf-8') as f:
					f.write(test.get('test', ''))
				answers = test.get('correct_answers') or []
				if answers:
					with open(os.path.join(target_dir, '%d.out' % i), 'w',
							  encoding='utf-8') as f:
						f.write(answers[0])
			sublime.status_message(t('export_done', count=len(tests), dir=target_dir))
		except Exception as e:
			sublime.error_message(t('import_failed', error=str(e)))

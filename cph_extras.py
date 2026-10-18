"""Algorithm Competition Assistant - 附加命令：抓题、题面预览、比赛计时、benchmark、统计、机器校准

这些命令都不改用户设置，数据写在本插件自己的文件里（`Packages/User/` 下的
`Algorithm Competition Assistant-*.json`）。逻辑放在 core/，这里只做命令外壳。
"""

import os
import re
import threading
import time

import sublime
import sublime_plugin

from .core.cph_calibrate import (format_report as calibrate_report,
								 load_factor, measure as calibrate_measure)
from .core.cph_fetch import (describe_samples, fetch, looks_like_url, samples,
							 statement, summarize, title_of)
from .core.cph_i18n import t
from .core.cph_resources import package_file_path
from .core.cph_settings import get_settings
from .core.cph_stats import (record_run, reset as stats_reset,
							 summary_localized as stats_summary_localized)
from .core.cph_tests_merge import merge_tests
from .core.cph_settings import load_all_tests, save_tests, notify_io_error


def _settings():
	try:
		return get_settings() or {}
	except Exception:
		return {}


def _contest_default_minutes():
	"""What the countdown input panel starts with: the configured length, or an
	empty box when it is 0 (0 means "ask me", not "a zero minute contest").
	"""
	try:
		value = float(_settings().get('contest_duration_minutes', 120))
	except (TypeError, ValueError):
		return ''
	if value <= 0:
		return ''
	return '%g' % value


def _run_view(view):
	return bool(view.settings().get('cph_run_view'))


def _add_samples_to(view, pairs):
	"""Append the fetched samples as tests of this source file."""
	file_name = view.file_name()
	if not file_name:
		sublime.status_message('[Algorithm Competition Assistant] ' + t('save_file_first'))
		return 0
	entries = [{'test': inp, 'correct_answers': [out] if out.strip() else []}
			   for inp, out in pairs]
	merged, _conflicts = merge_tests(load_all_tests(file_name), entries)
	if not save_tests(file_name, merged):
		# 'save' as the whole error told the user nothing about a sample set
		# that is not on disk. Name the file and the reason.
		notify_io_error(file_name)
		return 0
	# Refresh an already-open run panel so the new tests show up.
	window = view.window()
	if window is not None:
		target = os.path.basename(file_name) + ' -run'
		for other in window.views():
			if (other.name() or '') == target:
				other.run_command('cph_test_manager',
								  {'action': 'make_opd', 'load_session': True,
								   'run_file': file_name})
				break
	sublime.status_message('[Algorithm Competition Assistant] '
						   + t('fetch_added', n=len(entries), total=len(merged)))
	return len(entries)


class CphFetchProblemCommand(sublime_plugin.TextCommand):
	"""Fetch a problem page and turn its samples into tests.

	No Competitive Companion needed: paste a Luogu / Codeforces / AtCoder (or
	most other judge) URL and the sample input/output pairs are extracted from
	the `<pre>` blocks. The statement can be opened as Markdown too.
	"""

	def run(self, edit, url=None, open_statement=False):
		if url is None:
			clipboard = sublime.get_clipboard() or ''
			if looks_like_url(clipboard):
				self.fetch_and_add(clipboard.strip(), open_statement)
				return
			self.view.window().show_input_panel(
				t('fetch_enter_url'), '', 
				lambda text: self.fetch_and_add(text, open_statement), None, None)
			return
		self.fetch_and_add(url, open_statement)

	def fetch_and_add(self, url, open_statement=False):
		url = (url or '').strip()
		if not looks_like_url(url):
			sublime.status_message('[Algorithm Competition Assistant] ' + t('fetch_bad_url', url=url))
			return
		sublime.status_message('[Algorithm Competition Assistant] ' + url)

		view = self.view

		def worker():
			html, error = fetch(url)
			if error:
				sublime.set_timeout(
					lambda: sublime.status_message('[Algorithm Competition Assistant] ' + error), 0)
				return
			pairs = samples(html)
			title = title_of(html)

			def apply():
				if pairs:
					_add_samples_to(view, pairs)
				else:
					sublime.status_message('[Algorithm Competition Assistant] ' + t('fetch_no_samples'))
				# A page with no <title> and no <h1> leaves problem_title() empty,
				# so the status bar would stay at the bare URL from
				# fetch_and_add() and say nothing about what was fetched. Fall
				# back to one line of the statement itself - the longest visible
				# line, because nav and sample blocks are short and prose is not.
				label = title
				if not label:
					line = summarize(html)
					if line:
						label = t('statement_summary', text=line)
				if label:
					sublime.status_message('[Algorithm Competition Assistant] %s | %s'
										   % (label, describe_samples(pairs)))
				if open_statement:
					_open_markdown(statement(html, url))
			sublime.set_timeout(apply, 0)

		thread = threading.Thread(target=worker)
		thread.daemon = True
		thread.start()


class CphViewStatementCommand(sublime_plugin.TextCommand):
	"""Open the problem statement as Markdown in a scratch view."""

	def run(self, edit, url=None):
		if url is None:
			clipboard = sublime.get_clipboard() or ''
			if looks_like_url(clipboard):
				url = clipboard.strip()
			else:
				self.view.window().show_input_panel(
					t('fetch_enter_url'), '',
					lambda text: self.run(edit, url=text), None, None)
				return
		url = (url or '').strip()
		if not looks_like_url(url):
			sublime.status_message('[Algorithm Competition Assistant] ' + t('fetch_bad_url', url=url))
			return

		def worker():
			html, error = fetch(url)
			if error:
				sublime.set_timeout(
					lambda: sublime.status_message('[Algorithm Competition Assistant] ' + error), 0)
				return
			markdown = statement(html, url)
			sublime.set_timeout(lambda: _open_markdown(markdown), 0)

		thread = threading.Thread(target=worker)
		thread.daemon = True
		thread.start()


def _open_markdown(markdown):
	if not markdown or not markdown.strip():
		sublime.status_message('[Algorithm Competition Assistant] ' + t('statement_empty'))
		return
	window = sublime.active_window()
	if window is None:
		return
	view = window.new_file()
	view.set_scratch(True)
	view.set_name('problem statement')
	try:
		view.assign_syntax('Packages/Markdown/Markdown.sublime-syntax')
	except Exception:
		pass
	view.run_command('append', {'characters': markdown})


class CphCalibrateMachineCommand(sublime_plugin.TextCommand):
	"""Measure this machine's speed and store the factor.

	"Local TLE, accepted on the judge" is the most common false alarm; the
	factor turns a local limit into the equivalent judge limit. The reference
	value is a documented constant, so treat the absolute number as an
	order-of-magnitude anchor.
	"""

	def run(self, edit):
		sublime.status_message('[Algorithm Competition Assistant] calibrating...')

		def worker():
			factor, score, seconds, error = calibrate_measure()
			if error:
				sublime.set_timeout(
					lambda: sublime.status_message('[Algorithm Competition Assistant] ' + error), 0)
				return
			report = calibrate_report(factor, score, seconds)
			sublime.set_timeout(
				lambda: sublime.status_message('[Algorithm Competition Assistant] ' + report), 0)

		thread = threading.Thread(target=worker)
		thread.daemon = True
		thread.start()


class CphStatsCommand(sublime_plugin.TextCommand):
	"""Show the local practice statistics (last N days)."""

	def run(self, edit, days=None, reset=False):
		if reset:
			stats_reset()
			sublime.status_message('[Algorithm Competition Assistant] ' + t('stats_reset_done'))
			return
		try:
			days = int(days or _settings().get('stats_days', 7) or 7)
		except (TypeError, ValueError):
			days = 7
		report = stats_summary_localized(days)
		if not report or not report.strip():
			sublime.status_message('[Algorithm Competition Assistant] ' + t('stats_empty'))
			return
		window = self.view.window() or sublime.active_window()
		if window is None:
			return
		view = window.new_file()
		view.set_scratch(True)
		view.set_name('practice stats')
		view.run_command('append', {'characters': report})


class CphContestTimerCommand(sublime_plugin.TextCommand):
	"""A countdown in the status bar for a timed contest.

	`cph_contest_timer` with no argument asks for the length (the default comes
	from `contest_duration_minutes`); `stop` cancels. Nothing starts it by
	itself: a contest clock runs from the scheduled start, not from the moment
	the first sample landed, so the first thing a countdown needs is a human.
	"""

	STATE = {'until': 0.0, 'total': 0, 'warned': set()}

	def run(self, edit, minutes=None, stop=False):
		if stop:
			self.STATE['until'] = 0.0
			self.STATE['warned'] = set()
			self._paint('')
			sublime.status_message('[Algorithm Competition Assistant] ' + t('contest_stopped'))
			return
		if minutes is None:
			default = _contest_default_minutes()
			self.view.window().show_input_panel(
				t('contest_enter_minutes'), default,
				lambda text: self.start(text), None, None)
			return
		self.start(minutes)

	def start(self, minutes):
		try:
			minutes = float(minutes)
		except (TypeError, ValueError):
			return
		if minutes <= 0:
			return
		self.STATE['until'] = time.time() + minutes * 60
		self.STATE['total'] = minutes
		self.STATE['warned'] = set()
		sublime.status_message('[Algorithm Competition Assistant] '
							   + t('contest_started', minutes=int(minutes)))
		self.tick()

	def tick(self):
		remaining = self.STATE['until'] - time.time()
		if self.STATE['until'] <= 0:
			return
		if remaining <= 0:
			self.STATE['until'] = 0.0
			self._paint('')
			sublime.status_message('[Algorithm Competition Assistant] ' + t('contest_finished'))
			return
		hours = int(remaining // 3600)
		minutes = int((remaining % 3600) // 60)
		seconds = int(remaining % 60)
		self._paint('\u23f3 %d:%02d:%02d' % (hours, minutes, seconds))
		for mark in (30, 10, 5):
			if remaining <= mark * 60 and mark not in self.STATE['warned']:
				self.STATE['warned'].add(mark)
				sublime.status_message('[Algorithm Competition Assistant] '
									   + t('contest_warning', minutes=mark))
		sublime.set_timeout(self.tick, 1000)

	def _paint(self, text):
		for window in sublime.windows():
			view = window.active_view()
			if view is not None:
				view.set_status('cph_contest', text)


class CphBenchmarkCommand(sublime_plugin.TextCommand):
	"""Run the current test several times and report best/average/worst."""

	def run(self, edit, runs=None):
		view = self.view
		if _run_view(view):
			view.run_command('cph_test_manager', {'action': 'benchmark'})
			return
		file_name = view.file_name()
		window = view.window()
		if window is not None and file_name:
			target = os.path.basename(file_name) + ' -run'
			for other in window.views():
				if (other.name() or '') == target:
					other.run_command('cph_test_manager', {'action': 'benchmark'})
					return
		sublime.status_message('[Algorithm Competition Assistant] ' + t('benchmark_no_test'))


#: Spellings Sublime itself uses for the per-platform files.
_PLATFORM_FILE = {'windows': 'Windows', 'linux': 'Linux', 'osx': 'OSX'}


class CphOpenPackageFileCommand(sublime_plugin.WindowCommand):
	"""Open one of this package's own files (settings, README, keymaps).

	The Preferences entries used to spell their paths as
	${packages}/Algorithm Competition Assistant/..., which only resolves when
	the installed folder carries the display name. Package Control renames it
	on install; a manual install under the repository name opened a file that
	does not exist. The path is built from the directory the package really
	lives in instead, through package_file_path - edit_settings rejects a
	`Packages/...` resource path outright and answers "The settings file ...
	could not be opened", so it needs an OS-resolvable one.
	"""

	def run(self, file, edit_base=False, default=None):
		name = file
		if '$platform' in name:
			platform = sublime.platform()
			name = name.replace('$platform', _PLATFORM_FILE.get(platform, platform))
		target = package_file_path(name)
		if edit_base:
			self.window.run_command('edit_settings', {
				'base_file': target,
				'default': default or '{\n\t$0\n}\n',
			})
		else:
			self.window.run_command('open_file', {'file': target})


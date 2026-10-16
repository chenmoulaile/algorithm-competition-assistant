"""
Algorithm Competition Assistant - 主测试管理器
"""

import sublime, sublime_plugin
import os
import re
from sublime import Region, Phantom, PhantomSet
from os import path
from time import time, sleep
import threading

from .Modules.ProcessManager import ProcessManager
from .core.cph_settings import package_name, get_settings, get_tests_paths, load_all_tests, save_tests, is_run_supported_ext, get_problem_limits
from .core.cph_target import visible as context_menu_visible
from .core.cph_jump import (diagnostics as parse_diagnostics,
							links_html, one_link, set_targets, handle_event as handle_jump_event)
from .core.cph_build_mode import label as build_mode_label
from .core.cph_calibrate import label as calibrate_label
from .core.cph_parallel import run_batch as run_parallel_batch
from .core.cph_checker import (config as checker_config,
							   executable as checker_executable,
							   judge as checker_judge)
from .core.cph_stats import record_run as stats_record_run
from .core.cph_stats import take_save_error as stats_take_save_error
from .core.cph_subtasks import (parse as subtask_parse,
								evaluate as subtask_evaluate,
								html as subtask_html)
from .core.cph_interactive import (config as interactor_config,
								   executable as interactor_executable,
								   run_case as interactive_run_case)
from .core.cph_resources import read_resource
from .Highlight.test_interface import get_test_styles
from .core.cph_verdict import get_verdict, get_verdict_by_code, get_verdict_by_name, build_line_diff, outputs_equal, find_crash_location, looks_like_crash
from .core.cph_gdb_trace import names_source as _names_source_impl
from .core.cph_i18n import t


# ------------------------------------------------------ output pump plumbing
#: Bytes taken from the program's stdout per read. The pump must ask for a
#: bounded chunk: read() without a size blocks until EOF and hands back the
#: entire output as one string, which is how a silent-then-flood program got
#: past max_output_bytes and dumped megabytes into a view and the test store.
_OUT_CHUNK = 65536


def clamp_output(current, chunk, limit):
	"""Return the part of chunk that still fits, so current stays <= limit.

	limit <= 0 means unlimited. current is the text already collected, so the
	rule is applied before the append instead of only after it.
	"""
	if limit <= 0:
		return chunk
	room = limit - len(current)
	if room <= 0:
		return ''
	return chunk[:room]


def post_to_ui(fn):
	"""Run fn on the main thread.

	Sublime's view API is only safe there; the parallel path bounces for the
	same reason (see core/cph_parallel.py). Called from the main thread this
	just defers by one loop iteration, which keeps FIFO ordering.
	"""
	sublime.set_timeout(fn, 0)


#: How long past the limit the watchdog waits before killing. This only moves
#: the moment we *notice*, never how long a program is allowed to run: the
#: verdict is still made from the measured runtime against the plain limit, so
#: a run that finishes over the limit is still TLE and nothing here is more
#: generous than a judge. It saves the programs that would have finished inside
#: a judge's own slack, and matches core/cph_parallel.py's _GRACE_SECONDS.
_TLE_KILL_GRACE_S = 0.05


def tle_advisory(runtime_ms, time_limit_ms):
	"""Fraction of the time limit used, when a passing run came too close.

	Returns None when there is nothing to say. A machine faster than the judge
	turns 1.95 s against a 2 s limit into an AC here and a TLE there, and the
	user cannot see that from a plain "AC 1950ms". This only warns - it never
	changes a verdict, because a local result must not be more generous than
	the judge's.
	"""
	if not runtime_ms or not time_limit_ms:
		return None
	try:
		ratio = float(runtime_ms) / float(time_limit_ms)
	except (TypeError, ValueError, ZeroDivisionError):
		return None
	if ratio >= 1.0 or ratio < 0.9:
		return None
	return ratio


# --------------------------------------------------------------- compile cache
# Ctrl+Alt+B used to recompile synchronously (up to 30s) on every run even
# when nothing had changed, which made iterating on one sample painful.
_compile_cache = {}

#: Files already told "a WA in release mode may be hidden UB, try Debug".
#: One note per file per session - a wrong answer is normal, and a lecture on
#: every WA would be noise.
_WA_RELEASE_HINTED = set()

#: How many differing lines the detail view spells out. Every entry costs
#: three lines ("Line N:", expected, actual), and a diff can hold one entry
#: per line of output. At the default 8 MB max_output_bytes a single WA
#: produced 42 MB / 2.4 M lines of detail text (measured: 800k differing
#: lines -> 0.4 s to diff, 0.55 s to build, and then Sublime has to lay out
#: two million lines). The count line above still reports the true total, the
#: raw outputs right below are complete, and the run view shows the whole
#: output - so a cap here costs nothing but the wall of text.
_DETAIL_DIFF_LIMIT = 200


def _iter_local_includes(path, seen=None, depth=3):
	"""Local headers reachable through `#include "..."` (best effort).

	Without this, editing a header did not invalidate the compile cache and
	the plugin happily ran a stale binary.
	"""
	if seen is None:
		seen = set()
	try:
		abs_path = os.path.abspath(path)
	except Exception:
		return seen
	if abs_path in seen or depth < 0:
		return seen
	seen.add(abs_path)
	directory = os.path.dirname(abs_path)
	try:
		with open(abs_path, 'r', encoding='utf-8', errors='ignore') as f:
			content = f.read()
	except Exception:
		return seen
	for m in re.finditer(r'^\s*#\s*include\s+"([^"]+)"', content, re.M):
		candidate = os.path.join(directory, m.group(1))
		if os.path.exists(candidate):
			_iter_local_includes(candidate, seen, depth - 1)
	return seen


def _source_fingerprint(process_manager):
	"""(file, mtime, size) for every input of the compile + the command."""
	parts = []
	try:
		inputs = process_manager.get_compile_inputs()
	except Exception:
		inputs = [process_manager.file]

	expanded = []
	for f in inputs:
		# not named 'path': that shadows `from os import path` for the whole
		# function and would break any path.join() added here later.
		for include in sorted(_iter_local_includes(f)):
			if include not in expanded:
				expanded.append(include)

	for f in expanded:
		try:
			st = os.stat(f)
			parts.append((f, int(st.st_mtime), st.st_size))
		except Exception:
			parts.append((f, 0, 0))
	try:
		cmd = process_manager.get_compile_cmd()
	except Exception:
		cmd = None
	return (tuple(parts), cmd)


def should_skip_compile(process_manager, force=False):
	if force or not get_settings().get('compile_cache_enabled', True):
		return False
	entry = _compile_cache.get(process_manager.file)
	if not entry:
		return False
	return entry.get('fingerprint') == _source_fingerprint(process_manager)


def remember_compile(process_manager):
	if not get_settings().get('compile_cache_enabled', True):
		return
	_compile_cache[process_manager.file] = {
		'fingerprint': _source_fingerprint(process_manager),
	}


def run_view_status_label(run_file, time_limit_ms, memory_limit_mb):
	"""Status-bar label for the run panel (language + limits).

	Module level on purpose: it is needed by CphTestManagerCommand (which
	draws the run view), and as a CphViewTesterCommand method it raised
	AttributeError right after the view was created - leaving an empty -run
	tab behind (v1.4.5 regression).
	"""
	try:
		ext = path.splitext(run_file or '')[1][1:]
		entry = None
		for x in (get_settings().get('run_settings') or []):
			if ext in (x.get('extensions') or []):
				entry = x
				break
		if not entry:
			return ''
		parts = [entry.get('name', 'run')]
		tl = time_limit_ms or entry.get('time_limit_ms')
		ml = memory_limit_mb or entry.get('memory_limit_mb')
		if tl:
			parts.append('TL %dms' % int(tl))
		if ml:
			parts.append('ML %dMB' % int(ml))
		mode = build_mode_label(run_file)
		if mode:
			parts.append(mode)
		# The machine calibration was measured since v2.2 and never shown
		# anywhere, so calibrating changed nothing the user could see. This is
		# display only: the verdict keeps using the plain limit.
		cal = calibrate_label(tl) if tl else ''
		if cal:
			parts.append(cal)
		return ' · '.join(parts)
	except Exception:
		return ''


def _clean_newlines(s):
	"""Normalize CRLF / lone CR in stored test data.

	Test files written on Windows (or pasted from a Windows editor) carried
	\r inside their strings, which renders as '<0x0d>' in the panel.
	"""
	if not isinstance(s, str):
		return s
	return s.replace('\r\n', '\n').replace('\r', '\n')


def _replay_crash(pm, source_file, stdin_text, time_limit_ms):
	"""(file, line) from a gdb replay, or None. Never raises.

	One extra run of the binary under gdb turns "the program died somewhere
	in libstdc++" - or "it died and printed nothing" - into the user's own
	frame. It is skipped whenever the answer already exists, and any failure
	(no gdb, no binary, no symbols, a timeout) degrades to None so the
	caller keeps the verdict it already had.
	"""
	timeout = 5.0
	try:
		if time_limit_ms:
			timeout = min(5.0, max(2.0, float(time_limit_ms) / 1000.0))
	except (TypeError, ValueError):
		pass
	payload = b''
	if isinstance(stdin_text, str):
		payload = stdin_text.encode('utf-8', 'replace')
	try:
		return pm.locate_crash(source_file=source_file,
							   stdin_bytes=payload, timeout=timeout)
	except Exception:
		return None


def _names_source(location, source_file):
	"""True when `location` already points at the source under test.

	Decides whether the gdb replay is worth running: an abort whose own
	message named a libstdc++ header (``stl_vector.h:1263``) is real but not
	actionable, and the user's frame is what they have to fix.
	"""
	return _names_source_impl(location, source_file)


def _squash_ws(s):
	"""Drop every whitespace character, like the judge's answer comparison.

	`get_verdict_by_code` compares outputs with all whitespace removed, so
	the "is this answer already accepted?" check must not be stricter than
	the verdict: byte comparison made an AC sample keep offering 'accept'
	and refuse to collapse.
	"""
	if not s:
		return ''
	return ''.join(s.split())


def _count_text_units(s):
	"""Count ASCII vs wide (CJK / fullwidth) characters of a string."""
	ascii_n = 0
	wide_n = 0
	for ch in s:
		if ord(ch) > 0x2E7F:
			wide_n += 1
		else:
			ascii_n += 1
	return ascii_n, wide_n


def estimate_card_width_px(view, with_memory=False):
	"""
	Rough estimate of the rendered width (device px) of the widest row of a
	test config card, so the run panel can be widened until cards fit on a
	single line. minihtml phantoms inherit the view font, so the view's
	em_width is the base unit; the monospace assumption overestimates a
	little for proportional fonts, which is the safe direction here.
	"""
	try:
		em = view.em_width()
	except Exception:
		em = 0
	if not em or em <= 0:
		return 0
	try:
		scale = sublime.scale_factor()
	except Exception:
		scale = 1.0
	font_pt = view.settings().get('font_size')
	if not font_pt:
		font_pt = sublime.load_settings('Preferences.sublime-settings').get('font_size', 10)
	font_px = font_pt * 4.0 / 3.0

	# Visible text of each segment of the widest card row, worst case:
	# 2-digit test id, 6-digit runtime, optional memory segment. Localized
	# labels are counted as-is so zh/en both work.
	segments = (
		' ' + t('test_label') + ' 88 ',
		' ' + t('edit') + ' ',
		' ' + t('run') + ' ',
		' ' + t('detail') + ' ',
		' ' + t('time') + ': 9999ms ',
	)
	if with_memory:
		segments += (' ' + t('memory') + ': 888.88 MB ',)

	ascii_n = 0
	wide_n = 0
	for seg in segments:
		a, w = _count_text_units(seg)
		ascii_n += a
		wide_n += w
	ascii_n += 6  # single space between the inline <a> buttons

	width = ascii_n * em + wide_n * font_px * 1.05 * scale
	# verdict badge: 10px monospace bold, up to 3 chars + padding + margin
	width += (3 * 10 * 0.65 + 3 + 2) * scale
	# 1px padding around each of the ~6 buttons + block spacing (CSS px)
	width += (6 * 2 + 8) * scale
	return width * 1.08


class CphTestManagerCommand(sublime_plugin.TextCommand):
	REGION_BEGIN_KEY = 'test_begin_%d'
	REGION_OUT_KEY = 'test_out_%d'
	REGION_END_KEY = 'test_end_%d'
	# Built from package_name (the name Sublime addresses this package by, with
	# any .sublime-package suffix removed) rather than a hardcoded name: the two
	# arrow icons must resolve whether the package is installed as a zip by
	# Package Control or opened from a differently named checkout. base_name
	# would carry the .sublime-package suffix in the zip case and resolve to
	# nothing.
	REGION_BEGIN_PROP = ['string', 'Packages/%s/icons/arrow_right.png' % package_name, \
				sublime.DRAW_NO_FILL | sublime.DRAW_STIPPLED_UNDERLINE | \
					sublime.DRAW_NO_OUTLINE | sublime.DRAW_EMPTY_AS_OVERWRITE]
	REGION_END_PROP = ['variable.c++', 'Packages/%s/icons/arrow_left.png' % package_name, sublime.HIDDEN]

	# Dispatched actions that index the test model. A failed compile leaves
	# the -run panel open with tester = None, yet the bindings scoped to
	# source.TestSyntax still fire there (Ctrl+D, swap, the test menu), which
	# used to raise AttributeError: 'NoneType' object has no attribute ...
	ACTIONS_NEEDING_TESTER = frozenset((
		'insert_line', 'new_test', 'delete_test', 'delete_tests',
		'swap_tests', 'show_test_menu', 'show_test_action_menu',
		'apply_edit_changes', 'accept_test', 'decline_test', 'toggle_fold',
	))

	def __init__(self, view):
		self.view = view
		# Deferred attributes: the run view can be restored by Sublime's
		# hot_exit before any real run happened, and Ctrl+Alt+B there used
		# to crash with AttributeError on these.
		self.dbg_file = None
		self.code_view_id = None
		self.input_start = 0
		self.delta_input = 0
		self.tester = None
		self.session = None
		self.phantoms = PhantomSet(view, 'test-phantoms')
		self.test_phantoms = [PhantomSet(view, 'test-phantoms-' + str(i)) for i in range(10)]
		self.summary_phantom = PhantomSet(view, 'cph-summary-phantom')
		# Panel auto-fit scheduling state (see schedule_auto_fit_panel_width).
		# The run view can be restored by hot_exit, so read them with getattr
		# everywhere instead of relying on __init__ having run.
		self._fit_scheduled = False
		self._fit_dirty = False
		self._fit_last_cols1 = None

	class Test(object):
		def __init__(self, prop, start=None, end=None):
			super(CphTestManagerCommand.Test, self).__init__()
			if type(prop) == str:
				self.test_string = _clean_newlines(prop)
				self.correct_answers = set()
				self.uncorrect_answers = set()
			else:
				# .get(): load_all_tests()/is_meaningful_test() accept a dict
				# without a 'test' key (an answer-only entry), and the missing
				# key used to abort the whole run with KeyError.
				self.test_string = _clean_newlines(str(prop.get('test') or ''))
				# Normalise exactly like add_correct_answer() (strip), or a
				# stored answer such as '3\n' never matched the program's '3'
				# in is_correct_answer() and the card kept offering accept.
				# str(... or ''): a hand-edited tests file can hold null or
				# a number, and .strip() on those raised AttributeError.
				self.correct_answers = set(str(_clean_newlines(x) or '').strip()
										   for x in prop.get('correct_answers', ()))
				self.uncorrect_answers = set(str(_clean_newlines(x) or '').strip()
											 for x in prop.get('uncorrect_answers', ()))

			self.start = start
			self.fold = True
			self.end = end
			self.runtime = '-'
			# Initialized here on purpose: update_configs() reads .rtcode when
			# expanding a card that was skipped by 're-run failed tests'
			# (uninitialized attribute -> AttributeError inside a phantom
			# callback).
			self.rtcode = '0'
			self.memory = '-'
			self.verdict = None
			self.verdict_name = None
			self.stdout = ''
			self.stderr = ''
			self.expected_output = ''
			self.message = ''
			# 'file:line' of a runtime error, when the program's own output
			# told us where it died (Python traceback / Java stack trace /
			# -fsanitize diagnostic).
			self.crash_line = ''
			# The same location split into its parts, so the detail view can
			# turn it into a clickable jump link (cph_jump).
			self.crash_path = ''
			self.crash_lineno = 0
			self.time_limit_ms = None
			self.memory_limit_mb = None

			# Restore the last judge result so verdict badges and the
			# detail view survive a Sublime restart / session reload
			if type(prop) == dict:
				self.runtime = prop.get('runtime', '-')
				self.memory = prop.get('memory', '-')
				self.stdout = _clean_newlines(prop.get('stdout', ''))
				self.stderr = _clean_newlines(prop.get('stderr', ''))
				self.expected_output = _clean_newlines(prop.get('expected_output', ''))
				restored = get_verdict_by_name(prop.get('verdict'))
				if restored:
					self.verdict = restored
					self.verdict_name = restored['name']

		def add_correct_answer(self, answer):
			self.correct_answers.add(answer.lstrip().rstrip())

		def add_uncorrect_answer(self, answer):
			self.uncorrect_answers.add(answer.lstrip().rstrip())

		def remove_correct_answer(self, answer):
			answer = answer.lstrip().rstrip()
			if answer in self.correct_answers:
				self.correct_answers.remove(answer)

		def remove_uncorrect_answer(self, answer):
			answer = answer.lstrip().rstrip()
			if answer in self.uncorrect_answers:
				self.uncorrect_answers.remove(answer)

		def is_correct_answer(self, answer, float_tolerance=0):
			answer = answer.rstrip().lstrip()
			if answer in self.correct_answers:
				return True
			if answer in self.uncorrect_answers:
				return False
			# Floating point problems: 0.1 + 0.2 will never string-match 0.3
			if float_tolerance and float_tolerance > 0:
				for correct in self.correct_answers:
					if outputs_equal(answer, correct, float_tolerance):
						return True
			# Whitespace-only differences are AC for the judge, so they must
			# count as correct here too. Otherwise a sample that was judged
			# AC still showed an 'accept' button and never collapsed (the
			# user had to click accept on an answer they had just set).
			squashed = _squash_ws(answer)
			if squashed:
				for correct in self.correct_answers:
					if _squash_ws(correct) == squashed:
						return True
				for wrong in self.uncorrect_answers:
					if _squash_ws(wrong) == squashed:
						return False
			return None

		def append_string(self, s):
			self.test_string += s

		def set_inner_range(self, start, end):
			self.start = start
			self.end = end

		def set_tie_pos(self, pos):
			self.tie_pos = pos

		def set_cur_runtime(self, runtime):
			self.runtime = runtime

		def set_cur_rtcode(self, rtcode):
			self.rtcode = rtcode

		def set_verdict(self, verdict):
			self.verdict = verdict
			self.verdict_name = verdict['name'] if verdict else None

		def reset_run_state(self):
			"""Forget the previous run's result.

			Called when a fresh run starts: the verdicts loaded from the tests
			file are *last time's* results, and counting them again made the
			summary report 'N/N passed' for a run that had just failed, and
			kept green badges on tests this run never reached.
			"""
			self.verdict = None
			self.verdict_name = None
			self.runtime = '-'
			self.memory = '-'
			self.stdout = ''
			self.stderr = ''
			self.message = ''
			self.crash_line = ''
			self.crash_path = ''
			self.crash_lineno = 0
			self.rtcode = '0'

		def set_memory(self, memory):
			self.memory = memory

		def set_stdout(self, stdout):
			self.stdout = stdout

		def set_stderr(self, stderr):
			self.stderr = stderr

		def set_expected_output(self, expected):
			self.expected_output = expected

		def set_message(self, message):
			self.message = message

		def set_time_limit(self, time_ms):
			self.time_limit_ms = time_ms

		def set_memory_limit(self, mem_mb):
			self.memory_limit_mb = mem_mb

		def get_nice_runtime(self):
			runtime = self.runtime
			if runtime == '-' or runtime is None:
				return '-'
			try:
				runtime = int(runtime)
			except (ValueError, TypeError):
				return '-'
			if runtime < 5000:
				return '&nbsp;' * (2 - len(str(runtime))) + str(runtime) + 'ms'
			else:
				return str(runtime // 1000) + 's'

		def get_nice_memory(self):
			if self.memory == '-' or self.memory is None:
				return '-'
			try:
				memory = float(self.memory)
			except (ValueError, TypeError):
				return '-'
			# &nbsp; keeps "1 MB" on one line: minihtml breaks on a plain
			# space, and on a narrow panel the chip wrapped to "1" / "MB".
			if memory < 1024:
				return str(int(memory)) + '&nbsp;MB'
			else:
				return '%.2f&nbsp;GB' % (memory / 1024.0)

		def get_verdict_class(self):
			if not self.verdict:
				return 'verdict-UKE'
			return 'verdict-' + self.verdict['name']

		def get_test_class(self):
			# The CSS themes only define .test-accept / .test-decline (the
			# upstream FastOlympicCoding names). Emitting 'test-AC' /
			# 'test-wrong-answer' matched no rule, so every card silently lost
			# its green/red background tint.
			if not self.verdict:
				if str(getattr(self, 'rtcode', '0')) != '0':
					return 'test-decline'
				return ''
			name = self.verdict['name']
			if name == 'AC':
				return 'test-accept'
			if name == 'SK':
				# skipped is not a failure - keep the neutral card background
				return ''
			return 'test-decline'

		def get_config(self, i, pt, _cb_act, _out, view, running=False):
			if not running:
				styles = get_test_styles(view)
				content = read_resource('Highlight/test_config.html')

				verdict_class = self.get_verdict_class()
				verdict_short = self.verdict['name'] if self.verdict else 'UKE'
				verdict_icon = '✓' if (self.verdict and self.verdict['name'] == 'AC') else '×'
				test_type = self.get_test_class()

				memory_display = 'none'
				memory_str = '-'
				if self.memory != '-' and self.memory is not None:
					memory_display = 'inline'
					memory_str = self.get_nice_memory()

				# stderr is deliberately NOT shown on the card any more: the
				# chip made every card with debug output too wide / too tall.
				# It stays in the detail view ('Error output' section).
				content = content.format(
					test_id=i + 1,
					runtime=self.get_nice_runtime(),
					verdict_class=verdict_class,
					verdict_short=verdict_short,
					verdict_icon=verdict_icon,
					test_type=test_type,
					memory_display=memory_display,
					memory=memory_str,
					edit_label=t('edit'),
					run_label=t('run'),
					detail_label=t('detail'),
					time_label=t('time'),
					memory_label=t('memory'),
					test_label=t('test_label'),
				)
				content = '<style>' + styles + '</style>' + content

				def onclick(event, cb=_cb_act, i=i):
					_cb_act(i, event)

				phantom = Phantom(Region(pt), content, sublime.LAYOUT_BLOCK, onclick)
				return phantom
			else:
				styles = get_test_styles(view)
				content = read_resource('Highlight/test_running.html')
				content = content.format(
					test_id=i + 1,
					stop_label=t('stop'),
					test_label=t('test_label'),
				)
				content = '<style>' + styles + '</style>' + content
				def onclick(event, cb=_cb_act, i=i):
					_cb_act(i, event)

				phantom = Phantom(Region(pt), content, sublime.LAYOUT_BLOCK, onclick)
				return phantom

		def get_accdec(self, i, pt, _cb_act, type, _view):
			styles = get_test_styles(_view)
			content = read_resource('Highlight/test_accdec.html')
			if type == 'accept':
				type_label = t('accept')
			else:
				type_label = t('decline')
			content = content.format(
				test_id=i + 1,
				type=type,
				type_label=type_label,
				runtime='&nbsp;' * (2 - len(str(self.runtime))) + str(self.runtime)
			)
			content = '<style>' + styles + '</style>' + content

			def onclick(event, cb=_cb_act, i=i):
				_cb_act(i, event)

			phantom = Phantom(Region(pt), content, sublime.LAYOUT_BLOCK, onclick)
			return phantom

		def get_detail(self, i, pt, _cb_act, _view):
			styles = get_test_styles(_view)
			content = read_resource('Highlight/test_detail.html')

			verdict_short = self.verdict['name'] if self.verdict else 'UKE'
			verdict_class = self.get_verdict_class()

			memory_display = 'inline' if self.memory != '-' and self.memory is not None else 'none'
			memory_str = self.get_nice_memory() if memory_display == 'inline' else '-'

			stderr_display = 'block' if self.stderr else 'none'
			message_display = 'block' if self.message else 'none'

			def escape_html(s):
				if not s:
					return ''
				# lone \r would render as the mysterious '<0x0d>' in minihtml
				s = s.replace('\r\n', '\n').replace('\r', '')
				return (s.replace('&', '&amp;')
						 .replace('<', '&lt;')
						 .replace('>', '&gt;')
						 .replace('\n', '<br>'))

			# sample input section
			input_text = escape_html(self.test_string)
			input_display = 'block' if input_text.strip() else 'none'

			# Show a single answer section: the accepted correct answer if any,
			# otherwise the expected output; hidden when neither exists.
			if self.correct_answers:
				expected_label = t('correct_answer')
				expected = escape_html(next(iter(self.correct_answers)))
				expected_display = 'block'
			elif self.expected_output:
				expected_label = t('expected_output')
				expected = escape_html(self.expected_output)
				expected_display = 'block'
			else:
				expected_label = t('expected_output')
				expected = ''
				expected_display = 'none'

			content = content.format(
				test_id=i + 1,
				verdict_short=verdict_short,
				verdict_class=verdict_class,
				runtime=self.get_nice_runtime(),
				memory_display=memory_display,
				memory=memory_str,
				input=input_text,
				input_display=input_display,
				input_label=t('input'),
				expected=expected,
				expected_label=expected_label,
				expected_display=expected_display,
				stdout=escape_html(self.stdout),
				stderr=escape_html(self.stderr),
				stderr_display=stderr_display,
				message=escape_html(self.message),
				message_display=message_display,
				test_label=t('test_label'),
				actual_output_label=t('actual_output'),
				error_output_label=t('error_output'),
				message_label=t('message'),
				time_label=t('time'),
				memory_label=t('memory'),
			)
			content = '<style>' + styles + '</style>' + content

			def onclick(event, cb=_cb_act, i=i):
				_cb_act(i, event)

			phantom = Phantom(Region(pt), content, sublime.LAYOUT_BLOCK, onclick)
			return phantom

		def memorize(self):
			d = {'test': self.test_string}
			if self.correct_answers:
				d['correct_answers'] = list(self.correct_answers)
			if self.uncorrect_answers:
				d['uncorrect_answers'] = list(self.uncorrect_answers)
			if self.verdict:
				d['verdict'] = self.verdict['name']
			if self.runtime != '-':
				d['runtime'] = self.runtime
			if self.memory != '-':
				d['memory'] = self.memory
			if self.stdout:
				d['stdout'] = self.stdout
			if self.stderr:
				d['stderr'] = self.stderr
			if self.expected_output:
				d['expected_output'] = self.expected_output
			return d

		def __str__(self):
			return self.test_string

	class Tester(object):
		def __init__(self, process_manager, \
			on_insert, on_out, on_stop, on_status_change, \
			sync_out=False, tests=[], epoch=None, run_failed=False):
			super(CphTestManagerCommand.Tester, self).__init__()
			self.process_manager = process_manager
			self.sync_out = sync_out
			self.tests = tests
			self.test_iter = 0
			self.running_test = None
			self.running_new = None
			# Only re-run tests whose last verdict is not AC ('Run failed tests')
			self.run_failed = run_failed
			# Keep going even when a test fails ('Run all tests')
			self.run_all = False
			# Output size guard: huge output is truncated instead of being
			# accumulated in full (a runaway print would freeze the editor)
			self.output_truncated = False
			self.on_insert = on_insert
			self.on_out = on_out
			self.on_stop = on_stop
			self.proc_run = False
			self.prog_out = []
			# Keep prog_out exactly as long as tests. Session-restored tests
			# have no output yet, and get_tie_pos() / toggle_fold() index it
			# directly: a short list raised IndexError, which made "delete
			# test" (Ctrl+D and the test menu) fail silently for every test
			# this session had not run yet - typically an empty sample.
			while len(self.prog_out) < len(self.tests):
				self.prog_out.append('')
			self.on_status_change = on_status_change
			# Epoch of this tester within the owning CphTestManagerCommand.
			# Stale listener threads of a killed process may still fire
			# __on_stop after a new Tester replaced this one; on_stop uses
			# the epoch to drop those outdated callbacks.
			self.epoch = epoch
			# Set by the TLE watchdog when it kills the process for
			# exceeding the time limit, so on_stop can judge TLE
			self.tle_killed = False
			# Generation of the current run. Every insert_test() bumps it
			# and the listener / watchdog threads of the previous run stop
			# touching the state: a leftover watchdog used to observe the
			# *next* test still running with the old start_time and kill it
			# immediately, which was reported as a bogus TLE.
			self._active_gen = 0
			# The program's clock. It stays None while the plugin is still
			# waiting for the sample to be pasted into the panel, so neither
			# the displayed runtime nor the TLE watchdog counts the time the
			# user spends copying the sample from the statement.
			self._clock_start = None
			# True for a test that was just created interactively (its input
			# is not known yet).
			self.awaiting_input = False
			if type(self.process_manager) != ProcessManager:
				self.process_manager.set_calls(self.__on_out, self.__on_stop, on_status_change)

		def note_activity(self):
			"""Start the program clock: it has input, or produced output."""
			if self._clock_start is None:
				self._clock_start = time()

		def run_generation(self):
			return self._active_gen

		def __on_stop(self, rtcode, runtime=-1, crash_line=None, gen=None):
			if gen is not None and gen != self._active_gen:
				# Thread of an older run (its process was replaced): its
				# result belongs to a test that no longer exists.
				return
			self.prog_out[self.running_test] = self.prog_out[self.running_test].rstrip()
			self.proc_run = False

			# CRITICAL: clear the ProcessManager's running marker. Without
			# this the next insert_test()/run() raises AssertionError
			# ('cant run process because is already running') and both the
			# manual 'next test' button and the automatic multi-sample
			# advance chain die after the first test.
			if type(self.process_manager) == ProcessManager:
				self.process_manager.is_run = False

			if self.running_new:
				self.test_iter += 1

			# State above is updated inline; everything below touches views and
			# must run on the main thread. This callback arrives from the pump
			# thread, and building the verdict cards / phantoms off-thread is
			# what made the panel crash at random.
			def deliver(self=self, rtcode=rtcode, runtime=runtime,
						crash_line=crash_line):
				if type(self.process_manager) == ProcessManager:
					self.on_status_change('STOPPED')
				self.on_stop(rtcode, runtime, crash_line=crash_line, epoch=self.epoch)

			post_to_ui(deliver)

		def __on_out(self, s, gen=None):
			if gen is not None and gen != self._active_gen:
				return
			if not s:
				return
			n = self.running_test
			if s.strip():
				# The program is doing something: the run clock is running
				# even if it never reads the input we sent.
				self.note_activity()
			limit = get_settings().get('max_output_bytes', 0) or 0
			kept = clamp_output(self.prog_out[n], s, limit)
			if kept:
				self.prog_out[n] += kept
				self.post_out(kept)
			if len(kept) < len(s) and not self.output_truncated:
				# The cap is now reached. Keep draining the pipe so the program
				# is never blocked on a full pipe buffer, but say so instead of
				# silently showing a mysterious WA: a marker in the output plus
				# the detail view's message section.
				self.output_truncated = True
				marker = '\n' + t('output_truncated_warn') + '\n'
				self.prog_out[n] += marker
				self.tests[n].message = t('output_truncated_warn')
				self.post_out(marker)
				print('[Algorithm Competition Assistant] %s' % t('output_truncated_warn'))

		def post_out(self, text):
			"""Hand one output chunk to the main thread for display."""
			post_to_ui(lambda: self.on_out(text, epoch=self.epoch))

		def _start_pump(self):
			"""Drain the running program on a private thread.

			Sublime shares one worker between every set_timeout_async callback
			in every plugin, and this pump blocks for the whole run, so running
			it there stalls auto-run-on-save and every other plugin for as long
			as the program takes.
			"""
			pump = threading.Thread(target=self.__process_listener)
			pump.daemon = True
			pump.start()

		def __process_listener(self):
			proc = self.process_manager
			# This thread belongs to one run only. insert_test() bumps the
			# generation; a stale thread must stop reading (the shared
			# ProcessManager object has been replaced by then, so it would
			# else consume the new program's output twice).
			gen = self._active_gen
			# Hard TLE: kill the process once it exceeds the time limit
			# (cph-ng style). The blocking stdout read below would never
			# unblock for silent infinite loops, hence the watchdog thread.
			limit_ms = None
			try:
				limit_ms = proc.get_time_limit_ms()
			except Exception:
				limit_ms = None
			if limit_ms:
				watchdog = threading.Thread(
					target=self.__tle_watchdog,
					args=(gen, limit_ms)
				)
				watchdog.daemon = True
				watchdog.start()
			try:
				while proc.is_stopped() is None:
					if gen != self._active_gen:
						return
					if self.sync_out:
						s = proc.read(bfsize=1)
					else:
						s = proc.read(_OUT_CHUNK)
					self.__on_out(s, gen)
				# Final drain. The process is gone so whatever is left is
				# already in the pipe, but a single bounded read would drop
				# everything past the first chunk: keep going until EOF.
				try:
					while True:
						s = proc.read(_OUT_CHUNK)
						if not s:
							break
						self.__on_out(s, gen)
				except Exception:
					pass
			except Exception as e:
				# Draining the output must never skip the memory-sampler stop
				# and on_stop below: an exception here used to leak a thread
				# polling a dead pid and leave the status stuck on RUNNING.
				print('[Algorithm Competition Assistant] output listener error: %s' % e)
			if gen != self._active_gen:
				return
			# Only the time the program was actually working with its input
			# counts: while the plugin was still waiting for the sample to
			# be pasted, the clock never started.
			start_time = self._clock_start
			# Stop at the exit the manager stamped, not here: the drain above
			# and the memory-sampler teardown are our work, and a judge never
			# bills them to the program. Without the stamp (a duck-typed
			# manager) fall back to now.
			end_time = getattr(proc, 'exit_time', None)
			if start_time is None:
				runtime = 0
			elif end_time is not None and end_time >= start_time:
				runtime = int((end_time - start_time) * 1000)
			else:
				runtime = int((time() - start_time) * 1000)
			# Freeze the peak memory reading before the process disappears
			try:
				if type(proc) == ProcessManager:
					proc.finish_memory_sampling()
			except Exception:
				pass
			self.__on_stop(proc.is_stopped(), runtime, gen=gen)

		def __tle_watchdog(self, gen, limit_ms):
			limit_s = float(limit_ms) / 1000.0
			proc = self.process_manager
			while True:
				if gen != self._active_gen:
					# a newer run replaced this one: never touch it
					return
				if proc.is_stopped() is not None:
					return
				start_time = self._clock_start
				if start_time is not None and time() - start_time >= limit_s + _TLE_KILL_GRACE_S:
					self.tle_killed = True
					try:
						proc.terminate()
					except Exception:
						pass
					return
				sleep(0.05)

		def insert(self, s, call_on_insert=False):
			n = self.running_test
			if self.proc_run:
				self.tests[n].append_string(s)
				self.process_manager.write(s)
				if s and s.strip():
					# The user just fed the program: from here on the clock
					# runs and the time limit is enforced.
					self.note_activity()
				if call_on_insert:
					self.on_insert(s)

		def _spawn_moment(self):
			"""When the current process came to life, as the manager saw it.

			The manager owns that timestamp because it is the one calling Popen;
			falling back to now() keeps a duck-typed process manager (the
			interactive and parallel stand-ins) working unchanged.
			"""
			stamp = getattr(self.process_manager, 'spawn_time', None)
			return stamp if stamp is not None else time()

		def insert_test(self, id=None):
			if id is None:
				id = self.test_iter
			tests = self.tests

			# Both callers reach this on a worker thread (the process spawn
			# and the stdin write below block, which is why they do not belong
			# on the main thread), so the two view touches are handed over.
			def set_running(self=self):
				if type(self.process_manager) == ProcessManager:
					self.on_status_change('RUNNING')

			post_to_ui(set_running)

			self.proc_run = True
			self.tle_killed = False
			self.output_truncated = False
			# New generation: listeners / watchdogs of the previous run stop
			# here, and this run's threads can be told apart from theirs.
			self._active_gen += 1
			# A test whose input is not known yet (freshly created with
			# Ctrl+Enter / 'next test') starts with the clock paused: the
			# user still has to paste the sample, and that wait must not
			# count as runtime or trip the TLE watchdog.
			input_text = tests[id].test_string
			self.process_manager.run()
			# The program's clock starts when the process came to life, never
			# before it: creating the child costs tens of milliseconds on
			# Windows and none of that is the program's doing. Feeding the
			# sample is included, because on a judge the program spends its
			# own time reading stdin.
			self._clock_start = None if self.awaiting_input else self._spawn_moment()
			self.process_manager.write(input_text)
			# The stored sample is the whole input, so the program must see
			# EOF - exactly like on the judge. An EMPTY sample is the
			# placeholder waiting for the user to paste, so stdin stays open.
			if input_text and input_text.strip():
				self.process_manager.close_stdin()
			if self._clock_start is None and input_text and input_text.strip():
				self._clock_start = self._spawn_moment()
			post_to_ui(lambda: self.on_insert(input_text))

		def next_test(self, tie_pos, cb):
			n = self.test_iter
			tests = self.tests
			prog_out = self.prog_out

			if self.proc_run:
				# Recover from a stale proc_run whose listener thread died
				# without calling __on_stop: if the process has really
				# exited, proceed instead of blocking 'next test' forever.
				pm = self.process_manager
				stale = (type(pm) == ProcessManager and pm.is_stopped() is not None)
				if stale:
					self.proc_run = False
					if type(pm) == ProcessManager:
						pm.is_run = False
				else:
					sublime.status_message(t('process_already_running'))
					return

			# A test that does not exist yet is the interactive placeholder:
			# its input comes from the clipboard, so the run clock may not
			# start until the user provides something.
			self.awaiting_input = n >= len(tests)

			# Pad up to n: 'Run failed tests' can jump the iterator past
			# accepted tests, so a single append is not enough (it used to
			# raise IndexError on tests[n] / prog_out[i] and kill the whole
			# chain in both the listener thread and update_configs).
			while n >= len(tests):
				tests.append(CphTestManagerCommand.Test(''))
			while n >= len(prog_out):
				prog_out.append('')
			tests[n].set_tie_pos(tie_pos)
			self.running_test = n
			self.running_new = True

			def go(self=self, cb=cb):
				self.insert_test()
				if type(self.process_manager) == ProcessManager:
					self._start_pump()
				# The chain callback rebuilds the test cards - PhantomSets and
				# view settings - and this closure runs on a worker thread, so
				# the callback has to be handed over like everything else that
				# touches a view.
				post_to_ui(cb)

			sublime.set_timeout_async(go, 10)

		def run_test(self, id):
			# The test menu can run any test directly, before the chain ever
			# materialised it. prog_out starts empty, so self.prog_out[id]
			# raised IndexError and the run never started (status stuck at
			# COMPILING).
			while id >= len(self.prog_out):
				self.prog_out.append('')
			# Compiling inside a phantom click callback froze the whole
			# editor for up to 30s; do it on the async worker instead and
			# reuse the compile cache when the sources are unchanged.
			process_manager = self.process_manager

			def compile_worker(self=self, process_manager=process_manager):
				try:
					if should_skip_compile(process_manager):
						cmp_data = (0, t('compile_cached'))
					else:
						cmp_data = process_manager.compile()
						if cmp_data is not None and cmp_data[0] == 0:
							remember_compile(process_manager)
				except Exception as e:
					cmp_data = (1, str(e))

				def start():
					if cmp_data is not None and cmp_data[0] != 0:
						# do not run a stale binary after a failed compile
						post_to_ui(lambda: self.on_status_change('STOPPED'))
						sublime.status_message(t('compile_error'))
						return
					self.running_test = id
					self.running_new = False
					# Re-running a test whose input is on file: the clock
					# starts with the process, no interactive wait.
					self.awaiting_input = False
					self.prog_out[id] = ''
					self.insert_test(id)
					if type(self.process_manager) == ProcessManager:
						self._start_pump()

				# Spawning the process and pushing the whole sample into its
				# stdin both block; on the main thread that was an editor
				# freeze for as long as the program took to read.
				sublime.set_timeout_async(start, 0)

			self.on_status_change('COMPILING')
			sublime.set_timeout_async(compile_worker, 10)

		def have_pretests(self):
			n = self.test_iter
			tests = self.tests
			return n < len(tests)

		def get_tests(self):
			return self.tests

		def set_tests(self, tests):
			self.tests.clear()
			for test in tests:
				self.tests.append(CphTestManagerCommand.Test(test))
			# Same invariant as __init__: prog_out must be as long as tests,
			# or get_tie_pos()/toggle_fold() raise IndexError.
			while len(self.prog_out) < len(self.tests):
				self.prog_out.append('')

		def output_at(self, i):
			"""Output of test i; '' when it was never materialised.

			The test menu can act on any test - including session-restored
			ones this session never ran - so indexing prog_out directly used
			to raise IndexError inside a Sublime callback (silently swallowed).
			"""
			if 0 <= i < len(self.prog_out):
				return self.prog_out[i]
			return ''

		def accept_out(self, nth):
			outs = self.prog_out
			tests = self.tests
			if nth >= len(outs):
				return None
			tests[nth].add_correct_answer(outs[nth].rstrip().lstrip())
			tests[nth].remove_uncorrect_answer(outs[nth].rstrip().lstrip())

		def decline_out(self, nth):
			outs = self.prog_out
			tests = self.tests
			if nth >= len(outs):
				return None
			tests[nth].remove_correct_answer(outs[nth].rstrip().lstrip())
			tests[nth].add_uncorrect_answer(outs[nth].rstrip().lstrip())

		def check_test(self, nth):
			return self.tests[nth].is_correct_answer(self.output_at(nth))

		def terminate(self):
			self.process_manager.terminate()

		def terminate_async(self):
			"""Stop the program without blocking the caller.

			Every UI path that stops a run (closing the panel, re-running over
			a live process, the stop button) reaches this from the main thread,
			where the real kill would freeze the editor for up to 3 seconds.
			"""
			pm = self.process_manager
			if type(pm) == ProcessManager:
				pm.terminate_async()
			else:
				pm.terminate()

	def insert_text(self, edit, text=None):
		v = self.view
		if not self.tester:
			return None
		expected = v.line(self.delta_input).end()
		if len(v.sel()) > 1: return
		if v.sel()[0].a != expected or v.sel()[0].b != expected: return
		if text is None:
			if not self.tester.proc_run:
				return None
			to_shove = v.substr(Region(self.delta_input, v.sel()[0].b))
			v.insert(edit, v.sel()[0].b, '\n')
		else:
			to_shove = text
			v.insert(edit, v.sel()[0].b, to_shove + '\n')
		self.delta_input = v.sel()[0].b
		self.tester.insert(to_shove + '\n')

	def insert_cb(self, edit):
		v = self.view
		if not self.tester:
			return
		# Sublime prints "Unable to open clipboard" and returns '' when
		# another process holds the clipboard; just do nothing then.
		try:
			s = sublime.get_clipboard()
		except Exception:
			s = ''
		if not s:
			return
		# Insert the paste in one go. The old loop dispatched one command per
		# line, so pasting a 10k line sample meant 10k round trips through the
		# main thread (and 10k writes into the child's stdin).
		self.tester.insert(s, call_on_insert=True)

	def toggle_fold(self, i):
		v = self.view
		tester = self.tester

		_inp = self.tester.tests[i].test_string
		# output_at(): a test that was never run has no prog_out entry yet
		# (delete_test() calls this first, so a direct index crashed).
		_outp = self.tester.output_at(i)
		text = _inp + '\n' + _outp.rstrip() + '\n' + '\n'
		tie_pos = self.get_tie_pos(i)

		if tester.tests[i].fold:
			v.run_command('cph_test_manager', {
				'action': 'replace',
				'region': (tie_pos + 1, tie_pos + 1),
				'text': text
			})

			v.add_regions(self.REGION_BEGIN_KEY % i, \
				[Region(tie_pos + 1)], *self.REGION_BEGIN_PROP)

			v.add_regions('test_end_%d' % i, \
				[Region(tie_pos + len(_inp) + 2, tie_pos + len(_inp) + 2)], \
					*self.REGION_END_PROP)

			d = len(text)
			for j in range(i + 1, self.tester.test_iter):
				self.tester.tests[j].tie_pos += d

			tester.tests[i].fold = False
		else:
			v.run_command('cph_test_manager', {
				'action': 'replace',
				'region': (tie_pos + 1, tie_pos + 1 + len(text)),
				'text': ''
			})

			v.erase_regions(self.REGION_BEGIN_KEY % i)
			v.erase_regions('test_end_%d' % i)

			d = len(text)
			for j in range(i + 1, tester.test_iter):
				tester.tests[j].tie_pos -= d

			tester.tests[i].fold = True
			# an inline phantom detail would float detached after folding
			if self.get_detail_style() == 'phantom':
				self.close_test_detail(i)
		v.sel().clear()
		v.sel().add(Region(v.size()))
		self.update_configs()

	def _find_edit_view(self, test_id, mode):
		"""Existing 'test N -edit/-answer' tab owned by this source view."""
		window = self.view.window()
		if window is None:
			return None
		name = 'test %d %s' % (test_id, '-edit' if mode == 'input' else '-answer')
		for wv in window.views():
			if (wv.name() or '') != name:
				continue
			if not wv.settings().get('cph_edit_view'):
				continue
			if wv.settings().get('cph_edit_source') == self.view.id():
				return wv
		return None

	def open_test_edit(self, i):
		v = self.view
		window = v.window()
		tester = self.tester
		test = tester.tests[i]
		# 'data' carries the current correct answer for the answer view
		correct_answer = ''
		if test.correct_answers:
			correct_answer = next(iter(test.correct_answers))
		elif test.expected_output:
			correct_answer = test.expected_output
		# Two separate tabs: input and expected answer. A dedicated answer
		# view replaces the old fragile "------ answer ------" separator
		# line which was easy to delete by accident.
		window.focus_group(1)
		# Reuse an already-open tab for this test instead of stacking a
		# second identical one (saving then edited the first match only).
		input_view = self._find_edit_view(i, 'input')
		if input_view is None:
			input_view = window.new_file()
		window.set_view_index(input_view, 1, 1)
		input_view.run_command('cph_test_edit', {
			'action': 'init',
			'mode': 'input',
			'test_id': i,
			'test': test.test_string,
			'source_view_id': v.id()
		})
		answer_view = self._find_edit_view(i, 'answer')
		if answer_view is None:
			answer_view = window.new_file()
		window.set_view_index(answer_view, 1, 1)
		answer_view.run_command('cph_test_edit', {
			'action': 'init',
			'mode': 'answer',
			'test_id': i,
			'test': '',
			'data': correct_answer,
			'source_view_id': v.id()
		})
		window.focus_view(input_view)

	def test_index_at_cursor(self):
		"""Index of the test block the cursor currently sits in.

		Model based: the run view no longer writes 'Test N {' title lines
		(that marker was removed with the old syntax), so parsing the buffer
		always failed and every copy fell back to the last test.
		"""
		tester = self.tester
		if tester is None or not tester.tests:
			return None
		v = self.view
		pt = v.sel()[0].begin() if len(v.sel()) else 0
		best = None
		for i in range(len(tester.tests)):
			try:
				start = self.get_tie_pos(i)
			except Exception:
				continue
			if start <= pt:
				best = i
			else:
				break
		return best

	def copy_test_part(self, part='input'):
		"""Copy one part of the test under the cursor to the clipboard."""
		tester = self.tester
		if tester is None or not tester.tests:
			sublime.status_message(t('no_tests'))
			return

		idx = self.test_index_at_cursor()
		if idx is None or idx >= len(tester.tests):
			idx = len(tester.tests) - 1
		test = tester.tests[idx]

		if part == 'input':
			value = test.test_string
		elif part == 'expected':
			answers = sorted(test.correct_answers)
			value = answers[0] if answers else (test.expected_output or '')
		else:
			value = tester.prog_out[idx] if idx < len(tester.prog_out) else ''

		value = (value or '').rstrip('\n')
		if not value.strip():
			sublime.status_message(t('nothing_to_copy'))
			return
		sublime.set_clipboard(value)
		sublime.status_message(t('copied_test_part', id=idx + 1))

	def update_summary_bar(self):
		"""Phantom at the end of the run view: whole-run summary.

		Saves scanning a long row of cards: '4/5 passed - first failure
		test 3 - total 1.24s'.
		"""
		v = self.view
		tester = self.tester
		try:
			if not get_settings().get('show_summary_bar', True) or tester is None \
					or not tester.tests or tester.proc_run:
				self.summary_phantom.update([])
				return

			passed = 0
			judged = 0
			first_fail = None
			total_ms = 0
			for i, test in enumerate(tester.tests):
				vd = getattr(test, 'verdict', None)
				name = vd.get('name') if isinstance(vd, dict) else None
				if not name or name in ('WT', 'CP', 'CPD', 'JG', 'JGD', 'CMP'):
					continue
				judged += 1
				if name == 'AC':
					passed += 1
				elif first_fail is None:
					first_fail = i + 1
				rt = getattr(test, 'runtime', None)
				if isinstance(rt, (int, float)):
					total_ms += rt

			if judged == 0:
				self.summary_phantom.update([])
				return

			parts = [t('summary_passed', passed=passed, total=judged)]
			if first_fail is not None:
				parts.append(t('summary_first_fail', id=first_fail))
			parts.append(t('summary_total_time', time='%.2f' % (total_ms / 1000.0)))

			ok = passed == judged
			color = '#49cd32' if ok else '#d3140d'

			# Subtask groups (NOI style): a group scores only when every test
			# in it passed, so the plain pass count is not the whole story.
			groups = subtask_parse(get_settings().get('subtasks'))
			extra = ''
			if groups:
				verdicts = []
				for test in tester.tests:
					vd = getattr(test, 'verdict', None)
					verdicts.append(vd.get('name') if isinstance(vd, dict) else None)
				extra = ('<br>' + subtask_html(subtask_evaluate(verdicts, groups)))

			html = (
				'<div style="padding: 2px 0 6px 0;">'
				'<span style="color: %s; font-weight: bold;">%s</span>'
				'<span style="color: var(--foreground); opacity: 0.75;"> &nbsp;·&nbsp; %s</span>'
				'%s'
				'</div>'
			) % (color, parts[0], ' &nbsp;·&nbsp; '.join(parts[1:]), extra)
			self.summary_phantom.update([
				Phantom(Region(v.size(), v.size()), html, sublime.LAYOUT_BLOCK)
			])
		except Exception:
			pass

	def record_stats(self):
		"""Append this run's verdicts to the local practice statistics.

		Called once per finished chain (and once per parallel batch), not per
		test: the stats count *runs*, and a run is a whole evaluation.
		"""
		try:
			tester = self.tester
			file_name = self.dbg_file
			if tester is None or not tester.tests or not file_name:
				return
			verdicts = []
			for test in tester.tests:
				verdict = getattr(test, 'verdict', None)
				if isinstance(verdict, dict) and verdict.get('name'):
					verdicts.append(verdict['name'])
			if verdicts:
				stats_record_run(file_name, verdicts)
			# The store is best-effort, but silence is not: a read-only User
			# directory means the report quietly stays empty forever, and the
			# only trace used to be a console print nobody reads.
			problem = stats_take_save_error()
			if problem:
				sublime.status_message(t('stats_save_failed', error=problem))
		except Exception as e:
			print('[Algorithm Competition Assistant] stats: %s' % e)

	def manager_factory(self, time_limit_ms=None, memory_limit_mb=None):
		"""A fresh ProcessManager factory for one test.

		Parallel runs and the benchmark need a manager per test: sharing the
		serial one would mix up pipes, stderr and the running flag.
		"""
		settings = get_settings()
		run_settings = settings.get('run_settings')
		separate_stderr = bool(settings.get('ignore_stderr', True))
		file = self.dbg_file
		build_sys = getattr(self, 'build_sys', None)

		def make_manager():
			manager = ProcessManager(file, build_sys, run_settings=run_settings)
			if separate_stderr:
				manager.set_separate_stderr(True)
			if time_limit_ms is not None:
				manager.set_time_limit(time_limit_ms)
			if memory_limit_mb is not None:
				manager.set_memory_limit(memory_limit_mb)
			return manager

		return make_manager

	def checker_for(self):
		"""Checker config for the file being tested, or None."""
		target = None
		if self.tester is not None:
			target = getattr(self.tester.process_manager, 'file', None)
		return checker_config(get_settings().get('run_settings'),
							  target or self.dbg_file)

	def apply_checker(self, test_id, stdout, expected):
		"""(verdict, message) from a configured checker, or (None, '').

		The verdict is a verdict dict ready for set_verdict(); the message is
		whatever the checker printed (testlib's quitf text).
		"""
		cfg = self.checker_for()
		if not cfg:
			return None, ''
		exe, error = checker_executable(cfg)
		if error:
			return None, error
		test = self.tester.tests[test_id]
		result = checker_judge(cfg, exe, test.test_string, stdout, expected)
		message = result.get('message') or ''
		if message:
			message = '%s %s' % (t('checker_judged', verdict=result['verdict']), message)
		return get_verdict(result['verdict']), message

	def benchmark(self):
		"""Run one test N times and report min/avg/max.

		Constant-factor work (卡常) needs a stable number: a single run is
		dominated by process start-up and scheduler noise. Runs sequentially
		so the measurements do not compete with each other.
		"""
		tester = self.tester
		if tester is None or not tester.tests:
			sublime.status_message('[Algorithm Competition Assistant] ' + t('benchmark_no_test'))
			return
		index = self.test_index_at_cursor()
		if index is None or index >= len(tester.tests):
			index = 0
		settings = get_settings()
		try:
			runs = int(settings.get('benchmark_runs', 5) or 5)
		except (TypeError, ValueError):
			runs = 5
		runs = max(2, min(runs, 50))

		test = tester.tests[index]
		if test.correct_answers:
			expected = next(iter(test.correct_answers))
		else:
			expected = test.expected_output or ''

		time_limit_ms = None
		memory_limit_mb = None
		try:
			time_limit_ms = tester.process_manager.get_time_limit_ms()
			memory_limit_mb = tester.process_manager.get_memory_limit_mb()
		except Exception:
			pass

		sublime.status_message('[Algorithm Competition Assistant] ' + t('benchmark_running', n=runs))
		self.set_compile_bar(t('benchmark_running', n=runs))
		run_parallel_batch(
			self.manager_factory(time_limit_ms, memory_limit_mb),
			[(test.test_string, expected)] * runs,
			workers=1, time_limit_ms=time_limit_ms,
			memory_limit_mb=memory_limit_mb,
			float_tolerance=0, regard_pe_as_ac=False,
			on_done=lambda results: self.report_benchmark(index, runs, results))

	def report_benchmark(self, index, runs, results):
		times = [r.get('runtime') or 0 for r in (results or []) if r]
		# not named 't': that would shadow the i18n helper for this whole
		# function (see the AST guard in tests/run_tests.py).
		times = [value for value in times if value > 0]
		if not times:
			self.set_compile_bar('')
			sublime.status_message('[Algorithm Competition Assistant] ' + t('benchmark_no_test'))
			return
		best = min(times)
		worst = max(times)
		average = int(sum(times) / float(len(times)))
		text = t('benchmark_done', n=len(times), best=best, avg=average, worst=worst)
		self.set_compile_bar(text)
		sublime.status_message('[Algorithm Competition Assistant] ' + text)
		try:
			self.tester.tests[index].message = text
		except Exception:
			pass

	def interactor_for(self):
		"""Interactor config for the file being tested, or None."""
		target = None
		if self.tester is not None:
			target = getattr(self.tester.process_manager, 'file', None)
		return interactor_config(get_settings().get('run_settings'),
								 target or self.dbg_file)

	def run_parallel_batch(self, interactive=False):
		"""Judge every test at once with a small worker pool.

		Serial execution costs roughly TL x tests when several tests time out;
		with 4 workers that becomes about TL x tests / 4. Each test gets its
		OWN ProcessManager (own process, pipes and stderr file), so the
		workers share nothing.

		`interactive` swaps the per-case runner for the interactor dialogue
		(see core/cph_interactive) and forces a single worker: an interactor
		is usually not reentrant and the dialogue is slow.
		"""
		tester = self.tester
		if tester is None or not tester.tests:
			return
		settings = get_settings()
		try:
			workers = int(settings.get('parallel_workers', 4) or 4)
		except (TypeError, ValueError):
			workers = 4

		time_limit_ms = None
		memory_limit_mb = None
		try:
			time_limit_ms = tester.process_manager.get_time_limit_ms()
			memory_limit_mb = tester.process_manager.get_memory_limit_mb()
		except Exception:
			pass

		make_manager = self.manager_factory(time_limit_ms, memory_limit_mb)

		cases = []
		for test in tester.tests:
			expected = ''
			if test.correct_answers:
				expected = next(iter(test.correct_answers))
			elif test.expected_output:
				expected = test.expected_output
			cases.append((test.test_string, expected))

		# Build the checker (if any) BEFORE the workers start: compiling it
		# from several threads at once would race on the same output file.
		cfg = self.checker_for()
		checker_hook = None
		if cfg:
			exe, error = checker_executable(cfg)
			if error:
				sublime.status_message('[Algorithm Competition Assistant] ' + error)
			else:
				def checker_hook(input_text, stdout, expected, default_verdict):
					result = checker_judge(cfg, exe, input_text, stdout, expected)
					message = result.get('message') or ''
					if message:
						message = '%s %s' % (t('checker_judged',
											   verdict=result['verdict']), message)
					return get_verdict(result['verdict']), message

		case_runner = None
		if interactive:
			cfg = self.interactor_for()
			exe, error = (None, '')
			if cfg:
				exe, error = interactor_executable(cfg)
			if error:
				sublime.status_message('[Algorithm Competition Assistant] ' + error)
				self.set_compile_bar(error)
				return
			if exe and cfg:
				# One worker: an interactor is normally not reentrant, and a
				# dialogue is slow enough that concurrency buys nothing.
				workers = 1

				def case_runner(index, input_text, expected):
					return interactive_run_case(make_manager, exe, input_text,
												expected, cfg['time_limit_ms'],
												cwd=cfg.get('dir'))

		label = 'interactive_running' if interactive else 'parallel_running'
		self.set_compile_bar(t(label, done=0, total=len(cases)))
		run_parallel_batch(
			make_manager, cases, workers=workers,
			time_limit_ms=time_limit_ms, memory_limit_mb=memory_limit_mb,
			float_tolerance=settings.get('float_tolerance', 0) or 0,
			regard_pe_as_ac=bool(settings.get('regard_pe_as_ac', False)),
			on_done=self.apply_parallel_results,
			on_progress=lambda done, total: self.set_compile_bar(
				t(label, done=done, total=total)),
			judge=checker_hook, case_runner=case_runner)

	def store_parallel_result(self, i, result):
		"""Copy one parallel result onto its Test (same shape as on_stop)."""
		tester = self.tester
		test = tester.tests[i]
		verdict = result.get('verdict')
		if verdict:
			test.set_verdict(verdict)
		test.set_stdout(result.get('stdout') or '')
		test.set_stderr(result.get('stderr') or '')
		test.set_cur_runtime(result.get('runtime') or 0)
		rtcode = result.get('rtcode')
		test.set_cur_rtcode(rtcode if rtcode is not None else 0)
		test.rtcode = str(rtcode if rtcode is not None else 0)
		memory = result.get('memory')
		if memory:
			test.set_memory(memory)
		crash = result.get('crash')
		if crash:
			test.crash_line = '%s:%d' % crash
			test.crash_path = crash[0]
			test.crash_lineno = crash[1]
			test.message = t('runtime_error_at', location=test.crash_line)
		if result.get('error'):
			test.message = result['error']
		elif result.get('message'):
			# e.g. the checker's verdict message
			test.message = result['message']
		while len(tester.prog_out) <= i:
			tester.prog_out.append('')
		tester.prog_out[i] = result.get('stdout') or ''

	def apply_parallel_results(self, results):
		"""Write a finished parallel batch into the panel (main thread)."""
		v = self.view
		tester = self.tester
		if tester is None or not results:
			return
		tests = tester.tests
		blocks = []
		regions = []
		offset = 0
		for i, test in enumerate(tests):
			if i < len(results) and results[i]:
				self.store_parallel_result(i, results[i])
			block = test.test_string + '\n' + tester.output_at(i).rstrip() + '\n\n'
			regions.append((offset, offset + len(test.test_string)))
			blocks.append(block)
			offset += len(block)

		# One replace for the whole panel: the block layout matches what
		# get_tie_pos()/update_configs() expect for an unfolded test.
		v.run_command('cph_test_manager', {
			'action': 'replace',
			'region': [0, v.size()],
			'text': ''.join(blocks),
		})
		for i, (begin, end) in enumerate(regions):
			tests[i].fold = False
			v.add_regions(self.REGION_BEGIN_KEY % i, [Region(begin)],
						  *self.REGION_BEGIN_PROP)
			v.add_regions('test_end_%d' % i, [Region(end, end)],
						  *self.REGION_END_PROP)
		tester.test_iter = len(tests)
		v.sel().clear()
		v.sel().add(Region(v.size()))
		self.set_compile_bar(t('parallel_done', total=len(tests)))
		self.update_configs()
		self.record_stats()
		sublime.status_message('[Algorithm Competition Assistant] '
							   + t('parallel_done', total=len(tests)))

	def is_skippable(self, i):
		"""True when a test must not be executed in the current chain.

		Only used by 'Run failed tests': already accepted tests keep their
		verdict and are folded out of the way instead of being re-executed.
		"""
		if not getattr(self.tester, 'run_failed', False):
			return False
		tests = self.tester.tests
		if i >= len(tests):
			return False
		# A fresh run clears every verdict, so the tests that were AC before
		# it started are remembered in 'previously_accepted'.
		if i in getattr(self.tester, 'previously_accepted', ()):
			return True
		verdict = getattr(tests[i], 'verdict', None)
		return isinstance(verdict, dict) and verdict.get('name') == 'AC'

	def next_runnable_index(self):
		"""Index of the next test this chain should run, or None when done."""
		tester = self.tester
		if tester is None:
			return None
		i = tester.test_iter
		while i < len(tester.tests):
			if self.is_skippable(i):
				i += 1
				continue
			return i
		return None

	def advance_chain(self):
		"""Start/continue the automatic run chain. True when a test started."""
		v = self.view
		tester = self.tester
		next_i = self.next_runnable_index()
		if next_i is None:
			return False
		# Fold the skipped tests: they never get inserted into the run view,
		# and folding keeps the phantom position bookkeeping consistent.
		for j in range(tester.test_iter, next_i):
			tester.tests[j].fold = True
		tester.test_iter = next_i
		v.run_command('cph_test_manager', {'action': 'new_test'})
		return True

	def get_tie_pos(self, i):
		v = self.view
		tester = self.tester
		pt = 0
		for j in range(i):
			if j >= len(tester.tests):
				break
			# output_at(): prog_out can be shorter than tests (a test this
			# session never ran). Indexing it directly raised IndexError
			# inside the Ctrl+D handler, so the delete silently did nothing.
			out_len = len(tester.output_at(j))
			running = tester.proc_run and j == tester.running_test

			if running:
				pt += len(tester.tests[j].test_string) + out_len + 1
			elif not tester.tests[j].fold:
				pt += len(tester.tests[j].test_string) + out_len + 1

			if not tester.tests[j].fold:
				pt += 2

		return pt

	def on_test_action(self, i, event):
		v = self.view
		tester = self.tester
		if tester.proc_run and event in {'test-click', 'test-edit', 'test-run', 'test-detail', 'test-close-detail'}:
			sublime.status_message(t('cannot_action_while_running', action=event))
			return
		if event == 'test-click':
			self.toggle_fold(i)
		elif event == 'test-detail':
			# opens/refreshes the detail view of this test
			self.show_test_detail(i)
		elif event == 'test-close-detail':
			self.close_test_detail(i)
		elif event == 'test-edit':
			self.open_test_edit(i)
		elif event == 'test-stop':
			tester.terminate_async()
		elif event == 'test-run':
			if not tester.tests[i].fold:
				self.toggle_fold(i)
			tie_pos = self.get_tie_pos(i)
			v.run_command('cph_test_manager', {
				'action': 'replace',
				'region': (tie_pos, tie_pos),
				'text': '\n\n'
			})
			v.add_regions('type', \
				[Region(tie_pos + 1)], *self.REGION_BEGIN_PROP)

			self.input_start = tie_pos + 1
			self.delta_input = tie_pos + 1

			v.sel().clear()
			v.sel().add(Region(tie_pos + 1))

			self.prepare_code_view()

			tester.run_test(i)
			self.update_configs()

	def jump_for(self, test):
		"""[path, line, label] when this test has a located runtime error.

		The detail view turns it into a clickable link (see core/cph_jump).
		"""
		path = getattr(test, 'crash_path', '') or ''
		line = getattr(test, 'crash_lineno', 0) or 0
		if not path or not line:
			return None
		return [path, line, '%s:%d' % (path, line)]

	def get_detail_view_name(self, i):
		return path.split(self.dbg_file)[1] + ' - test %d detail' % (i + 1)

	@staticmethod
	def _clean_output(s):
		"""Normalize a program output for display: kill lone \r characters
		(rendered as the mysterious '<0x0d>' in Sublime) and unify CRLF."""
		if not s:
			return ''
		return s.replace('\r\n', '\n').replace('\r', '')

	def build_detail_content(self, i, test):
		verdict_short = test.verdict['name'] if test.verdict else 'UKE'
		runtime_str = test.get_nice_runtime().replace('&nbsp;', ' ').strip()
		header = '%s %d  |  %s  |  %s: %s' % (t('test_label').capitalize(), i + 1,
			verdict_short, t('time'), runtime_str)
		if test.memory not in ('-', None):
			header += '  |  %s: %s' % (t('memory'),
				test.get_nice_memory().replace('&nbsp;', ' '))
		lines = [header, '=' * max(len(header), 40), '']

		# sample input
		test_input = self._clean_output(test.test_string).rstrip('\n')
		lines.append('[%s]' % t('input'))
		lines.append(test_input if test_input.strip() else t('detail_empty'))
		lines.append('')

		expected = ''
		if test.correct_answers:
			expected = next(iter(test.correct_answers))
		elif test.expected_output:
			expected = test.expected_output
		expected = self._clean_output(expected)
		stdout = self._clean_output(test.stdout)

		lines.append('[%s]' % t('expected_output'))
		lines.append(expected.rstrip('\n') if expected.strip() else t('detail_empty'))
		lines.append('')
		lines.append('[%s]' % t('actual_output'))
		lines.append(stdout.rstrip('\n') if stdout.strip() else t('detail_empty'))
		lines.append('')

		lines.append('[%s] (%s)' % (t('diff'), t('diff_ignore_trailing')))
		if not expected.strip():
			lines.append(t('diff_no_expected'))
		else:
			ops, total = build_line_diff(expected, stdout)
			if ops is None:
				lines.append(t('diff_all_match', n=total))
			else:
				lines.append(t('diff_lines_differ', n=len(ops), total=total))
				lines.append('')
				shown = ops[:_DETAIL_DIFF_LIMIT]
				for kind, line_no, e, a in shown:
					lines.append('  Line %d:' % line_no)
					if kind != '+':
						lines.append('- %s: %s' % (t('expected_short'), e if e else t('detail_empty')))
					if kind != '-':
						lines.append('+ %s: %s' % (t('actual_short'), a if a else t('detail_empty')))
				if len(ops) > len(shown):
					lines.append('')
					lines.append(t('diff_shown_truncated', shown=len(shown),
						n=len(ops)))
		lines.append('')

		if test.stderr and test.stderr.strip():
			lines.append('[%s]' % t('error_output'))
			lines.append(self._clean_output(test.stderr).rstrip('\n'))
			lines.append('')

		if test.message:
			lines.append('[%s]' % t('message'))
			lines.append(test.message)
			lines.append('')

		return '\n'.join(lines)

	def get_detail_style(self):
		"""'view' (default): open a real, selectable detail view with
		highlighting. 'phantom': the original inline minihtml panel."""
		return get_settings().get('detail_style', 'view')

	def show_test_menu(self):
		"""Keyboard-only access to the test cards (competitive programmers
		rarely touch the mouse): pick a test, then pick an action."""
		tester = self.tester
		if tester is None or not tester.tests:
			sublime.status_message(t('no_tests'))
			return
		window = self.view.window()
		if window is None:
			return
		items = []
		for i in range(len(tester.tests)):
			test = tester.tests[i]
			name = test.verdict['name'] if test.verdict else '--'
			items.append(['%s %d   [%s]' % (t('test_label'), i + 1, name)])
		self._menu_tests = list(range(len(tester.tests)))

		def on_pick(idx):
			if idx < 0 or idx >= len(tester.tests):
				return
			self.show_test_action_menu(idx)

		window.show_quick_panel(items, on_pick)

	def show_test_action_menu(self, i):
		"""Action list for one test, mirroring its card buttons."""
		window = self.view.window()
		if window is None:
			return
		tester = self.tester
		if tester is None or i >= len(tester.tests):
			return
		options = [[t('run')], [t('detail')], [t('edit')],
				   [t('accept')], [t('decline')], [t('delete')]]

		def act(idx):
			if idx < 0:
				return
			event = {0: 'test-run', 1: 'test-detail', 2: 'test-edit'}.get(idx)
			if event:
				self.on_test_action(i, event)
			elif idx == 3:
				self.on_accdec_action(i, 'click-accept')
			elif idx == 4:
				self.on_accdec_action(i, 'click-decline')
			elif idx == 5:
				self.view.run_command('cph_test_manager',
									  {'action': 'delete_test', 'id': i})

		window.show_quick_panel(options, act)

	def show_test_detail(self, i):
		v = self.view
		tester = self.tester
		if tester is None or i >= len(tester.tests):
			return
		test = tester.tests[i]
		if test.verdict is None and test.runtime == '-' and not test.stdout:
			sublime.status_message(t('detail_no_result'))
			return

		if self.get_detail_style() == 'phantom':
			self.show_test_detail_phantom(i)
			return

		window = v.window()
		if window is None:
			return

		if test.fold and str(getattr(test, 'rtcode', '0')) != '0':
			# unfold so the detail matches what is visible in the run view
			self.toggle_fold(i)

		name = self.get_detail_view_name(i)
		detail_view = None
		for wv in window.views():
			if wv.name() == name:
				detail_view = wv
				break
		if detail_view is None:
			detail_view = window.new_file()
			window.set_view_index(detail_view, 1, 1)
			detail_view.set_name(name)
			detail_view.set_scratch(True)
			detail_view.run_command('set_setting', {'setting': 'word_wrap', 'value': False})
			detail_view.run_command('set_setting', {'setting': 'fold_buttons', 'value': False})
		# Deliberately NO custom syntax here: the detail stays plain text
		# (default syntax). Per user feedback the syntax-based coloring was
		# removed; the content format/layout is unchanged.
		detail_view.run_command('cph_test_detail_view', {
			'text': self.build_detail_content(i, test),
			'jump': self.jump_for(test)})
		window.focus_view(detail_view)

	def show_test_detail_phantom(self, i):
		"""Original inline minihtml detail panel (detail_style = 'phantom')."""
		v = self.view
		tester = self.tester
		test = tester.tests[i]
		if test.fold:
			# unfold first so the detail panel shows below
			# the expanded input/output of this test
			self.toggle_fold(i)

		pt = self.get_tie_pos(i)
		pt += len(test.test_string) + len(tester.output_at(i)) + 1

		detail = test.get_detail(i, pt, self.on_test_action, self.view)

		if not hasattr(self, 'detail_phantoms'):
			self.detail_phantoms = [PhantomSet(v, 'test-detail-' + str(j)) for j in range(10)]
			self.detail_open = set()
		while len(self.detail_phantoms) <= i:
			self.detail_phantoms.append(PhantomSet(v, 'test-detail-' + str(len(self.detail_phantoms))))

		self.detail_phantoms[i].update([detail])
		self.detail_open.add(i)

	def close_stale_detail_views(self, from_index):
		"""Close detail tabs of tests that shifted down (index >= from_index)."""
		window = self.view.window()
		if window is None:
			return
		for wv in window.views():
			m = re.search(r'- test (\d+) detail$', wv.name() or '')
			if m and int(m.group(1)) - 1 >= from_index:
				try:
					wv.close()
				except Exception:
					pass

	def close_test_detail(self, i):
		# close the inline phantom detail if one is open
		if hasattr(self, 'detail_open'):
			self.detail_open.discard(i)
		if hasattr(self, 'detail_phantoms') and i < len(self.detail_phantoms):
			self.detail_phantoms[i].update([])
		# close the detail view if one is open
		v = self.view
		window = v.window()
		if window is None:
			return
		name = self.get_detail_view_name(i)
		for wv in window.views():
			if wv.name() == name:
				wv.close()
				return

	def on_accdec_action(self, i, event):
		v = self.view
		tester = self.tester
		if event == 'click-accept':
			tester.accept_out(i)
		elif event == 'click-decline':
			tester.decline_out(i)
		self.update_configs()
		self.memorize_tests()

	def set_test_input(self, test=None, id=None):
		v = self.view
		tester = self.tester
		# The edit tabs can outlive their test (it was deleted, or the run
		# panel was rebuilt): saving then used to raise IndexError and the
		# edit was silently dropped.
		if test is None or id is None or tester is None:
			return
		if not (0 <= id < len(tester.tests)):
			sublime.status_message(t('test_gone'))
			return
		unfold = False
		if not tester.tests[id].fold:
			self.toggle_fold(id)
			unfold = True

		# A whitespace-only buffer (the empty placeholder the panel starts
		# with) is not an input: keeping it as '\n' made an "empty sample"
		# that could never be told apart from a real one.
		tester.tests[id].test_string = test if test.strip() else ''

		if unfold:
			self.toggle_fold(id)

		self.memorize_tests()

	def set_correct_answer(self, data=None, id=None):
		"""Set the correct answer for a test. Re-judges existing output."""
		if id is None or data is None:
			return
		tester = self.tester
		if tester is None or not (0 <= id < len(tester.tests)):
			sublime.status_message(t('test_gone'))
			return
		test = tester.tests[id]
		# Clear and set new correct answer
		test.correct_answers = set()
		answer = data.strip()
		if answer:
			test.add_correct_answer(answer)
		test.set_expected_output(answer)

		# Re-judge with the new answer if this test already has output
		if id < len(tester.prog_out):
			out = tester.prog_out[id].rstrip()
			if out and str(getattr(test, 'rtcode', '0')) == '0':
				pm = tester.process_manager
				float_tolerance = get_settings().get('float_tolerance', 0) or 0
				verdict = get_verdict_by_code(
					rtcode=0,
					runtime=int(test.runtime) if test.runtime not in ('-', None) else 0,
					time_limit_ms=pm.get_time_limit_ms() if hasattr(pm, 'get_time_limit_ms') else None,
					memory_limit_mb=pm.get_memory_limit_mb() if hasattr(pm, 'get_memory_limit_mb') else None,
					stderr=test.stderr,
					stdout=out,
					expected_output=answer,
					ignore_error=True,
					regard_pe_as_ac=bool(get_settings().get('regard_pe_as_ac', False)),
					float_tolerance=float_tolerance
				)
				test.set_verdict(verdict)
				# The answer matches what the program printed -> the sample
				# is accepted, so collapse it exactly like an automatic AC
				# run does. Leaving it expanded showed an 'accept' button
				# for an answer that was already correct.
				if (verdict['name'] == 'AC'
						and test.is_correct_answer(out, float_tolerance)
						and not test.fold):
					self.toggle_fold(id)

		self.memorize_tests()
		# Re-render to update the display
		self.update_configs()

	def get_next_title(self):
		v = self.view
		styles = get_test_styles(v)
		content = read_resource('Highlight/test_next.html')
		content = content.format(next_label=t('next_test'))
		content = '<style>' + styles + '</style>' + content

		def onclick(event, v=v):
			v.run_command('cph_test_manager', {
				'action': 'new_test'
			})

		phantom = Phantom(Region(self.view.size() - 1), content, sublime.LAYOUT_BLOCK, onclick)
		return phantom

	def update_configs(self, update_last=None):
		v = self.view
		tester = self.tester
		_float_tolerance = get_settings().get('float_tolerance', 0) or 0
		configs = []
		if tester.proc_run:
			k = tester.test_iter + 1
		else:
			k = tester.test_iter
		k = min(k, len(tester.tests))
		pt = 0
		_last_test_entry = -1
		for i in range(k):
			running = tester.proc_run and i == tester.running_test

			config = tester.tests[i].get_config(
				i,
				pt,
				self.on_test_action,
				tester.prog_out[i],
				self.view,
				running=running
			)
			_last_test_entry = len(configs)
			configs.append(config)

			if running:
				pt += len(tester.tests[i].test_string) + len(tester.prog_out[i]) + 2
			elif not tester.tests[i].fold:
				pt += len(tester.tests[i].test_string) + len(tester.prog_out[i]) + 1

			# getattr for safety, matching the other three call sites
			if (not running and not tester.tests[i].fold
					and str(getattr(tester.tests[i], 'rtcode', '0')) == '0'
					and tester.prog_out[i]):
				if tester.tests[i].is_correct_answer(tester.prog_out[i], _float_tolerance):
					type = 'decline'
				else:
					type = 'accept'
				accdec = tester.tests[i].get_accdec(
					i,
					pt,
					self.on_accdec_action,
					type,
					self.view
				)
				configs.append(accdec)

			if not tester.tests[i].fold:
				pt += 2

		if not tester.proc_run:
			configs.append(self.get_next_title())

		while len(self.test_phantoms) < len(configs):
			self.test_phantoms.append(PhantomSet(v, 'test-phantom-' + str(len(self.test_phantoms))))

		hide_phantoms = v.settings().get('hide_phantoms')
		if update_last:
			self.test_phantoms[_last_test_entry].update([configs[_last_test_entry]] if not hide_phantoms else [])
		else:
			for i in range(len(configs)):
				self.test_phantoms[i].update([configs[i]] if not hide_phantoms else [])

			for i in range(len(configs), len(self.test_phantoms)):
				self.test_phantoms[i].update([])

		if not hide_phantoms:
			# Delayed so the layout/viewport settles before measuring
			self.schedule_auto_fit_panel_width()

		# Re-anchor the summary bar here as well: expanding a folded (AC)
		# test inserts its text and would otherwise leave the summary
		# phantom stranded inside that test's block.
		self.update_summary_bar()

	def schedule_auto_fit_panel_width(self, delay=150):
		"""Coalesce auto-fit requests into one pending measurement.

		update_configs() runs after every card edit, every fold/unfold and
		every output line, so calling sublime.set_timeout() directly queued a
		full measure + possible window.set_layout() per edit. With a burst of
		output that is dozens of relayouts a second - the stutter users
		noticed. One timer is kept at most, and a request that arrives while
		it is pending just marks the work dirty so the next run re-measures.
		"""
		if getattr(self, '_fit_scheduled', False):
			self._fit_dirty = True
			return
		self._fit_scheduled = True
		self._fit_dirty = False
		sublime.set_timeout(self._run_auto_fit_panel_width, delay)

	def _run_auto_fit_panel_width(self):
		self._fit_scheduled = False
		try:
			self.auto_fit_panel_width()
		finally:
			if getattr(self, '_fit_dirty', False):
				self._fit_dirty = False
				self.schedule_auto_fit_panel_width()

	def auto_fit_panel_width(self):
		"""
		Widen the run panel group until the widest test card fits on a
		single line (buttons no longer stack / wrap), capped at
		'max_panel_width_ratio' of the window layout (default: 1/2).
		Only ever widens the panel, never shrinks it, and only touches the
		standard two-group layout.
		"""
		v = self.view
		w = v.window()
		if w is None or v.settings().get('hide_phantoms'):
			return
		settings = get_settings()
		if not settings.get('auto_fit_panel_width', True):
			return
		try:
			max_ratio = float(settings.get('max_panel_width_ratio', 0.5))
		except (TypeError, ValueError):
			max_ratio = 0.5
		max_ratio = min(max(max_ratio, 0.2), 0.9)

		# Cheap layout/viewport checks run before the expensive width
		# estimate: reading the panel viewport and comparing it against the
		# last applied column width settles the common "nothing to do" case
		# without touching settings or estimating text.
		layout = w.get_layout()
		cols = layout.get('cols') or []
		cells = layout.get('cells')
		if len(cols) != 3 or not cells:
			return  # only adjust the standard two-group layout
		panel_frac = 1.0 - cols[1]
		if panel_frac >= max_ratio - 1e-4:
			return  # panel already at the configured maximum
		try:
			viewport_px = v.viewport_extent()[0]
		except Exception:
			return
		if viewport_px <= 0:
			return

		with_memory = False
		tester = self.tester
		if tester is not None and getattr(tester, 'tests', None):
			with_memory = any(
				x.memory != '-' and x.memory is not None for x in tester.tests)

		needed = estimate_card_width_px(v, with_memory)
		if needed <= 0:
			return
		try:
			em = v.em_width()
		except Exception:
			em = 0
		# Allowance for gutter + phantom margins so we err on the wide side
		if viewport_px - 4 * em >= needed:
			return  # cards already fit on one line

		layout_px = viewport_px / max(panel_frac, 0.05)
		target_frac = min(max_ratio, needed / layout_px)
		target_col1 = 1.0 - target_frac
		if target_col1 >= cols[1] - 1e-3:
			return  # change too small or would only narrow - never shrink
		# Sub-pixel rounding made the target wobble by a fraction of a
		# percent between refreshes; relayouting the whole window for that
		# is what produced the visible stutter. Only a real change counts.
		last = getattr(self, '_fit_last_cols1', None)
		if last is not None and abs(target_col1 - last) < 0.01:
			return
		self._fit_last_cols1 = target_col1
		try:
			w.set_layout({
				'cols': [cols[0], target_col1, cols[2]],
				'rows': layout.get('rows', [0, 1]),
				'cells': cells,
			})
		except Exception:
			pass


	def new_test(self, edit):
		v = self.view

		self.input_start = v.size()
		self.delta_input = v.size()
		self.output_start = v.size() + 1
		self.out_region_set = False

		v.add_regions('type', \
			[Region(v.size(), v.size())], *self.REGION_BEGIN_PROP)

		v.sel().clear()
		v.sel().add(Region(v.size()))

		self.tester.next_test(v.size() - 1, lambda: self.update_configs(update_last=True))

	def memorize_tests(self):
		if self.tester and self.dbg_file:
			tests_data = [x.memorize() for x in (self.tester.get_tests())]
			if not save_tests(self.dbg_file, tests_data):
				# Every other save path reports this (cph_companion,
				# cph_extras, cph_import, cph_stress). Silently dropping it
				# here meant "my test points are gone" with no explanation.
				# Not a status_message: a read-only folder fails on every
				# single run, and the tests are already shown in the view.
				print('[Algorithm Competition Assistant] %s'
					% t('tests_save_failed', path=self.dbg_file))

	def on_insert(self, s):
		self.view.run_command('cph_test_manager', {'action': 'insert_opd_input', 'text': s})

	def on_out(self, s, epoch=None):
		v = self.view
		# Same stale-listener guard as on_stop: a killed process can still
		# drain bytes, and those would be inserted into the *new* run view at
		# the new delta_input, corrupting it.
		if epoch is not None and epoch != getattr(self, 'tester_epoch', None):
			print('[Algorithm Competition Assistant] dropped stale on_out from killed process')
			return
		self.view.run_command('cph_test_manager', {'action': 'insert_opd_out', 'text': s})
		if not self.out_region_set:
			self.out_region_set = True

	def on_stop(self, rtcode, runtime, crash_line=None, epoch=None):
		v = self.view
		# Drop callbacks from a stale listener thread whose process was
		# killed by a re-run: they would corrupt the new tester's state
		if epoch is not None and epoch != getattr(self, 'tester_epoch', None):
			print('[Algorithm Competition Assistant] dropped stale on_stop from killed process')
			return
		tester = self.tester

		test_id = self.tester.running_test
		_inp = self.tester.tests[test_id].test_string
		_outp = self.tester.prog_out[test_id]
		_outp = _outp.rstrip()

		if tester.running_new:
			_outp += '\n' + '\n'

		self.tester.tests[test_id].set_cur_runtime(runtime)
		self.tester.tests[test_id].set_cur_rtcode(rtcode)

		pm = tester.process_manager
		time_limit_ms = None
		memory_limit_mb = None
		if hasattr(pm, 'get_time_limit_ms'):
			time_limit_ms = pm.get_time_limit_ms()
		if hasattr(pm, 'get_memory_limit_mb'):
			memory_limit_mb = pm.get_memory_limit_mb()

		# Actually measured peak memory of the process. Without this the
		# memory_limit_mb setting was dead code and MLE could never happen.
		memory_used_mb = None
		try:
			if type(pm) == ProcessManager:
				memory_used_mb = pm.get_peak_memory_mb()
		except Exception:
			memory_used_mb = None

		float_tolerance = get_settings().get('float_tolerance', 0) or 0

		stderr = ''
		if getattr(pm, 'separate_stderr', False) and hasattr(pm, 'get_stderr'):
			stderr = pm.get_stderr()

		expected_output = ''
		if self.tester.tests[test_id].correct_answers:
			expected_output = next(iter(self.tester.tests[test_id].correct_answers))
		elif self.tester.tests[test_id].expected_output:
			expected_output = self.tester.tests[test_id].expected_output

		# A crash location recovered by the gdb replay above. Set before the
		# verdict is chosen so the RE branch below can prefer it over the
		# libstdc++ header line that appears in the program's own output.
		crash_location = None

		if getattr(tester, 'tle_killed', False):
			# The watchdog killed the process at the time limit. Unless the
			# error stream carries a crash signature: then the program died
			# on its own and the kill was collateral, so RE is the honest
			# verdict (the exit code is the one our own kill produced and
			# must not be used here).
			if looks_like_crash(stderr or _outp):
				verdict = get_verdict('runtime_error')
			else:
				# On Windows an aborting program is dead at t=0 but is only
				# reaped ~4.5s later (Windows Error Reporting on the abort
				# path), so its assertion text never reaches us before the
				# watchdog fires and every debug-mode crash came out as a TLE.
				# Replaying under gdb catches the abort frame in ~0.35s.
				crash_location = _replay_crash(
					pm, getattr(pm, 'file', None) or self.dbg_file, _inp,
					time_limit_ms)
				if crash_location:
					verdict = get_verdict('runtime_error')
				else:
					verdict = get_verdict('time_limit_exceed')
		elif getattr(pm, 'terminated', False):
			# stopped manually by the user -> not a real judge result
			verdict = get_verdict('skipped')
		else:
			verdict = get_verdict_by_code(
				rtcode=int(rtcode) if rtcode is not None else 0,
				runtime=runtime,
				time_limit_ms=time_limit_ms,
				memory_limit_mb=memory_limit_mb,
				stderr=stderr,
				stdout=_outp,
				expected_output=expected_output,
				ignore_error=True,
				# Many OJ treat PE as AC; user selectable (default: strict)
				regard_pe_as_ac=bool(get_settings().get('regard_pe_as_ac', False)),
				memory_used_mb=memory_used_mb,
				float_tolerance=float_tolerance
			)

		if memory_used_mb:
			self.tester.tests[test_id].set_memory(memory_used_mb)
		self.tester.tests[test_id].set_verdict(verdict)
		self.tester.tests[test_id].set_stdout(_outp)

		# An AC that used 95 % of the limit is a trap: on a judge whose machine
		# is slower the same code is a TLE. Warn in the status bar; the verdict
		# itself is left exactly as computed.
		if verdict['name'] in ('AC', 'PE'):
			_risk = tle_advisory(runtime, time_limit_ms)
			if _risk:
				sublime.status_message('[Algorithm Competition Assistant] ' + t(
					'near_tle_warn', pct=int(round(_risk * 100)),
					runtime=int(runtime or 0), limit=int(time_limit_ms or 0)))

		# Locate a runtime error instead of leaving a bare RE badge: a Python
		# traceback / Java stack trace / sanitizer diagnostic in the program's
		# output names the file and the line. (`crash_line` used to be a
		# parameter that was accepted but never computed.)
		if verdict['name'] == 'RE':
			source_file = getattr(pm, 'file', None) or self.dbg_file
			location = crash_location
			if not location:
				location = find_crash_location(stderr or _outp, source_file)
			# What the program's own output names is usually a libstdc++
			# header (stl_vector.h:1263 for a vector out-of-bounds read): real,
			# but useless, because the line the user has to fix is their own.
			# One gdb replay (~0.35s measured) turns it into their frame, and
			# is the only source of a line at all when the crash printed
			# nothing. Skipped when the output already named their file.
			if not _names_source(location, source_file):
				_replayed = _replay_crash(pm, source_file, _inp, time_limit_ms)
				if _replayed:
					location = _replayed
			if location:
				crash_line = '%s:%d' % location
				self.tester.tests[test_id].crash_line = crash_line
				# kept apart as well: the detail view turns them into a link
				self.tester.tests[test_id].crash_path = location[0]
				self.tester.tests[test_id].crash_lineno = location[1]
				self.tester.tests[test_id].message = t('runtime_error_at',
													   location=crash_line)
				sublime.status_message('[Algorithm Competition Assistant] '
									   + t('runtime_error_at', location=crash_line))

		# A TLE is the verdict the user can least explain, so say what is
		# most likely behind it instead of leaving a bare red badge.
		if verdict['name'] == 'TLE':
			hints = []
			if not (stderr or '').strip() and not (_outp or '').strip():
				# The watchdog killed the process, so there is no exit code
				# to judge by, and with no output there is no crash signature
				# either - the usual cause is a program still waiting for
				# input (an incomplete sample) or a silent infinite loop.
				hints.append(t('tle_no_output'))
			try:
				compile_cmd = pm.get_compile_cmd() or ''
			except Exception:
				compile_cmd = ''
			if '-DLOCAL' in compile_cmd or '-DDEBUG' in compile_cmd:
				# A debug build is not a judge build: the usual -DLOCAL
				# template pulls in algo/debug.h, whose debug() writes to the
				# unbuffered stderr on every call. Inside a loop that is
				# O(n^2) output, and the program runs orders of magnitude
				# slower than it would on the judge.
				hints.append(t('tle_debug_build'))
			if hints:
				self.tester.tests[test_id].message = '\n'.join(hints)

		# A configured checker (SPJ) decides AC/WA/PE itself instead of the
		# token comparison - and it is the only way to judge a problem whose
		# answer is "any valid construction". Runs last so the RE/TLE hints
		# above are kept and the checker message is appended to them.
		checker_verdict, checker_message = self.apply_checker(test_id, _outp, expected_output)
		if checker_verdict is not None:
			self.tester.tests[test_id].set_verdict(checker_verdict)
		if checker_message:
			existing = self.tester.tests[test_id].message
			self.tester.tests[test_id].message = \
				(existing + '\n' if existing else '') + checker_message

		# Release mode cannot see undefined behaviour, and that is the one
		# failure the user cannot explain from the output: at -O2 an
		# out-of-bounds read returns heap garbage, so the program prints a
		# random number and exits 0 - the verdict is a plain WA with nothing
		# pointing at the real cause. Checked after the checker so an SPJ that
		# accepted the answer does not get the note, and mentioned once per
		# file: a wrong answer is normal and must not come with a lecture.
		if self.tester.tests[test_id].verdict['name'] == 'WA' \
				and not build_mode_label(self.dbg_file):
			key = (self.dbg_file or '').replace('\\', '/').lower()
			if key and key not in _WA_RELEASE_HINTED:
				_WA_RELEASE_HINTED.add(key)
				existing = self.tester.tests[test_id].message
				self.tester.tests[test_id].message = \
					(existing + '\n' if existing else '') + t('wa_release_hint')

		self.tester.tests[test_id].set_stderr(stderr)
		self.tester.tests[test_id].set_expected_output(expected_output)

		v.erase_regions('type')
		line = v.line(self.input_start)

		input_end = v.line(Region(self.delta_input)).end()

		if tester.running_new and self.tester.tests[test_id].is_correct_answer(self.tester.prog_out[test_id], float_tolerance):
			v.run_command('cph_test_manager', {
				'action': 'replace',
				'region': (self.input_start, input_end),
				'text': ''
			})
		else:
			v.run_command('cph_test_manager', {
				'action': 'replace',
				'region': (self.input_start, input_end),
				'text': _inp + '\n' + _outp
			})

			self.tester.tests[test_id].fold = False

			v.add_regions(self.REGION_BEGIN_KEY % test_id, \
				[Region(line.begin(), line.end())], *self.REGION_BEGIN_PROP)

		v.show(self.input_start + 20)

		v.add_regions('test_end_%d' % test_id, \
			[Region(self.input_start + len(_inp) + 1, self.input_start + len(_inp) + 1)], \
				*self.REGION_END_PROP)

		v.run_command('cph_test_manager', {'action': 'set_cursor_to_end'})

		tester = self.tester
		self.memorize_tests()

		# Chain into the next test. The old code only continued when
		# rtcode == 0, so one WA silently skipped every remaining sample
		# (and Companion problems with several samples only ran the first).
		# Modes:
		#   normal     - stop at the first failure (stop_on_first_failure)
		#   run all    - always continue
		#   run failed - continue, skipping tests already marked AC
		stop_on_first = get_settings().get('stop_on_first_failure', True)
		ok = str(rtcode) == '0'
		cont = (ok or (not stop_on_first)
				or getattr(tester, 'run_all', False)
				or getattr(tester, 'run_failed', False))
		if tester.running_new and cont and self.next_runnable_index() is not None:
			self.update_configs(update_last=True)
			sublime.set_timeout(self.advance_chain, 10)
		else:
			sublime.set_timeout(self.update_configs, 100)
			self.update_summary_bar()
			# The chain is over: this is the one moment the whole run's
			# verdicts are known, so record them for the practice stats.
			self.record_stats()

		# Refresh the open detail of this test (view mode and phantom mode)
		window = v.window()
		if window is not None and test_id < len(tester.tests):
			if self.get_detail_style() == 'phantom':
				if test_id in getattr(self, 'detail_open', set()):
					if tester.tests[test_id].fold:
						self.close_test_detail(test_id)
					else:
						pt = self.get_tie_pos(test_id)
						pt += len(tester.tests[test_id].test_string) + len(tester.prog_out[test_id]) + 1
						detail = tester.tests[test_id].get_detail(test_id, pt, self.on_test_action, self.view)
						if test_id < len(self.detail_phantoms):
							self.detail_phantoms[test_id].update([detail])
			else:
				name = self.get_detail_view_name(test_id)
				for wv in window.views():
					if wv.name() == name:
						try:
							content = self.build_detail_content(test_id, tester.tests[test_id])
						except Exception:
							break
						wv.run_command('cph_test_detail_view', {
							'text': content,
							'jump': self.jump_for(tester.tests[test_id])})
						break

	def change_process_status(self, status):
		self.view.set_status('process_status', status)

	def close_edit_views(self):
		"""Close leftover 'test N -edit' / 'test N -answer' edit views left
		behind by a previous session (e.g. the user clicked edit, did not
		save, then re-ran the program). Their save button would be dead
		anyway since the run view they point to has been rebuilt."""
		window = self.view.window()
		if window is None:
			return
		stale = []
		for wv in window.views():
			name = wv.name() or ''
			if re.match(r'^test \d+ -(edit|answer)$', name):
				stale.append(wv)
		for wv in stale:
			try:
				wv.close()
			except Exception:
				pass

	def clear_all(self):
		v = self.view
		v.run_command('cph_test_manager', {'action': 'erase_all'})
		v.sel().clear()
		v.sel().add(Region(v.size(), v.size()))
		self.phantoms.update([])
		for phs in self.test_phantoms:
			phs.update([])
		if self.tester:
			v.erase_regions('type')
			for i in range(-1, self.tester.test_iter + 1):
				v.erase_regions(self.REGION_BEGIN_KEY % i)
				v.erase_regions(self.REGION_END_KEY % i)
				v.erase_regions('line_%d' % i)
				v.erase_regions('test_error_%d' % i)

	def set_compile_bar(self, cmd, type='', chip_text=None):
		view = self.view
		styles = get_test_styles(view)
		# escape html specials so compiler output shows up correctly in minihtml
		# chip_text: short text for the panel chip. Compile failures keep the
		# full stderr OUT of the chip - the plain text below already carries
		# it (and it is copyable there), showing it twice just pushed the
		# test cards further down.
		display = cmd if chip_text is None else chip_text
		cmd_escaped = (display or '').replace('&', '&amp;') \
			.replace('<', '&lt;').replace('>', '&gt;')

		# Compile state drives the chip colour: blue while compiling, red when
		# the compiler failed, yellow when it only warned. A failed compile used
		# to leave the same blue "Compiling..." chip on screen, so the panel
		# gave no hint that anything had gone wrong.
		state = {'compiling': ' test-compiling-compiling',
				 'error': ' test-compiling-error',
				 'warning': ' test-compiling-warning'}.get(type, '')

		# Compiler diagnostics carry file:line:col, so turn each one into a
		# clickable link instead of leaving a wall of text the user has to
		# read a path out of. Nothing is removed: the raw output stays above.
		base_dir = os.path.dirname(self.dbg_file or '') or None
		entries = parse_diagnostics(cmd or '')
		diag_html = ''
		if entries:
			diag_html, targets = links_html(entries, base_dir=base_dir)
			set_targets(view, targets)
		else:
			set_targets(view, [])

		content = read_resource('Highlight/compile.html').format(
			cmd=cmd_escaped,
			state=state,
			compilation_error_label=t('compilation_error'),
			diagnostics=diag_html,
		)
		content = '<style>' + styles + '</style>' + content

		def onclick(event):
			handle_jump_event(view, event)

		phantom = Phantom(Region(0), content, sublime.LAYOUT_BLOCK, onclick)
		self.test_phantoms[0].update([phantom])

	def get_view_by_id(self, id):
		# The panel view can already be detached from its window while a
		# callback is in flight; asking for it used to raise
		# AttributeError: 'NoneType' object has no attribute 'views'
		# and left an empty -run tab behind.
		window = self.view.window()
		if window is None:
			return None
		for view in window.views():
			if view.id() == id:
				return view

	def prepare_code_view(self):
		code_view = self.get_view_by_id(self.code_view_id)
		if code_view:
			if code_view.is_dirty():
				code_view.run_command('save')

	def _recover_session_from_run_view(self, v):
		"""Rebuild the in-memory session lost after a plugin reload/restart.

		The run view is named '<source file name> -run'. When Sublime reloads
		the plugin (or hot_exit restores the -run tab) the OLD results are
		still on screen but this command instance is brand new, so
		self.session is None - and make_opd used to clear_all() the old
		results away first and only then discover there was nothing to
		restore ("cant restore session"), which is exactly the reported
		"old result page disappears and the new UI never appears". Recover
		the session from the view name instead, so the press rebuilds and
		re-runs like every other press.

		Returns the session dict, or None when the source view cannot be
		found (then the original fallback message still applies).
		"""
		name = v.name() or ''
		if not name.endswith(' -run') or not name[:-len(' -run')]:
			return None
		base = name[:-len(' -run')]
		window = v.window()
		if window is None:
			return None
		code = None
		try:
			code = window.find_open_file(base)
		except Exception:
			code = None
		if code is None:
			# Fallback for hosts without Window.find_open_file: scan views.
			try:
				for cand in window.views():
					fname = cand.file_name() or ''
					if os.path.split(fname)[1] == base:
						code = cand
						break
			except Exception:
				return None
		if code is None or not code.file_name():
			return None
		code_file = code.file_name()
		try:
			limits = get_problem_limits(code_file) or {}
		except Exception:
			limits = {}
		try:
			scope = code.scope_name(0).split()[0]
		except Exception:
			scope = ''
		return {
			'run_file': code_file,
			'build_sys': scope,
			'clr_tests': False,
			'sync_out': bool(get_settings().get('sync_output', False)),
			'code_view_id': code.id(),
			'time_limit_ms': limits.get('time_limit_ms'),
			'memory_limit_mb': limits.get('memory_limit_mb'),
		}

	def make_opd(self, edit, run_file=None, build_sys=None, clr_tests=False, \
		sync_out=False, code_view_id=None, load_session=False,
		time_limit_ms=None, memory_limit_mb=None,
		run_all=False, run_failed=False, force_compile=False, parallel=False):

		v = self.view

		# A view that is no longer attached to a window (the tab was closed
		# while a callback was queued) cannot host the run panel: bailing out
		# here avoids an AttributeError half way through the setup, which
		# left an empty -run tab behind.
		if v.window() is None:
			print('[Algorithm Competition Assistant] the run view has no window any more, ignoring')
			return

		# Re-entry guard: only block while a compile is genuinely in flight.
		# A stale 'COMPILING' status left behind by an older crashed compile
		# is auto-cleared after 30s so the user is never stuck forever.
		compiling_since = getattr(self, 'compiling_since', None)
		if compiling_since is not None and (time() - compiling_since) < 30:
			sublime.status_message('[Algorithm Competition Assistant] compiling in progress, wait or press again after 30s')
			return

		if v.get_status('process_status') == 'RUNNING' or \
				(self.tester is not None and self.tester.proc_run):
			# Re-run: kill the still-running process, then wait for it to
			# really die *off the UI thread* (see wait_then_rerun below).
			tester = self.tester
			pm = None
			if tester is not None:
				# Invalidate the old tester's callbacks BEFORE killing so
				# the stale __on_stop fired by the dying process is dropped
				# (it would otherwise re-render regions / schedule a
				# spurious new_test between here and the rerun).
				self.tester_epoch = getattr(self, 'tester_epoch', 0) + 1
				try:
					tester.terminate_async()
				except Exception:
					pass
				pm = tester.process_manager
				# Make absolutely sure the next run_file() cannot trip over
				# the stale running marker of the killed process
				try:
					if hasattr(pm, 'is_run'):
						pm.is_run = False
				except Exception:
					pass
				tester.proc_run = False

			kwargs = {
				'run_file': run_file,
				'build_sys': build_sys,
				'clr_tests': clr_tests,
				'sync_out': sync_out,
				'code_view_id': code_view_id,
				'load_session': load_session,
				'time_limit_ms': time_limit_ms,
				'memory_limit_mb': memory_limit_mb,
				'run_all': run_all,
				'run_failed': run_failed,
				'force_compile': force_compile,
				'parallel': parallel,
				'action': 'make_opd'
			}

			def wait_then_rerun(self=self, v=v, pm=pm, kwargs=kwargs):
				# Poll off the UI thread. The old code slept up to 2s inside
				# a main-thread callback, freezing the editor on every
				# re-run while a process was still alive.
				waited = 0.0
				while waited < 2.0:
					try:
						if pm is None or pm.is_stopped() is not None:
							break
					except Exception:
						break
					sleep(0.05)
					waited += 0.05

				def go():
					self.change_process_status('STOPPED')
					v.run_command('cph_test_manager', kwargs)

				sublime.set_timeout(go, 0)

			sublime.set_timeout_async(wait_then_rerun, 10)
			return

		# Clear any stale COMPILING status from a previous crashed compile
		if v.get_status('process_status') == 'COMPILING':
			self.compiling_since = None

		if v.settings().get('edit_mode'):
			self.apply_edit_changes()

		# Re-run on a restored/reloaded run view: the in-memory session dies
		# with the old command instance while the OLD RESULTS are still on
		# screen. Recover it from the view name BEFORE clear_all() wipes
		# them, so this press rebuilds and re-runs instead of leaving the
		# panel empty behind a "cant restore session" note (issue 3).
		if load_session and self.session is None:
			recovered = self._recover_session_from_run_view(v)
			if recovered is not None:
				self.session = recovered
				self.dbg_file = recovered['run_file']
				self.code_view_id = recovered['code_view_id']

		v.set_scratch(True)
		v.run_command('set_setting', {'setting': 'fold_buttons', 'value': False})
		v.run_command('set_setting', {'setting': 'line_numbers', 'value': False})
		# 'cph_run_view' marks this scratch view as the run panel. The old
		# status key still exists but now carries something useful
		# (language and limits) instead of the FOC-era 'opdebugger-file'.
		v.settings().set('cph_run_view', True)
		try:
			v.set_status('opd_info', run_view_status_label(
				run_file, time_limit_ms, memory_limit_mb))
		except Exception as e:
			print('[Algorithm Competition Assistant] status label failed: %s' % e)
		self.clear_all()
		self.close_edit_views()
		if load_session:
			if self.session is None:
				# Nothing to restore. Continuing here used to crash:
				# run_file stayed None -> path.splitext(None) TypeError,
				# code_view_id/dbg_file were never assigned.
				v.run_command('cph_test_manager', {'action': 'insert_opd_out', 'text': t('cant_restore_session')})
				self.change_process_status('STOPPED')
				v.set_status('opd_info', '')
				return
			else:
				run_file = self.session['run_file']
				build_sys = self.session['build_sys']
				clr_tests = self.session['clr_tests']
				sync_out = self.session['sync_out']
				code_view_id = self.session['code_view_id']
				time_limit_ms = self.session.get('time_limit_ms')
				memory_limit_mb = self.session.get('memory_limit_mb')
		else:
			print('[Algorithm Competition Assistant] session saved')
			self.session = {
				'run_file': run_file,
				'build_sys': build_sys,
				'clr_tests': clr_tests,
				'sync_out': sync_out,
				'code_view_id': code_view_id,
				'time_limit_ms': time_limit_ms,
				'memory_limit_mb': memory_limit_mb,
			}
			self.dbg_file = run_file
			self.build_sys = build_sys
			self.code_view_id = code_view_id

		self.prepare_code_view()

		if not v.settings().get('word_wrap'):
			v.run_command('toggle_setting', {'setting': 'word_wrap'})

		previously_accepted = set()
		clear_failures = []
		if not clr_tests:
			# Try to load tests from all possible locations (traditional + cph-ng style folder)
			loaded_data = load_all_tests(run_file)
			if loaded_data:
				# Keep tests whose input is empty too: a legitimate test can
				# read nothing, and a stress-test counterexample with empty
				# input used to be dropped here on every reload.
				tests = [self.Test(x) for x in loaded_data if isinstance(x, dict)]
			else:
				tests = []
		else:
			# Clear EVERY tests file this source file uses. Only clearing
			# the traditional path left tests/foo.cpp__tests behind, which
			# load_all_tests() merges back in - so "clean" tests survived.
			for tests_path in get_tests_paths(run_file):
				if not tests_path or not os.path.exists(tests_path):
					continue
				try:
					with open(tests_path, 'w', encoding='utf-8') as f:
						f.write('[]')
				except Exception as e:
					# A file that cannot be emptied is not cosmetic: the next
					# load_all_tests() merges it straight back, so the user
					# presses "clear tests" and the tests return.
					print('[Algorithm Competition Assistant] failed to clear %s: %s' % (tests_path, e))
					clear_failures.append('%s: %s' % (tests_path, e))
			if clear_failures:
				sublime.status_message(t('tests_clear_failed', error='; '.join(clear_failures)[:300]))
			tests = []

		if not load_session:
			# Pressing Run starts a clean evaluation. The verdicts that came
			# out of the tests file describe the *previous* run: leaving them
			# in place made the summary report 'N/N passed' for a run that had
			# just failed, and kept green badges on the tests this run never
			# reached. 'Run failed tests' still has to know what was AC, so
			# remember that before wiping.
			# NOTE: the loop variable must not be called 't' - that shadows the
			# i18n helper t() imported at module level, and since Python treats
			# any assignment as making the name local for the WHOLE function,
			# the later self.set_compile_bar(t('compiling')) blew up with
			# UnboundLocalError: local variable 't' referenced before
			# assignment. tests/run_tests.py guards against this now.
			previously_accepted = set(
				index for index, card in enumerate(tests)
				if isinstance(getattr(card, 'verdict', None), dict)
				and card.verdict.get('name') == 'AC')
			for card in tests:
				card.reset_run_state()
		file_ext = path.splitext(run_file)[1][1:]

		self.change_process_status('COMPILING')

		process_manager = ProcessManager(
			run_file,
			build_sys,
			run_settings=get_settings().get('run_settings')
		)

		# Optional: capture stderr (cerr etc.) separately so it is ignored
		# when comparing the program output against the correct answer
		if get_settings().get('ignore_stderr', True):
			process_manager.set_separate_stderr(True)

		if time_limit_ms is not None:
			process_manager.set_time_limit(time_limit_ms)
		if memory_limit_mb is not None:
			process_manager.set_memory_limit(memory_limit_mb)

		def compile(self=self, v=v):
			cached = False
			try:
				if should_skip_compile(process_manager, force_compile):
					cached = True
					cmp_data = (0, t('compile_cached'))
					print('[Algorithm Competition Assistant] compile skipped (source unchanged)')
				else:
					cmp_data = process_manager.compile()
					print('[Algorithm Competition Assistant] compile rc: %s' % (cmp_data[0] if cmp_data else None))
			except Exception as e:
				print('[Algorithm Competition Assistant] compile exception: %s' % e)
				cmp_data = (1, '[Algorithm Competition Assistant] compile failed: %s' % e)
			finally:
				self.compiling_since = None
			self.change_process_status('COMPILED')
			self.delta_input = 0
			if cmp_data is None or cmp_data[0] == 0:
				remember_compile(process_manager)
				# The success path never touched the compile bar, so the
				# "Compiling..." chip set before this ran stayed on screen for
				# the whole session - including a cache hit, where it was the
				# only thing that ever mentioned the compile at all. Replace it
				# with the outcome: warnings (an overflowing constant, an unused
				# result, ...) are the compiler telling you about code the run
				# is about to exercise, and they used to be dropped on the floor
				# because only the failure branch wrote them to the panel.
				note = cmp_data[1] if cmp_data else ''
				if cached:
					self.set_compile_bar(t('compile_cached'))
				elif not (note or '').strip():
					self.set_compile_bar('')
				elif 'warning' in (note or '').lower():
					# Clickable file:line entries come with the text; a yellow
					# chip keeps a warning from reading like a failure.
					self.set_compile_bar(note, type='warning',
										 chip_text=t('compile_warning'))
				else:
					self.set_compile_bar(note)
				self.tester_epoch = getattr(self, 'tester_epoch', 0) + 1
				self.tester = self.Tester(process_manager, \
					self.on_insert, self.on_out, self.on_stop, self.change_process_status, \
					tests=tests, sync_out=sync_out, epoch=self.tester_epoch,
					run_failed=run_failed)
				self.tester.run_all = run_all
				# Which tests were AC *before* this run started (see above).
				self.tester.previously_accepted = previously_accepted
				v.settings().set('edit_mode', False)
				interactive = self.interactor_for() is not None
				if parallel or interactive:
					# Judge every test at once with a small worker pool
					# (see core/cph_parallel); an interactive problem has to
					# go through it too, because the dialogue replaces the
					# single run (see core/cph_interactive).
					self.run_parallel_batch(interactive=interactive)
				elif run_failed or run_all:
					# These modes pick their own starting test and may skip
					# already-accepted ones, so drive the chain explicitly.
					self.advance_chain()
				else:
					v.run_command('cph_test_manager', {'action': 'new_test'})
			else:
				v.run_command('cph_test_manager', {'action': 'insert_opd_out', 'text': '\n' + cmp_data[1]})
				self.set_compile_bar(cmp_data[1], type='error',
									 chip_text=t('compilation_error'))

		self.set_compile_bar(t('compiling'), type='compiling')
		# Mark compile start only now - the terminate/rerun path above
		# must never be blocked by this guard on its re-entry
		self.compiling_since = time()

		sublime.set_timeout_async(compile, 10)

	def delete_test(self, edit, id):
		v = self.view
		tester = self.tester
		if not tester.tests[id].fold:
			self.toggle_fold(id)

		k = tester.test_iter
		if tester.proc_run:
			k += 1
		iter = 0
		for i in range(k):
			if not tester.tests[i].fold:
				_beg_reg = v.get_regions(self.REGION_BEGIN_KEY % i)
				_end_reg = v.get_regions('test_end_%d' % i)

				v.erase_regions(self.REGION_BEGIN_KEY % i)
				v.erase_regions('test_end_%d' % i)

				v.add_regions(self.REGION_BEGIN_KEY % iter, \
				_beg_reg, *self.REGION_BEGIN_PROP)

				v.add_regions('test_end_%d' % iter, \
					_end_reg, *self.REGION_END_PROP)

			if i != id:
				iter += 1

		del tester.tests[id]
		if id < len(tester.prog_out):
			del tester.prog_out[id]
		# test_iter counts the tests already run: deleting one that was never
		# reached must not move the chain backwards (that made already-run
		# tests run a second time).
		if id < tester.test_iter:
			tester.test_iter -= 1
		self.close_test_detail(id)
		# Every detail tab is named '<file> - test N detail': after removing
		# one test all following ones shift down, so their open tabs would
		# keep a stale name and content.
		self.close_stale_detail_views(id)
		self.memorize_tests()
		self.update_configs()

	def delete_tests(self, edit):
		v = self.view
		tester = self.tester

		if tester.proc_run:
			sublime.status_message(t('stop_before_delete'))
			return

		k = len(tester.tests)

		to_del = []
		for i in range(k):
			begin = self.get_tie_pos(i)
			if i == k - 1:
				end = v.size()
			else:
				end = self.get_tie_pos(i + 1)
			r = Region(begin, end)
			for sel in v.sel():
				if sel.intersects(r):
					to_del.append(i)
					break

		sublime.status_message(t('deleted_tests', ids=', '.join(map(lambda x: str(x + 1), to_del))))
		for test in reversed(to_del):
			self.delete_test(edit, test)
		self.memorize_tests()

	def sync_read_only(self):
		view = self.view
		tester = self.tester

		err = True
		if tester and tester.proc_run:
			err = False
			forb_before = self.delta_input
			forb_after = view.line(self.delta_input).b
			forbs = [Region(0, forb_before)]
			forbs.append(Region(forb_after, view.size() - 1))

			for forb in forbs:
				for sel in view.sel():
					if forb.intersects(sel):
						err = True

			delete_forb = False
			for sel in view.sel():
				if sel.a == self.delta_input or sel.begin() == 0:
					delete_forb = True
					break

			if view.settings().get('delete_forb') != delete_forb:
				view.settings().set('delete_forb', delete_forb)

		# ModifiedListener calls this on *every* caret move in the run panel.
		# set_read_only() is a real view update, so only touch it when the
		# value actually changes - otherwise every arrow key re-tags the
		# whole view.
		try:
			if view.is_read_only() != err:
				view.set_read_only(err)
		except Exception:
			view.set_read_only(err)

	def apply_edit_changes(self):
		v = self.view

		tests = []
		i = 0
		while self.get_begin_region(i):
			st = self.get_begin_region(i)[0].begin()
			if not self.get_begin_region(i + 1):
				end = v.size()
			else:
				end = self.get_begin_region(i + 1)[0].begin()
			tests.append(v.substr(Region(st, end)).strip() + '\n')
			i += 1

		self.tester.set_tests(tests)
		self.memorize_tests()

	def swap_tests(self, edit, dir=-1):
		tester = self.tester
		view = self.view
		selected = []
		unfold = []

		for i in range(len(tester.tests)):
			begin = self.get_tie_pos(i)
			end = self.get_tie_pos(i + 1)

			tester.tests[i].__sel = []

			for reg in view.sel():
				if reg.intersects(Region(begin, end)):
					selected.append(i)
					inter = reg.intersection(Region(begin, end))
					tester.tests[i].__sel.append(Region(inter.a - begin, inter.b - begin))
					break

		for i in range(len(tester.tests)):
			if not tester.tests[i].fold:
				tester.tests[i].__unfold = True
				self.toggle_fold(i)
			else:
				tester.tests[i].__unfold = False

		# prog_out can be shorter than tests (session-restored tests that this
		# session never ran): pad it so the parallel swap below cannot raise
		# IndexError.
		while len(tester.prog_out) < len(tester.tests):
			tester.prog_out.append('')
		if dir == 1:
			selected.reverse()
		for sel in selected:
			if 0 <= sel + dir < len(tester.tests):
				tester.tests[sel], tester.tests[sel + dir] = tester.tests[sel + dir], tester.tests[sel]
				tester.prog_out[sel], tester.prog_out[sel + dir] = tester.prog_out[sel + dir], tester.prog_out[sel]

		for i in range(len(tester.tests)):
			if tester.tests[i].__unfold:
				self.toggle_fold(i)
		view.sel().clear()
		for i in range(len(tester.tests)):
			for x in tester.tests[i].__sel:
				begin = self.get_tie_pos(i)
				view.sel().add(Region(begin + x.a, begin + x.b))
		# The cards carry the verdict of the row they were drawn for, so
		# they must be re-rendered after the swap.
		self.update_configs()

	def toggle_hide_phantoms(self):
		view = self.view
		view.settings().set('hide_phantoms', not view.settings().get('hide_phantoms'))
		self.update_configs()

	def get_begin_region(self, id):
		v = self.view
		return v.get_regions(self.REGION_BEGIN_KEY % id)

	def run(self, edit, action=None, run_file=None, build_sys=None, text=None, clr_tests=False, \
			sync_out=False, code_view_id=None, var_name=None, pos=None, \
			load_session=False, region=None, frame_id=None, data=None, id=None, dir=1,
			time_limit_ms=None, memory_limit_mb=None,
			run_all=False, run_failed=False, force_compile=False, part=None,
			parallel=False):

		v = self.view

		# Lazy initialization: ensure settings are loaded
		try:
			from .core.cph_settings import get_settings, try_load_settings
			if not get_settings():
				try_load_settings()
		except Exception:
			pass

		v.set_read_only(False)

		if self.tester is None and action in self.ACTIONS_NEEDING_TESTER:
			sublime.status_message(t('panel_not_ready'))
			return

		if action == 'insert_line':
			self.insert_text(edit)

		elif action == 'insert_cb':
			self.insert_cb(edit)

		elif action == 'insert_opd_input':
			v.insert(edit, self.delta_input, text)
			self.delta_input += len(text)

		elif action == 'insert_opd_out':
			v.insert(edit, self.delta_input, text)
			self.delta_input += len(text)

		elif action == 'replace':
			v.replace(edit, Region(region[0], region[1]), text)

		elif action == 'erase':
			v.erase(edit, Region(region[0], region[1]))

		elif action == 'apply_edit_changes':
			self.apply_edit_changes()

		elif action == 'make_opd':
			self.make_opd(edit, run_file=run_file, build_sys=build_sys, clr_tests=clr_tests, \
				sync_out=sync_out, code_view_id=code_view_id,
				load_session=load_session, time_limit_ms=time_limit_ms,
				memory_limit_mb=memory_limit_mb,
				run_all=run_all, run_failed=run_failed, force_compile=force_compile,
				parallel=parallel)

		elif action == 'close':
			# CphTestManagerCommand has no .process_manager attribute; the
			# process lives on the tester. The old code raised AttributeError
			# here so closing the run view silently left the process alive.
			try:
				if self.tester is not None:
					self.tester.terminate_async()
			except Exception:
				print('[Algorithm Competition Assistant] process terminating error')

		elif action == 'new_test':
			self.new_test(edit)

		elif action == 'delete_tests':
			self.delete_tests(edit)

		elif action == 'erase_all':
			v.replace(edit, Region(0, v.size()), '\n')

		elif action == 'benchmark':
			self.benchmark()

		elif action == 'refresh_status':
			# Recompute the run panel's status label (language + limits +
			# build mode). Used by the Debug/Release toggle so the new mode
			# shows up immediately instead of only on the next run.
			try:
				tl = ml = None
				if self.tester is not None:
					tl = self.tester.process_manager.get_time_limit_ms()
					ml = self.tester.process_manager.get_memory_limit_mb()
			except Exception:
				tl = ml = None
			v.set_status('opd_info', run_view_status_label(self.dbg_file, tl, ml))

		elif action == 'kill_proc':
			# Ctrl+X is also the default cut shortcut: only take it over
			# while something is actually running, and never crash when
			# the run view has no tester yet (fresh / restored view).
			tester = self.tester
			alive = tester is not None and (
				tester.proc_run or self.view.get_status('process_status') == 'RUNNING')
			if not alive:
				sublime.status_message(t('no_running_process'))
				return
			try:
				tester.terminate_async()
			except Exception as e:
				print('[Algorithm Competition Assistant] terminate failed: %s' % e)
				sublime.status_message(t('no_running_process'))

		elif action == 'sync_read_only':
			self.sync_read_only()

		elif action == 'show_test_menu':
			self.show_test_menu()

		elif action == 'copy_test_part':
			self.copy_test_part(part=part)

		elif action == 'set_test_input':
			self.set_test_input(id=id, test=data)

		elif action == 'set_correct_answer':
			self.set_correct_answer(id=id, data=data)

		elif action == 'delete_test':
			self.delete_test(edit, id)

		elif action == 'swap_tests':
			self.swap_tests(edit, dir=dir)

		elif action == 'toggle_hide_phantoms':
			self.toggle_hide_phantoms()

		elif action == 'set_cursor_to_end':
			v.sel().clear()
			v.sel().add(Region(v.size(), v.size()))

		self.sync_read_only()

class CphTestDetailViewCommand(sublime_plugin.TextCommand):
	"""Fills the detail view with the (static) detail text of a test.
	A real view is used instead of a phantom so the text is selectable,
	comparable and copyable."""

	def __init__(self, view):
		self.view = view
		self.phantoms = PhantomSet(view, 'cph-detail-jump')

	def run(self, edit, text='', jump=None):
		v = self.view
		v.set_scratch(True)
		v.set_read_only(False)
		v.replace(edit, Region(0, v.size()), text)
		v.set_read_only(True)
		v.sel().clear()
		v.sel().add(Region(0))
		self.set_jump(jump)

	def set_jump(self, jump):
		"""Clickable 'jump to the source line' chip above the detail.

		The detail view is a real (plain text) view, and a plain text view has
		no links - a phantom is the only way to make the location clickable.
		`jump` is [path, line, label] or None.
		"""
		v = self.view
		if not jump or not jump[0]:
			self.phantoms.update([])
			return
		label = jump[2] if len(jump) > 2 else None
		html, targets = one_link(jump[0], jump[1], label=label)
		set_targets(v, targets)
		content = '<style>' + get_test_styles(v) + '</style>' + html

		def onclick(event):
			handle_jump_event(v, event)

		self.phantoms.update([
			Phantom(Region(0), content, sublime.LAYOUT_BLOCK, onclick)
		])


class ModifiedListener(sublime_plugin.EventListener):
	def on_selection_modified(self, view):
		if view.settings().get('cph_run_view') and not view.settings().get('edit_mode'):
			view.run_command('cph_test_manager', { 'action': 'sync_read_only' })


class CloseListener(sublime_plugin.EventListener):
	def on_pre_close(self, view):
		if view.settings().get('cph_run_view'):
			# Closing the panel stops the process it owns.
			view.run_command('cph_test_manager', {'action': 'close'})
			return

		if view.settings().get('cph_edit_view'):
			# An edit tab is paired with its source view; that side is
			# handled when the *source* file closes.
			return

		file_name = view.file_name()
		if not file_name:
			return
		window = view.window()
		if window is None:
			return

		# Closing xxx.cpp closes 'xxx.cpp -run' (and any 'test N -edit' /
		# '-answer' tabs opened from it). Leaving them behind meant the user
		# had to close the panel by hand every time - and a forgotten panel
		# keeps its process, its output and its own tab around.
		target = file_name.replace('\\', '/').split('/')[-1] + ' -run'
		view_id = view.id()
		for other in list(window.views()):
			if other is view:
				continue
			if (other.name() or '') == target:
				try:
					other.close()
				except Exception:
					pass
				continue
			if other.settings().get('cph_edit_source') == view_id:
				try:
					other.close()
				except Exception:
					pass


class CphViewTesterCommand(sublime_plugin.TextCommand):
	def create_opd(self, clr_tests=False, sync_out=None,
				   time_limit_ms=None, memory_limit_mb=None,
				   run_all=False, run_failed=False, force_compile=False,
				   parallel=False):
		v = self.view
		if v.is_dirty():
			v.run_command('save')
		# Char-by-char output synchronisation is off by default: it made
		# prog_out += char an O(n^2) job and re-rendered the view once per
		# byte. Interactive programs can opt back in via the setting.
		if sync_out is None:
			sync_out = bool(get_settings().get('sync_output', False))

		# Reuse the limits received from Competitive Companion for this
		# problem so a later manual run does not lose them.
		if time_limit_ms is None and memory_limit_mb is None:
			try:
				entry = get_problem_limits(v.file_name())
				time_limit_ms = entry.get('time_limit_ms')
				memory_limit_mb = entry.get('memory_limit_mb')
			except Exception:
				pass

		file_name = v.file_name()
		if not file_name:
			sublime.status_message(t('save_file_first'))
			return
		scope_name = v.scope_name(v.sel()[0].begin()).rstrip()
		file_syntax = scope_name.split()[0]
		file_ext = path.splitext(file_name)[1][1:]
		if file_ext and not is_run_supported_ext(file_ext):
			sublime.status_message(t('unsupported_language', ext=file_ext))
			return

		window = v.window()

		if self.have_tied_dbg:
			prop = (window.get_view_index(self.tied_dbg))
			if prop == (-1, -1):
				need_new = True
			else:
				need_new = False
		else:
			need_new = True

		if not need_new:
			dbg_view = self.tied_dbg
			create_new = False
		else:
			dbg_view = window.new_file()
			self.tied_dbg = dbg_view
			self.have_tied_dbg = True
			create_new = True
			if get_settings().get('close_sidebar'):
				try:
					sublime.set_timeout_async(lambda window=window: window.set_sidebar_visible(False), 50)
				except Exception:
					pass
			dbg_view.run_command('toggle_setting', {'setting': 'word_wrap'})

		if len(window.get_layout()['cols']) != 3 or window.get_layout()['cols'][1] >= 0.89:
			window.set_layout({
				'cols': [0, self.ruler_opd_panel, 1],
				'rows': [0, 1],
				'cells': [[0, 0, 1, 1], [1, 0, 2, 1]]
			})

		window.set_view_index(dbg_view, 1, 0)
		window.focus_view(v)
		window.focus_view(dbg_view)

		dbg_view.set_syntax_file('Packages/%s/TestSyntax.sublime-syntax' % package_name)
		dbg_view.set_name(os.path.split(v.file_name())[-1] + ' -run')
		dbg_view.run_command('set_setting', {'setting': 'fold_buttons', 'value': False})
		dbg_view.run_command('cph_test_manager', {
			'action': 'make_opd',
			'build_sys': file_syntax,
			'run_file': v.file_name(),
			'clr_tests': clr_tests,
			'sync_out': sync_out,
			'code_view_id': v.id(),
			'time_limit_ms': time_limit_ms,
			'memory_limit_mb': memory_limit_mb,
			'run_all': run_all,
			'run_failed': run_failed,
			'force_compile': force_compile,
			'parallel': parallel,
		})

	def is_enabled(self, action=None, **kwargs):
		"""Run only makes sense on a saved file with a configured language.

		Only 'make_opd' is gated: the panel commands (sync_opdebugs) are
		invoked from the -run scratch view, which has no file name.
		"""
		if action != 'make_opd':
			return True
		file_name = self.view.file_name()
		if not file_name:
			return False
		ext = path.splitext(file_name)[1][1:]
		return bool(ext) and is_run_supported_ext(ext)

	def is_visible(self, action=None, event=None, **kwargs):
		"""Context menu entry: only offered where Run can do something.

		A .sublime-menu item has no `context` key in Sublime Text; the command
		decides instead (Default's own `open_context_url` does the same).
		"""
		return context_menu_visible(self.view, event)

	def close_opds(self):
		"""Close the run view paired with THIS source file only.

		The old version closed every '-run' view in the window, so opening
		problem B destroyed problem A's panel together with its session
		state (multi-problem workflow). It also crashed on views whose
		name() is None.
		"""
		w = self.view.window()
		if w is None:
			return
		file_name = self.view.file_name()
		if not file_name:
			return
		target = os.path.split(file_name)[-1] + ' -run'
		for v in w.views():
			if (v.name() or '') == target:
				try:
					v.close()
				except Exception:
					pass

	def run(self, edit, action=None, clr_tests=False, text=None, sync_out=None, \
			time_limit_ms=None, memory_limit_mb=None, \
			run_all=False, run_failed=False, force_compile=False, parallel=False):
		v = self.view
		# Per-view state: these used to be class attributes, so two windows
		# with a run panel shared (and clobbered) each other's view handles.
		if not hasattr(self, 'ruler_opd_panel'):
			self.ruler_opd_panel = 0.68
			self.have_tied_dbg = False
			self.tied_dbg = None

		if action == 'insert':
			v.insert(edit, v.sel()[0].begin(), text)
		elif action == 'make_opd':
			if v.settings().get('syntax') == 'Packages/%s/TestSyntax.sublime-syntax' % package_name:
				v.run_command('cph_test_manager', {
					'action': 'make_opd',
					'load_session': True,
					'run_all': run_all,
					'run_failed': run_failed,
					'force_compile': force_compile,
					'parallel': parallel,
				})
			else:
				self.close_opds()
				self.create_opd(clr_tests=clr_tests, sync_out=sync_out,
								time_limit_ms=time_limit_ms,
								memory_limit_mb=memory_limit_mb,
								run_all=run_all, run_failed=run_failed,
								force_compile=force_compile,
								parallel=parallel)
		elif action == 'sync_opdebugs':
			w = v.window()
			layout = w.get_layout()

			if len(layout['cols']) == 3:
				if layout['cols'][1] != 1:
					self.ruler_opd_panel = min(layout['cols'][1], 0.93)
					layout['cols'][1] = 1
					w.set_layout(layout)
				else:
					layout['cols'][1] = self.ruler_opd_panel
					w.set_layout(layout)


class LayoutListener(sublime_plugin.EventListener):
	def __init__(self):
		super(LayoutListener, self).__init__()


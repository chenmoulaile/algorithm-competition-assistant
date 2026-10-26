"""
Algorithm Competition Assistant - 主测试管理器
"""

import sublime, sublime_plugin
import os
import re
from sublime import Region, Phantom, PhantomSet
from os import path
from time import monotonic, sleep, time
import threading

from .Modules.ProcessManager import ProcessManager
from .Modules.build_artifact import reset_announced as reset_build_announcements
from .core.cph_settings import package_name, get_settings, get_tests_paths, load_all_tests, save_tests, save_tests_async, is_run_supported_ext, get_problem_limits, notify_io_error, get_problem_io, record_runtime, last_runtime, runtime_runs
from .core.cph_target import visible as context_menu_visible
from .core.cph_jump import (diagnostics as parse_diagnostics,
							esc, links_html, one_link, set_targets,
							handle_event as handle_jump_event)
from .core.cph_build_mode import label as build_mode_label
from .core.cph_std_ladder import reset as std_ladder_reset
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
from .core.cph_i18n import t, t_message


# ------------------------------------------------------ output pump plumbing
#: Bytes taken from the program's stdout per read. The pump must ask for a
#: bounded chunk: read() without a size blocks until EOF and hands back the
#: entire output as one string, which is how a silent-then-flood program got
#: past max_output_bytes and dumped megabytes into a view and the test store.
_OUT_CHUNK = 65536


def clamp_output(current, chunk, limit):
	"""Return the part of chunk that still fits, so current stays <= limit.

	limit <= 0 means unlimited. current is the text already collected (or its
	length, when the caller keeps the text as an unbuilt list of parts), so
	the rule is applied before the append instead of only after it.
	"""
	if limit <= 0:
		return chunk
	size = current if isinstance(current, int) else len(current)
	room = limit - size
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


def should_skip_compile(process_manager, force=False, fingerprint=None):
	if force or not get_settings().get('compile_cache_enabled', True):
		return False
	entry = _compile_cache.get(process_manager.file)
	if not entry:
		return False
	if fingerprint is None:
		fingerprint = _source_fingerprint(process_manager)
	return entry.get('fingerprint') == fingerprint


def remember_compile(process_manager, fingerprint=None):
	if not get_settings().get('compile_cache_enabled', True):
		return
	if fingerprint is None:
		fingerprint = _source_fingerprint(process_manager)
	_compile_cache[process_manager.file] = {
		'fingerprint': fingerprint,
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
		# Which channel the answer comes from belongs next to the limits: a
		# problem whose statement says `add.in / add.out` and a panel judging
		# stdin output are two different runs, and the WA that follows looks
		# exactly like an algorithm bug.
		io_spec = get_problem_io(run_file)
		if (io_spec or {}).get('mode') == 'files':
			parts.append('%s %s->%s' % (t('opt_io_files'), io_spec.get('input'),
										io_spec.get('output')))
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


#: How much of the program's own output `_squash_ws` is allowed to remember.
#: The cache holds the strings it was given, so it needs a byte ceiling: the
#: default max_output_bytes is megabytes, and a run with many samples would
#: otherwise trade CPU for memory without limit.
_SQUASH_MEMO_MAX_BYTES = 4 * 1024 * 1024
_squash_memo = {}
_squash_memo_bytes = [0]


def _squash_ws(s):
	"""Drop every whitespace character, like the judge's answer comparison.

	`get_verdict_by_code` compares outputs with all whitespace removed, so
	the "is this answer already accepted?" check must not be stricter than
	the verdict: byte comparison made an AC sample keep offering 'accept'
	and refuse to collapse.

	Memoised because a panel refresh asks every card the same question again,
	so a run of N samples squashed the whole output of every earlier card N
	times over - the quadratic term of the chain. CPython caches a string's
	hash per object, which is what makes the repeat lookup free; the byte cap
	keeps it from growing with the program's output.
	"""
	if not s:
		return ''
	cached = _squash_memo.get(s)
	if cached is not None:
		return cached
	result = ''.join(s.split())
	if _squash_memo_bytes[0] + len(s) > _SQUASH_MEMO_MAX_BYTES:
		_squash_memo.clear()
		_squash_memo_bytes[0] = 0
	_squash_memo[s] = result
	_squash_memo_bytes[0] += len(s)
	return result


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


def _card_row_segments(with_memory=False):
	"""Worst-case card row: 2-digit id, 6-digit runtime, optional memory."""
	segments = (
		' ' + t('test_label') + ' 88 ',
		' ' + t('edit') + ' ',
		' ' + t('run') + ' ',
		' ' + t('detail') + ' ',
		' 9999ms ',
	)
	if with_memory:
		segments += (' 888.88 MB ',)
	return segments


def _label_prefixed(value, key):
	""""time: 2542ms" when the labels setting is on, the bare value otherwise.

	&nbsp; between the label and the value keeps minihtml from wrapping
	between them on a narrow panel (the reason the labels went away in the
	first place - the wrapped value landed on the row below).
	"""
	if not get_settings().get('show_time_mem_labels', False):
		return value
	return t(key) + ':&nbsp;' + value


def _row_font_metrics(view):
	"""(em, full_width_char_px) for the view's phantom font.

	minihtml phantoms inherit the view font, so em_width is the base unit for
	ASCII. A CJK character is about font_px wide - wider than one em at the
	default 10 pt - and counting it as such is what stops a Chinese card row
	from being measured too narrow (which, now that the panel also NARROWS to
	its measurement, would wrap a chip).
	"""
	try:
		em = view.em_width()
	except Exception:
		em = 0
	if not em or em <= 0:
		return 0.0, 0.0
	try:
		scale = sublime.scale_factor()
	except Exception:
		scale = 1.0
	font_pt = view.settings().get('font_size')
	if not font_pt:
		font_pt = sublime.load_settings('Preferences.sublime-settings').get('font_size', 10)
	font_px = font_pt * 4.0 / 3.0
	return em, font_px * 1.05 * scale


def _widest_row_px(view, rows, badge=False):
	"""Widest one-line chip row in `rows`, in device px (0 when unmeasurable).

	`rows` holds tuples of segment strings, one tuple per row. `badge` adds
	the verdict badge the run panel's test cards carry; a row that has none
	(the compile chip, the 'next test' chip) must not be charged for it.
	"""
	em, wide_px = _row_font_metrics(view)
	if not em:
		return 0.0
	try:
		scale = sublime.scale_factor()
	except Exception:
		scale = 1.0

	widest = 0.0
	for segments in rows:
		ascii_n = 0
		wide_n = 0
		for seg in segments:
			a, w = _count_text_units(seg)
			ascii_n += a
			wide_n += w
		ascii_n += 6  # single space between the inline <a> buttons

		width = ascii_n * em + wide_n * wide_px
		if badge:
			# verdict badge: 10px monospace bold, up to 3 chars + padding + margin
			width += (3 * 10 * 0.65 + 3 + 2) * scale
		# 1px padding around each of the ~6 buttons + block spacing (CSS px)
		width += (6 * 2 + 8) * scale
		widest = max(widest, width * 1.08)
	return widest


def estimate_card_width_px(view, with_memory=False, rows=None):
	"""
	Rough estimate of the rendered width (device px) of the widest row of a
	test config card, so the run panel can be widened until cards fit on a
	single line. minihtml phantoms inherit the view font, so the view's
	em_width is the base unit; the monospace assumption overestimates a
	little for proportional fonts, which is the safe direction here.

	`rows` carries the ACTUAL card rows (tuples of segment strings) when the
	caller has real cards to measure; the panel is then widened just enough
	for what is on it instead of for the worst case the panel could ever
	show (a 6-digit runtime and an 8-char memory chip on every card made the
	panel twice as wide as most problems ever need). Without it the old
	worst-case row is used, which stays the safe fallback.
	"""
	# The value carries its unit (ms / MB), so the card shows no "time:" /
	# "mem:" label unless the user turned the labels back on.
	if rows is None:
		row = list(_card_row_segments(with_memory))
		if get_settings().get('show_time_mem_labels', False):
			# segments end with the runtime and, when measured, the memory chip
			if with_memory:
				row[-2] = ' ' + t('time') + ':&nbsp;9999ms '
				row[-1] = ' ' + t('memory') + ':&nbsp;888.88 MB '
			else:
				row[-1] = ' ' + t('time') + ':&nbsp;9999ms '
		rows = (tuple(row),)
	return _widest_row_px(view, rows, badge=True)


def estimate_panel_width_px(view, rows):
	"""Widest chip row the run panel itself has to lay out on one line.

	Same estimator as the cards, minus the verdict badge: this feeds a width
	that is applied in BOTH directions, so a row that does not carry a badge
	must not be charged for one (an inflated measurement would keep the panel
	from ever narrowing to what it actually shows).
	"""
	return _widest_row_px(view, rows, badge=False)


#: The run() branches that actually write the panel buffer (directly or via
#: a helper they call). Everything else - including the sync_read_only
#: dispatch every caret move sends - must NOT unlock the view: unlocking on
#: every dispatch meant two real view writes per cursor movement (the unlock
#: in run(), and the read-only restore at its end).
_WRITE_ACTIONS = frozenset((
	'insert_line', 'insert_cb', 'insert_opd_input', 'insert_opd_out',
	'replace', 'erase', 'erase_all', 'make_opd', 'new_test',
))

class CphTestManagerCommand(sublime_plugin.TextCommand):
	REGION_BEGIN_KEY = 'test_begin_%d'
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
		self.test_phantoms = [PhantomSet(view, 'test-phantoms-' + str(i)) for i in range(10)]
		# The compile chip used to be written into test_phantoms[0] - the very
		# slot the first test card occupies, since both anchor at Region(0).
		# Whichever was drawn last erased the other: "Compiling..." hid card 1,
		# and the next card refresh hid a compile failure nobody had seen yet.
		self.compile_phantom = PhantomSet(view, 'cph-compile-phantom')
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
				test_type = self.get_test_class()

				memory_display = 'none'
				memory_str = '-'
				if self.memory != '-' and self.memory is not None:
					memory_display = 'inline'
					memory_str = _label_prefixed(self.get_nice_memory(), 'memory')

				# stderr is deliberately NOT shown on the card any more: the
				# chip made every card with debug output too wide / too tall.
				# It stays in the detail view ('Error output' section).
				content = content.format(
					test_id=i + 1,
					runtime=_label_prefixed(self.get_nice_runtime(), 'time'),
					verdict_class=verdict_class,
					verdict_short=verdict_short,
					test_type=test_type,
					memory_display=memory_display,
					memory=memory_str,
					edit_label=t('edit'),
					run_label=t('run'),
					detail_label=t('detail'),
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
				type=type,
				type_label=type_label,
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
			# Unflushed output chunks per test, parallel to prog_out. The pump
			# thread appends here; only a fold (the 16 ms display flush, a
			# verdict delivery, a card rebuild) joins them into the str that
			# prog_out carries. `prog_out[n] += chunk` per chunk was O(n^2) in
			# the output size, and the display got one run_command dispatch
			# per chunk on top of it.
			self._prog_out_parts = []
			self._prog_out_total = []
			self._out_lock = threading.Lock()
			self._out_pending = []
			self._out_flush_scheduled = False
			# Keep prog_out exactly as long as tests. Session-restored tests
			# have no output yet, and get_tie_pos() / toggle_fold() index it
			# directly: a short list raised IndexError, which made "delete
			# test" (Ctrl+D and the test menu) fail silently for every test
			# this session had not run yet - typically an empty sample.
			while len(self.prog_out) < len(self.tests):
				self.prog_out.append('')
				self._prog_out_parts.append([])
				self._prog_out_total.append(0)
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
				self._clock_start = monotonic()

		def run_generation(self):
			"""The current run generation.

			Not used by the shipped code - it is how the regression suite can
			assert the guard that stops a killed thread from an older run from
			writing over the new one, without reaching into a private name.
			"""
			return self._active_gen

		def __on_stop(self, rtcode, runtime=-1, crash_line=None, gen=None):
			if gen is not None and gen != self._active_gen:
				# Thread of an older run (its process was replaced): its
				# result belongs to a test that no longer exists.
				return
			# The verdict is about to be made from prog_out: fold whatever the
			# pump still held in its parts list in first.
			self.fold_outputs(self.running_test)
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
				# The verdict redraws the panel from prog_out on the main
				# thread: anything the pump queued for display but the 16 ms
				# coalescer had not inserted yet must be on screen and inside
				# prog_out BEFORE the judgement is drawn above it, or the
				# late flush inserted it after the rewrite - at whatever
				# delta_input the chain had moved to - and the text strayed
				# into the wrong card (or the top of the panel).
				self.flush_pending_out()
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
			with self._out_lock:
				while n >= len(self._prog_out_parts):
					self._prog_out_parts.append([])
					self._prog_out_total.append(0)
				kept = clamp_output(self._prog_out_total[n], s, limit)
				if kept:
					self._prog_out_parts[n].append(kept)
					self._prog_out_total[n] += len(kept)
			if kept:
				self.post_out(kept)
			if len(kept) < len(s) and not self.output_truncated:
				# The cap is now reached. Keep draining the pipe so the program
				# is never blocked on a full pipe buffer, but say so instead of
				# silently showing a mysterious WA: a marker in the output plus
				# the detail view's message section.
				self.output_truncated = True
				# The marker inside the output stream follows the interface
				# language; the [Message] copy below follows message_language
				# (t_message) - they are different surfaces with different
				# settings.
				marker = '\n' + t('output_truncated_warn') + '\n'
				with self._out_lock:
					self._prog_out_parts[n].append(marker)
					self._prog_out_total[n] += len(marker)
				self.tests[n].message = t_message('output_truncated_warn')
				self.post_out(marker)
				print('[Algorithm Competition Assistant] %s' % t('output_truncated_warn'))

		def post_out(self, text):
			"""Hand output to the main thread for display, coalesced.

			One dispatch per chunk tied the panel redraw count to the
			program's write() count - 8 MB of sync output meant millions of
			main-thread hops. Chunks queue here and ONE 16 ms timer folds
			them into a single dispatch; a burst therefore costs at most
			~60 redraws a second no matter how fast the program prints.
			"""
			self._out_pending.append(text)
			if not self._out_flush_scheduled:
				self._out_flush_scheduled = True
				post_to_ui(self._flush_out)

		def _flush_out(self):
			"""Main thread: one display insert for everything queued."""
			self._out_flush_scheduled = False
			if self._out_pending:
				batch = ''.join(self._out_pending)
				del self._out_pending[:]
				self.on_out(batch, epoch=self.epoch)

		def fold_outputs(self, n=None):
			"""Join the unflushed pump chunks into prog_out (any thread).

			Threads that own a run (the pump) and the main thread both call
			this; the lock keeps an append racing a join from corrupting the
			parts list. Cheap when there is nothing to fold, so callers do
			not need to check first.
			"""
			with self._out_lock:
				indices = range(len(self._prog_out_parts)) if n is None else (n,)
				for i in indices:
					if i >= len(self._prog_out_parts) or i >= len(self.prog_out):
						continue
					parts = self._prog_out_parts[i]
					if not parts:
						continue
					joined = self.prog_out[i] + ''.join(parts)
					del parts[:]
					self._prog_out_total[i] = len(joined)
					self.prog_out[i] = joined

		def flush_pending_out(self):
			"""Main thread: display + fold everything still pending.

			Called before a verdict is delivered and before the cards are
			rebuilt: the output must be on screen and inside prog_out before
			the judgement drawn above it is made from it.
			"""
			self._out_flush_scheduled = False
			self._flush_out()
			self.fold_outputs()

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
			files_io = bool(getattr(proc, 'is_io_files', lambda: False)())
			try:
				while proc.is_stopped() is None:
					if gen != self._active_gen:
						return
					if self.sync_out:
						# 1024, not 1: read() returns as soon as ANY byte is
						# available, so a larger bfsize only cuts the number
						# of pump loops (per byte it also cost one decode and
						# one __on_out hop). Interactivity is unchanged.
						s = proc.read(bfsize=1024)
					else:
						s = proc.read(_OUT_CHUNK)
					# A file-IO run is answered in a file. Its stdout is debug
					# printing at best, and showing it as the program's answer
					# would judge the wrong thing - but the pipe is still drained
					# above, because a program whose stdout fills up blocks and
					# would then be reported as TLE.
					if not files_io:
						self.__on_out(s, gen)
				# Final drain. The process is gone so whatever is left is
				# already in the pipe, but a single bounded read would drop
				# everything past the first chunk: keep going until EOF.
				try:
					while True:
						s = proc.read(_OUT_CHUNK)
						if not s:
							break
						if not files_io:
							self.__on_out(s, gen)
				except Exception as e:
					# A failed final drain can leave the tail of the answer
					# unread, and a truncated output judged against a complete
					# one is a WA that does not exist. The truncation flag is the
					# honest thing to raise here - it already says "what you see
					# may not be all the program printed".
					print('[Algorithm Competition Assistant] final output drain '
					      'failed: %s' % e)
					if not self.output_truncated:
						self.output_truncated = True
						n = self.running_test
						self.tests[n].message = t_message('output_truncated_warn')
						self.post_out('\n' + t('output_truncated_warn') + '\n')
			except Exception as e:
				# Draining the output must never skip the memory-sampler stop
				# and on_stop below: an exception here used to leak a thread
				# polling a dead pid and leave the status stuck on RUNNING.
				print('[Algorithm Competition Assistant] output listener error: %s' % e)
			if files_io and gen == self._active_gen:
				# Read the answer back from the file the program wrote (or say
				# that it never created it - not the same thing at all).
				try:
					text, note = proc.collect_output()
				except Exception as e:
					print('[Algorithm Competition Assistant] file output not read: %s' % e)
					text, note = ('', '')
				if note:
					self.__on_out('\n' + note + '\n', gen)
				elif text:
					self.__on_out(text, gen)
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
				# monotonic(): _clock_start is monotonic (note_activity /
				# _spawn_moment); mixing time() in here handed the fallback
				# a wall-vs-monotonic difference (~1.8e9 s) as the runtime.
				runtime = int((monotonic() - start_time) * 1000)
			# Freeze the peak memory reading before the process disappears
			try:
				if type(proc) == ProcessManager:
					proc.finish_memory_sampling()
			except Exception as e:
				# The card then shows "-" for memory, which the user reads as
				# "this platform cannot measure it" rather than "the sampler
				# failed this run". Name it in the console at least: an MLE that
				# was never measured must not look like a pass.
				print('[Algorithm Competition Assistant] memory sampling failed: %s' % e)
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
				# monotonic(): _clock_start is a monotonic stamp (note_activity
				# / _spawn_moment). Reading it with time() made the watchdog
				# see a ~1.8e9 s wall-vs-monotonic difference on its very
				# first pass and kill every program as a TLE.
				if start_time is not None and monotonic() - start_time >= limit_s + _TLE_KILL_GRACE_S:
					self.tle_killed = True
					try:
						proc.terminate()
					except Exception as e:
						# The watchdog is the only thing standing between a dead
						# loop and a panel that hangs forever. If the kill itself
						# failed, the user must see that the program is still
						# running rather than waiting on a verdict that will
						# never arrive.
						print('[Algorithm Competition Assistant] TLE kill failed: %s' % e)
						sublime.status_message('[Algorithm Competition Assistant] '
											   + t('process_kill_failed'))
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
					self.on_insert(s, epoch=self.epoch)

		def _spawn_moment(self):
			"""When the current process came to life, as the manager saw it.

			The manager owns that timestamp because it is the one calling Popen;
			falling back to now() keeps a duck-typed process manager (the
			interactive and parallel stand-ins) working unchanged.
			"""
			stamp = getattr(self.process_manager, 'spawn_time', None)
			# monotonic(): must share ProcessManager.spawn_time's clock, the
			# runtime is (exit_time - this) and a mixed pair would wander
			# with wall-clock steps.
			return stamp if stamp is not None else monotonic()

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
			# NOI / CSP-S style problems are judged from files: the program
			# itself opens add.in and writes add.out, so the sample has to exist
			# as a file it can find and the answer has to be read back from the
			# file it produces. Everything else keeps running over a pipe, with
			# no change to what a stdin problem does. The source file comes from
			# the manager this tester drives, not from the panel: a duck-typed
			# tester (the interactive path, the suite's fakes) has no dbg_file.
			io_spec = get_problem_io(getattr(self.process_manager, 'file', None))
			files_io = (io_spec or {}).get('mode') == 'files'
			if files_io and type(self.process_manager) == ProcessManager:
				self.process_manager.set_io_spec(io_spec, input_text)
			elif type(self.process_manager) == ProcessManager:
				self.process_manager.set_io_spec({'mode': 'stdin'})
			self.process_manager.run()
			# The program's clock starts when the process came to life, never
			# before it: creating the child costs tens of milliseconds on
			# Windows and none of that is the program's doing. Feeding the
			# sample is included, because on a judge the program spends its
			# own time reading stdin.
			# A file-IO run has nothing to wait for: the sample was on disk
			# before the program started, so the clock starts now even for an
			# empty test point (the judge would have given it an empty file too).
			self._clock_start = None if (self.awaiting_input and not files_io) else self._spawn_moment()
			self.process_manager.write(input_text)
			# The stored sample is the whole input, so the program must see
			# EOF - exactly like on the judge. An EMPTY sample is the
			# placeholder waiting for the user to paste, so stdin stays open.
			if (input_text and input_text.strip()) or files_io:
				self.process_manager.close_stdin()
			if self._clock_start is None and input_text and input_text.strip():
				self._clock_start = self._spawn_moment()
			post_to_ui(lambda: self.on_insert(input_text, epoch=self.epoch))

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
				self._prog_out_parts.append([])
				self._prog_out_total.append(0)
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
				self._prog_out_parts.append([])
				self._prog_out_total.append(0)
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
					self._prog_out_parts[id] = []
					self._prog_out_total[id] = 0
					self.insert_test(id)
					if type(self.process_manager) == ProcessManager:
						self._start_pump()

				# Spawning the process and pushing the whole sample into its
				# stdin both block; on the main thread that was an editor
				# freeze for as long as the program took to read.
				sublime.set_timeout_async(start, 0)

			self.on_status_change('COMPILING')
			sublime.set_timeout_async(compile_worker, 10)

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
				self._prog_out_parts.append([])
				self._prog_out_total.append(0)

		def output_at(self, i):
			"""Output of test i; '' when it was never materialised.

			The test menu can act on any test - including session-restored
			ones this session never ran - so indexing prog_out directly used
			to raise IndexError inside a Sublime callback (silently swallowed).
			"""
			self.fold_outputs()
			if 0 <= i < len(self.prog_out):
				return self.prog_out[i]
			return ''

		def accept_out(self, nth):
			tests = self.tests
			if nth >= len(self.prog_out):
				return None
			out = self.output_at(nth).rstrip().lstrip()
			tests[nth].add_correct_answer(out)
			tests[nth].remove_uncorrect_answer(out)

		def decline_out(self, nth):
			tests = self.tests
			if nth >= len(self.prog_out):
				return None
			out = self.output_at(nth).rstrip().lstrip()
			tests[nth].remove_correct_answer(out)
			tests[nth].add_uncorrect_answer(out)

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
			value = tester.output_at(idx) if idx < len(tester.prog_out) else ''

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
				# The summary line is what a contestant reads mid-run, and the
				# failing test it names was plain text: the number you would
				# instinctively click did nothing, so the one piece of actionable
				# information on the panel had to be acted on by hand.
				parts.append('<a href="summary-detail">%s</a>'
							 % t('summary_first_fail', id=first_fail))
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
			# The phantom set had no navigation callback at all, which is why
			# the link above used to be decoration: a Phantom only receives
			# hrefs when one is given here.
			fail_index = (first_fail - 1) if first_fail is not None else None

			def on_summary(event, cb=self.on_test_action, i=fail_index):
				if event == 'summary-detail' and i is not None:
					cb(i, 'test-detail')

			self.summary_phantom.update([
				Phantom(Region(v.size(), v.size()), html, sublime.LAYOUT_BLOCK,
						on_summary if fail_index is not None else None)
			])
		except Exception as e:
			# The summary line going missing used to be silent, and it is the
			# one place that says "4/5 passed, first failure is test 3".
			print('[Algorithm Competition Assistant] summary bar failed: %s' % e)

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
		# The cap has to be handed to the manager itself: the parallel path reads
		# the pipe in core, which cannot reach the panel's clamp_output().
		try:
			output_limit = int(settings.get('max_output_bytes', 0) or 0)
		except (TypeError, ValueError):
			output_limit = 0
		file = self.dbg_file
		build_sys = getattr(self, 'build_sys', None)

		def make_manager():
			manager = ProcessManager(file, build_sys, run_settings=run_settings)
			# Parallel runs and the benchmark build their own managers, and a
			# file-IO problem has to be a file-IO problem on every path: the
			# serial panel, Ctrl+Alt+Shift+P and Ctrl+Alt+Shift+K must not
			# disagree about where the answer comes from for the same binary.
			manager.set_io_spec(get_problem_io(file))
			if separate_stderr:
				manager.set_separate_stderr(True)
			if output_limit > 0:
				manager.set_output_limit(output_limit)
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
			# The prefix is ours (and honours message_language); the rest is
			# whatever the checker itself printed and stays verbatim.
			message = '%s %s' % (t_message('checker_judged',
			                               verdict=result['verdict']), message)
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

		# Compile first, or the benchmark times whatever binary happens to be on
		# disk. This command exists to answer "did my optimisation actually help",
		# and an un-recompiled artifact answers it with a number that belongs to
		# the previous version of the code - after Ctrl+Alt+G (Debug/Release) or
		# Ctrl+Alt+Shift+R that is exactly the case the user is looking at.
		# Same cache rules and the same fingerprint as a normal Run, on a worker
		# thread, because a compile can take up to 30 s.
		def compile_then_run(self=self, manager=tester.process_manager, runs=runs,
		                     index=index, expected=expected, test=test,
		                     time_limit_ms=time_limit_ms, memory_limit_mb=memory_limit_mb):
			cmp_data = None
			try:
				fingerprint = None
				if get_settings().get('compile_cache_enabled', True):
					try:
						fingerprint = _source_fingerprint(manager)
					except Exception:
						fingerprint = None
				if should_skip_compile(manager, False, fingerprint):
					print('[Algorithm Competition Assistant] benchmark: compile skipped (source unchanged)')
				else:
					cmp_data = manager.compile()
					if cmp_data and cmp_data[0] == 0:
						remember_compile(manager, fingerprint)
			except Exception as e:
				cmp_data = (1, '[Algorithm Competition Assistant] compile failed: %s' % e)
			if cmp_data is not None and cmp_data[0] != 0:
				# A failed compile means the binary being timed is the previous
				# build (or nothing). Say so, and do not report times.
				outcome = (cmp_data[1] or '')[:400]
				sublime.set_timeout(lambda: self._benchmark_compile_failed(outcome), 0)
				return
			sublime.set_timeout(lambda: self._benchmark_batch(runs, index, expected, test,
			                                                  time_limit_ms, memory_limit_mb), 0)

		sublime.set_timeout_async(compile_then_run, 0)

	def _benchmark_compile_failed(self, outcome):
		sublime.status_message('[Algorithm Competition Assistant] ' + t('benchmark_ce'))
		self.set_compile_bar(outcome, type='error', chip_text=t('compilation_error'))
		print('[Algorithm Competition Assistant] benchmark aborted: compile failed')

	def _benchmark_batch(self, runs, index, expected, test, time_limit_ms, memory_limit_mb):
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
			# The bar/status line follow the interface language; the copy kept
			# on the test (shown in the detail's [Message] card) follows
			# message_language.
			self.tester.tests[index].message = t_message(
				'benchmark_done', n=len(times), best=best, avg=average, worst=worst)
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
						message = '%s %s' % (t_message('checker_judged',
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
		# Same history the serial path writes, or the parallel runner - which is
		# how most people run a full set - would leave "did my edit help?"
		# unanswerable.
		if (verdict or {}).get('name') != 'SK':
			record_runtime(self.dbg_file, i + 1, result.get('runtime') or 0)
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
			test.message = t_message('runtime_error_at', location=test.crash_line)
		if result.get('error'):
			test.message = result['error']
		elif result.get('message'):
			# e.g. the checker's verdict message
			test.message = result['message']
		if result.get('capped'):
			# The manager's own byte cap cut this answer short. Without a word
			# about it, a truncated answer reads exactly like a wrong one.
			notice = t_message('output_truncated_warn')
			test.message = (test.message + '\n' + notice) if test.message else notice
		while len(tester.prog_out) <= i:
			tester.prog_out.append('')
			tester._prog_out_parts.append([])
			tester._prog_out_total.append(0)
		tester._prog_out_parts[i] = []
		tester._prog_out_total[i] = len(result.get('stdout') or '')
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

	def build_detail_content(self, i, test, marks=None):
		"""The detail text for one test.

		`marks`, when given, is filled with (line_index, kind) pairs so the view
		can colour the diff lines with regions: the buffer itself stays plain
		text, so the detail remains selectable and copyable and a paste out of it
		is exactly what is shown. kind is 'want' (the expected line), 'got' (the
		program's line) or 'head' (a section title; recorded for bookkeeping but
		deliberately not coloured - a blue slab behind every title and every
		"vs last" fact read as noise, and the two colours that carry meaning
		here are the green/red diff pair).
		"""
		return self.build_detail(i, test, marks)[0]

	def build_detail(self, i, test, marks=None):
		"""(text, blocks) for the detail of one test.

        `text` and `marks` are exactly what build_detail_content documents
        (this method IS that builder). `blocks` is the card layout the detail
        view turns into phantom card heads: one record per foldable section,
        in document order, naming the lines the block owns in `text` (its
        bracket title line + body + trailing blank), a short card title, the
        "N lines / M bytes" meta chip and the raw payload its copy action
        copies. The facts block owns its fact lines only - it has no bracket
        title in the buffer, its card carries the title. The subtask line and
        the file-IO line stay static (always visible, no card): each is one
        line that never needs hiding.
		"""
		# The page opens with ONE empty line. It is the line the first card
		# hangs from (a LAYOUT_BLOCK phantom is drawn BELOW the line its
		# region points at - see _card_phantom), and it replaces the text
		# header that used to repeat "Test 2 | WA | 51ms" directly under the
		# badge bar. Every section title the body used to spell out
		# ("[Input]", "[Expected Output]" ...) is on its card instead: the
		# same words twice on one page read as a rendering bug.
		lines = ['']
		blocks = []

		def begin_block(bid, title, meta='', copy=None, kind=''):
			rec = {'id': bid, 'title': title, 'meta': meta, 'copy': copy,
			       'kind': kind, 'start': len(lines), 'count': 0}
			blocks.append(rec)
			return rec

		def add_body(text):
			# One entry per PHYSICAL line. A section body is a whole output,
			# newlines and all: appending it as one entry made `start` and
			# `count` count entries while the view, the fold and every mark
			# row count physical lines - so folding erased the wrong span and
			# the diff colours landed one line off on any real test.
			lines.extend((text or '').split('\n'))

		def end_block(rec):
			rec['count'] = len(lines) - rec['start']

		# How much of the limit this run used, and what changed since the last
		# run of the same test point. Both numbers already existed and were
		# thrown away: the panel had the limit it judged against, and the
		# history file had the previous runtime. "AC 但用了 96%" and "比上次慢
		# 3 倍" are the two things that decide what to do next.
		facts = []
		tester = getattr(self, 'tester', None)
		pm = getattr(tester, 'process_manager', None) if tester else None
		time_limit_ms = None
		memory_limit_mb = None
		if pm is not None:
			try:
				time_limit_ms = pm.get_time_limit_ms()
			except Exception:
				time_limit_ms = None
			try:
				memory_limit_mb = pm.get_memory_limit_mb()
			except Exception:
				memory_limit_mb = None
		# not every object that reaches this method is a real Test (the suite's
		# duck-typed detail object has no runtime at all), so read it softly
		_runtime = getattr(test, 'runtime', None)
		runtime_ms = _runtime if isinstance(_runtime, (int, float)) else None
		if time_limit_ms and runtime_ms:
			pct = int(round(100.0 * float(runtime_ms) / float(time_limit_ms)))
			facts.append('%s %dms/%dms = %d%%' % (t('detail_used_limit'),
				int(runtime_ms), int(time_limit_ms), pct))
		mb = getattr(test, 'memory_mb', None)
		if memory_limit_mb and mb:
			try:
				pct = int(round(100.0 * float(mb) / float(memory_limit_mb)))
				facts.append('%s %.0fMB/%dMB = %d%%' % (t('detail_used_memory'),
					float(mb), int(memory_limit_mb), pct))
			except (TypeError, ValueError):
				pass
		previous = last_runtime(getattr(self, 'dbg_file', None), i + 1)
		if previous and runtime_ms:
			try:
				ratio = float(runtime_ms) / float(previous) if previous else 0.0
				facts.append('%s %dms -> %dms (%.2fx)' % (t('detail_vs_last'),
					int(previous), int(runtime_ms), ratio))
			except (TypeError, ValueError, ZeroDivisionError):
				pass
		history = runtime_runs(getattr(self, 'dbg_file', None), i + 1)
		if len(history) >= 3:
			facts.append('%s %dms / %dms / %dms (n=%d)' % (
				t('detail_best_avg_worst'), min(history),
				int(sum(history) / len(history)), max(history), len(history)))
		if facts:
			rec = begin_block('facts', t('detail_facts'))
			for fact in facts:
				lines.append(fact)
				if marks is not None:
					marks.append((len(lines) - 1, 'head'))
			lines.append('')
			end_block(rec)

		# sample input, with its size: a pasted 20000-line sample and a two-
		# number sample are different problems to read, and the panel used to
		# show both as an unlabeled block.
		test_input = self._clean_output(test.test_string).rstrip('\n')
		in_bytes = len((test.test_string or '').encode('utf-8'))
		dbg_file = getattr(self, 'dbg_file', None)
		in_meta = '%d %s / %d %s' % (
			test_input.count('\n') + 1 if test_input else 0, t('detail_lines'),
			in_bytes, t('detail_bytes'))
		rec = begin_block('input', t('input'), in_meta, test.test_string or '')
		add_body(test_input if test_input.strip() else t('detail_empty'))
		lines.append('')
		end_block(rec)

		expected = ''
		if test.correct_answers:
			expected = next(iter(test.correct_answers))
		elif test.expected_output:
			expected = test.expected_output
		expected = self._clean_output(expected)
		stdout = self._clean_output(getattr(test, 'stdout', '') or '')

		exp_meta = '%d %s / %d %s' % (
			expected.count('\n') + 1 if expected else 0, t('detail_lines'),
			len((expected or '').encode('utf-8')), t('detail_bytes'))
		same = outputs_equal(expected, stdout)
		rec = begin_block('expected', t('expected_output'), exp_meta,
			self.detail_expected(test), kind='test-accept')
		add_body(expected.rstrip('\n') if expected.strip() else t('detail_empty'))
		lines.append('')
		end_block(rec)

		out_meta = '%d %s / %d %s' % (
			stdout.count('\n') + 1 if stdout else 0, t('detail_lines'),
			len((test.stdout or '').encode('utf-8')), t('detail_bytes'))
		rec = begin_block('actual', t('actual_output'), out_meta,
			getattr(test, 'stdout', '') or '',
			kind=('test-accept' if same else 'test-decline'))
		add_body(stdout.rstrip('\n') if stdout.strip() else t('detail_empty'))
		lines.append('')
		end_block(rec)

		# Which subtask group this test point belongs to and what that group
		# scored: with groups configured, a single WA is only half the story.
		groups = subtask_parse(get_settings().get('subtasks'))
		if groups:
			verdicts = []
			for one in (tester.tests if tester else []):
				vd = getattr(one, 'verdict', None)
				verdicts.append(vd.get('name') if isinstance(vd, dict) else None)
			scored = subtask_evaluate(verdicts, groups)
			n = i + 1
			for group in (scored or {}).get('groups') or []:
				if group['from'] <= n <= group['to']:
					lines.append('%s %s: %d/%d (%s %d%%)' % (
						t('detail_subtask'), group['name'], group['score'],
						group['max'], t('detail_group_passed') if group['passed']
						else t('detail_group_failed'),
						int(round(100.0 * group['score'] / group['max']))
						if group['max'] else 0))
					if marks is not None:
						marks.append((len(lines) - 1, 'head'))
					lines.append('')
					break

		rec = begin_block('diff', t('diff'), t('diff_ignore_trailing'),
			kind=('test-accept' if same else 'test-decline'))
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
				for _kind, line_no, e, a in shown:
					lines.append('  Line %d:' % line_no)
					if marks is not None:
						marks.append((len(lines) - 1, 'head'))
					# Both sides of the pair are ALWAYS emitted (a '-' op shows
					# the actual side as (empty), a '+' op the expected side):
					# want and got therefore always exist, sit on adjacent
					# lines and are always highlighted as a green/red pair -
					# a lone green line with nothing red beside it reads as
					# "the colours are broken", not "this side has no line".
					lines.append('- %s: %s' % (t('expected_short'),
						e if e else t('detail_empty')))
					if marks is not None:
						marks.append((len(lines) - 1, 'want'))
					lines.append('+ %s: %s' % (t('actual_short'),
						a if a else t('detail_empty')))
					if marks is not None:
						marks.append((len(lines) - 1, 'got'))
				if len(ops) > len(shown):
					lines.append('')
					lines.append(t('diff_shown_truncated', shown=len(shown),
						n=len(ops)))
		lines.append('')
		end_block(rec)

		if test.stderr and test.stderr.strip():
			rec = begin_block('stderr', t('error_output'), '',
				getattr(test, 'stderr', '') or '', kind='test-decline')
			add_body(self._clean_output(test.stderr).rstrip('\n'))
			lines.append('')
			end_block(rec)

		if test.message:
			rec = begin_block('message', t('message'), '',
				getattr(test, 'message', '') or '')
			add_body(test.message)
			lines.append('')
			end_block(rec)

		# A file-IO problem is judged from a file, and the most common reason a
		# detail like this one shows nothing is a freopen name that disagrees
		# with the statement. Name the channel every time.
		io_spec = get_problem_io(dbg_file)
		if (io_spec or {}).get('mode') == 'files':
			lines.append('[%s] %s -> %s' % (t('opt_io_files'),
				io_spec.get('input'), io_spec.get('output')))
			if marks is not None:
				marks.append((len(lines) - 1, 'head'))

		return '\n'.join(lines), blocks

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
			# Wrapping ON, for the card, not for the text: minihtml lays a
			# block phantom out at the viewport width only while wrapping is
			# enabled. With it off, every card shrank to the width of its own
			# chips - a small cluster floating in the middle of the page
			# instead of the full-width bar the run panel draws, which is the
			# one thing that made the detail page look unlike the run panel.
			# Long output lines now wrap; the run panel already does the same.
			detail_view.run_command('set_setting', {'setting': 'word_wrap', 'value': True})
			detail_view.run_command('set_setting', {'setting': 'fold_buttons', 'value': False})
		# The run panel is sized for a row of chips; a detail page is not. It
		# shows the sample, the answer and the diff, and at the panel's card
		# width (~40 characters) every line of it is a scrollbar. Give the
		# column the widest share the run panel is ever allowed to take -
		# max_panel_width_ratio, half the window by default - before showing
		# the page. The fit narrows it back once the run panel is the tab in
		# front again, which is the one place a narrow column is right.
		self.widen_column_for_detail(window)
		# Deliberately NO custom syntax here: the detail stays plain text
		# (default syntax). Per user feedback the syntax-based coloring was
		# removed; the content format/layout is unchanged.
		#
		# `detail_rich` (default on) adds decoration that never touches the
		# buffer - regions for the diff lines, a phantom badge bar with two
		# copy buttons, and one foldable CARD per section ([Input] /
		# [Expected Output] / [Actual Output] / [Diff] / [Message] ...) styled
		# like the run panel's cards - so the copy/select behaviour the plain
		# view was chosen for is exactly preserved, and setting it to false
		# restores today's view with no colour, no bar and no cards.
		detail_view.run_command('cph_test_detail_view',
		                        self.detail_view_args(i, test))
		window.focus_view(detail_view)

	def widen_column_for_detail(self, window):
		"""Give the detail page a readable column before it is shown.

		Only ever widens, and only the standard two-group layout: the run
		panel's column is sized for one row of chips, which is the wrong
		width for a page of sample input, answer and diff. The cap is the
		same `max_panel_width_ratio` the run panel obeys, so the detail page
		never takes more of the window than the panel is allowed to.
		"""
		try:
			layout = window.get_layout()
		except Exception:
			return
		cols = layout.get('cols') or []
		cells = layout.get('cells')
		if len(cols) != 3 or not cells:
			return
		panel_frac = 1.0 - cols[1]
		try:
			want = float(get_settings().get('max_panel_width_ratio', 0.5))
		except (TypeError, ValueError):
			want = 0.5
		want = min(max(want, 0.2), 0.9)
		if panel_frac >= want - 1e-3:
			return
		try:
			window.set_layout({
				'cols': [cols[0], 1.0 - want, cols[2]],
				'rows': layout.get('rows', [0, 1]),
				'cells': cells,
			})
		except Exception:
			pass

	def detail_view_args(self, i, test):
		"""Fill arguments for 'cph_test_detail_view', per detail_rich.

		Shared by the manual open and the after-verdict refresh, so a refresh
		keeps the full visual layer (it used to drop the marks, so the
		automatically refreshed detail came back uncoloured).
		"""
		rich = bool(get_settings().get('detail_rich', True))
		marks = [] if rich else None
		text, blocks = self.build_detail(i, test, marks)
		args = {'text': text, 'jump': self.jump_for(test),
		        'copy_actual': getattr(test, 'stdout', '') or '',
		        'copy_expected': self.detail_expected(test)}
		if rich:
			args['marks'] = marks
			args['header'] = self.detail_header_html(i, test)
			args['blocks'] = blocks
		return args

	def detail_expected(self, test):
		"""The raw expected answer text, for the detail's copy button."""
		if test.correct_answers:
			return next(iter(test.correct_answers))
		return test.expected_output or ''

	def detail_header_html(self, i, test):
		"""One bar above the detail text: badge, test, runtime, memory, actions.

		Built from the same classes the cards use, so it follows the panel
		theme. It is a phantom, not text: selecting or copying the detail still
		gives exactly the plain body, which is the property this view exists for.

		Like the cards, the values carry no "time:" / "mem:" label - a bar with
		labels ran off the edge of a half-width pane and the copy buttons were
		clipped away. The badge takes the verdict's own class: per-verdict rules
		(.verdict-TLE etc.) exist for the full set, while the hand-picked map
		here did not (it even looked up 'TL' for TLE, so every TLE detail
		showed a bare, unbackgrounded verdict).
		"""
		name = test.verdict['name'] if test.verdict else 'UKE'
		# a Unicode NBSP survives esc() and still stops minihtml from wrapping
		# between the number and its unit (get_nice_memory's own comment)
		runtime_str = esc(_label_prefixed(
			test.get_nice_runtime().replace('&nbsp;', '\u00a0').strip(), 'time'))
		# minihtml does not implement margin-left: a bar built out of margins
		# rendered as "WA test 251ms", with the runtime glued to the test
		# number. The gaps are explicit non-breaking spaces now.
		gap = '&nbsp;&nbsp;'
		parts = ['<span class="verdict-badge verdict-%s">%s</span>' % (esc(name), esc(name)),
		         '<span>%s %d</span>' % (esc(t('test_label')), i + 1),
		         '<span class="runtime-info">%s</span>' % runtime_str]
		if test.memory not in ('-', None):
			parts.append('<span class="memory-info">%s</span>'
			             % esc(_label_prefixed(
			                 test.get_nice_memory().replace('&nbsp;', '\u00a0').strip(),
			                 'memory')))
		parts.append('<a class="config" href="detail-copy-expected">%s</a>'
		             % esc(t('detail_copy_expected')))
		parts.append('<a class="config" href="detail-copy-actual">%s</a>'
		             % esc(t('detail_copy_actual')))
		parts.append('<a class="config" href="detail-close" style="color: var(--redish);">&#10005;</a>')
		return '<div class="panel">' + gap.join(parts) + '</div>'

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
		# The answer set changed for card i only - rebuild that card, not
		# every card on the panel (an N-sample run used to redraw N cards
		# per click at the end of the chain).
		self.update_configs(only_ids=(i,))
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
			out = tester.output_at(id).rstrip()
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

	def update_configs(self, update_last=None, only_ids=None):
		"""Rebuild the test cards.

		`update_last` keeps its old meaning (rebuild only the newest card of a
		chain step). `only_ids` widens that to any set of test indices: the
		walk still visits every card, because their text lengths decide where
		every later phantom sits, but only the listed cards are redrawn.
		"""
		v = self.view
		tester = self.tester
		if tester is None:
			return
		# Anything the pump queued for display must be on screen and folded
		# into prog_out before the walk below measures text lengths and pins
		# phantoms to offsets: a chunk flushed after the rebuild inserted at
		# the moved delta_input and landed inside the NEXT card's region.
		tester.flush_pending_out()
		_float_tolerance = get_settings().get('float_tolerance', 0) or 0
		configs = []
		if tester.proc_run:
			k = tester.test_iter + 1
		else:
			k = tester.test_iter
		k = min(k, len(tester.tests))
		pt = 0
		_last_test_entry = -1
		# A chain step pushes one card, but the walk still has to visit the
		# earlier ones: their text lengths decide where the last card sits and
		# what region its phantom covers. What it does not have to do is build
		# their markup and re-decide accept/decline for every stored output -
		# that made a run of N samples rebuild all N cards once per sample.
		only = set(only_ids) if only_ids else ((k - 1,) if update_last else None)
		touched = set()
		for i in range(k):
			build = only is None or i in only
			running = tester.proc_run and i == tester.running_test

			config = tester.tests[i].get_config(
				i,
				pt,
				self.on_test_action,
				tester.output_at(i),
				self.view,
				running=running
			) if build else None
			_last_test_entry = len(configs)
			if build:
				touched.add(_last_test_entry)
			configs.append(config)

			out_len = len(tester.output_at(i))
			if running:
				pt += len(tester.tests[i].test_string) + out_len + 2
			elif not tester.tests[i].fold:
				pt += len(tester.tests[i].test_string) + out_len + 1

			# getattr for safety, matching the other three call sites
			out_text = tester.output_at(i)
			if (not running and not tester.tests[i].fold
					and str(getattr(tester.tests[i], 'rtcode', '0')) == '0'
					and out_text):
				if build:
					if tester.tests[i].is_correct_answer(out_text, _float_tolerance):
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
				else:
					# None keeps the phantom indices of every later card exact.
					accdec = None
				if build and accdec is not None:
					touched.add(len(configs))
				configs.append(accdec)

			if not tester.tests[i].fold:
				pt += 2

		if not tester.proc_run and only is None:
			configs.append(self.get_next_title())

		while len(self.test_phantoms) < len(configs):
			self.test_phantoms.append(PhantomSet(v, 'test-phantom-' + str(len(self.test_phantoms))))

		hide_phantoms = v.settings().get('hide_phantoms')
		if only is None:
			for i in range(len(configs)):
				self.test_phantoms[i].update([configs[i]] if not hide_phantoms else [])

			for i in range(len(configs), len(self.test_phantoms)):
				self.test_phantoms[i].update([])
		else:
			for i in sorted(touched):
				if i < len(self.test_phantoms):
					self.test_phantoms[i].update(
						[configs[i]] if not hide_phantoms else [])

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

	#: The narrowest the run panel may be squeezed to, as a fraction of the
	#: window layout. A panel that fits one short card is still ~20 em wide;
	#: this only stops a degenerate measurement from collapsing the column.
	_MIN_PANEL_FRAC = 0.08

	def _panel_chip_rows(self):
		"""Every chip row the run panel has to lay out on ONE line.

		The compile chip, the test cards and the 'next test' chip cannot wrap
		without breaking a chip in half, so they decide the panel width. The
		compiler diagnostics, the summary bar and the test input/output text
		are flowing text: they wrap between words, so counting them would keep
		the panel as wide as the longest line of a 20 KB sample - the very
		empty strip this measurement exists to remove.
		"""
		rows = []
		chip = getattr(self, '_compile_chip_text', '') or ''
		if chip:
			rows.append((' ' + chip + ' ',))
		# The full card row is measured even while a test is running (when the
		# card is temporarily the much narrower 'test N / stop' row): the
		# steady state is the one that must fit, or the panel would jump every
		# time a run starts and ends.
		tester = getattr(self, 'tester', None)
		if tester is not None and getattr(tester, 'tests', None):
			for i, card in enumerate(tester.tests):
				segs = [' ' + t('test_label') + ' ' + str(i + 1) + ' ',
						' ' + t('edit') + ' ',
						' ' + t('run') + ' ',
						' ' + t('detail') + ' ']
				runtime = card.get_nice_runtime().replace('&nbsp;', ' ').strip()
				if runtime == '-':
					runtime = '0ms'
				segs.append(' ' + _label_prefixed(runtime, 'time') + ' ')
				if card.memory not in ('-', None):
					memory = card.get_nice_memory().replace('&nbsp;', ' ')
					segs.append(' ' + _label_prefixed(memory, 'memory') + ' ')
				rows.append(tuple(segs))
		# Always present when the panel is not mid-run, and the only row an
		# empty panel has: without it a fresh -run view measured 0 and the
		# column would be squeezed to the floor.
		rows.append((' ' + t('next_test') + ' ',))
		return rows

	def auto_fit_panel_width(self):
		"""
		Fit the run panel group to the cards it currently shows: widen it when
		a chip row would wrap, and NARROW it when the panel is wider than the
		widest row needs.

		It used to widen only ('never shrink'). A panel that one long compile
		command or one wide card had pushed open therefore stayed open for the
		rest of the session, leaving a slab of panel background to the right of
		every short card row - the empty strip in the screenshot that prompted
		this. The panel is a column of the window layout, so the only way to
		remove that strip is to give the column back.

		A dead band keeps the two directions from fighting each other: widen
		when the viewport is narrower than needed + 4 em, narrow when it is
		wider than needed + 8 em, do nothing in between. Both targets land
		INSIDE the band (fit, or fit + 2 em), so one measurement settles and the
		next one has nothing to do - no relayout loop, no visible flicker.
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
		try:
			viewport_px = v.viewport_extent()[0]
		except Exception:
			return
		if viewport_px <= 0:
			return

		needed = estimate_panel_width_px(v, self._panel_chip_rows())
		if needed <= 0:
			return
		try:
			em = v.em_width()
		except Exception:
			em = 0
		if not em or em <= 0:
			return

		# `needed` is text width only; the gutter and the phantom's own block
		# margins are not in it, so the width the content actually wants is
		# needed + 4 em (the same allowance the widen-only version used).
		fit_px = needed + 4 * em
		if fit_px <= viewport_px <= fit_px + 4 * em:
			return  # inside the dead band: no change worth a relayout
		want_px = fit_px if viewport_px < fit_px else fit_px + 2 * em

		# The detail page shares this column. While it is the tab in front,
		# the column is not narrowed to the run panel's card width: the page
		# is being read, and a relayout under the reader is worse than a
		# column that is wider than the cards need. Widening stays allowed.
		if want_px < viewport_px:
			try:
				active = w.active_view()
				group = w.active_group()
			except Exception:
				active, group = None, None
			if active is not None and active is not v and group == 1:
				return

		# The window's own width, derived from the panel's current share: the
		# layout fractions are relative to it, and viewport_extent() only
		# knows the panel's text area.
		layout_px = viewport_px / max(panel_frac, 0.05)
		target_frac = min(max(want_px / layout_px, self._MIN_PANEL_FRAC), max_ratio)
		target_col1 = 1.0 - target_frac
		if abs(target_col1 - cols[1]) < 1e-3:
			return  # already there, or clamped at max_ratio
		# Sub-pixel rounding made the target wobble by a fraction of a
		# percent between refreshes; relayouting the whole window for that
		# is what produced the visible stutter. Only a real change counts.
		last = getattr(self, '_fit_last_cols1', None)
		if last is not None and abs(target_col1 - last) < 0.01:
			return
		try:
			w.set_layout({
				'cols': [cols[0], target_col1, cols[2]],
				'rows': layout.get('rows', [0, 1]),
				'cells': cells,
			})
		except Exception:
			# Not applied, so not remembered either: a remembered target that
			# never reached the layout would suppress every retry.
			return
		self._fit_last_cols1 = target_col1


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

	def memorize_tests(self, force=False):
		"""Persist the test cards, coalesced.

		Each finished sample used to serialise every card and rewrite every
		copy of the file, so N samples cost N writes of N cards - with a
		multi-megabyte output that is hundreds of megabytes of disk traffic in
		one run, on the thread that draws the cards. Mid-chain the write is
		deferred and folded into one; the end of a chain forces it.
		"""
		if not (self.tester and self.dbg_file):
			return
		self._tests_dirty = True
		if force:
			self._flush_tests()
			return
		if getattr(self, '_tests_flush_scheduled', False):
			return
		self._tests_flush_scheduled = True
		sublime.set_timeout(self._flush_tests, 250)

	def _flush_tests(self):
		self._tests_flush_scheduled = False
		if not getattr(self, '_tests_dirty', False):
			return
		if getattr(self, '_tests_busy', False):
			# A write is in flight on the worker thread. Mark dirty again so
			# _tests_saved() runs one more round when it lands, instead of
			# either dropping the fresh data or queueing a second thread.
			return
		self._tests_dirty = False
		tests_data = [x.memorize() for x in (self.tester.get_tests())]
		# Pretty printing (tab-indented, hand-editable) inflates a
		# multi-megabyte output a lot; past ~256 KB of test data the file is
		# not hand-edited in practice, so it is written compact.
		total = 0
		for entry in tests_data:
			if isinstance(entry, dict):
				for key in ('test', 'stdout', 'expected_output'):
					try:
						total += len(entry.get(key) or '')
					except Exception:
						pass
		pretty = total <= 256 * 1024
		dbg = self.dbg_file
		self._tests_busy = True
		# Serialization and the (up to four) file writes happen on the
		# save_tests_async worker; _tests_saved lands on the main thread.
		save_tests_async(dbg, tests_data, pretty=pretty,
						 on_done=self._tests_saved)

	def _tests_saved(self, ok):
		self._tests_busy = False
		if not ok:
			# Every other save path reports this (cph_companion,
			# cph_extras, cph_import, cph_stress). Silently dropping it
			# here meant "my test points are gone" with no explanation -
			# and this is the path that stores the answers marked by hand.
			# notify_io_error names the file and the OS reason, and repeats
			# itself only to the console: a read-only folder fails on every
			# single run, and a dialog on every run is a disabled plugin.
			notify_io_error(self.dbg_file)
		if getattr(self, '_tests_dirty', False):
			# Data changed while the write was in flight: one more round.
			self._flush_tests()

	def on_insert(self, s, epoch=None):
		# Same stale-listener guard as on_out/on_stop: a killed process can
		# still be fed its sample while the next run is being set up, and
		# that text would land at the *new* delta_input, above the first card.
		if epoch is not None and epoch != getattr(self, 'tester_epoch', None):
			print('[Algorithm Competition Assistant] dropped stale on_insert from killed process')
			return
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
		# The pump has folded its parts by now (tester __on_stop), but fold
		# again here: the panel also lands on this path for parallel results.
		self.tester.fold_outputs()
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
		# Remember the measurement per test point. set_cur_runtime() overwrites
		# the single previous number, so "did my optimisation help" was
		# unanswerable one run after the edit - the detail can only show a
		# before/after if the before was written down. Manual stops are not
		# measurements, so a SK is not recorded.
		if verdict['name'] != 'SK':
			record_runtime(self.dbg_file, test_id + 1, runtime)

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
				self.tester.tests[test_id].message = t_message('runtime_error_at',
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
				hints.append(t_message('tle_no_output'))
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
				hints.append(t_message('tle_debug_build'))
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
					(existing + '\n' if existing else '') + t_message('wa_release_hint')

		self.tester.tests[test_id].set_stderr(stderr)
		self.tester.tests[test_id].set_expected_output(expected_output)

		v.erase_regions('type')
		line = v.line(self.input_start)

		input_end = v.line(Region(self.delta_input)).end()

		if tester.running_new and self.tester.tests[test_id].is_correct_answer(self.tester.output_at(test_id), float_tolerance):
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
			# Whatever the chain deferred is on disk before anything else can
			# touch these cards.
			self.memorize_tests(force=True)
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
						pt += len(tester.tests[test_id].test_string) + len(tester.output_at(test_id)) + 1
						detail = tester.tests[test_id].get_detail(test_id, pt, self.on_test_action, self.view)
						if test_id < len(self.detail_phantoms):
							self.detail_phantoms[test_id].update([detail])
			else:
				name = self.get_detail_view_name(test_id)
				for wv in window.views():
					if wv.name() == name:
						try:
							wv.run_command('cph_test_detail_view',
							               self.detail_view_args(test_id,
							                                     tester.tests[test_id]))
						except Exception:
							break
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
		for phs in self.test_phantoms:
			phs.update([])
		# "clear tests" also drops the compile chip: it belongs to the run that
		# was just erased, and a stale red "编译错误" over an empty panel reads
		# as if the empty panel itself had failed to compile.
		self.compile_phantom.update([])
		# ... and its text, or the width measurement keeps reserving room for
		# a chip nobody can see any more.
		self._compile_chip_text = ''
		if self.tester:
			v.erase_regions('type')
			for i in range(-1, self.tester.test_iter + 1):
				v.erase_regions(self.REGION_BEGIN_KEY % i)
				v.erase_regions(self.REGION_END_KEY % i)

	def set_compile_bar(self, cmd, type='', chip_text=None):
		view = self.view
		styles = get_test_styles(view)
		# escape html specials so compiler output shows up correctly in minihtml
		# chip_text: short text for the panel chip. Compile failures keep the
		# full stderr OUT of the chip - the plain text below already carries
		# it (and it is copyable there), showing it twice just pushed the
		# test cards further down.
		display = cmd if chip_text is None else chip_text
		# What the chip actually says, kept for the panel width measurement:
		# the chip is one of the rows that must not wrap, and the content of
		# the last measurement is the only place that knows its text (a
		# Phantom's contents cannot be read back). A multi-line note renders
		# as several lines, so its longest one is what has to fit. An empty
		# chip clears it, so a finished run stops reserving width for a bar
		# that is gone.
		_chip_lines = (display or '').splitlines() or ['']
		self._compile_chip_text = max(_chip_lines, key=len).strip()
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
			# A failed compile used to be *colour only* (the chip turns red) and
			# the raw compiler text, which on a Chinese Windows machine arrives
			# as a wall of paths and line numbers. Name it.
			compilation_error_label=(
				'<span style="color: #d3140d; font-weight: bold;">%s</span> '
				% esc(t('compilation_error')) if type == 'error' else ''),
			diagnostics=diag_html,
		)
		content = '<style>' + styles + '</style>' + content

		def onclick(event):
			handle_jump_event(view, event)

		phantom = Phantom(Region(0), content, sublime.LAYOUT_BLOCK, onclick)
		self.compile_phantom.update([phantom])

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
			# The panel was closed between the click and this callback. It used
			# to be a console line only, so the button simply did nothing and
			# the user clicked it again, and again.
			sublime.status_message('[Algorithm Competition Assistant] ' + t('run_view_gone'))
			print('[Algorithm Competition Assistant] the run view has no window any more, ignoring')
			return

		# Re-entry guard: only block while a compile is genuinely in flight.
		# A stale 'COMPILING' status left behind by an older crashed compile
		# is auto-cleared after 30s so the user is never stuck forever.
		compiling_since = getattr(self, 'compiling_since', None)
		if compiling_since is not None and (time() - compiling_since) < 30:
			sublime.status_message('[Algorithm Competition Assistant] ' + t('compile_reentry_warn'))
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

		# Loading the samples reads up to four tests files and decodes all of
		# them; with a multi-megabyte stored output that is several MB of work
		# that used to run on the main thread before a single card was on
		# screen, so pressing Run on a big problem froze the editor. The panel
		# now comes up empty immediately and the load runs on a worker thread;
		# `start_runner` fires once BOTH the compile and the load have landed,
		# whichever is later. The epoch keeps the in-flight callbacks of a
		# previous press (a slow sample load, a late compile) from reaching
		# the panel this press is about to rebuild.
		self.tester_epoch = getattr(self, 'tester_epoch', 0) + 1
		loaded_holder = {'tests': None, 'previously_accepted': set()}
		my_epoch = getattr(self, 'tester_epoch', 0)

		def load_worker(rf=run_file):
			loaded = load_all_tests(rf)
			sublime.set_timeout(lambda: tests_loaded(loaded), 0)

		def tests_loaded(loaded_data):
			if self.view.window() is None:
				return
			if getattr(self, 'tester_epoch', 0) != my_epoch:
				return  # the panel was rebuilt while the load was in flight
			cards = [self.Test(x) for x in (loaded_data or [])
					 if isinstance(x, dict)]
			# A tests file that cannot be parsed yields []: the panel then shows
			# an empty problem, and the only symptom is "my samples disappeared"
			# - which reads exactly like the plugin forgetting to load them.
			notify_io_error(run_file, kind='read')
			if not load_session:
				# Pressing Run starts a clean evaluation. The verdicts that came
				# out of the tests file describe the *previous* run: leaving them
				# in place made the summary report 'N/N passed' for a run that had
				# just failed, and kept green badges on the tests this run never
				# reached. 'Run failed tests' still has to know what was AC, so
				# remember that before wiping.
				# NOTE: the loop variable must not be called 't' - that shadows the
				# i18n helper t() imported at module level.
				previously_accepted = set(
					index for index, card in enumerate(cards)
					if isinstance(getattr(card, 'verdict', None), dict)
					and card.verdict.get('name') == 'AC')
				for card in cards:
					card.reset_run_state()
				loaded_holder['previously_accepted'] = previously_accepted
			loaded_holder['tests'] = cards
			start_runner()

		clear_failures = []
		if not clr_tests:
			# Try to load tests from all possible locations (traditional + cph-ng style folder)
			sublime.set_timeout_async(load_worker, 0)
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
			tests_loaded([])

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

		def start_runner():
			"""Build the Tester and start the first run.

			Called once, after BOTH the compile and the (threaded) sample load
			have landed - whichever finishes last re-arms this from its own
			callback (after_compile / tests_loaded). While one half is still in
			flight the compile bar already shows the outcome; only the cards
			wait for both.
			"""
			if loaded_holder.get('started') or loaded_holder.get('failed'):
				return
			tests = loaded_holder.get('tests')
			if tests is None or not loaded_holder.get('compiled'):
				return
			loaded_holder['started'] = True
			# The insertion point for everything this run writes begins at the
			# top of the (just cleared) panel. This reset used to live in
			# after_compile, unconditionally - but the load is dispatched
			# first and a big tests file beats g++, so the first run could
			# already be under way (new_test had anchored delta_input at the
			# first card) when after_compile landed and zeroed it: the sample
			# text and the program output were then inserted ABOVE the first
			# card, and the verdict rewrite stranded a stray line at the very
			# top of the panel. Resetting here, in the same callback that
			# dispatches the first run, keeps the reset and the anchor atomic
			# again.
			self.delta_input = 0
			v.settings().set('edit_mode', False)
			self.tester_epoch = getattr(self, 'tester_epoch', 0) + 1
			self.tester = self.Tester(process_manager, \
				self.on_insert, self.on_out, self.on_stop, self.change_process_status, \
				tests=tests, sync_out=sync_out, epoch=self.tester_epoch,
				run_failed=run_failed)
			self.tester.run_all = run_all
			# Which tests were AC *before* this run started (see above).
			self.tester.previously_accepted = loaded_holder.get(
				'previously_accepted') or set()
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

		def after_compile(cmp_data, cached, fingerprint):
			"""Apply the compile result. Runs on the main thread, on purpose."""
			if getattr(self, 'tester_epoch', 0) != my_epoch:
				# A newer Run was pressed while this compile was in flight:
				# its after_compile would repaint the compile bar of the run
				# that replaced it (and, before the epoch check existed,
				# reset delta_input under the new run's feet).
				return
			self.compiling_since = None
			v = self.view
			self.change_process_status('COMPILED')
			if cmp_data is None or cmp_data[0] == 0:
				remember_compile(process_manager, fingerprint)
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
				# The compile half of the gate is down: start_runner fires the
				# first run only now that this AND the sample load are both in.
				loaded_holder['compiled'] = True
				start_runner()
			else:
				loaded_holder['failed'] = True
				# The compile error text is the first thing this panel shows:
				# anchor it at the top (the success path resets delta_input in
				# start_runner instead - see there for why this moved).
				self.delta_input = 0
				v.run_command('cph_test_manager', {'action': 'insert_opd_out', 'text': '\n' + cmp_data[1]})
				self.set_compile_bar(cmp_data[1], type='error',
									 chip_text=t('compilation_error'))

		def compile():
			# The fingerprint is taken once, before the compiler runs: the old
			# code walked the headers and globbed the extra sources a second
			# time on every Run just to remember what it had checked.
			fingerprint = None
			if force_compile:
				# 'force recompile' also forgets the -std this file had climbed
				# to: the key means start over, and it is the way back down when
				# the code no longer uses anything newer than the floor.
				std_ladder_reset(process_manager.file)
				# ...and the binary-name mismatches reported once per pair: a
				# fresh build deserves to be heard again (see build_artifact).
				reset_build_announcements()
			if get_settings().get('compile_cache_enabled', True):
				try:
					fingerprint = _source_fingerprint(process_manager)
				except Exception:
					fingerprint = None
			cached = False
			try:
				if should_skip_compile(process_manager, force_compile, fingerprint):
					cached = True
					cmp_data = (0, t('compile_cached'))
					print('[Algorithm Competition Assistant] compile skipped (source unchanged)')
				else:
					cmp_data = process_manager.compile()
					print('[Algorithm Competition Assistant] compile rc: %s' % (cmp_data[0] if cmp_data else None))
					if getattr(process_manager, 'std_escalated', False):
						# the command changed while we were compiling, so the binary
						# on disk is not the one the pre-compile fingerprint describes;
						# caching that would skip the next Run of a file that has never
						# been built at its own standard
						try:
							fingerprint = _source_fingerprint(process_manager)
						except Exception:
							fingerprint = None
			except Exception as e:
				print('[Algorithm Competition Assistant] compile exception: %s' % e)
				cmp_data = (1, '[Algorithm Competition Assistant] compile failed: %s' % e)
			# g++ was the right thing to run on Sublime's one shared async
			# worker; the panel is not. Drawing the compile chip, building the
			# Tester and starting the chain all touch views, and doing that
			# here queued every other plugin's set_timeout_async work behind a
			# compile - which is what made a run freeze the editor.
			sublime.set_timeout(lambda: after_compile(cmp_data, cached, fingerprint), 0)

		self.set_compile_bar(t('compiling'), type='compiling')
		# Mark compile start only now - the terminate/rerun path above
		# must never be blocked by this guard on its re-entry
		self.compiling_since = time()

		sublime.set_timeout_async(compile, 10)

	def delete_test(self, edit, id, commit=True):
		"""Remove one test. `commit=False` skips the persist + redraw so a
		batch delete can do both exactly once (delete_tests)."""
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
			del tester._prog_out_parts[id]
			del tester._prog_out_total[id]
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
		if commit:
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
		# One erase + one renumber + ONE redraw for the whole selection; the
		# old loop re-ran the full card rebuild after every single deletion.
		for test in reversed(to_del):
			self.delete_test(edit, test, commit=False)
		self.memorize_tests()
		self.update_configs()

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
			tester._prog_out_parts.append([])
			tester._prog_out_total.append(0)
		if dir == 1:
			selected.reverse()
		for sel in selected:
			if 0 <= sel + dir < len(tester.tests):
				tester.tests[sel], tester.tests[sel + dir] = tester.tests[sel + dir], tester.tests[sel]
				tester.prog_out[sel], tester.prog_out[sel + dir] = tester.prog_out[sel + dir], tester.prog_out[sel]
				parts = tester._prog_out_parts
				parts[sel], parts[sel + dir] = parts[sel + dir], parts[sel]
				totals = tester._prog_out_total
				totals[sel], totals[sel + dir] = totals[sel + dir], totals[sel]

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

		# Unlock only for a branch that really writes the buffer (see
		# _WRITE_ACTIONS); the trailing sync_read_only() restores read-only
		# after a write and no-ops otherwise.
		if action in _WRITE_ACTIONS and v.is_read_only():
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
	comparable and copyable.

	Each section ([Input] / [Expected Output] / [Actual Output] / [Diff] /
	[Message] / [Error Output] / the facts block) also gets a phantom CARD
	head, built from the same classes and stylesheet as the run panel's test
	cards: title, fold/unfold and - where the block has content - a copy
	action. The card is decoration; the body below it stays real text in the
	view. Folding ERASES the block's lines from the buffer and unfolding
	writes them back (the run panel's toggle_fold approach), so the buffer
	is always plain detail text - what selecting/copying gives is what is on
	screen, and no byte is ever edited to fake a highlight width."""

	def __init__(self, view):
		self.view = view
		self.phantoms = PhantomSet(view, 'cph-detail-jump')
		self._copy_payload = ('', '')
		# card layout (installed by a fill that carries `blocks`)
		self._blocks = []      # [{'id','title','meta','copy','text','len','start','count'}]
		self._order = []       # ('static', char_len) / ('block', index), document order
		self._marks_full = []  # diff marks in the EXPANDED layout
		self._folded = set()   # ids currently folded (kept across refreshes)
		self._header_html = None
		self._jump_spec = None

	def run(self, edit, text='', jump=None, marks=None, header=None,
	        copy_actual='', copy_expected='', blocks=None, action=None,
	        block=None):
		if action == 'fold':
			self._run_fold(edit, block)
			return
		v = self.view
		v.set_scratch(True)
		v.set_read_only(False)
		v.replace(edit, Region(0, v.size()), text)
		self._copy_payload = (copy_actual or '', copy_expected or '')
		self._header_html = header
		self._jump_spec = jump
		self._marks_full = list(marks or [])
		if blocks:
			self._install_blocks(text, blocks)
			# Re-apply the folds the user already made (a refresh after a
			# re-run must not expand everything under their hands).
			for one in self._blocks:
				if one['id'] in self._folded:
					v.erase(edit, Region(self._block_point(one['idx']),
					                     self._block_point(one['idx']) + one['len']))
		else:
			self._blocks = []
			self._order = []
			self._folded = set()
		v.set_read_only(True)
		v.sel().clear()
		v.sel().add(Region(0))
		self._refresh_decoration()

	def _run_fold(self, edit, block_id):
		"""Fold/unfold one block: erase its lines / write them back."""
		one = self._block_by_id(block_id)
		if one is None:
			return
		v = self.view
		start = self._block_point(one['idx'])
		v.set_read_only(False)
		if one['id'] in self._folded:
			self._folded.discard(one['id'])
			v.insert(edit, start, one['text'])
		else:
			self._folded.add(one['id'])
			v.erase(edit, Region(start, start + one['len']))
		v.set_read_only(True)
		self._refresh_decoration()

	def _install_blocks(self, text, blocks):
		"""Compute each block's character span in the expanded text.

		Everything fold-related is plain arithmetic on stored lengths, so
		folding never needs to read the buffer back (a real PhantomSet is not
		the only Sublime API that exposes less than the tests' stand-ins).
		"""
		lines = text.split('\n')
		offsets = []
		off = 0
		for one_line in lines:
			offsets.append(off)
			off += len(one_line) + 1
		offsets.append(off)
		total = len(text)
		self._blocks = []
		self._order = []
		kept = set()
		pos_line = 0
		for idx, rec in enumerate(blocks):
			start = max(0, int(rec.get('start', 0)))
			count = max(0, int(rec.get('count', 0)))
			if start > len(lines):
				start = len(lines)
			if start + count > len(lines):
				count = max(0, len(lines) - start)
			char_start = min(total, offsets[start])
			char_end = min(total, offsets[start + count])
			one = {'id': str(rec.get('id', idx)), 'idx': idx,
			       'title': rec.get('title', ''), 'meta': rec.get('meta', ''),
			       'copy': rec.get('copy'), 'text': text[char_start:char_end],
			       'len': char_end - char_start,
			       'start': start, 'count': count}
			if start > pos_line and pos_line < len(offsets):
				self._order.append(('static', offsets[start] - offsets[pos_line]))
			self._order.append(('block', idx))
			self._blocks.append(one)
			kept.add(one['id'])
			pos_line = start + count
		if pos_line < len(lines) and pos_line < len(offsets):
			self._order.append(('static', total - offsets[pos_line]))
		# ids that vanished (the re-run lost that section) cannot stay folded
		self._folded &= kept

	def _block_by_id(self, block_id):
		for one in self._blocks:
			if one['id'] == block_id:
				return one
		return None

	def _block_point(self, idx):
		"""Current buffer point where block `idx` starts (0 when folded)."""
		pos = 0
		for entry in self._order:
			if entry[0] == 'static':
				pos += entry[1]
			else:
				other = self._blocks[entry[1]]
				if entry[1] == idx:
					return pos
				if other['id'] not in self._folded:
					pos += other['len']
		return pos

	def _visible_marks(self):
		"""Marks re-expressed for the CURRENT layout.

		A mark inside a folded block is dropped (its text is not on screen);
		every other mark shifts up by the lines hidden above it.
		"""
		if not self._blocks:
			return list(self._marks_full), None
		hidden = {}
		out = []
		for row, kind in self._marks_full:
			drop = False
			above = 0
			for one in self._blocks:
				folded = one['id'] in self._folded
				if one['start'] + one['count'] <= row:
					if folded:
						above += one['count']
				elif one['start'] <= row < one['start'] + one['count'] and folded:
					drop = True
			if drop:
				continue
			hidden_key = int(row)
			hidden[hidden_key] = above
			out.append((row, kind))
		return out, hidden

	def _refresh_decoration(self):
		"""Repaint cards, header, jump chip and marks for the current fold
		state. One PhantomSet update: its contents cannot be read back."""
		v = self.view
		style = '<style>' + get_test_styles(v) + '</style>'
		out = []
		header = self._header_phantom(self._header_html)
		if header is not None:
			out.append(header)
		jump = self._jump_phantom(self._jump_spec)
		if jump is not None:
			out.append(jump)
		for one in self._blocks:
			out.append(self._card_phantom(one, style))
		self.phantoms.update(out)
		marks, hidden = self._visible_marks()
		self.set_marks(marks, hidden_lines=hidden)

	def _card_phantom(self, one, style):
		"""The card head of one block, in the run panel's card shape.

		The -run panel's card (Highlight/test_config.html) is one .panel row of
		inline chips: an identity chip (.test), the action chips (.config), then
		the measured value inside a .config chip. This card is the same row -
		identity, fold, copy, and the "N lines / M bytes" value in a .config
		chip - with the same colours the run card uses: the identity chip takes
		the section's verdict tint (expected green, actual/stderr/diff red when
		they disagree), fold is the blue "run" action and copy the purple
		"detail" one. No arrow: the fold state is spelled out by the chip.

		The anchor is the character BEFORE the block's first line, not its
		first character: Sublime draws a LAYOUT_BLOCK phantom BELOW the line
		its region points at, so anchoring on the first line put every card one
		line too low - and once the block was folded, the card drifted onto the
		previous section, which is the misalignment that was reported.
		"""
		v = self.view
		folded = one['id'] in self._folded
		template = read_resource('Highlight/test_detail_card.html')
		# The size chip keeps its spaces unbreakable, like the run card's
		# "4&nbsp;MB": a chip that wraps in half reads as two chips.
		meta = esc(one['meta'] or '').replace(' ', '&nbsp;')
		content = template.format(
			title=esc(one['title'] or one['id']),
			test_type=one.get('kind') or '',
			meta=meta,
			meta_display='inline' if one['meta'] else 'none',
			fold_label=(t('detail_unfold') if folded else t('detail_fold')),
			fold_href='detail-fold-%s' % one['id'],
			copy_label=t('detail_copy'),
			copy_display='inline' if one['copy'] is not None else 'none',
			copy_href='detail-copy-%s' % one['id'],
		)
		content = style + content

		def onclick(event, cmd=self):
			return cmd.on_card_event(event)

		return Phantom(Region(max(0, self._block_point(one['idx']) - 1)), content,
		               sublime.LAYOUT_BLOCK, onclick)

	def on_card_event(self, event):
		"""Card clicks: fold/unfold (a buffer edit, so it re-enters this
		command through run_command to obtain an edit object) and copy."""
		v = self.view
		if event.startswith('detail-fold-'):
			v.run_command('cph_test_detail_view', {
				'action': 'fold', 'block': event[len('detail-fold-'):]})
			return True
		if event.startswith('detail-copy-'):
			one = self._block_by_id(event[len('detail-copy-'):])
			payload = (one['copy'] or '') if one else ''
			sublime.set_clipboard(payload)
			sublime.status_message('[Algorithm Competition Assistant] ' + t(
				'detail_copied', n=len(payload)))
			return True
		return False

	_DETAIL_SCOPES = {
		# Diff colours come from regions, not from a syntax definition: the
		# buffer stays plain text (selectable, copyable, and what you paste out
		# is what you saw), which is the reason a custom syntax was removed
		# here once before. Regions are decoration only and touch no bytes.
		'want': ('cph-detail-want', 'region.greenish'),
		'got': ('cph-detail-got', 'region.redish'),
	}

	def set_marks(self, marks, hidden_lines=None):
		"""Paint the diff pairs. `marks` are (line_index, kind) in the FULL
		(expanded) layout; `hidden_lines` is the count of lines currently
		folded away ABOVE a given mark (see _visible_marks: the detail view's
		blocks can be folded, and a mark must follow its text).

		Each want/got region spans the WHOLE line - from column 0 to the end
		of the line, trailing whitespace included. Width is never bought by
		editing buffer bytes: the pair reads as one green/red unit no matter
		how short the text on a line is, and an empty side (a '-'/'+' op)
		still gets its band because the region is kept even when the line is
		shorter than its partner.
		"""
		v = self.view
		bucket = {}
		for line, kind in (marks or []):
			bucket.setdefault(kind, []).append(line)
		for kind, (key, scope) in self._DETAIL_SCOPES.items():
			regions = []
			for line in bucket.get(kind, []):
				try:
					row = max(0, int(line))
					if hidden_lines:
						row = max(0, row - int(hidden_lines.get(int(line), 0)))
					# from the first column of the row to the end of that
					# line (the newline itself excluded, trailing spaces in)
					start = v.text_point(row, 0)
					end = v.line(start).end()
					if end >= start:
						regions.append(Region(start, end))
				except Exception:
					continue
			if regions and scope:
				v.add_regions(key, regions, scope, '', sublime.DRAW_NO_OUTLINE)
			else:
				v.erase_regions(key)
		for kind, (key, _scope) in self._DETAIL_SCOPES.items():
			if kind not in bucket:
				v.erase_regions(key)

	def _jump_phantom(self, jump):
		"""The clickable 'jump to the source line' chip, or None.

		The detail view is a real (plain text) view, and a plain text view has
		no links - a phantom is the only way to make the location clickable.
		`jump` is [path, line, label] or None.
		"""
		v = self.view
		if not jump or not jump[0]:
			return None
		label = jump[2] if len(jump) > 2 else None
		html, targets = one_link(jump[0], jump[1], label=label)
		set_targets(v, targets)
		content = '<style>' + get_test_styles(v) + '</style>' + html

		def onclick(event):
			handle_jump_event(v, event)

		return Phantom(Region(0), content, sublime.LAYOUT_BLOCK, onclick)

	def _header_phantom(self, header):
		"""The badge bar above the detail text, or None.

		Header and jump chip are assembled into ONE update() call: Sublime's
		PhantomSet has no public list of what it currently holds, so a method
		that read them back to insert one more would work in the unit tests
		(whose stand-in keeps a list) and raise AttributeError in the editor.
		"""
		v = self.view
		if not header:
			return None
		content = '<style>' + get_test_styles(v) + '</style>' + header

		def onclick(event, cmd=self):
			cmd.on_header_event(event)

		return Phantom(Region(0), content, sublime.LAYOUT_BLOCK, onclick)

	def on_header_event(self, event):
		v = self.view
		if event == 'detail-close':
			try:
				v.close()
			except Exception:
				pass
			return True
		if event in ('detail-copy-actual', 'detail-copy-expected'):
			payload = self._copy_payload[0] if event == 'detail-copy-actual' \
				else self._copy_payload[1]
			sublime.set_clipboard(payload or '')
			sublime.status_message('[Algorithm Competition Assistant] ' + t(
				'detail_copied', n=len(payload or '')))
			return True
		return handle_jump_event(v, event)

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


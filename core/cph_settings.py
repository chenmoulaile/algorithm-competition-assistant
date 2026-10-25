"""
Algorithm Competition Assistant - 设置管理 + 测试数据存储路径解析

支持两种测试数据存储模式:
1. 传统模式 (FastOlympicCoding 兼容): tests 存在 "{filename}{suffix}" 同目录下
   e.g. "foo.cpp" -> "foo.cpp__tests"
2. cph-ng 模式: tests 存在 "{source_dir}/{tests_folder_name}/{filename}{suffix}"
   e.g. "foo.cpp" -> "tests/foo.cpp__tests"

两种模式可以同时存在,合并时去重(同目录下文件优先)
"""

import sublime
from os import path
import os

from .cph_i18n import t


_package_root = path.dirname(path.dirname(__file__))
root_dir = _package_root
base_name = path.split(_package_root)[1]


def package_name_for(root):
    """The name Sublime addresses a package by in a resource path.

    Installed as a .sublime-package zip - Package Control's default, and what
    every user without a git clone gets - `__file__` sits inside
    `Packages/<name>.sublime-package/`, but the resource layer, set_syntax_file,
    region icons and edit_settings all want the name WITHOUT that extension.
    That is how the zip-installed packages themselves spell it
    (`Packages/BracketHighlighter/...`, `Packages/LSP/...`). Leaving the suffix
    in breaks the run panel cards, the fold arrows and the syntax of the -run
    view, and makes Preferences answer "could not be opened".
    """
    name = path.split(root)[1] if root else ''
    if name.endswith('.sublime-package'):
        name = name[:-len('.sublime-package')]
    return name


#: The folder (or zip) the host actually loaded us from; for diagnostics only.
#: Every path handed to a Sublime API uses `package_name` instead.
package_name = package_name_for(_package_root)

# The display name changed in v2.4.4. The DATA files (practice statistics,
# machine calibration, tests suffix) deliberately keep their old names so a
# user who already has months of data does not lose it - only the settings
# file the package itself is loaded from was renamed.
settings_file = 'Algorithm Competition Assistant.sublime-settings'

#: User-visible name. The directory on disk may still be 'cph-by-chenkx' (the
#: repository name), so never build a packages/ path out of this constant -
#: base_name above is the directory, this is only for captions and messages.
PACKAGE_NAME = 'Algorithm Competition Assistant'

tests_file_suffix_default = '__tests'
tests_relative_dir_default = ''
tests_folder_name_default = 'tests'

settings = {}
run_supported_exts = set()


def get_settings():
	return settings


def init_settings(_settings):
	global settings
	settings = _settings


def is_run_supported_ext(ext):
	_run_settings = get_settings().get('run_settings', None)
	if _run_settings is not None:
		for option in _run_settings:
			if ext in option['extensions']:
				return True
	return False


def get_supported_exts(lang):
	_run_settings = get_settings().get('run_settings', None)
	if _run_settings is not None:
		for option in _run_settings:
			if option['name'] == lang:
				return option['extensions']
		return []
	return []


def try_load_settings():
	_settings = sublime.load_settings(settings_file)
	if _settings is None:
		sublime.set_timeout_async(try_load_settings, 200)
	else:
		init_settings(_settings)
		sublime.status_message('Algorithm Competition Assistant: ' + t('settings_loaded'))


# Per-problem limits received from Competitive Companion.
#
# They used to be written into the user's settings file from a background
# HTTP callback, which is not something a plugin should do. They are now
# written next to that problem's own test data instead: the file belongs to
# one source file, travels with its tests, and is the only place a restarted
# Sublime can read the received limits back from (they used to live in
# memory only, so the next manual Ctrl+Alt+B ran without them).
_problem_limits = {}


def primary_tests_path(file, create=False):
	"""Where save_tests() puts this file's test data.

	`create` only when the caller really writes there: load_all_tests() and
	the limits reader must not leave an empty tests folder behind in every
	directory the user ever runs a program in.
	"""
	if not file:
		return None

	dirname = os.path.dirname(file)
	filename = os.path.basename(file)
	suffix = get_tests_file_suffix()
	relative_dir = get_tests_relative_dir()

	if relative_dir:
		target_dir = os.path.join(dirname, relative_dir)
	elif os.path.exists(os.path.join(dirname, filename + suffix)):
		target_dir = dirname
	else:
		target_dir = os.path.join(dirname, get_tests_folder_name())

	if create and not os.path.exists(target_dir):
		try:
			os.makedirs(target_dir)
		except Exception:
			pass
	return os.path.join(target_dir, filename + suffix)


def get_limits_file_path(file):
	"""The sidecar holding the limits received for this problem."""
	primary = primary_tests_path(file, create=True)
	if not primary:
		return None
	return primary + '.limits'


# Which std and generator a stress test of THIS file was last run with. Saved
# per source file, not in the settings: one contest folder has one problem per
# subfolder and picking them once per problem is what a real session does,
# while a single global pair would be wrong for all but the last file.
_stress_files = {}


def _stress_files_path(file, create=False):
	primary = primary_tests_path(file, create=create)
	if not primary:
		return None
	return primary + '.stress'


def set_stress_files(file, std_file, generator_file):
	"""Remember the two programs used to stress-test this source file."""
	if not file:
		return
	_stress_files[file] = {'std_file': std_file or '', 'generator_file': generator_file or ''}
	path = _stress_files_path(file, create=True)
	if not path:
		return
	try:
		with open(path, 'w', encoding='utf-8') as f:
			f.write(sublime.encode_value(_stress_files[file], True))
	except Exception as e:
		_note_io_error('write', path, e, what='the std/gen pairing')


def usable_program(path):
	"""A remembered stress program must exist and actually contain something.

	The empty `std.cpp` a template leaves behind is the exact file the picker
	used to remember, and stress testing against it "proves" that any output is
	a counterexample.
	"""
	if not path:
		return False
	try:
		return os.path.isfile(path) and os.path.getsize(path) > 0
	except OSError:
		return False


def get_stress_files(file):
	"""(std_file, generator_file) for this source file, or (None, None).

	Paths that no longer exist are dropped: a remembered pair from a folder the
	user has since reorganised must not be started. Neither is an empty file.
	"""
	entry = _stress_files.get(file)
	if entry is None:
		path = _stress_files_path(file)
		entry = None
		if path and os.path.exists(path):
			try:
				with open(path, 'r', encoding='utf-8') as f:
					data = sublime.decode_value(f.read().strip())
				if isinstance(data, dict):
					entry = data
			except Exception:
				entry = None
		if entry is None:
			return (None, None)
		_stress_files[file] = entry
	std = entry.get('std_file') or ''
	gen = entry.get('generator_file') or ''
	return ((std if usable_program(std) else None),
			(gen if usable_program(gen) else None))


# Which -std this source file actually compiles with. Per file like the pair
# above: the problem that needs C++23 must not drag every other problem's
# compile up to the expensive standard, and the one that does not must not pay
# for a standard it never uses.
_remembered_std = {}


def _remembered_std_path(file, create=False):
	primary = primary_tests_path(file, create=create)
	if not primary:
		return None
	return primary + '.std'


def set_remembered_std(file, std):
	"""Remember (or, with None, forget) the compile standard of this file."""
	if not file:
		return
	path = _remembered_std_path(file)
	if not std:
		_remembered_std.pop(file, None)
		if path and os.path.exists(path):
			try:
				os.remove(path)
			except OSError:
				pass
		return
	_remembered_std[file] = std
	path = _remembered_std_path(file, create=True)
	if not path:
		return
	try:
		with open(path, 'w', encoding='utf-8') as f:
			f.write(sublime.encode_value({'std': std}, True))
	except Exception as e:
		_note_io_error('write', path, e, what='the remembered -std')


def get_remembered_std(file):
	"""The remembered -std for this file, or None."""
	if not file:
		return None
	std = _remembered_std.get(file)
	if std:
		return std
	path = _remembered_std_path(file)
	if not path or not os.path.exists(path):
		return None
	try:
		with open(path, 'r', encoding='utf-8') as f:
			data = sublime.decode_value(f.read().strip())
	except Exception:
		return None
	if isinstance(data, dict) and data.get('std'):
		_remembered_std[file] = data['std']
		return data['std']
	return None


# --------------------------------------------------------------------------
# 文件输入输出（NOI / CSP-S 复赛那种 add.in / add.out）
#
# 题面会指定文件名，而且每题不一样，所以这份配置跟着题目文件走 —— 和 .limits
# / .std / .stress 完全同一套做法：存在测试数据旁边，重启不丢，也不会因为给 A 题
# 设过就把 B 题的读写名改掉。
# --------------------------------------------------------------------------
_problem_io = {}


# --------------------------------------------------------------------------
# 每个测试点的耗时历史
#
# "我这次优化到底起作用没有"是训练里最常问的问题，而插件以前只有一个格子：
# Test.memorize() 每次运行都覆盖上一次的 runtime，所以第二次跑完之后，第一次的
# 数字已经不存在了。历史单独存 <名>__tests.hist，不进 __tests —— 那个文件已经
# 把 stdout/stderr 正文存进去了，再塞历史会让每次加载都变慢。
# --------------------------------------------------------------------------
_runtime_hist = {}
_RUNTIME_KEEP = 20


def get_hist_file_path(file_name, create=False):
	path = primary_tests_path(file_name, create=create)
	return (path + '.hist') if path else None


def _read_hist_file(file_name):
	path = get_hist_file_path(file_name)
	if not path:
		return {}
	try:
		if not os.path.exists(path):
			return {}
		with open(path, 'r', encoding='utf-8') as f:
			data = sublime.decode_value(f.read().strip())
	except Exception:
		return {}
	return data if isinstance(data, dict) else {}


def record_runtime(file_name, index, runtime_ms):
	"""Append one measured runtime for test `index` (1-based). Never raises.

	Best effort by contract: a statistics write must be able to take a judged
	run down with it.
	"""
	if not file_name or runtime_ms is None:
		return
	try:
		runtime_ms = int(runtime_ms)
	except (TypeError, ValueError):
		return
	if runtime_ms < 0:
		return
	key = str(int(index))
	history = dict(_runtime_hist.get(file_name) or _read_hist_file(file_name))
	runs = history.get(key) or []
	if not isinstance(runs, list):
		runs = []
	runs.append(runtime_ms)
	history[key] = runs[-_RUNTIME_KEEP:]
	_runtime_hist[file_name] = history
	path = get_hist_file_path(file_name, create=True)
	if not path:
		return
	try:
		with open(path, 'w', encoding='utf-8') as f:
			f.write(sublime.encode_value(history, True))
	except Exception as e:
		_note_io_error('write', path, e, what='the runtime history')


def last_runtime(file_name, index):
	"""The previous recorded runtime for this test, or None.

	'previous' means the one before the run that was just recorded, so the
	detail can say 412ms -> 1.24s rather than comparing a number with itself.
	"""
	history = _runtime_hist.get(file_name)
	if history is None:
		history = _read_hist_file(file_name)
		if history:
			_runtime_hist[file_name] = history
	runs = (history or {}).get(str(int(index))) or []
	if not isinstance(runs, list) or len(runs) < 2:
		return None
	try:
		return int(runs[-2])
	except (TypeError, ValueError):
		return None


def runtime_runs(file_name, index):
	"""All recorded runtimes for this test, oldest first."""
	history = _runtime_hist.get(file_name)
	if history is None:
		history = _read_hist_file(file_name)
	runs = (history or {}).get(str(int(index))) or []
	out = []
	for value in runs if isinstance(runs, list) else []:
		try:
			out.append(int(value))
		except (TypeError, ValueError):
			continue
	return out


def get_io_file_path(file_name, create=False):
	"""`<tests file>.io`. `create` only for the writer: a read must not leave an
	elsewhere-empty `tests/` folder in every directory the user ever runs in."""
	path = primary_tests_path(file_name, create=create)
	return (path + '.io') if path else None


def _read_io_file(file_name):
	path = get_io_file_path(file_name)
	if not path:
		return {}
	try:
		if not os.path.exists(path):
			return {}
		with open(path, 'r', encoding='utf-8') as f:
			data = sublime.decode_value(f.read().strip())
	except Exception:
		return {}
	return data if isinstance(data, dict) else {}


def default_io_names(file_name):
	"""The names a C++/NOI-style problem usually wants: add.cpp -> add.in/add.out."""
	base = os.path.basename(file_name or '')
	root = os.path.splitext(base)[0] or 'data'
	return root + '.in', root + '.out'


def get_problem_io(file_name):
	"""{'mode': 'stdin'|'files', 'input': name, 'output': name} for this problem."""
	spec = _problem_io.get(file_name)
	if spec is None:
		spec = _read_io_file(file_name)
		if spec:
			_problem_io[file_name] = spec
	if not isinstance(spec, dict):
		return {'mode': 'stdin', 'input': '', 'output': ''}
	mode = spec.get('mode')
	if mode not in ('stdin', 'files'):
		mode = 'stdin'
	input_name = (spec.get('input') or '').strip()
	output_name = (spec.get('output') or '').strip()
	if mode == 'files' and not input_name:
		# A half-saved spec (name forgotten, file hand-edited) must not run a
		# program that then reads nothing: fall back to the conventional names
		# for this source file and say which ones are being used.
		input_name, output_name = default_io_names(file_name)
	if mode == 'files' and not output_name:
		output_name = os.path.splitext(input_name)[0] + '.out'
	return {'mode': mode, 'input': input_name, 'output': output_name}


def set_problem_io(file_name, mode, input_name=None, output_name=None):
	"""Remember how this problem is evaluated; returns the stored spec."""
	if not file_name:
		return None
	spec = get_problem_io(file_name)
	mode = mode if mode in ('stdin', 'files') else 'stdin'
	spec = {'mode': mode,
			'input': (input_name or '').strip() if mode == 'files' else '',
			'output': (output_name or '').strip() if mode == 'files' else ''}
	if mode == 'files' and not spec['input']:
		spec['input'], spec['output'] = default_io_names(file_name)
	if mode == 'files' and not spec['output']:
		spec['output'] = os.path.splitext(spec['input'])[0] + '.out'
	_problem_io[file_name] = spec
	path = get_io_file_path(file_name, create=True)
	if path:
		try:
			with open(path, 'w', encoding='utf-8') as f:
				f.write(sublime.encode_value(spec, True))
		except Exception as e:
			_note_io_error('write', path, e, what='the file-IO settings')
	return spec


def _limit_value(value):
	try:
		return int(value)
	except (TypeError, ValueError):
		return None


def _read_limits_file(file_name):
	path = primary_tests_path(file_name)
	if not path:
		return {}
	try:
		if not os.path.exists(path + '.limits'):
			return {}
		with open(path + '.limits', 'r', encoding='utf-8') as f:
			data = sublime.decode_value(f.read().strip())
	except Exception:
		return {}
	if not isinstance(data, dict):
		return {}
	return {'time_limit_ms': _limit_value(data.get('time_limit_ms')),
			'memory_limit_mb': _limit_value(data.get('memory_limit_mb'))}


def _write_limits_file(file_name, limits):
	if limits.get('time_limit_ms') is None and limits.get('memory_limit_mb') is None:
		return
	path = get_limits_file_path(file_name)
	if not path:
		return
	try:
		with open(path, 'w', encoding='utf-8') as f:
			f.write(sublime.encode_value(limits, True))
	except Exception as e:
		_note_io_error('write', path, e, what='the problem limits')


def set_problem_limits(file_name, time_limit_ms, memory_limit_mb):
	"""Remember this problem's limits, filling only what was actually sent.

	A problem that arrives without a memory limit must not erase the one the
	same file received a minute earlier.
	"""
	if not file_name:
		return
	old = _problem_limits.get(file_name) or _read_limits_file(file_name)
	limits = {
		'time_limit_ms': old.get('time_limit_ms'),
		'memory_limit_mb': old.get('memory_limit_mb'),
	}
	if time_limit_ms is not None:
		limits['time_limit_ms'] = time_limit_ms
	if memory_limit_mb is not None:
		limits['memory_limit_mb'] = memory_limit_mb
	_problem_limits[file_name] = limits
	_write_limits_file(file_name, limits)


def get_problem_limits(file_name):
	limits = _problem_limits.get(file_name)
	if limits is None:
		limits = _read_limits_file(file_name)
		if limits:
			_problem_limits[file_name] = limits
	return limits or {}


def companion_log_path():
	"""Where companion_debug_log appends to: <Data>/Log/aca-companion.log.

	One function because both the writer and the panel that opens the file
	need the same answer, and neither should re-derive it from packages_path().
	"""
	return os.path.join(os.path.dirname(sublime.packages_path()), 'Log',
						'aca-companion.log')


def get_tests_file_suffix():
	return get_settings().get('tests_file_suffix', tests_file_suffix_default)


def get_tests_relative_dir():
	return get_settings().get('tests_relative_dir', tests_relative_dir_default)


def get_tests_folder_name():
	return get_settings().get('tests_folder_name', tests_folder_name_default)


def get_tests_paths(file):
	if not file:
		return []

	dirname = os.path.dirname(file)
	filename = os.path.basename(file)
	suffix = get_tests_file_suffix()
	folder_name = get_tests_folder_name()
	relative_dir = get_tests_relative_dir()

	paths = []

	# save_tests() writes here when tests_relative_dir is set, so it has to be
	# probed as well - otherwise the data was written but never loaded (and
	# "clear tests" never removed it).
	if relative_dir:
		paths.append(os.path.join(dirname, relative_dir, filename + suffix))

	traditional_path = os.path.join(dirname, filename + suffix)
	paths.append(traditional_path)

	folder_path = os.path.join(dirname, folder_name, filename + suffix)
	paths.append(folder_path)

	folder_path_no_suffix = os.path.join(dirname, folder_name, filename + '.json')
	if folder_path_no_suffix not in paths:
		paths.append(folder_path_no_suffix)

	return paths


# The last failure to read or write a tests file. A `print()` lands in a console
# nobody opens, and on a folder install the only symptom of a refused write was
# "the answer I marked by hand vanished" - indistinguishable from a bug in the
# judging code. Any caller that puts tests on disk can now name the path and the
# reason instead of going quiet.
_last_io_error = {}


def last_io_error(clear=False):
	"""The most recent sidecar failure as {'kind','what','path','reason'} or None.

	The snapshot is taken BEFORE anything is cleared: clearing first made the
	function return None on every `clear=True` call, which is exactly how a
	failure report ends up reporting nothing.
	"""
	snapshot = dict(_last_io_error) if _last_io_error else None
	if clear:
		_last_io_error.clear()
	return snapshot


def _note_io_error(kind, target, reason, what='tests'):
	_last_io_error['kind'] = kind
	_last_io_error['what'] = what
	_last_io_error['path'] = target or ''
	_last_io_error['reason'] = str(reason)[:300]
	print('[Algorithm Competition Assistant] Failed to %s %s in %s: %s'
	      % (kind, what, target, reason))


# A read-only folder, a full disk or a "synced with OneDrive" directory fails
# the same write on *every* run. A dialog on every run is a plugin the user
# disables, so one distinct failure is announced once per session and the rest
# go to the console - where they cost nothing and stay available for a doctor
# report. Different files and different reasons are different failures.
_io_notified = set()


def notify_io_error(fallback, kind=None, always=False):
	"""Announce the last sidecar failure once; return the sentence shown.

	`kind` filters: a load site must not report an older write failure it
	happened to leave in the slot, or the message blames the wrong operation.
	"""
	err = last_io_error()
	if not err:
		return ''
	if kind and err.get('kind') != kind:
		# Not this site's failure - leave it in the slot for the site that
		# actually caused it instead of consuming someone else's error.
		return ''
	_last_io_error.clear()
	reason = (err.get('reason') or '').strip() or t('io_reason_unknown')
	target = err.get('path') or fallback
	if err.get('kind') == 'read':
		msg = t('tests_read_failed', path=target, reason=reason)
	else:
		msg = t('tests_save_failed', path=target, reason=reason)
	key = (err.get('kind'), target, reason)
	if not always and key in _io_notified:
		print('[Algorithm Competition Assistant] %s' % msg)
		return msg
	if len(_io_notified) > 64:
		_io_notified.clear()
	_io_notified.add(key)
	sublime.error_message('[Algorithm Competition Assistant] %s' % msg)
	return msg


def get_tests_file_path(file):
	if not file:
		return None
	dirname = os.path.dirname(file)
	filename = os.path.basename(file)
	suffix = get_tests_file_suffix()
	return os.path.join(dirname, filename + suffix)


def load_all_tests(file):
	all_paths = get_tests_paths(file)
	seen = set()
	merged = []

	for p in all_paths:
		if not os.path.exists(p):
			continue
		try:
			with open(p, 'r', encoding='utf-8') as f:
				content = f.read().strip()
			if not content:
				continue
			data = sublime.decode_value(content)
			if not isinstance(data, list):
				continue
			for test in data:
				# Skip the empty placeholder here as well as in save_tests().
				# Older versions wrote it, and it is enough for ONE stale copy
				# to live in one of the other candidate files for the entry to
				# be merged back on every reload - which is why an empty
				# sample could not be deleted for good.
				if not is_meaningful_test(test):
					continue
				key = (test.get('test', ''),
					   tuple(sorted(test.get('correct_answers', []))))
				if key in seen:
					continue
				seen.add(key)
				merged.append(test)
		except Exception as e:
			_note_io_error('read', p, e)

	return merged


def is_meaningful_test(test):
	"""False for the empty placeholder the run panel starts with.

	`new_test` creates a test with no input and starts the program so the
	user can paste the sample into the panel. That placeholder was saved
	too, so every problem ended up with a `[{"test": ""}]` entry that came
	back on every reload and could never be deleted for good.
	A test is worth keeping as soon as it has an input, an answer or an
	expected output - a verdict alone does not count, because a run with no
	input only proves the program started.
	"""
	if not isinstance(test, dict):
		return False
	if (test.get('test') or '').strip():
		return True
	for key in ('correct_answers', 'uncorrect_answers'):
		if test.get(key):
			return True
	return bool((test.get('expected_output') or '').strip())


def save_tests(file, tests, pretty=True):
	return _save_tests_impl(file, tests, pretty)


def save_tests_async(file, tests, pretty=True, on_done=None):
	"""Serialize + write every tests file on a worker thread.

	The run panel's end-of-chain flush used to do this on the main thread:
	up to four files, each with the full pretty-printed test data (a
	multi-megabyte output inflated by pretty printing) - the editor paused
	for the whole write. The serialization runs on the caller's thread only
	in `save_tests`; here everything moves to a private thread and
	`on_done(ok)` is posted back to the main thread (or skipped when None).

	sublime.encode_value / decode_value are pure converters (no view state),
	so they are safe off the main thread; the data is plain str/list/dict.
	"""
	import threading

	def work():
		ok = _save_tests_impl(file, tests, pretty)
		if on_done is not None:
			try:
				sublime.set_timeout(lambda: on_done(ok), 0)
			except Exception:
				pass

	thread = threading.Thread(target=work)
	thread.daemon = True
	thread.start()


def _save_tests_impl(file, tests, pretty=True):
	_last_io_error.clear()
	if not file:
		return False

	# Never persist an empty placeholder (see is_meaningful_test).
	tests = [t for t in (tests or []) if is_meaningful_test(t)]

	# Same choice the limits sidecar is keyed off, so the two always travel
	# together (see primary_tests_path()).
	target_path = primary_tests_path(file, create=True)

	if target_path is None:
		_note_io_error('write', None, 'no writable tests directory for this file')
		return False

	# load_all_tests() merges EVERY candidate file, so writing only one of
	# them is not symmetric: a test the user deleted came straight back from
	# a stale copy in another one. They all belong to the same source file
	# (the two storage layouts plus cph-ng's tests/<name>.json), so they are
	# written together. Files that do not exist yet are left alone - only
	# the primary target is ever created.
	targets = [target_path]
	for other in get_tests_paths(file):
		if other and other != target_path and os.path.exists(other):
			targets.append(other)

	payload = sublime.encode_value(tests, pretty)

	ok = False
	for path_ in targets:
		try:
			with open(path_, 'w', encoding='utf-8') as f:
				f.write(payload)
			ok = True
		except Exception as e:
			_note_io_error('write', path_, e)
	return ok

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
		sublime.status_message('Algorithm Competition Assistant: settings loaded')


# Per-problem limits received from Competitive Companion, kept in memory
# only. They used to be written into the user's settings file from a
# background HTTP callback, which is not something a plugin should do.
_problem_limits = {}


def set_problem_limits(file_name, time_limit_ms, memory_limit_mb):
	if not file_name:
		return
	_problem_limits[file_name] = {
		'time_limit_ms': time_limit_ms,
		'memory_limit_mb': memory_limit_mb,
	}


def get_problem_limits(file_name):
	return _problem_limits.get(file_name) or {}


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
			print('[Algorithm Competition Assistant] Failed to load tests from %s: %s' % (p, e))

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


def save_tests(file, tests):
	if not file:
		return False

	# Never persist an empty placeholder (see is_meaningful_test).
	tests = [t for t in (tests or []) if is_meaningful_test(t)]

	dirname = os.path.dirname(file)
	filename = os.path.basename(file)
	suffix = get_tests_file_suffix()
	folder_name = get_tests_folder_name()
	relative_dir = get_tests_relative_dir()

	target_path = None
	if relative_dir:
		target_dir = os.path.join(dirname, relative_dir)
		if not os.path.exists(target_dir):
			try:
				os.makedirs(target_dir)
			except Exception:
				pass
		target_path = os.path.join(target_dir, filename + suffix)
	elif os.path.exists(os.path.join(dirname, filename + suffix)):
		target_path = os.path.join(dirname, filename + suffix)
	else:
		target_dir = os.path.join(dirname, folder_name)
		if not os.path.exists(target_dir):
			try:
				os.makedirs(target_dir)
			except Exception:
				pass
		target_path = os.path.join(target_dir, filename + suffix)

	if target_path is None:
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

	ok = False
	for path_ in targets:
		try:
			with open(path_, 'w', encoding='utf-8') as f:
				f.write(sublime.encode_value(tests, True))
			ok = True
		except Exception as e:
			print('[Algorithm Competition Assistant] Failed to save tests to %s: %s' % (path_, e))
	return ok

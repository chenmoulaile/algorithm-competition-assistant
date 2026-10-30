"""Algorithm Competition Assistant - 自定义判定器（Special Judge / SPJ）

只有 token 比较 + 浮点容差时，"输出任意合法方案"的多解题根本没法测。这里支持
挂一个 checker 程序来判定，兼容 testlib。

两种调用约定（由 ``checker_style`` 选择）：

- ``testlib``（默认）：``checker <input> <output> <answer>``，三个**文件路径**。
  这正是 testlib 的 ``main`` 读 ``argv[1..3]`` 的方式，所以现成的 testlib
  checker（含 ``quitf(_ok/_wa/_pe, ...)``）可以直接用。
- ``stdin``：把「输入 / 用户输出 / 标准答案」按这个顺序写到 checker 的 stdin，
  每段用一行 ``---`` 分隔（方便写简单的 Python/脚本 checker）。

退出码按 testlib 约定映射：0=AC，1=WA，2=PE，3=FAIL（当 UKE），7=PC。
checker 自己打印的内容（testlib 的 ``quitf`` 消息）会带到详情视图里。

放在 core/ 是因为根级插件模块之间不允许互相 import（Package Control 审查规则）。
"""

import os
import shlex
import subprocess
import tempfile

from .cph_i18n import t

#: testlib exit code -> verdict name
EXIT_CODES = {
	0: 'accepted',
	1: 'wrong_answer',
	2: 'presentation_error',
	3: 'unknown_error',
	4: 'unknown_error',
	5: 'unknown_error',
	6: 'unknown_error',
	7: 'partially_correct',
}

DEFAULT_COMPILE_CMD = 'g++ -O2 -std=c++17 -I "{checker_dir}" "{checker}" -o "{checker}.exe"'

#: checker path -> (mtime, exe_path) so a checker is built once per session
_compiled = {}
#: checker path -> (mtime, error). A failed build is remembered for the same
#: source mtime so a broken checker does not retry (and block the UI) on every
#: single test; editing the source changes the mtime and clears it.
_failed = {}

#: A checker build is a compiler invocation like any other: the main compile
#: path bounds it at 30s, so this one must not be able to hang the caller
#: (which used to be the UI thread) forever.
_COMPILE_TIMEOUT = 30


def _source_newer(source, binary):
	"""True when `source` was edited after `binary` was built.

	A generated `<checker>.exe` that already exists must not shadow the
	mtime cache: without this check, fixing a checker's rule never rebuilt
	it and every later verdict silently used the old binary.

	A missing source is not "newer": the pre-built exe is all that is left,
	and refusing it turned a working checker into checker_missing.
	"""
	try:
		source_mtime = os.path.getmtime(source)
	except Exception:
		return False
	try:
		binary_mtime = os.path.getmtime(binary)
	except Exception:
		return True
	return source_mtime > binary_mtime


def config(run_settings, file, settings=None):
	"""The checker settings for this file, or None when no checker is set.

	Reads the ``checker`` key of the matching ``run_settings`` entry. An empty
	or missing value means "no checker", so the normal comparison is used.
	"""
	if not run_settings or not file:
		return None
	ext = os.path.splitext(file)[1][1:]
	for entry in run_settings:
		if ext in (entry.get('extensions') or []):
			checker = (entry.get('checker') or '').strip()
			if not checker:
				return None
			src_dir = os.path.dirname(os.path.abspath(file))
			# A value with a space is a COMMAND, not a source path:
			# "python checker.py" is the natural way to write a script
			# checker, and forcing it through a C++ compile command failed.
			if ' ' in checker:
				parts = shlex.split(checker)
				head = parts[0]
				path = parts[-1] if len(parts) > 1 else head
				if not os.path.isabs(path):
					path = os.path.join(src_dir, path)
				return {
					'path': os.path.normpath(path),
					'dir': src_dir,
					'name': os.path.splitext(os.path.basename(path))[0],
					'style': (entry.get('checker_style') or 'testlib').strip().lower(),
					'compile_cmd': '',
					'time_limit_ms': entry.get('checker_time_limit_ms') or 10000,
					'command': parts,
				}
			path = checker
			if not os.path.isabs(path):
				path = os.path.join(src_dir, path)
			return {
				'path': os.path.normpath(path),
				'dir': os.path.dirname(os.path.normpath(path)),
				'name': os.path.splitext(os.path.basename(path))[0],
				'style': (entry.get('checker_style') or 'testlib').strip().lower(),
				'compile_cmd': entry.get('checker_compile_cmd') or DEFAULT_COMPILE_CMD,
				'time_limit_ms': entry.get('checker_time_limit_ms') or 10000,
				'command': None,
			}
	return None


def _expand(cmd, cfg):
	values = {
		'checker': cfg['path'],
		'checker_dir': cfg['dir'],
		'checker_name': cfg['name'],
	}
	out = cmd
	for key, value in values.items():
		out = out.replace('{%s}' % key, value)
	return out


def _is_binary(path):
	"""A .exe / extension-less file that exists is used as-is (no compile)."""
	if not os.path.isfile(path):
		return False
	ext = os.path.splitext(path)[1].lower()
	if ext in ('.exe', '.out', '.bin'):
		return True
	return ext == '' and os.access(path, os.X_OK)


def executable(cfg):
	"""Path of the runnable checker, compiling it on first use.

	Returns (path, error_message). error_message is '' on success.
	"""
	if cfg.get('command'):
		# Nothing to build: the command itself is the checker.
		if not os.path.isfile(cfg['path']):
			return None, t('checker_missing', path=cfg['path'], error='not found')
		return cfg['command'], ''

	exe = cfg['path'] + '.exe'
	if _is_binary(cfg['path']):
		return cfg['path'], ''
	if _is_binary(exe) and not _source_newer(cfg['path'], exe):
		return exe, ''

	try:
		key = cfg['path']
		mtime = os.path.getmtime(cfg['path'])
	except Exception as e:
		return None, t('checker_missing', path=cfg['path'], error=str(e))

	failed = _failed.get(key)
	if failed and failed[0] == mtime:
		return None, failed[1]

	cached = _compiled.get(key)
	if cached and cached[0] == mtime and os.path.isfile(cached[1]):
		return cached[1], ''

	cmd = _expand(cfg['compile_cmd'], cfg)
	proc = None
	try:
		startupinfo = None
		startupinfo = hidden_startupinfo()
		proc = subprocess.Popen(cmd, shell=True, stdin=None,
								stdout=subprocess.PIPE,
								stderr=subprocess.STDOUT,
								cwd=cfg['dir'], startupinfo=startupinfo)
		out = proc.communicate(timeout=_COMPILE_TIMEOUT)[0]
	except subprocess.TimeoutExpired:
		_reap(proc)
		msg = t('checker_compile_failed', cmd=cmd,
				error='timed out after %ss' % _COMPILE_TIMEOUT)
		_failed[key] = (mtime, msg)
		return None, msg
	except Exception as e:
		_reap(proc)
		msg = t('checker_compile_failed', cmd=cmd, error=str(e))
		_failed[key] = (mtime, msg)
		return None, msg

	text = decode_output(out)
	if proc.returncode != 0:
		msg = t('checker_compile_failed', cmd=cmd, error=text[-800:])
		_failed[key] = (mtime, msg)
		return None, msg
	_failed.pop(key, None)
	_compiled[key] = (mtime, exe)
	return exe, ''


def hidden_startupinfo():
	"""STARTUPINFO that keeps a console window from flashing (Windows).

	Returns None on every other platform, so it can be passed to Popen
	unconditionally.
	"""
	windows = os.name == 'nt'
	try:
		import sublime
		windows = sublime.platform() == 'windows'
	except Exception:
		pass
	if not windows:
		return None
	try:
		startupinfo = subprocess.STARTUPINFO()
		startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
		return startupinfo
	except Exception:
		return None


def decode_output(data):
	"""bytes -> str (utf-8, replace); str passes through."""
	if isinstance(data, str):
		return data
	try:
		return data.decode('utf-8', 'replace')
	except Exception:
		return ''


def _write(path, text):
	with open(path, 'wb') as f:
		f.write((text or '').encode('utf-8', 'replace'))


def _reap(proc):
	"""Kill and collect a child we are abandoning, bounded and never raising.

	A checker that hangs holds its pipes and its temp directory open. Without a
	kill here, every judged test of a runaway SPJ leaked a process, and the
	following _cleanup() then failed to remove the files it still had open -
	quietly, because _cleanup swallows everything.
	"""
	if proc is None:
		return
	try:
		proc.kill()
	except Exception:
		pass
	try:
		proc.wait(timeout=2)
	except Exception:
		pass


def judge(cfg, exe, input_text, output_text, expected_text, workdir=None):
	"""Run the checker. Returns a dict:

	{'ok': bool, 'verdict': 'accepted'|'wrong_answer'|..., 'message': str}

	`workdir` lets a caller reuse one scratch directory across rounds (the
	stress session does): each call wrote ~1 MB of files and deleted them
	again, so 1000 rounds churned ~3 GB of disk traffic. When None, a
	per-call temp directory is created and cleaned up as before.
	"""
	owns_dir = workdir is None
	if owns_dir:
		workdir = tempfile.mkdtemp(prefix='cph-checker-')
	in_path = os.path.join(workdir, 'input.txt')
	out_path = os.path.join(workdir, 'output.txt')
	ans_path = os.path.join(workdir, 'answer.txt')
	try:
		if cfg['style'] != 'stdin':
			_write(in_path, input_text)
			_write(out_path, output_text)
			_write(ans_path, expected_text)
		# A stdin-style checker reads its payload from stdin and never opens
		# the three files, so nothing is written for it.
	except Exception as e:
		# A full disk or a locked folder used to raise straight through
		# apply_checker() into on_stop(), which aborted the verdict for that
		# test with nothing on the card. Say which stage failed instead.
		if owns_dir:
			_cleanup(workdir)
		return {'ok': False, 'verdict': 'unknown_error',
				'message': t('checker_write_failed', error=str(e)[:200])}

	if isinstance(exe, list):
		prefix = list(exe)
	else:
		prefix = [exe]
	argv = prefix
	if cfg['style'] == 'stdin':
		payload = '\n---\n'.join([input_text or '', output_text or '',
								  expected_text or ''])
	else:
		argv = prefix + [in_path, out_path, ans_path]
		payload = None

	limit_s = max(1.0, float(cfg['time_limit_ms']) / 1000.0)
	proc = None
	out = b''
	code = None
	try:
		startupinfo = hidden_startupinfo()
		proc = subprocess.Popen(
			argv, shell=False,
			stdin=subprocess.PIPE if payload is not None else None,
			stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
			cwd=cfg['dir'], startupinfo=startupinfo)
		out = proc.communicate(
			payload.encode('utf-8', 'replace') if payload is not None else None,
			timeout=limit_s)[0]
		code = proc.returncode
	except subprocess.TimeoutExpired:
		_reap(proc)
		if owns_dir:
			_cleanup(workdir)
		return {'ok': False, 'verdict': 'unknown_error',
				'message': t('checker_timeout', limit='%g' % limit_s)}
	except Exception as e:
		_reap(proc)
		if owns_dir:
			_cleanup(workdir)
		return {'ok': False, 'verdict': 'unknown_error',
				'message': t('checker_run_failed', error=str(e))}

	if owns_dir:
		_cleanup(workdir)
	message = decode_output(out).strip()
	name = EXIT_CODES.get(code, 'unknown_error')
	if code is not None and code < 0:
		name = 'runtime_error'
	return {'ok': name == 'accepted', 'verdict': name, 'message': message}


def _cleanup(workdir):
	try:
		for name in os.listdir(workdir):
			try:
				os.remove(os.path.join(workdir, name))
			except Exception:
				pass
		os.rmdir(workdir)
	except Exception:
		pass

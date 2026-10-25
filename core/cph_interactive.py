"""Algorithm Competition Assistant - 交互题（Interactor）

交互题没法用"跑完看输出"来判：选手程序和 interactor 要**来回对话**。这里按
testlib 的约定接线：

- interactor 由 ``interactor <输入文件> <输出文件> <标准答案文件>`` 启动，
  它从 **stdin** 读选手的输出，往 **stdout** 写要发给选手的内容；
- 选手程序照常跑，只是它的 stdout 被接到 interactor 的 stdin，interactor 的
  stdout 被接回选手的 stdin；
- interactor 的**退出码**就是判决（0=AC，1=WA，2=PE，3=FAIL，7=PC）。

选手程序复用 ProcessManager（它本来就有 read()/insert()，正好当双向中转），
所以解码、换行归一化、内存采样、终止逻辑都不用重写。

交互题不能并发跑（interactor 往往不是可重入的，而且很慢），所以走批处理但
worker 固定为 1。

放在 core/ 是因为根级插件模块之间不允许互相 import（Package Control 审查规则）。
"""

import os
import subprocess
import tempfile
import threading
import time

from .cph_checker import EXIT_CODES, decode_output, hidden_startupinfo
from .cph_i18n import t

#: interactor 路径 -> (mtime, exe) 只编译一次
_compiled = {}

DEFAULT_COMPILE_CMD = 'g++ -O2 -std=c++17 -I "{interactor_dir}" "{interactor}" -o "{interactor}.exe"'

#: 单次交互的总墙钟上限 = 时限 + 这个余量（交互本身有进程创建/管道开销）
_GRACE_SECONDS = 1.0


def config(run_settings, file):
	"""The interactor settings for this file, or None when none is set."""
	if not run_settings or not file:
		return None
	ext = os.path.splitext(file)[1][1:]
	for entry in run_settings:
		if ext in (entry.get('extensions') or []):
			interactor = (entry.get('interactor') or '').strip()
			if not interactor:
				return None
			src_dir = os.path.dirname(os.path.abspath(file))
			# A value with a space is a COMMAND ("python interactor.py").
			if ' ' in interactor:
				parts = interactor.split()
				path = parts[-1]
				if not os.path.isabs(path):
					path = os.path.join(src_dir, path)
				return {
					'path': os.path.normpath(path),
					'dir': src_dir,
					'name': os.path.splitext(os.path.basename(path))[0],
					'compile_cmd': '',
					'time_limit_ms': entry.get('interactor_time_limit_ms') or 20000,
					'command': parts,
				}
			path = interactor
			if not os.path.isabs(path):
				path = os.path.join(src_dir, path)
			path = os.path.normpath(path)
			return {
				'path': path,
				'dir': os.path.dirname(path),
				'name': os.path.splitext(os.path.basename(path))[0],
				'compile_cmd': entry.get('interactor_compile_cmd') or DEFAULT_COMPILE_CMD,
				'time_limit_ms': entry.get('interactor_time_limit_ms') or 20000,
				'command': None,
			}
	return None


def _expand(cmd, cfg):
	out = cmd
	for key, value in (('interactor', cfg['path']),
					   ('interactor_dir', cfg['dir']),
					   ('interactor_name', cfg['name'])):
		out = out.replace('{%s}' % key, value)
	return out


def _is_binary(path):
	if not os.path.isfile(path):
		return False
	ext = os.path.splitext(path)[1].lower()
	if ext in ('.exe', '.out', '.bin'):
		return True
	return ext == '' and os.access(path, os.X_OK)


def executable(cfg):
	"""Path of the runnable interactor, compiling it on first use.

	Returns (path, error_message); error_message is '' on success.
	"""
	if cfg.get('command'):
		if not os.path.isfile(cfg['path']):
			return None, t('interactor_missing', path=cfg['path'], error='not found')
		return cfg['command'], ''

	exe = cfg['path'] + '.exe'
	if _is_binary(cfg['path']):
		return cfg['path'], ''
	if _is_binary(exe):
		return exe, ''

	try:
		key = cfg['path']
		mtime = os.path.getmtime(cfg['path'])
	except Exception as e:
		return None, t('interactor_missing', path=cfg['path'], error=str(e))

	cached = _compiled.get(key)
	if cached and cached[0] == mtime and os.path.isfile(cached[1]):
		return cached[1], ''

	cmd = _expand(cfg['compile_cmd'], cfg)
	try:
		proc = subprocess.Popen(cmd, shell=True, stdin=None,
								stdout=subprocess.PIPE,
								stderr=subprocess.STDOUT,
								cwd=cfg['dir'],
								startupinfo=hidden_startupinfo())
		out = proc.communicate()[0]
	except Exception as e:
		return None, t('interactor_compile_failed', cmd=cmd, error=str(e))

	if proc.returncode != 0:
		return None, t('interactor_compile_failed', cmd=cmd,
					   error=decode_output(out)[-800:])
	_compiled[key] = (mtime, exe)
	return exe, ''


def _write(path, text):
	with open(path, 'wb') as f:
		f.write((text or '').encode('utf-8', 'replace'))


def run_case(make_solution, exe, input_text, answer_text, time_limit_ms,
			 cwd=None):
	"""Run one interaction and return a result dict.

	Same shape as cph_parallel._run_one so the panel code can be shared:
	{'rtcode','stdout','stderr','runtime','memory','verdict','crash','error',
	 'message','interactor_output'}
	"""
	from .cph_verdict import get_verdict, looks_like_crash

	result = {
		'rtcode': None, 'stdout': '', 'stderr': '', 'runtime': 0,
		'memory': None, 'verdict': None, 'crash': None, 'error': None,
		'message': '', 'interactor_output': '',
	}

	workdir = tempfile.mkdtemp(prefix='cph-interactor-')
	in_path = os.path.join(workdir, 'input.txt')
	out_path = os.path.join(workdir, 'output.txt')
	ans_path = os.path.join(workdir, 'answer.txt')
	_write(in_path, input_text)
	_write(out_path, '')
	_write(ans_path, answer_text)

	solution = None
	interactor = None
	stop = threading.Event()
	collected = {'solution': [], 'interactor': [], 'interactor_err': []}

	try:
		solution = make_solution()
		solution.set_separate_stderr(True)
		if time_limit_ms:
			solution.set_time_limit(time_limit_ms)

		prefix = list(exe) if isinstance(exe, list) else [exe]
		# cwd must come from the config: with a command-style interactor
		# ("python interactor.py") `exe` is a LIST and os.path.dirname() on it
		# raised TypeError, so every interaction ended as UKE.
		if cwd is None and isinstance(exe, str):
			cwd = os.path.dirname(exe) or None
		interactor = subprocess.Popen(
			prefix + [in_path, out_path, ans_path], shell=False,
			stdin=subprocess.PIPE, stdout=subprocess.PIPE,
			stderr=subprocess.PIPE, cwd=cwd,
			startupinfo=hidden_startupinfo())

		started = time.monotonic()
		solution.run_file()

		def pump_solution():
			"""选手 -> interactor（同时留一份给面板显示）。

			成块搬运（4096）：read(bfsize) 的语义是"有数据即返回"，所以块
			大小只影响圈数，不影响交互性；逐字节那版每次对话行要跑上千圈。
			"""
			while not stop.is_set():
				try:
					chunk = solution.read(bfsize=4096)
				except Exception:
					return
				if not chunk:
					# The solution said its last word: pass the EOF on to the
					# interactor, whose readline()/cin loop would otherwise
					# wait forever and push the whole dialogue to the deadline.
					try:
						interactor.stdin.close()
					except Exception:
						pass
					return
				collected['solution'].append(chunk)
				try:
					interactor.stdin.write(chunk.encode('utf-8', 'replace'))
					interactor.stdin.flush()
				except Exception:
					return

		def pump_interactor():
			"""interactor -> 选手。"""
			while not stop.is_set():
				try:
					# read1(): at most one low-level read, so a short prompt
					# still comes through immediately; plain read(4096) would
					# block until the full 4096 bytes and deadlock a dialogue
					# whose prompts are shorter than that.
					chunk = interactor.stdout.read1(4096)
				except Exception:
					return
				if not chunk:
					# The interactor judged and left: pass the EOF on to the
					# solution, whose `while (cin >> x)` would otherwise block
					# on stdin until the deadline - and the timed-out branch
					# below then overrode the interactor's exit code, turning
					# every truly accepted interaction into a TLE.
					try:
						solution.close_stdin()
					except Exception:
						pass
					return
				# One decode, used for both the transcript and the pipe.
				decoded = decode_output(chunk)
				collected['interactor'].append(decoded)
				try:
					solution.insert(decoded)
				except Exception:
					return

		def pump_interactor_err():
			"""Drain the interactor's stderr.

			An interactor explains itself there (testlib's quitf writes to
			stderr), and an unread pipe fills up and blocks the interactor
			once the buffer is full - a deadlock that looked like a TLE.
			"""
			while not stop.is_set():
				try:
					chunk = interactor.stderr.read1(4096)
				except Exception:
					return
				if not chunk:
					return
				collected['interactor_err'].append(decode_output(chunk))

		threads = [threading.Thread(target=pump_solution),
				   threading.Thread(target=pump_interactor),
				   threading.Thread(target=pump_interactor_err)]
		for thread in threads:
			thread.daemon = True
			thread.start()

		limit_s = float(time_limit_ms or 10000) / 1000.0
		deadline = started + limit_s + _GRACE_SECONDS
		timed_out = False
		while True:
			if interactor.poll() is not None and solution.is_stopped() is not None:
				break
			if time.monotonic() > deadline:
				timed_out = True
				break
			time.sleep(0.01)

		stop.set()
		runtime = int((time.monotonic() - started) * 1000)
		solution_output = ''.join(collected['solution'])
		interactor_output = ''.join(collected['interactor'])
		interactor_error = ''.join(collected['interactor_err']).strip()

		if timed_out:
			try:
				solution.terminate()
			except Exception:
				pass
			try:
				interactor.kill()
			except Exception:
				pass

		solution_err = solution.get_stderr() if hasattr(solution, 'get_stderr') else ''
		try:
			memory = solution.finish_memory_sampling()
		except Exception:
			memory = None

		code = interactor.poll()
		if timed_out:
			verdict = ('runtime_error'
					   if looks_like_crash(solution_err or solution_output)
					   else 'time_limit_exceed')
		elif code is None:
			verdict = 'unknown_error'
		else:
			verdict = EXIT_CODES.get(code, 'unknown_error')
			if code < 0:
				verdict = 'runtime_error'

		result.update(
			rtcode=code, stdout=solution_output, stderr=solution_err,
			runtime=runtime, memory=memory,
			verdict=get_verdict(verdict),
			interactor_output=interactor_output,
		)
		if verdict in ('wrong_answer', 'presentation_error',
					   'partially_correct', 'unknown_error', 'runtime_error'):
			# The interactor's own message is the only explanation available:
			# prefer its stderr (that is where testlib writes quitf), and fall
			# back to the prompts it sent when it stayed silent.
			result['message'] = (interactor_error or interactor_output.strip())[-1000:]
	except Exception as e:
		result['error'] = '%s: %s' % (type(e).__name__, e)
		result['verdict'] = get_verdict('unknown_error')
	finally:
		stop.set()
		for proc in (solution, interactor):
			if proc is None:
				continue
			try:
				if isinstance(proc, subprocess.Popen):
					proc.kill()
				else:
					proc.close_stderr()
			except Exception:
				pass
		try:
			for name in os.listdir(workdir):
				try:
					os.remove(os.path.join(workdir, name))
				except Exception:
					pass
			os.rmdir(workdir)
		except Exception:
			pass
	return result

"""Algorithm Competition Assistant - 机器速度校准

"本地 TLE、OJ 能过" 是竞赛里最常见的误判：本机比评测机慢（或快）一倍，
本地时限就完全没有参考价值。这里跑一段固定工作量的基准程序，算出本机相对
一个"典型评测机"的速度系数，再把时限换算成"相当于评测机多少毫秒"。

参考值 ``REFERENCE_SCORE`` 是一个**文档化的经验常数**（典型 OJ 上这段循环
每秒约能跑 2.5e8 次），不是精确测量；它只用来把量级对齐，绝对值不必当真。
用户随时可以重新校准，系数存在 ``Packages/User/cph-by-chenkx-machine.json``
（本插件自己的数据文件，不动用户的 settings）。

放在 core/ 是因为根级插件模块之间不允许互相 import（Package Control 审查规则）。
"""

import json
import os
import re
import subprocess
import tempfile
import time

from .cph_i18n import t
from .cph_checker import hidden_startupinfo

#: ops/second of the reference machine. Calibrated so that a modern desktop
#: lands near 1.0; treat the absolute value as an order-of-magnitude anchor.
REFERENCE_SCORE = 2.5e8

#: Loop count of the benchmark. Big enough to swamp process start-up
#: (~10ms) while still finishing in about a second on a fast machine.
BENCH_ITERATIONS = 200000000

BENCH_SOURCE = '''#include <cstdio>
int main() {
    volatile unsigned long long acc = 1;
    unsigned long long x = 123456789ULL;
    for (unsigned long long i = 0; i < %dULL; ++i) {
        x ^= x << 13; x ^= x >> 7; x ^= x << 17;
        acc += x;
    }
    return (int)(acc & 1);
}
''' % BENCH_ITERATIONS

DEFAULT_COMPILE_CMD = 'g++ -O2 -std=c++17 "{src}" -o "{exe}"'

_cache = {}


def store_path():
	"""JSON file holding the measured factor (our own data file)."""
	try:
		import sublime
		base = os.path.join(sublime.packages_path(), 'User')
	except Exception:
		base = tempfile.gettempdir()
	try:
		if not os.path.isdir(base):
			os.makedirs(base)
	except Exception:
		base = tempfile.gettempdir()
	# Deliberately the old data-file name: v2.4.4 renamed the package, not the
	# files a user already has on disk.
	return os.path.join(base, 'cph-by-chenkx-machine.json')


def load_factor():
	"""The stored factor, or None when the machine was never calibrated."""
	if 'factor' in _cache:
		return _cache['factor']
	try:
		with open(store_path(), 'r', encoding='utf-8') as f:
			data = json.load(f)
		factor = float(data.get('factor'))
		_cache['factor'] = factor if factor > 0 else None
	except Exception:
		_cache['factor'] = None
	return _cache['factor']


def save_factor(factor, score=None):
	_cache['factor'] = factor
	try:
		payload = {'factor': factor, 'score': score,
				   'measured': time.strftime('%Y-%m-%d %H:%M:%S')}
		with open(store_path(), 'w', encoding='utf-8') as f:
			json.dump(payload, f, indent=1)
		return True
	except Exception:
		return False


def measure(workdir=None, compile_cmd=DEFAULT_COMPILE_CMD, compiler='g++'):
	"""Compile and run the benchmark. Returns (factor, score, seconds, error)."""
	workdir = workdir or tempfile.mkdtemp(prefix='cph-calibrate-')
	src = os.path.join(workdir, 'bench.cpp')
	exe = os.path.join(workdir, 'bench.exe')
	try:
		with open(src, 'w', encoding='utf-8') as f:
			f.write(BENCH_SOURCE)
	except Exception as e:
		return None, None, None, t('calibrate_write_failed', error=str(e))

	cmd = compile_cmd.replace('{src}', src).replace('{exe}', exe)
	try:
		proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
								stderr=subprocess.STDOUT,
								startupinfo=hidden_startupinfo())
		out = proc.communicate()[0]
	except Exception as e:
		return None, None, None, t('calibrate_compile_failed',
								   compiler=compiler, error=str(e))
	if proc.returncode != 0:
		text = out.decode('utf-8', 'replace') if out else ''
		return None, None, None, t('calibrate_compile_failed',
								   compiler=compiler, error=text[-400:])

	started = time.time()
	try:
		run = subprocess.Popen([exe], stdout=subprocess.DEVNULL,
							   stderr=subprocess.DEVNULL,
							   startupinfo=hidden_startupinfo())
		run.communicate(timeout=60)
	except Exception as e:
		return None, None, None, t('calibrate_run_failed', error=str(e))
	seconds = time.time() - started
	if seconds <= 0:
		return None, None, None, t('calibrate_run_failed', error='no time measured')

	score = float(BENCH_ITERATIONS) / seconds
	factor = score / REFERENCE_SCORE
	save_factor(factor, score)
	_cleanup(workdir)
	return factor, score, seconds, ''


def _cleanup(workdir):
	for name in ('bench.cpp', 'bench.exe'):
		try:
			os.remove(os.path.join(workdir, name))
		except Exception:
			pass
	try:
		os.rmdir(workdir)
	except Exception:
		pass


def adjusted_limit_ms(time_limit_ms):
	"""Local limit -> the limit the judge would effectively give this machine.

	A machine twice as fast as the reference means a local run finishing in
	1000ms corresponds to 2000ms on the judge.
	"""
	factor = load_factor()
	if not factor or not time_limit_ms:
		return None
	return int(round(float(time_limit_ms) * factor))


def label(time_limit_ms=None):
	"""Short status-bar text, or '' when the machine is not calibrated."""
	factor = load_factor()
	if not factor:
		return ''
	if time_limit_ms:
		adjusted = adjusted_limit_ms(time_limit_ms)
		return t('calibrate_label', factor='%.2f' % factor, ms=adjusted)
	return t('calibrate_factor', factor='%.2f' % factor)


def format_report(factor, score, seconds):
	return t('calibrate_report', factor='%.2f' % factor,
			 score='%.2e' % score, seconds='%.2f' % seconds)

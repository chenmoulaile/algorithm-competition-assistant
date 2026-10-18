"""Algorithm Competition Assistant - 编译标准（-std）自动升降档

实测（这台机器，g++ 15.2 MSYS2，day9 的那份题解，含 <bits/stdc++.h>）：
钱几乎全花在**解析头文件**上，而解析时间随标准涨：

    -fsyntax-only      c++11   c++17   c++20   c++23
      只 include bits    1.43s   1.69s   2.34s   3.27s
      整份题解代码        1.11s   1.29s   2.43s   2.53s
    完整编译（含生成/链接）  c++17 1.69s            c++23 3.12s（均值 3.78s）

而他那份代码只用到 C++11 的东西 —— compile_cmd 里写死 -std=c++23，每次 Run
都白付 1.4~2 秒。所以：**从低档开始编，只有编译器自己说"这个要 C++NN 才有"
时才升档**，并把这道题最终编得过的档位记在它自己的 sidecar 里（和 __tests /
.limits / .stress 同一套做法），下一题不会被牵连。

不做预编译头（PCH）：同一台机器实测 c++23 3.12s → 3.46s（更慢），而且 .gch
151 MB、光建它就要 9.8 秒。这条路在这套 MinGW 上是死的。

判"是不是标准不够"只认 GCC 自己的说法（note: ... only available from C++23
onwards / only available with '-std=c++20' / 只有新标准才有的头文件或 std::
名字 / consteval 这类新关键字），**普通拼写错误与语法错误一律不升档**，
免得一真打错就要多等两趟编译才知道错在哪。
"""

import os
import re

from .cph_i18n import t
from .cph_settings import (get_remembered_std, get_settings,
	set_remembered_std)

#: 档位顺序。只列 libstdc++ 里真实可用的写法；c++98/03 不在内（太老，
#: 而且降级到那里会把常见的 C++11/14 代码编坏）。
RUNGS = ('c++11', 'c++14', 'c++17', 'c++20', 'c++23', 'c++26')

#: GCC 的别名写法 -> 档位
_ALIASES = {
	'c++0x': 'c++11', 'c++1y': 'c++14', 'c++1z': 'c++17',
	'c++2a': 'c++20', 'c++2b': 'c++23', 'c++2c': 'c++26',
}

#: -std= / -std / /std: 都当成一个 token 处理（不匹配 "--std="？匹配）
_STD_TOKEN = re.compile(r'(?<![\w/])(-{1,2}std=|/std:)(\S+)')

#: 编译器自己说"需要更新的标准"的措辞
_VERSION_NOTE = re.compile(
	r"only available from \+{0,2}C\+\+(\d\d)|"
	r"only available with '-std=(?:gnu|c)\+\+(\d\dl?)|"
	r"requires '-std=(?:gnu|c)\+\+(\d\dl?)", re.I)

#: 只有新标准才有的头文件（libstdc++ 在旧标准下不提供或报错）
_NEWER_HEADERS = (
	'span', 'concepts', 'version', 'bit', 'numbers', 'syncstream',
	'source_location', 'expected', 'print', 'mdspan', 'generator',
	'flat_map', 'stream', 'stacktrace', 'stdfloat', 'spanstream',
	'text_encoding', 'execution', 'latch', 'barrier', 'semaphore',
)

#: 只有新标准才有的 std:: 名字（出现在 "'X' is not a member of 'std'"）
_NEWER_STD_NAMES = (
	'views', 'ranges', 'expected', 'print', 'println', 'format', 'vformat',
	'to_underlying', 'mdspan', 'flat_map', 'flat_set', 'generator', 'span',
	'midpoint', 'bit_ceil', 'bit_floor', 'bit_width', 'has_single_bit',
	'countl_zero', 'countr_zero', 'rotl', 'rotr', 'construct_at',
	'destroy_at', 'to_address', 'bind_front', 'erase', 'erase_if',
	'make_unique_for_overwrite', 'atomic_ref', 'jthread', 'stop_token',
	'stop_source', 'in_place_vector', 'to_array', 'is_constant_evaluated',
	'seekg', 'starts_with', 'ends_with', 'contains',
)

#: 新标准关键字 / 语法（报错措辞不带版本号，只能认关键字本身）
_NEWER_KEYWORDS = (
	'consteval', 'constinit', 'co_await', 'co_yield', 'co_return',
	'concept', 'explicit object member', 'deduced template parameter',
	'lambda capture of', 'pack indexing', 'using enum',
)

_MISSING_HEADER = re.compile(r"fatal error: (\S+): No such file or directory")
_NOT_A_MEMBER = re.compile(r"'std::([A-Za-z_]+)' is not a member of 'std'|"
                           r"'([A-Za-z_]+)' is not a member of 'std'")


def normalize_std(value):
	"""`gnu++20` / `c++2b` / `20` -> the rung, or None when it is not one."""
	if not value:
		return None
	text = str(value).strip().lower()
	if text in _ALIASES:
		return _ALIASES[text]
	if text.startswith('gnu++'):
		text = 'c++' + text[5:]
	if text in RUNGS:
		return text
	digits = re.sub(r'[^0-9]', '', text)
	if digits in ('11', '14', '17', '20', '23', '26'):
		return 'c++' + digits
	return None


def configured_std(cmd):
	"""The -std the command itself asks for, or None when it says nothing."""
	if not cmd or not isinstance(cmd, str):
		return None
	found = None
	for match in _STD_TOKEN.finditer(cmd):
		found = match.group(2)
	if not found:
		return None
	return normalize_std(found)


def with_std(cmd, std):
	"""The same command with every -std token rewritten to `std`.

	Everything else is left exactly as the user wrote it, output path included:
	the ladder changes a standard, not a build.
	"""
	std = normalize_std(std)
	if not cmd or not isinstance(cmd, str) or not std:
		return cmd

	def replace(match):
		prefix, original = match.group(1), match.group(2)
		if original.lower().startswith('gnu++') or original in ('gnu++2b',):
			return prefix + 'gnu++' + std[3:]
		return prefix + std

	return _STD_TOKEN.sub(replace, cmd)


def enabled(settings=None):
	try:
		settings = settings if settings is not None else get_settings()
		return settings.get('auto_std_switch', True) is not False
	except Exception:
		return True


def floor_std(settings=None):
	try:
		settings = settings if settings is not None else get_settings()
		return normalize_std(settings.get('std_floor', 'c++17')) or 'c++17'
	except Exception:
		return 'c++17'


def rungs(cmd, settings=None):
	"""The standards worth trying for this command, cheapest first.

	Empty when there is nothing to gain: no -std in the command, a standard at
	or below the floor, or a spelling this module does not know.
	"""
	return _rungs(configured_std(cmd), settings)


def _rungs(top, settings=None):
	floor = floor_std(settings)
	if not top or top not in RUNGS or floor not in RUNGS:
		return []
	if RUNGS.index(top) <= RUNGS.index(floor):
		return []
	return list(RUNGS[RUNGS.index(floor):RUNGS.index(top) + 1])


#: What each source file's own command asked for, recorded by apply(). The
#: command handed to escalate() has already been brought down to its rung, so
#: without this the ceiling - the standard the user wrote - would be lost and
#: nothing above the floor could ever be tried.
_ceilings = {}


#: What a brand new file starts on, read off the source itself. This only ever
#: raises the starting rung, and the compiler still decides the truth: guessing
#: high costs a slow parse on a file that did not need it, guessing low costs
#: one failed compile - the hint just removes that failure for the obvious
#: cases (`#include <expected>` is a requirement, not a hint).
_HEADER_RUNG = {}
for _name in ('expected', 'print', 'mdspan', 'generator', 'flat_map',
              'stacktrace', 'stdfloat', 'spanstream', 'text_encoding'):
	_HEADER_RUNG[_name] = 'c++23'
for _name in ('span', 'concepts', 'version', 'bit', 'numbers', 'syncstream',
              'source_location', 'execution', 'barrier', 'latch', 'semaphore'):
	_HEADER_RUNG[_name] = 'c++20'

_ANGLE_INCLUDE = re.compile(r'#\s*include\s*<([A-Za-z0-9_/+.-]+)>')
_STD_IDENT = re.compile(
	r'std::(views|ranges|expected|print|println|format|vformat|to_underlying|'
	r'mdspan|flat_map|flat_set|generator|jthread|stop_token|stop_source|'
	r'atomic_ref|construct_at|destroy_at|to_address|bind_front|erase_if|'
	r'midpoint|bit_ceil|bit_floor|bit_width|has_single_bit|countl_zero|'
	r'countr_zero|to_array|is_constant_evaluated|starts_with|ends_with)\b')
_NEW_KEYWORD = re.compile(r'\b(consteval|constinit|co_await|co_yield)\b')
_MAX_HINT_BYTES = 512 * 1024


def suggested_rung(source_file):
	"""The rung this source clearly needs, or None when nothing says.

	Only angle-bracket includes count, so a local `#include "print.h"` cannot
	push a whole file up to C++23, and header names are matched exactly, so
	`<bitset>` is not `<bit>`.
	"""
	if not source_file:
		return None
	try:
		with open(source_file, 'rb') as f:
			raw = f.read(_MAX_HINT_BYTES)
	except Exception:
		return None
	try:
		text = raw.decode('utf-8', 'replace')
	except Exception:
		return None
	best = None
	for header in _ANGLE_INCLUDE.findall(text):
		rung = _HEADER_RUNG.get(header.split('/')[-1])
		if rung and (best is None or RUNGS.index(rung) > RUNGS.index(best)):
			best = rung
	if _STD_IDENT.search(text) or _NEW_KEYWORD.search(text):
		for rung in ('c++23',):
			if best is None or RUNGS.index(rung) > RUNGS.index(best):
				best = rung
	return best


def apply(cmd, source_file, settings=None):
	"""(command to compile with, rung). The remembered rung wins when it is one
	of the rungs this command could still ask for.

	Order of preference: what this file already compiled with (sidecar), then
	what the source plainly asks for, then the floor.
	"""
	top = configured_std(cmd)
	if top:
		_ceilings[source_file] = top
	if not enabled(settings):
		return (cmd, top)
	candidates = _rungs(top, settings)
	if not candidates:
		return (cmd, top)
	chosen = candidates[0]
	hint = suggested_rung(source_file)
	if hint in candidates:
		chosen = hint
	remembered = get_remembered_std(source_file) if source_file else None
	if remembered in candidates:
		chosen = remembered
	return (with_std(cmd, chosen), chosen)


def requested_std(stderr):
	"""The standard GCC itself names in the failure, if any."""
	best = None
	for match in _VERSION_NOTE.finditer(stderr or ''):
		value = next((g for g in match.groups() if g), '')
		candidate = normalize_std('c++' + value if len(value) == 2
								  else value)
		if candidate and (best is None or RUNGS.index(candidate)
						  > RUNGS.index(best)):
			best = candidate
	return best


def needs_newer_std(stderr):
	"""True when the failure is about the standard, not about the program.

	A typo must never cost two more compiles: 'vectr' is not a member of std
	and no newer keyword appears in that message, so it is reported as it is.
	"""
	text = stderr or ''
	if _VERSION_NOTE.search(text):
		return True
	for header in _MISSING_HEADER.findall(text):
		name = os.path.splitext(header.split('/')[-1])[0]
		if name in _NEWER_HEADERS:
			return True
	for groups in _NOT_A_MEMBER.findall(text):
		if next((g for g in groups if g), '') in _NEWER_STD_NAMES:
			return True
	low = text.lower()
	for word in _NEWER_KEYWORDS:
		if word in low:
			return True
	return False


def escalate(cmd, source_file, stderr, settings=None):
	"""(new command, note) when the failure asks for a newer standard.

	Only ever between the rung that just failed and the standard the user's own
	command asked for: it skips to the one GCC named, remembers it for THIS
	file, and stops where the command stops - if the top rung still fails, the
	program is what is wrong, and its diagnostics must not be hidden behind two
	more compiles.
	"""
	if not enabled(settings):
		return (None, '')
	current = configured_std(cmd)
	if current not in RUNGS:
		return (None, '')
	ceiling = _ceilings.get(source_file)
	if not ceiling or ceiling not in RUNGS or RUNGS.index(ceiling) <= RUNGS.index(current):
		return (None, '')
	if not needs_newer_std(stderr):
		return (None, '')
	above = list(RUNGS[RUNGS.index(current) + 1:RUNGS.index(ceiling) + 1])
	target = requested_std(stderr)
	if target not in above:
		target = above[0]
	set_remembered_std(source_file, target)
	return (with_std(cmd, target),
			t('std_switched', frm=current, to=target))


def reset(source_file):
	"""Forget this file's rung (what force-recompile does)."""
	set_remembered_std(source_file, None)

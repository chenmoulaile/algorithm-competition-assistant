"""
Algorithm Competition Assistant - 环境自检 (doctor)

一条命令把"为什么跑不起来"的常见原因全查一遍，输出到独立视图：
- 每种语言配置的编译器/解释器是否真的在 PATH 里
- Competitive Companion 端口是否被占用
- 测试数据文件路径能否写入
- 设置文件是否可解析、资源（HTML/CSS）能否加载
- 当前文件扩展名是否有对应的 run_settings

排查 issue 时先跑这个，能省掉大半来回。
"""

import os
import re
import sys
import threading

import sublime
import sublime_plugin

from .core.cph_i18n import t
from .core.cph_settings import (get_settings, get_tests_file_path,
                                PACKAGE_NAME, base_name, package_name)
from .core.cph_resources import read_resource
from .core.cph_state import plugin_version

try:
	from .core.cph_settings import is_run_supported_ext
except ImportError:  # pragma: no cover
	def is_run_supported_ext(ext):
		return False


#: {checker} / {interactor} and their directories are substituted by the
#: checker and interactor runners (see core/cph_checker.py), so a command
#: using them is not a typo. Listing only the core placeholders made doctor
#: report every configured SPJ as having an unknown placeholder.
KNOWN_PLACEHOLDERS = {'file', 'source_file', 'source_file_dir', 'file_name',
                      'args', 'extra_sources', 'include_dirs',
                      'checker', 'checker_dir', 'interactor', 'interactor_dir'}

#: No leading word boundary: `go version go1.22.5 ...` glues the number to the
#: tool name, and requiring one made it read the tail ("22.5") of the version.
_VERSION_RE = re.compile(r'(\d+\.\d+(?:\.\d+)?)')

#: How each toolchain actually prints its version. `go` takes a bare
#: `go version`, `fpc` wants `-iV`, and the JDK spelled it `-version` before 9.
#: Trying the wrong spelling and reporting FAIL would call a working compiler
#: broken, so the shipped tools are listed; the generic `--version` / `-version`
#: fallback still covers a user-supplied path.
_VERSION_ARGS = {
	'go': ('version',),
	'fpc': ('-iV',),
	'javac': ('-version',),
	'java': ('-version',),
}

#: Trivial programs the build probe compiles and runs with the user's own
#: commands. Only languages whose hello world needs no naming convention or
#: extra files are probed - a probe template that is wrong for the toolchain
#: would report a false FAIL, which is worse than not checking at all. Those
#: still get the version probe. {cpp} also tests <bits/stdc++.h>, which is what
#: competitive code actually writes and what a bare clang install lacks.
_BUILD_PROBES = {
	'cpp': ('main.cpp',
			'#include <bits/stdc++.h>\nusing namespace std;\n'
			'int main(){ios::sync_with_stdio(0);cin.tie(0);'
			'cout<<"hi"<<endl;return 0;}\n', 'hi', 'iostream'),
	'c': ('main.c',
		  '#include <stdio.h>\nint main(void){puts("hi");return 0;}\n',
		  'hi', 'stdio.h'),
}


def parse_compiler_version(banner):
	"""Version number out of a compiler's own --version text, '' if unreadable."""
	match = _VERSION_RE.search(banner or '')
	return match.group(0) if match else ''


def _startupinfo():
	"""Keep the probe consoles from flashing on Windows."""
	if sublime.platform() != 'windows':
		return None
	import subprocess
	info = subprocess.STARTUPINFO()
	info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
	return info


def _run_capture(args, cwd=None, timeout=90):
	"""Run one probe command. Returns (returncode, decoded output).

	A list runs directly: with shell=True on POSIX the shell gets only the first
	item and the rest become arguments to the shell itself, so `[exe,
	'--version']` silently ran a bare `g++` there and reported FAIL for a working
	toolchain - Windows joins the list, which is why this only showed up on Linux
	and macOS. A string command is the user's own compile line, which may
	legitimately carry shell syntax, so that one keeps a shell.
	"""
	import subprocess
	try:
		proc = subprocess.Popen(args, shell=isinstance(args, str),
							stdout=subprocess.PIPE,
							stderr=subprocess.STDOUT, cwd=cwd,
							startupinfo=_startupinfo())
	except Exception as e:
		return None, '%s' % e
	try:
		out = proc.communicate(timeout=timeout)[0]
	except subprocess.TimeoutExpired:
		try:
			proc.kill()
		except Exception:
			pass
		return None, 'timed out after %ds' % timeout
	if out is None:
		return proc.returncode, ''
	if not isinstance(out, str):
		for enc in ('utf-8', 'gb18030'):
			try:
				out = out.decode(enc)
				break
			except Exception:
				continue
		else:
			out = out.decode('utf-8', 'replace')
	return proc.returncode, (out or '').replace('\r\n', '\n').replace('\r', '\n')


def probe_compiler_version(exe):
	"""(True, 'g++ 15.2.0') when the compiler actually runs.

	Finding the file on PATH is not enough: a build whose runtime DLL is
	missing, a 32/64-bit mismatch and a WSL stub all sit on PATH and refuse to
	execute. Returns (None, reason) when there is nothing to run.
	"""
	if not exe:
		return None, 'n/a'
	if '{' in exe or exe.strip().startswith('-'):
		return None, 'not probed'
	name = os.path.basename(exe).lower()
	for suffix in ('.exe', '.cmd', '.bat'):
		if name.endswith(suffix):
			name = name[:-len(suffix)]
	attempts = []
	if name in _VERSION_ARGS:
		attempts.append([exe] + list(_VERSION_ARGS[name]))
	attempts.append([exe, '--version'])
	bare = [exe, '-version']
	if bare not in attempts:
		attempts.append(bare)
	last = ''
	for args in attempts:
		rc, out = _run_capture(args)
		if rc == 0:
			version = parse_compiler_version(out)
			return True, ('%s %s' % (exe, version)).strip()
		last = last or out or ''
	first = ''
	for line in (last or '').splitlines():
		if line.strip():
			first = line.strip()
			break
	return False, first or 'refused to run'


def probe_build(entry):
	"""Compile and run a trivial program with this language's own commands.

	Returns (ok, detail), or (None, reason) when the language is not probed.
	"""
	exts = [e for e in (entry.get('extensions') or []) if e in _BUILD_PROBES]
	if not exts:
		return None, 'not probed'
	ext = exts[0]
	file_name, source, expected, fallback_header = _BUILD_PROBES[ext]
	compile_cmd = entry.get('compile_cmd')
	run_cmd = entry.get('run_cmd')
	if not compile_cmd or not run_cmd:
		return None, 'no compile/run command'

	import tempfile
	workdir = tempfile.mkdtemp(prefix='cph-doctor-')
	src = os.path.join(workdir, file_name)
	try:
		with open(src, 'w', encoding='utf-8') as handle:
			handle.write(source)
	except Exception as e:
		_rmtree(workdir)
		return False, 'cannot write probe: %s' % e

	values = {
		'file': file_name,
		'source_file': src,
		'source_file_dir': workdir,
		'file_name': os.path.splitext(file_name)[0],
		'args': '',
		'extra_sources': '',
		'include_dirs': '',
	}
	cmd = _expand_cmd(compile_cmd, values)
	rc, out = _run_capture(cmd, cwd=workdir)
	if rc != 0 and ext == 'cpp' and 'bits/stdc++.h' in source:
		# The single header every CP template includes. Without it the user's
		# own solutions will not build, but the toolchain itself is fine, so
		# retry with the portable header and report that distinction instead
		# of a plain FAIL.
		_retry = source.replace('<bits/stdc++.h>', '<%s>' % fallback_header)
		try:
			with open(src, 'w', encoding='utf-8') as handle:
				handle.write(_retry)
		except Exception:
			_rmtree(workdir)
			return False, 'cannot write the retry probe'
		rc, out = _run_capture(cmd, cwd=workdir)
		if rc == 0:
			_rmtree(workdir)
			return False, '%s: <bits/stdc++.h> missing, your CP templates will not build' % entry.get('name', ext)
	if rc != 0:
		first = ''
		for line in (out or '').splitlines():
			if line.strip():
				first = line.strip()
				break
		_rmtree(workdir)
		return False, 'compile failed: %s' % (first or 'exit %s' % rc)

	rc, out = _run_capture(_expand_cmd(run_cmd, values), cwd=workdir)
	_rmtree(workdir)
	if rc != 0:
		return False, 'ran but exited %s' % rc
	if (out or '').strip() != expected:
		return False, 'ran, got %r' % ((out or '').strip()[:40],)
	return True, 'compiles and runs'


def _expand_cmd(cmd, values):
	"""Substitute the known placeholders, drop the unknown ones.

	Mirrors what ProcessManager does at run time (unknown names become ''), so
	the probe exercises the command the plugin would really execute.
	"""
	out = cmd
	for key, value in values.items():
		out = out.replace('{%s}' % key, value)
	return re.sub(r'\{[A-Za-z_][A-Za-z0-9_]*\}', '', out)


def _rmtree(path):
	import shutil
	try:
		shutil.rmtree(path, ignore_errors=True)
	except Exception:
		pass


def _check_helper(exe, needed_for):
	"""Presence of an optional tool a feature depends on."""
	if not exe:
		return None
	import shutil
	found = shutil.which(exe)
	return (bool(found), '%s (%s)' % (exe, needed_for))



def _unknown_placeholders(cmd):
    """Placeholder names in a command that this plugin does not provide.

    A typo such as {file_nmae} is silently substituted with '' at run time,
    which used to leave the user staring at a weird compiler command.
    """
    if not cmd:
        return []
    names = set(re.findall(r'\{([A-Za-z_][A-Za-z0-9_]*)\}', cmd))
    return sorted(n for n in names if n not in KNOWN_PLACEHOLDERS)


def _first_token(cmd):
	"""Executable of a shell command string ('g++ "x.cpp" -o x')."""
	if not cmd:
		return None
	cmd = cmd.strip()
	if cmd.startswith('"'):
		end = cmd.find('"', 1)
		return cmd[1:end] if end > 0 else cmd.strip('"')
	return cmd.split()[0]


def _check_command(cmd):
	exe = _first_token(cmd)
	if not exe:
		return None, 'n/a'
	if '{' in exe:
		# Unresolved placeholder: run_cmd points at the binary the plugin
		# itself builds ({source_file_dir}/{file_name}.exe), so there is
		# nothing to look up. Reporting FAIL here was misinformation.
		return True, '%s (template)' % exe
	if os.path.isabs(exe) or os.sep in exe:
		return (os.path.exists(exe), exe)
	# shutil and socket are only needed by the diagnostics run, not by every
	# Sublime start: together they were ~70 ms of the package's cold import.
	import shutil
	found = shutil.which(exe)
	return (bool(found), found or exe)


def _check_port(port):
	"""True when we can bind the port.

	Returns False both when another program holds it and when this plugin's
	own listener is already running, so report that distinction upstream.
	"""
	import socket
	sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
	try:
		sock.bind(('localhost', port))
		return True
	except OSError:
		return False
	finally:
		sock.close()


def _our_listener_running():
	# Read the shared state from the subpackage: importing a root level
	# plugin module (.cph_companion) is not allowed, Sublime loads each
	# root level .py as an independent plugin.
	from .core.cph_state import is_listener_running
	return is_listener_running()


def _check_tests_path(file_name):
	"""Can we write test data next to the source file? No side effects."""
	path_to_use = get_tests_file_path(file_name)
	if not path_to_use:
		return False, 'n/a'
	directory = os.path.dirname(path_to_use)
	# Do NOT create the directory or write a probe file here: a health check
	# must not leave anything behind.
	if not os.path.isdir(directory):
		parent = os.path.dirname(directory)
		if not os.path.isdir(parent):
			return False, '%s (%s)' % (path_to_use, _t_missing_parent())
		writable = os.access(parent, os.W_OK)
		return writable, '%s (%s)' % (path_to_use, 'dir not created yet' if writable else 'parent not writable')
	writable = os.access(directory, os.W_OK)
	return writable, '%s%s' % (path_to_use, '' if writable else ' (not writable)')


def _t_missing_parent():
	return 'parent directory missing'


class CphDoctorCommand(sublime_plugin.TextCommand):
	"""Check the environment and print a report."""

	def run(self, edit):
		lines = [t('doctor_title'), '=' * 46, '']

		settings = get_settings()
		run_settings = settings.get('run_settings') or []
		# Read early: the per-language checks below resolve helper file paths
		# relative to the source file's directory.
		file_name = self.view.file_name()
		if not run_settings:
			lines.append('[!] ' + t('doctor_no_run_settings'))
		for entry in run_settings:
			name = entry.get('name', '?')
			lines.append('[%s] %s' % (name, t('doctor_languages')))
			for label, key in ((t('doctor_compile'), 'compile_cmd'),
							   (t('doctor_run'), 'run_cmd')):
				cmd = entry.get(key)
				if not cmd:
					lines.append('    ok   %s: (none)' % label)
					continue
				ok, detail = _check_command(cmd)
				lines.append('    %s %s: %s' % ('ok  ' if ok else 'FAIL', label, detail))
				if not ok:
					lines.append('         -> ' + t('doctor_fix_path', name=_first_token(cmd)))
				unknown = _unknown_placeholders(cmd)
				if unknown:
					lines.append('         -> ' + t('doctor_placeholder_unknown',
													names=', '.join(unknown)))
			# A typo in `checker` / `interactor` used to disable the SPJ in
			# silence: the run fell back to plain token comparison, and a
			# correct-but-different answer became a WA with no explanation.
			for key, detail_ok, detail_missing in self._helper_lines(entry, file_name):
				lines.append('    %s %s: %s' % (detail_ok, key, detail_missing))
			lines.append('')

		port = settings.get('companion_port', 12345) or 12345
		free = _check_port(port)
		ours = _our_listener_running()
		if free:
			state = 'ok  '
		elif ours:
			# Our own listener holds the port - that is the expected state.
			state = 'ok  '
		else:
			state = 'WARN'
		lines.append('    %s %s: %s%s' % (state, t('doctor_port'), port,
										  ' (listening)' if ours else ''))
		if not free and not ours:
			lines.append('         -> ' + t('doctor_port_busy'))
		lines.append('')

		if file_name:
			ok, detail = _check_tests_path(file_name)
			lines.append('    %s %s: %s' % ('ok  ' if ok else 'FAIL',
											t('doctor_tests_path'), detail))
			ext = os.path.splitext(file_name)[1][1:]
			supported = is_run_supported_ext(ext)
			lines.append('    %s %s: .%s' % ('ok  ' if supported else 'FAIL',
											 t('doctor_extension'), ext))
		else:
			lines.append('    --   %s' % t('doctor_no_file'))
		lines.append('')

		res = read_resource('Highlight/test_config.html')
		lines.append('    %s %s' % ('ok  ' if res else 'FAIL', t('doctor_resources')))
		lines.append('    %s settings: %s' % ('ok  ' if run_settings else 'FAIL',
											  t('doctor_settings')))

		# Optional tools a feature silently degrades without. gdb is what turns
		# a libstdc++ abort into the user's own line; clang-format backs
		# auto_format_on_save.
		for exe, why in (('gdb', t('doctor_gdb')),
						 ('clang-format', t('doctor_clang_format'))):
			found = _check_helper(exe, why)
			if found is None:
				continue
			lines.append('    %s %s: %s' % ('ok  ' if found[0] else 'WARN',
											why, found[1]))
			if not found[0]:
				lines.append('         -> ' + t('doctor_helper_missing', exe=exe))
		lines.append('')
		lines.append(t('doctor_footer'))

		# Copy-pasteable Markdown block: the fastest path from "it is broken"
		# to a report someone else can act on.
		lines.append('')
		lines.append(t('doctor_markdown_hint'))
		lines.append('')
		lines.append('```markdown')
		lines.append('### %s doctor report' % PACKAGE_NAME)
		lines.append('')
		lines.append('- Sublime Text: build %s' % sublime.version())
		# Which build this host really loaded, and where it lives: the two
		# lines that settle most reports without a single follow-up question.
		# Sublime does not hot reload .py files, so a verdict can easily come
		# from an older install than the source the user is reading.
		lines.append('- Plugin: %s' % (plugin_version() or 'unknown'))
		# Both names, because the difference is the bug: installed as a zip the
		# folder carries a .sublime-package suffix that must NOT appear in a
		# resource path, and every "could not be opened" report so far came from
		# exactly that confusion.
		lines.append('- Package folder: %s' % base_name)
		lines.append('- Package name (resources): %s' % package_name)
		lines.append('- Installed as: %s' % ('.sublime-package zip'
											 if base_name.endswith('.sublime-package')
											 else 'loose folder'))
		lines.append('- Platform: %s (%s)' % (sublime.platform(), sys.platform))
		lines.append('- Python: %s' % sys.version.split()[0])
		lines.append('- companion_port: %s' % port)
		for entry in run_settings:
			lines.append('')
			lines.append('**%s** (`%s`)' % (entry.get('name', '?'),
											 ', '.join(entry.get('extensions') or [])))
			for key in ('compile_cmd', 'run_cmd'):
				cmd = entry.get(key)
				if cmd:
					lines.append('- %s: `%s`' % (key, cmd))
			for key in ('time_limit_ms', 'memory_limit_mb'):
				if entry.get(key):
					lines.append('- %s: %s' % (key, entry.get(key)))
		lines.append('')
		lines.append('```')

		report = '\n'.join(lines)
		window = self.view.window()
		view = window.new_file()
		view.set_name('%s - doctor' % PACKAGE_NAME)
		view.set_scratch(True)
		view.settings().set('word_wrap', False)
		view.run_command('cph_test_detail_view', {'text': report})
		window.focus_view(view)
		print(report)

		# A probe compiles and runs a real program per language: seconds each on
		# a cold toolchain. Sublime shares ONE async worker between every plugin,
		# so running them there would stall auto-run-on-save and everyone else
		# for as long as the probes take - the same mistake the run panel pump
		# used to make. They get their own thread; the report is already on
		# screen and the results stream in.
		probe = threading.Thread(target=self._probe_tools,
								 args=(view, run_settings))
		probe.daemon = True
		probe.start()

	def _emit(self, view, lines):
		"""Append probe output on the main thread, where views live."""
		text = '\n'.join(lines)

		def append():
			if view.is_valid():
				view.run_command('append', {'characters': text + '\n'})

		sublime.set_timeout(append, 0)

	def _helper_lines(self, entry, file_name):
		"""Status of the optional files a language entry points at.

		A typo in `checker` used to disable the SPJ silently: the run fell back
		to plain token comparison and a correct-but-different answer became a WA
		with nothing to explain it.
		"""
		out = []
		base = os.path.dirname(file_name) if file_name else ''
		for key in ('checker', 'interactor'):
			configured = (entry.get(key) or '').strip()
			if not configured:
				continue
			full = configured if os.path.isabs(configured) \
				else os.path.join(base, configured)
			exists = os.path.exists(full)
			label = t('doctor_checker') if key == 'checker' else t('doctor_interactor')
			out.append(('%s (%s)' % (key, label),
						'ok  ' if exists else 'FAIL',
						full if exists
						else '%s (%s)' % (full, t('doctor_missing_file'))))
		return out

	def _probe_tools(self, view, run_settings):
		"""Version and build probe for every configured language."""
		out = ['', t('doctor_probe_title'), '-' * 46]
		for entry in run_settings:
			name = entry.get('name', '?')
			# One entry must not take the whole section down: a toolchain that
			# makes the probe itself raise (a locked temp dir, an unreadable
			# setting) would otherwise end the loop silently, because this runs
			# on a worker thread where an exception goes no further than the
			# console.
			try:
				out.append(self._probe_entry(entry, name))
			except Exception as e:
				out.append('    WARN %s: probe failed: %s: %s'
						   % (name, type(e).__name__, e))
		out.append('')
		self._emit(view, out)

	def _probe_entry(self, entry, name):
		exe = _first_token(entry.get('compile_cmd'))
		if not exe:
			exe = _first_token(entry.get('run_cmd'))
		ok, detail = probe_compiler_version(exe)
		if ok is None:
			return '    --   %s: %s' % (name, detail)
		if not ok:
			# On PATH but refuses to execute: the missing-DLL, wrong-bitness
			# and blocked-by-antivirus class that which() cannot see.
			return '    FAIL %s: %s %s' % (name, exe, detail)
		build_ok, build_detail = probe_build(entry)
		if build_ok is None:
			return '    ok   %s: %s (%s)' % (name, detail, build_detail)
		if build_ok:
			return '    ok   %s: %s; %s' % (name, detail, build_detail)
		return '    FAIL %s: %s; %s' % (name, detail, build_detail)

	def is_enabled(self):
		return True

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
import shutil
import socket
import sys

import sublime
import sublime_plugin

from .core.cph_i18n import t
from .core.cph_settings import (get_settings, get_tests_file_path,
                                PACKAGE_NAME)
from .core.cph_resources import read_resource

try:
	from .core.cph_settings import is_run_supported_ext
except ImportError:  # pragma: no cover
	def is_run_supported_ext(ext):
		return False


KNOWN_PLACEHOLDERS = {'file', 'source_file', 'source_file_dir', 'file_name',
                      'args', 'extra_sources', 'include_dirs'}


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
	found = shutil.which(exe)
	return (bool(found), found or exe)


def _check_port(port):
	"""True when we can bind the port.

	Returns False both when another program holds it and when this plugin's
	own listener is already running, so report that distinction upstream.
	"""
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

		file_name = self.view.file_name()
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

	def is_enabled(self):
		return True

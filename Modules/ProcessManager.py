from os.path import dirname
from os import path
from subprocess import Popen, PIPE
import os
import re
import sys
import subprocess
import shlex
import signal
import sublime
import tempfile
import threading
import time
import codecs
import locale

from .memprobe import MemorySampler, bytes_to_mb, sample_memory_bytes
from .build_artifact import output_path_from_compile_cmd, resolve_artifact, retarget_command
from ..core.cph_i18n import t
from ..core import cph_state
from ..core.cph_build_mode import get_mode as _build_mode, transform as _apply_build_mode
from ..core.cph_std_ladder import apply as _std_apply, escalate as _std_escalate
from ..core.cph_gdb_trace import (find_gdb as _find_gdb, find_crash as _find_crash,
                                   replay as _replay_under_gdb,
                                   names_source as _names_source)

#: How many bytes read() fetches from the pipe in one syscall when the
#: caller asks for a small chunk (byte-at-a-time sync mode, interactor).
#: One syscall then feeds the next 65536 calls instead of one syscall per
#: byte, which was the slow path described in 问题2.
_READ_CHUNK = 65536

#: Every -fsanitize flag, value included (the value may be a comma list, and
#: the trap form is a second flag: `-fsanitize=undefined
#: -fsanitize-trap=undefined`). Matched with \S* so both are removed whole -
#: r'-fsanitize=\S+' used to leave the trap half behind as an orphan, which
#: still compiles but silently turns undefined behaviour into ud2.
_SANITIZE_FLAG = re.compile(r'-fsanitize\S*')


def _hidden_startupinfo():
	"""STARTUPINFO that keeps helper consoles from flashing on Windows."""
	if sublime.platform() != 'windows':
		return None
	si = subprocess.STARTUPINFO()
	si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
	return si


def _sanitizer_fallback_note():
	"""One-line explanation shown when a sanitizer build had to be retried.

	Since debug mode's own default pair (-fsanitize=undefined
	-fsanitize-trap=undefined) needs no runtime library, this note now only
	fires for a sanitizer the user wrote into compile_cmd themselves - hence
	the hint line, which says so instead of leaving them to wonder why the
	plugin's own debug mode suddenly lost its sanitizer.
	"""
	return '[Algorithm Competition Assistant] %s %s\n' % (
		t('sanitizer_fallback'), t('sanitizer_fallback_hint'))


_SHELL_METACHARS = set('|&;<>()`$\n*?[]{}~')


class _LenientFormat(dict):
	"""Format mapping that renders unknown placeholders as ''.

	`str.format` raises KeyError for a missing key, which used to abort the
	whole run with a cryptic "compile failed: 'extra_sources'".
	"""

	def __init__(self, values):
		dict.__init__(self, values)
		self.unknown = set()

	def __missing__(self, key):
		self.unknown.add(key)
		return ''


def _needs_shell(cmd):
	"""True when a command relies on shell features (pipes, &&, globs...).

	Characters inside quotes (i.e. a plain path with spaces) do not count.
	"""
	in_single = False
	in_double = False
	for ch in cmd:
		if ch == "'" and not in_double:
			in_single = not in_single
		elif ch == '"' and not in_single:
			in_double = not in_double
		elif not in_single and not in_double and ch in _SHELL_METACHARS:
			return True
	return False


def _decode_output(data, errors='replace'):
	"""Decode subprocess text: UTF-8 first, then the locale encoding.

	Chinese Windows reports cp936, so g++'s localized diagnostics used to
	come out as garbage when only UTF-8 was attempted. Newlines are also
	normalized: a raw CR renders as '<0x0d>' inside the test panel.
	"""
	if data is None:
		return ''
	if not isinstance(data, str):
		text = None
		for enc in ('utf-8', locale.getpreferredencoding(False)):
			try:
				text = data.decode(enc)
				break
			except (UnicodeDecodeError, LookupError, TypeError):
				continue
		if text is None:
			text = data.decode('utf-8', errors)
	else:
		text = data
	return text.replace('\r\n', '\n').replace('\r', '\n')


class ProcessManager(object):
	def __init__(self, file, syntax, run_settings=None):

		super(ProcessManager, self).__init__()
		self.syntax = syntax
		self.file = file
		self.is_run = False
		# Keys this manager registered in cph_state (run + compile), so it can
		# drop them itself once the child is reaped.
		self._live_keys = set()
		# How the next run is fed and collected: a stdin pipe (the default, and
		# what every online judge's "standard input" problem uses) or named files
		# (NOI / CSP-S style, where the program itself opens add.in). See
		# set_io_spec().
		self.io_spec = {'mode': 'stdin', 'input': '', 'output': ''}
		self.io_input_text = ''
		self.io_dir = None
		# Output size cap (the `max_output_bytes` setting, 0 = unlimited).
		# Enforced here as well as in the panel because the parallel runner and
		# the file-IO read bypassed the panel's clamp entirely: one program that
		# prints without end filled the view, the test store and RAM - precisely
		# the hang this setting exists to prevent.
		self.output_limit = 0
		self.output_taken = 0
		self.output_capped = False
		self.test_counter = 0
		self.write = self.insert
		self.run = self.run_file
		self.run_settings = run_settings
		self.file_name = path.splitext(path.split(file)[1])[0]

		# When enabled, stderr (e.g. cerr debug output) is captured separately
		# so it never mixes into stdout and is ignored when comparing answers
		self.separate_stderr = False
		self.stderr_file = None

		# Set to True when the process was killed by the plugin (manual stop
		# or TLE watchdog) so the verdict logic can tell it apart from a
		# genuine non-zero exit of the program itself
		self.terminated = False

		# Set when a compile had to be retried at a higher -std, so the
		# caller knows the artifact it just built is not described by the
		# fingerprint it took before the compiler ran.
		self.std_escalated = False

		# Extract time/memory limits from run_settings
		self.time_limit_ms = None
		self.memory_limit_mb = None
		if run_settings:
			file_ext = path.splitext(self.file)[1][1:]
			for x in run_settings:
				if file_ext in x['extensions']:
					self.time_limit_ms = x.get('time_limit_ms')
					self.memory_limit_mb = x.get('memory_limit_mb')
					break

		self.time_limit_override = None
		self.memory_limit_override = None

		# Peak memory sampling (makes the MLE verdict real instead of dead code)
		self.pid = None
		self._mem_sampler = None
		self.peak_memory_mb = None

		# Run clock (see runtime_ms): spawned when, exited when. Both stay
		# None until a process actually exists / is actually seen gone.
		self.spawn_time = None
		self.exit_time = None

		self.pgid = None
		self.stdin_closed = False
		self.unknown_placeholders = set()

		# Incremental UTF-8 decoder for the binary stdout pipe (recreated on
		# every run in run_file()).
		self._out_decoder = codecs.getincrementaldecoder('utf-8')('replace')
		# Set while a CR is waiting for the next chunk, so a CRLF split
		# across two reads is still translated into a single newline.
		self._pending_cr = False

		# Small-read buffer (see read()): bytes fetched from the pipe but
		# not yet handed to the caller (consumed through _read_pos), and
		# whether the pipe hit EOF. Also reset in run_file() for every new
		# process.
		self._read_buffer = b''
		self._read_pos = 0
		self._read_eof = False

	def set_time_limit(self, time_ms):
		self.time_limit_override = time_ms

	def set_memory_limit(self, mem_mb):
		self.memory_limit_override = mem_mb

	def set_separate_stderr(self, separate=True):
		self.separate_stderr = separate

	def get_stderr(self):
		"""Return captured stderr content (only in separate_stderr mode)."""
		if self.stderr_file is not None:
			try:
				self.stderr_file.seek(0)
				data = self.stderr_file.read()
				return _decode_output(data)
			except Exception:
				return ''
		return ''

	def close_stderr(self):
		if self.stderr_file is not None:
			try:
				self.stderr_file.close()
			except Exception:
				pass
			self.stderr_file = None

	def get_time_limit_ms(self):
		if self.time_limit_override is not None:
			return self.time_limit_override
		return self.time_limit_ms

	def get_memory_limit_mb(self):
		if self.memory_limit_override is not None:
			return self.memory_limit_override
		return self.memory_limit_mb

	def format_command(self, cmd, args=''):
		file = path.split(self.file)[1]
		values = _LenientFormat({
			'file': file,
			'source_file': self.file,
			'source_file_dir': path.dirname(self.file),
			'file_name': self.file_name,
			'args': args,
		})
		out = cmd.format_map(values)
		if values.unknown:
			# Never abort a run because of a placeholder typo or a command
			# written for a newer version: substitute '' and say so. The set
			# is kept so doctor and the compile panel can report it too.
			unknown = getattr(self, 'unknown_placeholders', None)
			if unknown is None:
				unknown = self.unknown_placeholders = set()
			unknown.update(values.unknown)
			print('[Algorithm Competition Assistant] unknown placeholder(s) in command: %s'
				  % ', '.join(sorted(values.unknown)))
		return out

	def get_lang_entry(self):
		"""Return the run_settings entry matching this file's extension."""
		opt = self.run_settings
		if not opt:
			return None
		file_ext = path.splitext(self.file)[1][1:]
		for x in opt:
			if file_ext in x['extensions']:
				return x
		return None

	def get_extra_sources(self, entry=None):
		"""Files matched by the 'extra_sources' glob patterns (multi-file
		problems). Patterns are relative to the source file directory."""
		if entry is None:
			entry = self.get_lang_entry()
		if not entry:
			return []
		import glob
		src_dir = path.dirname(self.file)
		found = []
		main_abs = path.abspath(self.file)
		for pat in (entry.get('extra_sources') or []):
			pat = self.format_command(pat)
			full = pat if path.isabs(pat) else path.join(src_dir, pat)
			for f in sorted(glob.glob(full)):
				if path.abspath(f) == main_abs:
					continue
				if f not in found:
					found.append(f)
		return found

	def get_include_dirs(self, entry=None):
		"""Extra -I directories from the 'include_dirs' setting."""
		if entry is None:
			entry = self.get_lang_entry()
		if not entry:
			return []
		src_dir = path.dirname(self.file)
		out = []
		for d in (entry.get('include_dirs') or []):
			d = self.format_command(d)
			if not path.isabs(d):
				d = path.join(src_dir, d)
			out.append(d)
		return out

	def get_run_entry(self):
		"""The run_settings entry that applies to this file, or None.

		Used by the optional features that live next to the run itself
		(checker / interactor / subtasks) so they read the same entry the
		compiler and runner do.
		"""
		ext = path.splitext(self.file)[1][1:]
		for entry in (self.run_settings or []):
			if ext in (entry.get('extensions') or []):
				return entry
		return None

	def get_compile_inputs(self):
		"""All files a compile depends on (for the compile cache)."""
		files = [self.file]
		try:
			files.extend(self.get_extra_sources())
		except Exception:
			pass
		return files

	def _expand_optional(self, cmd, entry):
		"""Expand the optional multi-file placeholders.

		MUST run before format_command(): they are not keys of the format
		mapping, so str.format() would raise KeyError on them. The shipped
		default C++ compile_cmd contains both, which made every fresh install
		fail with "compile failed: 'extra_sources'".
		"""
		if '{extra_sources}' in cmd:
			extra = self.get_extra_sources(entry)
			cmd = cmd.replace('{extra_sources}',
							  ' '.join('"%s"' % f for f in extra))
		if '{include_dirs}' in cmd:
			inc = self.get_include_dirs(entry)
			cmd = cmd.replace('{include_dirs}',
							  ' '.join('-I "%s"' % d for d in inc))
		return cmd

	def get_compile_cmd(self):
		opt = self.run_settings
		file_ext = path.splitext(self.file)[1][1:]
		for x in opt:
			if file_ext in x['extensions']:
				if x['compile_cmd'] is None:
					return None
				cmd = self.format_command(self._expand_optional(x['compile_cmd'], x))
				# Debug / Release switch (see core/cph_build_mode). Applied
				# here so every consumer - the run itself, the compile cache
				# fingerprint and doctor - sees the same command.
				cmd = _apply_build_mode(cmd, _build_mode(self.file))
				# -std ladder (see core/cph_std_ladder): the command carries the
				# standard this file has proven it needs, which is usually the
				# cheap one. Also resolved here on purpose: the compile cache
				# fingerprints the command, so a rung change re-compiles.
				return _std_apply(cmd, self.file)[0]
		else:
			return -1

	def artifact_paths(self, cmd=None):
		"""(expected, actual) paths for the artifact of this compile.

		`expected` is what the -o argument asked for, `actual` is the file
		that is really on disk (None when the scan found nothing).
		"""
		if cmd is None:
			cmd = self.get_compile_cmd()
		wanted = output_path_from_compile_cmd(cmd)
		if not wanted:
			return None, None
		src_dir = os.path.split(self.file)[0]
		if not path.isabs(wanted):
			wanted = os.path.join(src_dir, wanted)
		return wanted, resolve_artifact(wanted, src_dir)

	def _artifact_note(self, cmd):
		"""One line when the compiled binary is not where we asked for it.

		Without this the first run either succeeded silently (we retarget the
		command) or ended in a bare 'file not found' after the compiler had
		already reported success.
		"""
		wanted, actual = self.artifact_paths(cmd)
		if wanted is None:
			return ''
		if actual is None:
			return ('[Algorithm Competition Assistant] the compiler exited successfully but no '
					'%s was produced\n' % path.basename(wanted))
		if actual != wanted:
			return ('[Algorithm Competition Assistant] the binary was written as %s (the '
					'compiler did not use the -o name)\n' % path.basename(actual))
		return ''

	def program_path(self):
		"""The binary this compile produced, or None.

		`artifact_paths()` is the authoritative answer (it already copes with
		a non-ASCII `-o` name written through the ANSI codepage); the run
		command is the fallback for toolchains without an `-o` at all.
		"""
		try:
			wanted, actual = self.artifact_paths()
		except Exception:
			wanted, actual = None, None
		if actual:
			return actual
		if wanted:
			return wanted
		try:
			cmd = self.get_run_cmd('')
		except Exception:
			return None
		if not isinstance(cmd, str) or not cmd:
			return None
		token = cmd.strip().split(' ')[0].strip('"')
		return token if path.isfile(token) else None

	def locate_crash(self, source_file=None, stdin_bytes=None, timeout=5.0):
		"""(file, line) of the user's own frame when the program died, else None.

		Runs the compiled binary once more under gdb (see
		core/cph_gdb_trace). Used in two places, both of which used to point
		at libstdc++ internals or at nothing at all:

		* an abort / segfault whose own output only names a header
		  (``stl_vector.h:1263``) - the frame the user needs is theirs.
		* a run the watchdog killed at the time limit. On Windows an aborting
		  program is already dead at t=0 but is only reaped ~4.5s later (WER),
		  so the assertion text never reaches the plugin and the honest
		  verdict was lost.

		Every failure path returns None: no gdb, no binary, no symbol info -
		the caller keeps whatever verdict it had.
		"""
		try:
			program = self.program_path()
		except Exception:
			program = None
		if not program:
			return None
		gdb = _find_gdb()
		if not gdb:
			return None
		try:
			log_path = path.join(tempfile.gettempdir(),
								 'cph-gdb-%d.log' % os.getpid())
			_out, log_text = _replay_under_gdb(
				gdb, program, log_path,
				stdin_bytes=stdin_bytes, timeout=timeout,
				cwd=path.split(self.file)[0])
			return _find_crash(log_text, source_file or self.file)
		except Exception:
			return None

	def get_run_cmd(self, args):
		opt = self.run_settings
		file_ext = path.splitext(self.file)[1][1:]
		for x in opt:
			if file_ext in x['extensions']:
				if x['run_cmd'] is None:
					return None
				cmd = self.format_command(x['run_cmd'], args=args)
				# The compiler does not always write the name we asked for
				# (non-ASCII -o names go through the ANSI codepage on
				# Windows), so point the command at the file that is really
				# there instead of failing with "file not found".
				return retarget_command(cmd, os.path.split(self.file)[0])
		else:
			return -1

	def _track_live(self, proc, what):
		"""Register one child so plugin_unloaded can reap it.

		Sublime does not wait for this process when the package unloads (an
		upgrade, a .py edit on a folder install, a restart that is not a
		quit): the program and any compiler it spawned keep running, and on
		Windows the orphan holds its own .exe open - the next compile then
		fails with "cannot open output file: Permission denied" without
		naming the process that is holding it.
		"""
		if proc is None:
			return None
		key = '%s:%s:%s' % (what, id(self), getattr(proc, 'pid', '?'))
		try:
			self._live_keys.add(key)
			cph_state.register_live(
				key,
				lambda p=proc: p.poll() is None,
				lambda p=proc, s=self: s._kill_tree(p))
		except Exception as e:
			print('[Algorithm Competition Assistant] child process not tracked: %s' % e)
		return key

	def _untrack_live(self):
		for key in list(getattr(self, '_live_keys', ()) or ()):
			try:
				cph_state.unregister_live(key)
			except Exception:
				pass
			try:
				self._live_keys.discard(key)
			except Exception:
				pass

	def _kill_tree(self, p):
		"""Kill a shell-spawned child *and* whatever that shell started.

		Compiles always go through a shell, so p.kill() alone reaped only sh /
		cmd.exe and left a live g++ holding the output file open (Windows) or
		still burning CPU (POSIX). On POSIX the child leads its own session
		(setsid below), so a group kill reaches it.
		"""
		if sublime.platform() == 'windows':
			try:
				subprocess.Popen(
					['taskkill', '/F', '/T', '/PID', str(p.pid)],
					stdout=subprocess.DEVNULL,
					stderr=subprocess.DEVNULL,
					startupinfo=_hidden_startupinfo()
				).wait(timeout=3)
				return
			except Exception:
				pass
		else:
			try:
				os.killpg(os.getpgid(p.pid), signal.SIGKILL)
				return
			except Exception:
				pass
		try:
			p.kill()
		except Exception:
			pass

	def _popen_compile(self, cmd):
		"""Run one compile command; return (returncode, decoded output)."""
		PIPE = subprocess.PIPE
		# Windows: hide console window to avoid flashing
		startupinfo = None
		preexec_fn = None
		if sublime.platform() == 'windows':
			startupinfo = _hidden_startupinfo()
		else:
			# Own session so _kill_tree() can take the shell and the compiler
			# down together instead of orphaning the compiler.
			preexec_fn = os.setsid
		# Same rule the run path applies: a shell only when the command needs
		# one. Quoted paths (the common compile command) still take the shell;
		# a bare `g++ -O2 main.cpp` no longer drags one in, which keeps the
		# compile and the run path from disagreeing about the same command.
		use_shell = _needs_shell(cmd)
		if not use_shell:
			try:
				cmd = shlex.split(cmd)
			except Exception:
				use_shell = True
		p = subprocess.Popen(
			cmd,
			shell=use_shell,
			stdin=None,
			stdout=PIPE,
			stderr=subprocess.STDOUT,
			cwd=os.path.split(self.file)[0],
			startupinfo=startupinfo,
			preexec_fn=preexec_fn
		)
		# A compile the user walked away from is still a live g++ holding the
		# output file open (see _track_live).
		self._track_live(p, 'compile')
		# Timeout so a hanging compiler doesn't freeze the plugin forever
		try:
			compile_result = _decode_output(p.communicate(timeout=30)[0])
		except subprocess.TimeoutExpired:
			self._kill_tree(p)
			return (1, '[Algorithm Competition Assistant] compile timed out after 30s\n(cmd: %s)' % cmd)
		unknown = sorted(getattr(self, 'unknown_placeholders', ()) or ())
		if unknown:
			# A typo like {file_nmae} silently becomes '' and the user just
			# sees a weird command; say which name was ignored.
			compile_result = ('[Algorithm Competition Assistant] ignored unknown placeholder(s): %s\n' % ', '.join(unknown)) + compile_result
		return (p.returncode, compile_result)

	def compile(self, wait_close=True):
		cmd = self.get_compile_cmd()
		if cmd == -1:
			return (1, '[Algorithm Competition Assistant] no compile command configured for this file extension')
		if cmd is not None:
			try:
				returncode, compile_result = self._popen_compile(cmd)
				if returncode != 0 and isinstance(cmd, str) \
						and '-fsanitize' in cmd:
					# The sanitizer runtimes are not part of every toolchain
					# (MSYS2 mingw gcc 15.2 fails to link with
					# "ld.exe: cannot find -lubsan/-lasan"), which made the
					# debug build mode a dead mode on those machines. Drop
					# the -fsanitize flags and retry once, and say so - the
					# rest of the command (e.g. -g, -fno-omit-frame-pointer)
					# links fine everywhere.
					fallback = _SANITIZE_FLAG.sub(' ', cmd)
					if fallback != cmd:
						rc2, out2 = self._popen_compile(fallback)
						note = _sanitizer_fallback_note()
						if rc2 == 0:
							returncode, compile_result = rc2, note + out2
						else:
							returncode = rc2
							compile_result = (note + compile_result.rstrip()
											  + '\n' + out2)
						cmd = fallback
				if returncode != 0 and isinstance(cmd, str):
					# The other failure that is not the user's fault: the cheap -std the
					# ladder picked cannot see a construct that only exists in a newer
					# standard. Retry at the rung the compiler itself named - and only
					# then, so a plain typo still gets its own diagnostics on the first
					# attempt, which is what makes the fast default safe.
					upgrade, note = _std_escalate(cmd, self.file, compile_result)
					if upgrade and upgrade != cmd:
						rc2, out2 = self._popen_compile(upgrade)
						if rc2 == 0:
							returncode, compile_result = rc2, note + out2
							# the artifact on disk belongs to the newer standard now, so
							# the compile cache has to be re-fingerprinted
							self.std_escalated = True
						else:
							returncode = rc2
							compile_result = (compile_result.rstrip() + '\n' + note + out2)
						cmd = upgrade

				if returncode == 0:
					compile_result = compile_result + self._artifact_note(cmd)
				return (returncode, compile_result)
			except Exception as e:
				return (1, '[Algorithm Competition Assistant] failed to run compile command: %s\n(cmd: %s)' % (e, cmd))

	def set_io_spec(self, spec, input_text=None):
		"""Choose between a stdin run and a named-file run for the next run().

		File IO is how NOI / CSP-S problems are judged: the program itself opens
		`add.in` and writes `add.out`. The plugin cannot feed that through a
		pipe, so the sample has to exist as a file the program will find.
		"""
		mode = (spec or {}).get('mode') or 'stdin'
		self.io_spec = {'mode': mode if mode in ('stdin', 'files') else 'stdin',
						'input': (spec or {}).get('input') or '',
						'output': (spec or {}).get('output') or ''}
		if input_text is not None:
			self.io_input_text = input_text

	def set_io_input(self, input_text):
		"""The sample the next file-IO run puts into the input file."""
		self.io_input_text = input_text or ''

	def is_io_files(self):
		return (getattr(self, 'io_spec', None) or {}).get('mode') == 'files'

	def _io_dir_for(self):
		"""The scratch directory a file-IO run reads and writes inside, or None.

		It is never the problem folder: a contestant who downloaded the real data
		has an `add.in` sitting there, and overwriting it with the sample being
		judged would quietly corrupt the file they are testing against. The
		program is started inside this directory instead, so its `freopen` finds
		what the judge would have given it.
		"""
		if not self.is_io_files():
			return None
		root = tempfile.mkdtemp(prefix='cph-io-')
		cph_state.register_temp_dir(root)
		self.io_dir = root
		input_name = self.io_spec.get('input') or 'data.in'
		# An empty file is written on purpose: a judge always hands the program a
		# file, so freopen must not fail just because this test point is empty.
		try:
			with open(os.path.join(root, input_name), 'wb') as f:
				f.write((getattr(self, 'io_input_text', '') or '').encode('utf-8'))
		except Exception as e:
			print('[Algorithm Competition Assistant] could not write %s: %s'
				  % (input_name, e))
		return root

	def release_io_dir(self):
		root = getattr(self, 'io_dir', None)
		if not root:
			return
		self.io_dir = None
		cph_state.unregister_temp_dir(root)
		try:
			import shutil
			shutil.rmtree(root, ignore_errors=True)
		except Exception as e:
			print('[Algorithm Competition Assistant] could not remove %s: %s'
				  % (root, e))

	def io_output(self):
		"""(text, existed) for the file the program was told to write.

		existed is False when the program never created it. That is not the same
		as an empty output: it means the name in the statement and the name in the
		code disagree - `freopen` missing, or `.out` where the problem said `.ans`
		- and judging it as "printed nothing" would send the user hunting a wrong
		algorithm for a typo in a string literal.
		"""
		if not self.is_io_files():
			return ('', False)
		output_name = self.io_spec.get('output') or ''
		root = getattr(self, 'io_dir', None)
		if not output_name or not root:
			return ('', False)
		target = os.path.join(root, output_name)
		try:
			if not os.path.exists(target):
				return ('', False)
			with open(target, 'rb') as f:
				if self.output_limit > 0:
					# Read one byte past the cap: that is how "the program wrote
					# more than we keep" becomes a fact instead of a guess. A
					# runaway freopen to a 2 GB add.out would otherwise be read
					# into memory whole, which is the exact failure the cap
					# exists to stop.
					data = f.read(self.output_limit + 1)
					if len(data) > self.output_limit:
						self.output_capped = True
						data = data[:self.output_limit]
						self.output_taken = self.output_limit
				else:
					data = f.read()
		except Exception as e:
			print('[Algorithm Competition Assistant] could not read %s: %s' % (target, e))
			return ('', False)
		# Decoded exactly like the pipe: bytes in, UTF-8 out, undecodable bytes
		# replaced rather than raising in the middle of a verdict.
		return (_decode_output(data), True)

	def collect_output(self):
		"""(text, note): the program's answer, however this run produced it.

		stdin runs answer on stdout. A file-IO run answers in the file it was
		told to write, and stdout is whatever debug printing it left behind - so
		the pipe is drained (a full pipe would block the program) and the file
		is what gets judged. When the file never appeared the note explains that
		in preference to a blank answer reading like a wrong algorithm.
		"""
		if not self.is_io_files():
			return (self.read(), '')
		text, existed = self.io_output()
		if existed:
			# The answer is in hand and the program is gone: the scratch
			# directory has nothing left to give, and a parallel batch makes one
			# manager per test case, so leaving them behind would litter the temp
			# root one hundred directories deep.
			self.release_io_dir()
			return (text, '')
		# The name is quoted back because the usual cause is a disagreement
		# between the statement and the code: freopen missing, or ".out" written
		# where the problem said ".ans".
		return ('', t('io_output_missing', name=self.io_spec.get('output') or '?',
					 input=self.io_spec.get('input') or '?'))

	def run_file(self, args=None):
		if self.is_run:
			# Recover from a stale flag instead of crashing: if the old
			# process already exited (finished, killed or TLE-terminated)
			# the 'is_run' marker is meaningless and must not block the
			# next run. Same when Popen never even started (e.g. it raised),
			# which used to poison the plugin until Sublime was restarted.
			proc = getattr(self, 'process', None)
			if proc is None or proc.poll() is not None:
				self.is_run = False
			else:
				raise AssertionError('cant run process because is already running')
		cmd = self.get_run_cmd(' '.join(args or []))
		# File-IO problems run inside a scratch directory that holds the sample
		# under the name the statement gives; stdin problems keep running next
		# to the source as they always did.
		run_cwd = self._io_dir_for() or os.path.split(self.file)[0]

		self.is_run = True
		self.terminated = False
		self.close_stderr()
		PIPE = subprocess.PIPE
		preexec_fn = None

		creationflags = 0
		if sublime.platform() == 'windows':
			use_shell = False
			startupinfo = _hidden_startupinfo()
			preexec_fn = None
			# Own process group so the shell + program can be killed as a
			# tree instead of leaving the program running (see terminate()).
			creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
		else:
			startupinfo = None
			preexec_fn = os.setsid
			# Only go through a shell when the command actually needs one
			# (pipes, redirection, &&...). Spawning through sh made the
			# memory sampler measure the shell and left the real program
			# orphaned in its own group when killed.
			use_shell = _needs_shell(cmd)
			if not use_shell:
				try:
					cmd = shlex.split(cmd)
				except Exception:
					use_shell = True

		if self.separate_stderr:
			stderr_target = tempfile.TemporaryFile(mode='w+b')
			self.stderr_file = stderr_target
		else:
			stderr_target = subprocess.STDOUT

		# Binary pipes + explicit UTF-8 codec below. Sublime's plugin host is
		# Python 3.3, where Popen has no encoding/errors/text arguments (those
		# arrived in 3.6/3.7), and the default text mode uses the locale
		# encoding (cp936 on Chinese Windows), which corrupted non-ASCII test
		# data and program output.
		self._out_decoder = codecs.getincrementaldecoder('utf-8')('replace')
		self._pending_cr = False
		self._read_buffer = b''
		self._read_pos = 0
		self._read_eof = False
		self.stdin_closed = False

		self.process = subprocess.Popen(
			cmd,
			shell=use_shell,
			stdin=PIPE,
			stdout=PIPE,
			stderr=stderr_target,
			bufsize=0,
			cwd=run_cwd,
			startupinfo=startupinfo,
			preexec_fn=preexec_fn,
			creationflags=creationflags
		)

		# The program exists from this instant: everything before it (our own
		# command formatting, Popen itself) is plugin overhead and is excluded
		# from the runtime the verdict is judged on.
		# monotonic(): the runtime the verdict is made from spans two
		# timestamps taken seconds apart, and an NTP step between them used to
		# move the number without the program doing anything.
		self.spawn_time = time.monotonic()
		self.exit_time = None
		self._track_live(self.process, 'run')

		# Remember the process group now: once the direct child is reaped,
		# os.getpgid(pid) raises ProcessLookupError and a backgrounded
		# grandchild would survive the SIGKILL escalation.
		try:
			self.pgid = os.getpgid(self.process.pid)
		except Exception:
			self.pgid = self.process.pid

		# Sample peak memory while the process is alive: /proc/<pid> and the
		# Windows handle both disappear once it exits. One synchronous sample
		# first, because a program that finishes in a few milliseconds could
		# otherwise exit before the polling thread ever looks at it.
		self.peak_memory_mb = None
		try:
			self.pid = self.process.pid
			first = sample_memory_bytes(self.pid)
			self._mem_sampler = MemorySampler(self.pid)
			if first:
				self._mem_sampler.peak = max(self._mem_sampler.peak, first)
			self._mem_sampler.start()
		except Exception:
			self._mem_sampler = None

	def finish_memory_sampling(self):
		"""Stop the sampler and return the peak memory usage in MB."""
		if self._mem_sampler is None:
			return self.peak_memory_mb
		try:
			peak = self._mem_sampler.stop()
			if peak:
				self.peak_memory_mb = bytes_to_mb(peak)
		except Exception:
			pass
		self._mem_sampler = None
		return self.peak_memory_mb

	def get_peak_memory_mb(self):
		return self.peak_memory_mb

	def insert(self, s):
		proc = getattr(self, 'process', None)
		if proc is None or proc.poll() is not None:
			return
		if self.is_io_files():
			# The sample is already in the file the program opens. Feeding it to
			# stdin as well would be worse than useless: a program that reads
			# only files never drains the pipe, so a large input fills it and the
			# write blocks forever - a hang that looks like a TLE.
			return
		if getattr(self, 'stdin_closed', False):
			# The pipe broke earlier in this run; stop retrying (and stop
			# printing the same status message for every pasted line).
			return
		if isinstance(s, str):
			s = s.encode('utf-8', 'replace')
		try:
			# bufsize=0 gives a raw pipe: one write() may accept only part of
			# the data (POSIX raw writes are partial by nature), so everything
			# past the first ~64 KB used to be dropped silently. Loop until the
			# buffer is consumed; a 0 return is a defensive stop against a
			# writer that would otherwise spin forever.
			data = s
			while data:
				n = proc.stdin.write(data)
				if not n or n < 0:
					break
				data = data[n:]
			proc.stdin.flush()
		except (OSError, ValueError) as e:
			# The program exited before reading everything (BrokenPipeError
			# on a closed stdin). Drop the input instead of letting the
			# exception escape into the command stack / listener thread.
			self.stdin_closed = True
			sublime.status_message('[Algorithm Competition Assistant] %s'
								   % t('process_already_exited'))
			print('[Algorithm Competition Assistant] stdin closed (%s), input dropped' % e)

	def close_stdin(self):
		"""Signal EOF: the program's input is complete.

		Programs that read *to EOF* - `sys.stdin.read()`, `for line in
		sys.stdin`, C++'s `while (cin >> x)` - otherwise block forever
		waiting for more input, and the watchdog reports a TLE for a program
		that runs fine on the judge, where stdin is a file that simply ends.

		Called only after a *stored* test input was written. The manual flow
		(pasting into the panel) and the interactor must keep stdin open.
		"""
		proc = getattr(self, 'process', None)
		if proc is None or proc.poll() is not None:
			return
		if getattr(self, 'stdin_closed', False):
			return
		try:
			proc.stdin.close()
			self.stdin_closed = True
		except Exception:
			pass

	def is_stopped(self):
		"""Exit code, or None while still running.

		Returns 0 when no process was ever started: callers treat anything
		that is not None as 'not running', so a failed Popen cannot leave
		them waiting forever.
		"""
		proc = getattr(self, 'process', None)
		if proc is None:
			return 0
		rc = proc.poll()
		# First observation that the child is gone is its stop timestamp: a
		# judge bills the program up to its exit and nothing after it, so
		# draining the pipe, reading stderr and stopping the memory sampler
		# must not land in the runtime the verdict is made from.
		if rc is not None and self.exit_time is None:
			self.exit_time = time.monotonic()
			# Nothing is left for an unload to reap; keep the registry small
			# instead of carrying dead pids until the cap forces a prune.
			self._untrack_live()
		return rc

	def runtime_ms(self):
		"""Milliseconds the program itself was alive, or None while running.

		Measured from just after the process was created to the first sight of
		it having exited, so neither our spawn cost nor our own cleanup is
		charged to the program. This is the single source of the number the TLE
		rule compares against, so the serial and the parallel path cannot drift
		apart any more.
		"""
		if self.spawn_time is None or self.exit_time is None:
			return None
		return int((self.exit_time - self.spawn_time) * 1000)

	def wait_for_exit(self, timeout):
		"""Block until the process exits or `timeout` (seconds) passes.

		Returns the exit code on exit, None on timeout (or whenever the wait
		cannot be performed - a duck-typed stand-in just keeps polling).
		Touches no UI, so worker threads may call it: the parallel judge uses
		it instead of a fixed 10 ms poll, and ``wait()`` returns the instant
		the child dies, so the exit timestamp no longer lags the real death by
		up to one poll step - which on a 100 ms limit was enough to flip AC
		into TLE.
		"""
		try:
			return self.process.wait(timeout)
		except Exception:
			return None

	def _normalize_newlines(self, text, final=False):
		"""Translate CRLF / lone CR into LF, like universal_newlines did.

		The binary pipe kept the program's CR, and Sublime renders a raw CR
		as '<0x0d>' on every line of the test panel.

		A trailing CR is emitted as a newline immediately and remembered, so
		a CRLF split across two reads still produces exactly one newline.
		"""
		if self._pending_cr:
			self._pending_cr = False
			if text.startswith('\n'):
				# the LF half of a CRLF pair that spanned two chunks; the
				# newline itself was already emitted with the CR
				text = text[1:]
		if not final and text.endswith('\r'):
			self._pending_cr = True
			text = text[:-1] + '\n'
		return text.replace('\r\n', '\n').replace('\r', '\n')

	def set_output_limit(self, limit):
		"""Cap how much output this run may produce; 0 or a bad value = unlimited."""
		try:
			limit = int(limit)
		except (TypeError, ValueError):
			limit = 0
		self.output_limit = limit if limit > 0 else 0
		self.output_taken = 0
		self.output_capped = False

	def _cap(self, text):
		"""Trim `text` to what is left of the output budget, flagging a cut.

		Counted in characters, the same unit the panel's own clamp uses, so the
		two enforcement points cannot disagree about where the limit ended.

		The over-budget part is still *consumed* (the budget moves even when the
		text is dropped): stopping mid-pipe would block the program on a full
		pipe buffer and get a merely talkative run reported as TLE. Same rule the
		panel's pump already follows when it hits the cap.
		"""
		if not text:
			return text
		if self.output_limit <= 0:
			self.output_taken += len(text)
			return text
		room = self.output_limit - self.output_taken
		if room <= 0:
			# Past the cap: consume, return nothing.
			self.output_capped = True
			return ''
		if len(text) > room:
			self.output_capped = True
			self.output_taken = self.output_limit
			return text[:room]
		self.output_taken += len(text)
		return text

	def read(self, bfsize=None):
		"""Read raw bytes, decode incrementally and normalize newlines.

		The incremental decoder matters for the byte-at-a-time sync mode:
		a multi-byte UTF-8 character split across two reads would otherwise
		come out as mojibake.

		Small reads used to cost one os.read() syscall per call: the sync
		listener (proc.read(bfsize=1)) and the interactor paid a syscall
		plus a decode per single byte, which made output-heavy runs drag.
		read() now fetches _READ_CHUNK bytes per syscall into an internal
		buffer and serves the caller from there. The observable semantics
		are unchanged: a call with bfsize returns at most that many bytes,
		blocks until at least one byte is available (or EOF), and the
		decoder flush at EOF happens exactly once the buffer is drained.
		"""
		try:
			if bfsize is None:
				data = self._read_buffer[self._read_pos:] \
					+ self.process.stdout.read()
				self._read_buffer = b''
				self._read_pos = 0
			else:
				if bfsize <= 0:
					bfsize = 1
				# One refill at most: like the raw pipe read before, the
				# call returns as soon as *any* byte is available and must
				# never block waiting for the full bfsize (an interactor
				# partner that prints a short prompt and then waits for
				# input would deadlock otherwise). The buffer is consumed
				# through an offset - re-slicing it per byte would turn
				# every call into a memmove of the rest of the chunk.
				if self._read_pos >= len(self._read_buffer) \
						and not self._read_eof:
					chunk = self.process.stdout.read(
						max(_READ_CHUNK, int(bfsize)))
					if not chunk:
						self._read_eof = True
					else:
						self._read_buffer = chunk
						self._read_pos = 0
				start = self._read_pos
				data = self._read_buffer[start:start + bfsize]
				self._read_pos = start + len(data)
		except Exception:
			return ''
		if not data:
			# EOF (and nothing buffered): flush whatever a partial
			# sequence left in the decoder
			try:
				tail = self._out_decoder.decode(b'', final=True)
			except Exception:
				tail = ''
			return self._cap(self._normalize_newlines(tail, final=True))
		decoded = self._out_decoder.decode(data)
		# _normalize_newlines() only ever changes text around '\r'; with
		# none pending and none in the chunk it is the identity, so skip
		# its two str.replace() scans - the per-byte path in the sync
		# listener / interactor calls this once per byte.
		if self._pending_cr or '\r' in decoded:
			return self._cap(self._normalize_newlines(decoded))
		return self._cap(decoded)

	def terminate_async(self):
		"""Ask for a stop without blocking the caller.

		terminate() waits for the child to actually die (taskkill, then the
		SIGTERM -> SIGKILL escalation), up to about 3 s. Called from a UI
		handler - closing the panel tab, or re-running over a live process -
		that wait froze the editor for the same length of time. The markers are
		set here and the kill continues on a private thread.
		"""
		proc = getattr(self, 'process', None)
		if proc is None:
			self.is_run = False
			return
		try:
			if proc.poll() is not None:
				# The run ended on its own a moment before the click: saying
				# "the user killed this" here would rewrite its verdict to
				# SKIPPED, exactly the bug terminate() guards against.
				self.is_run = False
				return
		except Exception:
			pass
		self.terminated = True
		self.is_run = False
		thread = threading.Thread(target=lambda: self.terminate(proc))
		thread.daemon = True
		thread.start()

	def terminate(self, target=None):
		proc = target if target is not None else getattr(self, 'process', None)
		pid = getattr(proc, 'pid', None)
		if pid is None:
			self.is_run = False
			return

		try:
			already_exited = proc.poll() is not None
		except Exception:
			already_exited = False
		if already_exited:
			# The process already exited on its own (its verdict was already
			# delivered by the listener). Killing a dead pid is not only
			# pointless: on Windows taskkill /F /T /PID would hit whatever
			# process the OS has since assigned that pid to (pid reuse), and
			# the spawn+wait happens on the UI thread in the close/re-run
			# paths. Just clear the running marker.
			self.is_run = False
			return

		# Only a real kill counts as "terminated": a stop clicked a moment
		# after the program finished must not rewrite its verdict to SKIPPED.
		self.terminated = True

		# Everything below works on `proc`, not on self.process: terminate_async
		# hands us the process it meant to kill, and a new run may already have
		# replaced self.process while this thread was still queueing.
		if sublime.platform() == 'windows':
			# The evaluated program can be a child of the shell, so a plain
			# kill(pid) would leave it running: kill the whole tree.
			try:
				subprocess.Popen(
					['taskkill', '/F', '/T', '/PID', str(pid)],
					stdout=subprocess.DEVNULL,
					stderr=subprocess.DEVNULL,
					startupinfo=_hidden_startupinfo()
				).wait(timeout=3)
			except Exception:
				try:
					proc.kill()
				except Exception:
					pass
		else:
			# linux AND osx: the child leads its own session (setsid), so
			# killing the process group is the only way to reach the real
			# program. Escalate to SIGKILL if SIGTERM is ignored.
			# self.pgid belongs to whichever process is current, so it may only
			# be used when this call targets that one.
			pgid = getattr(self, 'pgid', None) if proc is getattr(self, 'process', None) else None
			if not pgid:
				try:
					pgid = os.getpgid(pid)
				except Exception:
					pgid = None
			for sig, grace in ((signal.SIGTERM, 0.6), (signal.SIGKILL, 0.0)):
				try:
					if pgid:
						os.killpg(pgid, sig)
					else:
						proc.kill()
				except Exception:
					try:
						proc.kill()
					except Exception:
						pass
				if grace:
					waited = 0.0
					while proc.poll() is None and waited < grace:
						time.sleep(0.05)
						waited += 0.05
					if proc.poll() is not None:
						break

		# The process is being killed: clear the running marker right here
		# so a follow-up run_file() never trips over the stale flag even
		# if the listener thread's __on_stop has not fired yet. Only when this
		# still is the current process - a late thread for an older run must
		# not mark a fresh run as idle.
		if proc is getattr(self, 'process', None):
			self.is_run = False
		self._untrack_live()

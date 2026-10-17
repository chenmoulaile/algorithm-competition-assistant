"""Algorithm Competition Assistant - 设置开关面板

赛前要临时改的键都在 JSON 里：判定口径（浮点容差、PE 当 AC、首错即停、忽略
stderr）、监听行为（目标模式、自动监听、日志）、对拍（找文件方式、挑文件方式、
轮数、每轮时限）。为了切一个开关去开设置文件、数缩进、改字符串、保存，罚时全
丢在这上面。

这三个面板改的就是同一批键，而且**立刻生效**：它们都是每次使用时现读的
（`get_settings()` 拿到的是 `sublime.load_settings()` 的同一个对象）。唯一例外
是 `companion_auto_listen`——端口和"要不要自动监听"只在启动监听那一次读，所以
面板上把这一行标成"下次启动生效"。

放在单独的模块里是因为 `cph_extras.py` 的模块注释承诺了"这些命令都不改用户设
置"，这里改。
"""

import os

import sublime
import sublime_plugin

from .core.cph_i18n import t
from .core.cph_settings import companion_log_path, settings_file


def _settings():
	return sublime.load_settings(settings_file)


# The palette names come from description() rather than a "caption" in
# Default.sublime-commands, for the same reason the build-mode entry does: only
# a Python string can follow the `language` setting.
PREFIX = 'Algorithm Competition Assistant: '


def _write(key, value):
	"""Set one key and save it."""
	settings = _settings()
	settings.set(key, value)
	sublime.save_settings(settings_file)
	return value


def _onoff(value):
	return t('opt_on') if value else t('opt_off')


def _cycle(current, values):
	"""The next value in a small list, wrapping around."""
	try:
		return values[(list(values).index(current) + 1) % len(values)]
	except ValueError:
		return values[0]


def _number(text, current):
	"""Parse a panel answer, or keep the old value and say so."""
	try:
		value = float(text)
	except (TypeError, ValueError):
		sublime.status_message('%s: %s' % (t('opt_number_kept', value=current),
										   repr(text)))
		return None
	return int(value) if value == int(value) else value


class CphVerdictModeCommand(sublime_plugin.WindowCommand):
	"""判定口径。四个键都在每次判定时现读，改完下一题就用。"""

	def description(self):
		return PREFIX + t('opt_verdict_mode')

	def run(self):
		settings = _settings()
		tolerance = settings.get('float_tolerance', 0) or 0
		pe = bool(settings.get('regard_pe_as_ac', False))
		stop_first = bool(settings.get('stop_on_first_failure', True))
		stderr = bool(settings.get('ignore_stderr', False))
		# literal keys: the suite checks that every t('key') it can see is
		# defined and every defined key is reachable, so no 'opt_' + key here
		items = [
			[t('opt_float_tolerance'), ('%g' % tolerance) if tolerance else t('opt_off')],
			[t('opt_regard_pe_as_ac'), _onoff(pe)],
			[t('opt_stop_on_first_failure'), _onoff(stop_first)],
			[t('opt_ignore_stderr'), _onoff(stderr)],
		]
		current = [pe, stop_first, stderr]
		keys = ('regard_pe_as_ac', 'stop_on_first_failure', 'ignore_stderr')

		def choose(index):
			if index < 0:
				return
			if index == 0:
				start = ('%g' % tolerance) if tolerance else '1e-6'
				self.window.show_input_panel(
					t('opt_value_prompt', label=t('opt_float_tolerance')),
					start, self._set_tolerance, None, None)
				return
			key = keys[index - 1]
			new = _write(key, not current[index - 1])
			self.window.status_message('%s: %s' % (items[index][0], _onoff(new)))

		self.window.show_quick_panel(items, choose)

	def _set_tolerance(self, text):
		value = _number(text, 0)
		if value is None:
			return
		_write('float_tolerance', value)
		self.window.status_message('%s: %g' % (t('opt_float_tolerance'), value))


class CphStressOptionsCommand(sublime_plugin.WindowCommand):
	"""对拍的取文件方式与轮数/时限。"""

	SOURCES = ('pick_once', 'same_dir', 'auto')
	PICKERS = ('auto', 'native', 'panel')

	def description(self):
		return PREFIX + t('opt_stress_options')

	def run(self):
		settings = _settings()
		source = settings.get('stress_file_source', 'pick_once')
		picker = settings.get('stress_file_picker', 'auto')
		items = [
			[t('opt_stress_source'), str(source)],
			[t('opt_stress_picker'), str(picker)],
			[t('opt_save_counterexample'),
			 _onoff(settings.get('stress_save_counterexample', True))],
			[t('opt_stress_rounds'), str(settings.get('stress_max_rounds', 1000))],
			[t('opt_stress_time_limit'),
			 str(settings.get('stress_time_limit_seconds', 2))],
		]

		def choose(index):
			if index < 0:
				return
			if index == 0:
				new = _write('stress_file_source', _cycle(source, self.SOURCES))
			elif index == 1:
				new = _write('stress_file_picker', _cycle(picker, self.PICKERS))
			elif index == 2:
				new = _write('stress_save_counterexample',
							 not bool(settings.get('stress_save_counterexample', True)))
			elif index == 3:
				self.window.show_input_panel(
					t('opt_value_prompt', label=t('opt_stress_rounds')),
					str(settings.get('stress_max_rounds', 1000)),
					lambda text: self._number_pair('stress_max_rounds', text,
												   items[index][1]),
					None, None)
				return
			else:
				self.window.show_input_panel(
					t('opt_value_prompt', label=t('opt_stress_time_limit')),
					str(settings.get('stress_time_limit_seconds', 2)),
					lambda text: self._number_pair('stress_time_limit_seconds',
												   text, items[index][1]),
					None, None)
				return
			self.window.status_message('%s: %s' % (items[index][0], new))

		self.window.show_quick_panel(items, choose)

	def _number_pair(self, key, text, previous):
		value = _number(text, previous)
		if value is None:
			return
		_write(key, value)
		self.window.status_message('%s: %s' % (key, value))


class CphCompanionOptionsCommand(sublime_plugin.WindowCommand):
	"""监听行为。目标模式与日志立刻生效；自动监听要下次启动监听。

	目标模式只占一行、点一下就换：它是比赛中最常切的一项（收样例时 new_file，
	手工调试时 current_file），两行就得先想一下点哪个。
	"""

	TARGETS = ('new_file', 'current_file')

	def description(self):
		return PREFIX + t('opt_companion_options')

	def run(self):
		settings = _settings()
		log_path = companion_log_path()
		target = settings.get('companion_target', 'new_file')
		target_label = (t('opt_target_mode') if target == 'new_file'
						else t('opt_target_mode_current'))
		has_log = os.path.isfile(log_path)
		items = [
			[t('opt_target_row'), target_label],
			[t('opt_auto_listen'),
			 '%s (%s)' % (_onoff(settings.get('companion_auto_listen', True)),
						  t('opt_next_start'))],
			[t('opt_debug_log'), _onoff(settings.get('companion_debug_log', False))],
			[t('opt_open_log'), log_path if has_log else t('opt_log_missing')],
		]

		def choose(index):
			if index < 0:
				return
			if index == 0:
				new = _cycle(target, self.TARGETS)
				_write('companion_target', new)
				self.window.status_message(
					'%s: %s' % (items[0][0], t('opt_target_mode') if new == 'new_file'
								else t('opt_target_mode_current')))
			elif index == 1:
				new = _write('companion_auto_listen',
							 not bool(settings.get('companion_auto_listen', True)))
				self.window.status_message('%s: %s' % (items[1][0], _onoff(new)))
			elif index == 2:
				new = _write('companion_debug_log',
							 not bool(settings.get('companion_debug_log', False)))
				self.window.status_message('%s: %s' % (items[2][0], _onoff(new)))
			elif has_log:
				self.window.open_file(log_path)
			else:
				sublime.status_message(t('opt_log_missing'))

		self.window.show_quick_panel(items, choose)

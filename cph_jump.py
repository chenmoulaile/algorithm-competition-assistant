"""Algorithm Competition Assistant - 跳到源码位置（命令入口）

逻辑在 core/cph_jump.py（根级插件模块之间不允许互相 import）。
"""

import os

import sublime
import sublime_plugin

from .core.cph_jump import jump_to, TARGETS_KEY
from .core.cph_i18n import t


class CphJumpToLocationCommand(sublime_plugin.TextCommand):
	"""Jump to the Nth target stored in a view's `cph_jump_targets`.

	The targets live on the run panel and on the detail tab, never on the source
	file - so asking for a jump while standing in `main.cpp` used to do nothing:
	`is_enabled()` only looked at the current view and greyed the command out,
	and the palette entry said "last" while passing the index of the *first*
	diagnostic. A jump asked for from the source view now means "take me to the
	error of the run I am looking at": the panel for this file is used when there
	is one, otherwise the most recent panel in the window.
	"""

	def _target_view(self):
		view = self.view
		if view.settings().get(TARGETS_KEY):
			return view
		window = view.window()
		if window is None:
			return None
		base = os.path.basename(view.file_name() or '')
		fallback = None
		for candidate in window.views():
			if not candidate.settings().get(TARGETS_KEY):
				continue
			# Views are listed most-recently-used first, so the first match is
			# the panel this file was last judged on.
			name = candidate.name() or ''
			if base and base in name:
				return candidate
			if fallback is None:
				fallback = candidate
		return fallback

	def run(self, edit, target=None):
		view = self._target_view()
		if view is None or not jump_to(view, target):
			sublime.status_message('[Algorithm Competition Assistant] ' + t('jump_no_target'))

	def is_enabled(self, target=None):
		return self._target_view() is not None

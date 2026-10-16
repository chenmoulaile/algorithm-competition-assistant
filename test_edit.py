"""
Algorithm Competition Assistant - 测试编辑窗口
两个独立标签页:
  - "test N -edit"   编辑测试输入
  - "test N -answer" 编辑标准答案 (预期输出)
save 按钮同时保存两者; 相比旧的 "------ answer ------" 分隔行,
标准答案不再可能被误删。
"""
import sublime, sublime_plugin
from sublime import Region, Phantom, PhantomSet

from .core.cph_settings import package_name
from .core.cph_resources import read_resource
from .core.cph_i18n import t as _i18n_t
from .Highlight.test_interface import get_test_styles


class CphTestEditCommand(sublime_plugin.TextCommand):

	def __init__(self, view):
		self.view = view
		self.mode = 'input'
		self.test_id = None
		self.source_view_id = None
		self.phantoms = PhantomSet(view, 'test-edit-phantoms')

	# ---------- sibling view helpers ----------

	def _sibling_name(self):
		if self.mode == 'input':
			return 'test ' + str(self.test_id) + ' -answer'
		return 'test ' + str(self.test_id) + ' -edit'

	def _find_sibling(self):
		window = self.view.window()
		if window is None:
			return None
		name = self._sibling_name()
		for wv in window.views():
			if wv.name() == name:
				# Same test number opened from another source view must not
				# be mistaken for this test's sibling: saving would then
				# copy that other test's input / answer over here.
				source = wv.settings().get('cph_edit_source')
				if source is not None and source != self.source_view_id:
					continue
				return wv
		return None

	def _find_source_view(self):
		window = self.view.window()
		if window is None or self.source_view_id is None:
			return None
		for wv in window.views():
			if wv.id() == self.source_view_id:
				return wv
		return None

	# ---------- buffer helpers ----------

	def _content(self):
		"""The editable text, without the position-0 sentinel newline.

		See init() for why the buffer starts with a '\n'. Saving the whole
		buffer used to be harmless while there was no sentinel, but it must
		never end up inside the stored sample.
		"""
		v = self.view
		start = 1 if v.substr(Region(0, 1)) == '\n' else 0
		return v.substr(Region(start, v.size()))

	# ---------- phantom actions ----------

	def cb_action(self, event):
		v = self.view
		if event == 'test-save':
			source = self._find_source_view()
			if source is None:
				sublime.status_message('[Algorithm Competition Assistant] source run view is gone')
				return

			content = self._content()
			test_input, answer = None, None
			if self.mode == 'input':
				test_input = content
			else:
				answer = content

			sibling = self._find_sibling()
			if sibling is not None:
				sibling_content = sibling.substr(Region(0, sibling.size()))
				if sibling_content[:1] == '\n':
					sibling_content = sibling_content[1:]
				if self.mode == 'input':
					answer = sibling_content
				else:
					test_input = sibling_content

			# Only push back what actually has a source. The sibling tab may
			# be closed (the user closed '-answer' and kept '-edit'): sending
			# an empty answer then wiped the stored one, because
			# set_correct_answer() clears the answer set before setting it.
			if test_input is not None:
				if test_input.strip():
					test_input = test_input.rstrip('\n') + '\n'
				source.run_command('cph_test_manager', {
					'action': 'set_test_input',
					'data': test_input,
					'id': self.test_id
				})
			if answer is not None:
				source.run_command('cph_test_manager', {
					'action': 'set_correct_answer',
					'data': answer,
					'id': self.test_id
				})

			# close both edit views
			if sibling is not None:
				sibling.close()
			v.close()

		elif event == 'test-delete':
			source = self._find_source_view()
			sibling = self._find_sibling()
			if source is not None:
				source.run_command('cph_test_manager', {
					'action': 'delete_test',
					'id': self.test_id
				})
			if sibling is not None:
				sibling.close()
			v.close()

	def update_config(self):
		v = self.view
		styles = get_test_styles(v)
		content = read_resource('Highlight/test_edit.html')

		hint = _i18n_t('edit_input_hint') if self.mode == 'input' \
			else _i18n_t('edit_answer_hint')
		content = content.format(
			test_id=self.test_id,
			test_label=_i18n_t('test_label'),
			save_label=_i18n_t('save'),
			delete_label=_i18n_t('delete'),
			hint=hint,
		)
		self._phantom_html = '<style>' + styles + '</style>' + content
		self.update_phantom()

	def update_phantom(self):
		"""Re-anchor the button phantom at position 0. Called after buffer
		modifications: an insertion exactly at point 0 (typing at the very
		start of the input) can push the phantom anchor below the first
		line, so we re-pin it to keep the buttons on top."""
		if not getattr(self, '_phantom_html', None):
			return
		phantom = Phantom(Region(0), self._phantom_html, sublime.LAYOUT_BLOCK, self.cb_action)
		self.phantoms.update([phantom])

	# ---------- init ----------

	def init(self, edit, mode='input', test='', test_id=None,
			 source_view_id=None, correct_answer=''):
		v = self.view
		self.mode = mode if mode in ('input', 'answer') else 'input'
		self.test_id = test_id
		self.source_view_id = source_view_id

		v.set_scratch(True)
		suffix = ' -edit' if self.mode == 'input' else ' -answer'
		v.set_name('test ' + str(test_id) + suffix)
		v.run_command('toggle_setting', {'setting': 'line_numbers'})
		v.run_command('set_setting', {'setting': 'fold_buttons', 'value': False})
		# 'cph_edit_view' marks this tab as a test-edit view (the run view
		# also uses 'edit_mode' for its own inline-edit phase, so listeners
		# must be able to tell the two apart).
		v.settings().set('cph_edit_view', True)
		v.settings().set('cph_edit_source', source_view_id)
		v.settings().set('edit_mode', True)
		v.set_syntax_file('Packages/%s/TestSyntax.sublime-syntax' % package_name)
		# Position 0 holds a sentinel '\n' and the button phantom is anchored
		# at Region(0). minihtml has no "draw above this line" layout
		# (sublimehq/sublime_text#4469): a block phantom always renders
		# BELOW the line it is anchored to. With the sample starting at
		# position 0 the card therefore landed between the first and the
		# second line of the sample - the empty first line is what keeps it
		# on top, exactly like the -run panel does (see 'erase_all', which
		# leaves a single '\n' behind for the very same reason).
		# Position 0 is reserved: EditModifyListener keeps the caret at
		# >= 1 and _content() saves from position 1, so the sentinel can
		# never leak into the stored sample.
		if self.mode == 'input':
			initial_content = test.rstrip('\n') + '\n' if test.strip() else ''
		else:
			initial_content = (correct_answer or '').lstrip('\n')
		# replace(), not insert(): init() also runs when an existing edit
		# tab is reopened (e.g. the user closed only the -answer tab), and
		# inserting the content a second time duplicated it.
		v.replace(edit, Region(0, v.size()), '\n' + initial_content)
		v.sel().clear()
		v.sel().add(Region(v.size()))
		self.update_config()

	def sync_read_only(self):
		view = self.view
		if view.settings().get('edit_mode'):
			view.set_read_only(False)

	def run(self, edit, action=None, mode='input', test='', test_id=None,
			source_view_id=None, data=None, region=None, text=None):
		v = self.view
		v.set_read_only(False)

		if action == 'init':
			correct_answer = data if data else ''
			self.init(edit, mode=mode, test=test, test_id=test_id,
					  source_view_id=source_view_id, correct_answer=correct_answer)

		elif action == 'replace':
			v.replace(edit, Region(region[0], region[1]), text or '')

		elif action == 'sync_read_only':
			self.sync_read_only()

		elif action == 'update_phantom':
			self.update_phantom()

		elif action == 'set_cursor_to_end':
			v.sel().clear()
			v.sel().add(Region(v.size(), v.size()))


class EditModifyListener(sublime_plugin.EventListener):
	@staticmethod
	def _clamp_caret(view):
		"""Keep the caret out of the position-0 sentinel line.

		Typing there would insert text *before* the sentinel, which pushes
		the button phantom down into the sample - the exact bug this
		sentinel exists to prevent.
		"""
		mod = []
		change = False
		for reg in view.sel():
			if reg.a < 1 or reg.b < 1:
				change = True
			mod.append(Region(max(reg.a, 1), max(reg.b, 1)))
		if change:
			view.sel().clear()
			view.sel().add_all(mod)

	def on_selection_modified(self, view):
		if view.settings().get('cph_edit_view'):
			# The 'test N -edit' / '-answer' tabs keep a sentinel newline at
			# position 0 (see CphTestEditCommand.init). Select-all + delete
			# can wipe it, so put it back; otherwise the card would end up
			# in the middle of the sample again.
			if view.size() == 0:
				view.run_command('cph_test_edit', {
					'action': 'replace',
					'region': [0, 0],
					'text': '\n'
				})
				return
			self._clamp_caret(view)
			return
		if view.settings().get('edit_mode'):
			if view.size() == 0:
				view.run_command('cph_test_edit', {
					'action': 'replace',
					'region': [0, view.size()],
					'text': '\n'
				})
			self._clamp_caret(view)

	def on_modified(self, view):
		# Keep the save/delete buttons pinned to the top of the edit tabs:
		# an insertion at point 0 can push the phantom anchor into the text.
		if view.settings().get('cph_edit_view'):
			view.run_command('cph_test_edit', {'action': 'update_phantom'})

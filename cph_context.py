"""Algorithm Competition Assistant - 自定义快捷键上下文

Package Control 审查要求包内快捷键必须带具体 context（不能用"全局绑定"），
所以这里提供几个自定义 context key，让快捷键只在真正有意义时生效：

- ``cph_has_run_panel``   当前视图是运行面板，或当前源文件已经有配对的运行面板
- ``cph_panel_toggle_ok`` 收缩/展开运行面板的快捷键（ctrl+k ctrl+p）能否在
  当前视图触发：除输入浮层（快速面板、查找替换等 widget 视图）外全部放行，
  对齐原版"任何视图都生效"的行为
- ``cph_stress_view``     当前视图是对拍输出页（停止对拍的键只在这里生效）
- ``cph_listener_running`` Competitive Companion 监听器正在运行
- ``cph_keybindings_enabled``  ``enable_keybindings`` 设置是否为 true

状态从 ``core/cph_state`` 读取，避免根级插件之间互相 import。

除 ``cph_keybindings_enabled`` 本身，上面每一个键都在 `on_query_context` 的最后
统一过一遍 ``enable_keybindings``：README 说"关掉后包内快捷键全部变成空操作"，
那句就得对每一条绑定成立，包括那些还要看状态的（停止对拍、停止监听、同步面板）。
回归测试盯着"keymap 里用到的键这里有分支、这里的分支 keymap 里有人用"。
"""

import sublime
import sublime_plugin

from .core.cph_settings import get_settings
from .core.cph_state import is_listener_running, is_stress_view


def _is_run_view(view):
	return bool(view.settings().get('cph_run_view'))


def keybindings_enabled():
	"""``enable_keybindings`` 设置：关掉后包内快捷键全部变成空操作。

	包内的绑定随包加载、随包卸载，所以禁用/卸载本插件后按键会自动还给
	其它插件；这个设置只是让你**不改 keymap** 也能临时让出键位。
	"""
	try:
		value = get_settings().get('enable_keybindings', True)
	except Exception:
		return True
	return value is not False


def _has_run_panel(view):
	"""True when collapsing/restoring a run panel would do something."""
	if _is_run_view(view):
		return True
	file_name = view.file_name()
	if not file_name:
		return False
	window = view.window()
	if window is None:
		return False
	target = file_name.replace('\\', '/').split('/')[-1] + ' -run'
	for other in window.views():
		if (other.name() or '') == target:
			return True
	return False


def _panel_toggle_ok(view):
	"""True when the toggle-panel hotkey (ctrl+k ctrl+p) may fire here.

	原版对这个键没有加任何 context，任何视图都能按；Package Control 评审
	不允许包内绑定不带 context，所以用这个专用键应答：widget 视图
	（快速面板、查找替换等输入浮层）不放行，其余视图全部放行——
	没跑过本插件的源文件也能收缩窗口里已经存在的三栏布局。
	"""
	if bool(view.settings().get('is_widget')):
		return False
	return True


class CphContextListener(sublime_plugin.EventListener):
	"""Answer the custom context keys used by this package's key bindings."""

	def on_query_context(self, view, key, operator, operand, match_all):
		if key == 'cph_keybindings_enabled':
			# the gate answers for itself, and nothing else goes through it
			value = keybindings_enabled()
		elif key == 'cph_has_run_panel':
			value = _has_run_panel(view)
		elif key == 'cph_panel_toggle_ok':
			value = _panel_toggle_ok(view)
		elif key == 'cph_stress_view':
			# 当前视图是对拍输出页：停止对拍的快捷键只在这里生效，
			# 多个文件同时对拍时停的就是眼前这一个。
			value = is_stress_view(view.id())
		elif key == 'cph_listener_running':
			value = is_listener_running()
		else:
			return None

		if not keybindings_enabled():
			value = False

		if operator == sublime.OP_EQUAL:
			return value == bool(operand)
		if operator == sublime.OP_NOT_EQUAL:
			return value != bool(operand)
		return None

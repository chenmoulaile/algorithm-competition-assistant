"""Algorithm Competition Assistant - 跨模块共享的运行时状态

放在子包里是有原因的：Sublime 会把包根目录下的每个 .py 当成独立插件加载，
根级模块之间互相 import 会被 Package Control 审查判为错误
（"Do not import root-level plugin module"）。把共享状态放到 core/ 下，
根级插件只允许 import 子包内的模块，这样既没有该问题，也避免重复加载。

这里只放"当前是否正在做什么"这类瞬时标志，不放用户设置（那属于 cph_settings）。
"""

state = {
	# 对拍输出页注册表：view id -> {'user_file': 被测源文件}
	# 多个文件可同时对拍，每个输出页一个会话；快捷键上下文靠它判断
	# "当前视图是不是对拍页"。
	'stress_views': {},
	# Competitive Companion 监听器
	'listener_running': False,
	'listener_port': None,
	'listener_view_id': None,
	# 当前加载的构建号。版本号的唯一来源仍是 plugin_init.VERSION（发布检查
	# 认它），这里只是把它公布给 doctor 等根级模块——根级模块之间不许互相
	# import，不放进 core 就取不到。
	'plugin_version': '',
}


def set_plugin_version(version):
	state['plugin_version'] = version or ''


def plugin_version():
	"""The build this host actually loaded, or '' before it was recorded."""
	return state.get('plugin_version') or ''


def register_stress_view(view_id, user_file=None):
	state.setdefault('stress_views', {})[view_id] = {'user_file': user_file}


def unregister_stress_view(view_id):
	state.setdefault('stress_views', {}).pop(view_id, None)


def is_stress_view(view_id):
	return view_id in state.get('stress_views', {})


def set_listener(server, port=None, view_id=None):
	state['listener_running'] = server is not None
	if server is None:
		state['listener_port'] = None
		state['listener_view_id'] = None
	else:
		state['listener_port'] = port
		state['listener_view_id'] = view_id


def is_listener_running():
	return bool(state.get('listener_running'))

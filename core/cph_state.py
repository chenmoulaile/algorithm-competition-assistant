"""Algorithm Competition Assistant - 跨模块共享的运行时状态

放在子包里是有原因的：Sublime 会把包根目录下的每个 .py 当成独立插件加载，
根级模块之间互相 import 会被 Package Control 审查判为错误
（"Do not import root-level plugin module"）。把共享状态放到 core/ 下，
根级插件只允许 import 子包内的模块，这样既没有该问题，也避免重复加载。

这里只放"当前是否正在做什么"这类瞬时标志，不放用户设置（那属于 cph_settings）。
"""

import os

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


# --------------------------------------------------------------------------
# 子进程与临时目录的"还给系统"登记
#
# Sublime 不会等插件的线程：升级、Package Control 动这个包、folder install 改了
# .py 之后的重启，都会把插件卸载掉，而被测程序和它下面派生的 g++ 还在跑。Windows
# 上那个孤儿进程继续占着自己的 .exe，于是下一次编译报 "cannot open output file:
# Permission denied"，错误信息里一个字都没提到真正抓着文件的进程。
# 登记放在 core/ 里是因为根级模块之间不许互相 import（见本文件开头），卸载入口
# plugin_init 只能靠这里取到它们。
# --------------------------------------------------------------------------

# key -> (is_alive, kill)
_live = {}
# 本插件建出来的临时目录
_temp_dirs = set()

# 每个"在源码目录之外编译程序"的功能都会开一个临时目录；启动时按这些前缀清理
# 上一次会话留下的残骸。加新功能时同步加到这里，否则它的目录永远不会被清掉。
SCRATCH_PREFIXES = ('cph-stress-', 'cph-calibrate-', 'cph-checker-',
					'cph-interactor-', 'cph-doctor-')

# 一次跑几十轮的调试里，登记表不应该无限增长。超过这个数就把已经结束的丢掉。
_LIVE_CAP = 64


def register_live(key, is_alive, kill):
	"""Track one child process so an unload can reap it.

	`is_alive` decides whether the process still needs killing (killing a pid
	that already exited can hit whatever the OS has since handed that pid to),
	`kill` reaps it. Nothing here raises: the caller is on a spawn path.
	"""
	try:
		_live[key] = (is_alive, kill)
		if len(_live) > _LIVE_CAP:
			prune_live()
	except Exception as e:
		print('[Algorithm Competition Assistant] could not track a child process: %s' % e)


def unregister_live(key):
	_live.pop(key, None)


def prune_live():
	"""Forget registrations whose process is already gone."""
	for key in list(_live.keys()):
		try:
			if not _live[key][0]():
				_live.pop(key, None)
		except Exception:
			_live.pop(key, None)


def live_count():
	return len(_live)


def kill_all_live():
	"""Kill everything still tracked; returns (killed, pruned, errors).

	Each callback is guarded on its own: one process that refuses to die must
	not keep the others alive, and this runs while the host is going down.
	"""
	killed = 0
	pruned = 0
	errors = 0
	for key in sorted(_live.keys(), key=str):
		is_alive, kill = _live.pop(key)
		try:
			if not is_alive():
				pruned += 1
				continue
			kill()
			killed += 1
		except Exception as e:
			errors += 1
			print('[Algorithm Competition Assistant] could not kill %r: %s' % (key, e))
	return killed, pruned, errors


def register_temp_dir(directory):
	"""Remember a scratch directory the plugin is responsible for."""
	if directory:
		_temp_dirs.add(directory)


def unregister_temp_dir(directory):
	_temp_dirs.discard(directory)


def temp_dir_count():
	return len(_temp_dirs)


def clean_temp_dirs():
	"""Remove this session's scratch directories; returns how many went away."""
	import shutil
	removed = 0
	for directory in list(_temp_dirs):
		_temp_dirs.discard(directory)
		try:
			shutil.rmtree(directory, ignore_errors=True)
			removed += 1
		except Exception as e:
			print('[Algorithm Competition Assistant] could not remove %s: %s'
			      % (directory, e))
	return removed


def sweep_stale_temp_dirs(prefixes, older_than_seconds=86400, keep=None):
	"""Delete scratch directories a previous session left behind, older than a day.

	A crash, a taskkill from outside or a machine that lost power leaves
	directories in the temp root forever - the plugin makes one per stress run,
	checker build, interactor run, calibration and doctor probe - and nobody ever
	looks there. Only entries older than `older_than_seconds` are touched:
	another Sublime window on this machine may be inside a fresh one right now,
	and clearing a live scratch directory mid-run would corrupt *that* run to
	tidy up a past one. `keep` protects the caller's own directories.
	"""
	import shutil
	import time
	if isinstance(prefixes, str):
		prefixes = (prefixes,)
	keep = set(keep or ())
	root = None
	try:
		import tempfile
		root = tempfile.gettempdir()
		names = os.listdir(root)
	except Exception as e:
		print('[Algorithm Competition Assistant] stale scratch sweep skipped: %s' % e)
		return 0
	now = time.time()
	removed = 0
	for name in names:
		if not name.startswith(tuple(prefixes)):
			continue
		directory = os.path.join(root, name)
		if directory in keep:
			continue
		try:
			if not os.path.isdir(directory):
				continue
			if now - os.path.getmtime(directory) < older_than_seconds:
				continue
			shutil.rmtree(directory, ignore_errors=True)
			removed += 1
		except Exception:
			continue
	if removed:
		print('[Algorithm Competition Assistant] removed %d stale scratch director%s'
		      % (removed, 'y' if removed == 1 else 'ies'))
	return removed

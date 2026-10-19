"""
Algorithm Competition Assistant - 资源读取（zip 安全）

Package Control 默认把包打成 .sublime-package（其实是一个 zip）。此时
`__file__` 指向 zip 内部，用内置 open() 读 Highlight/*.html 会直接抛
FileNotFoundError —— 卡片、详情、编译错误面板全部渲染失败。

sublime.load_resource() 在 zip 内同样可用，所以所有资源读取都走这里，
并按资源名做进程内缓存（原来每张卡片每次刷新都重新读盘 + 读一次 CSS）。
缓存由 plugin_init 在插件重载时清空。

`edit_settings` 也一样：它要的是 `${packages}/<包名>/<文件>` 这种资源形式。
绝对路径只有散装文件夹安装才存在，zip 安装里那个目录根本不存在。
"""

import sublime
from os import path

from .cph_settings import base_name, package_name_for


_cache = {}


def resource_dir():
	"""This package's name as Sublime addresses it in a path.

	Derived from `base_name` on every call rather than captured at import, so
	the .sublime-package suffix is always removed from whatever the host
	actually loaded us from.
	"""
	return package_name_for(base_name)


def package_resource_path(rel_path):
	return 'Packages/%s/%s' % (resource_dir(), rel_path)


def package_variable_path(rel_path):
	"""The `base_file` form `edit_settings` resolves in both install shapes.

	Package Control installs this package as a `.sublime-package` zip, so the
	file is not at an OS path under Packages/ at all - `open_file` on such a
	path answers "The settings file ... could not be opened". Sublime's own
	packages and Package Control pass `${packages}/<package>/<file>` here; the
	resource layer resolves that inside the zip as well as in a loose folder.
	The package component must carry no .sublime-package suffix (see
	package_name_for).
	"""
	return '${packages}/%s/%s' % (resource_dir(), rel_path)


def read_resource(rel_path, use_cache=True):
	"""Return the text content of a package resource (zip-safe).

	Falls back to the real filesystem for unusual layouts; returns ''
	when the resource cannot be read at all (never raises).
	"""
	if use_cache and rel_path in _cache:
		return _cache[rel_path]

	content = ''
	try:
		content = sublime.load_resource(package_resource_path(rel_path))
	except Exception:
		try:
			root = path.dirname(path.dirname(path.abspath(__file__)))
			with open(path.join(root, rel_path), 'r', encoding='utf-8') as f:
				content = f.read()
		except Exception:
			content = ''

	if use_cache:
		_cache[rel_path] = content
	return content


def clear_cache():
	_cache.clear()

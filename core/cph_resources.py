"""
Algorithm Competition Assistant - 资源读取（zip 安全）

Package Control 默认把包打成 .sublime-package（其实是一个 zip）。此时
`__file__` 指向 zip 内部，用内置 open() 读 Highlight/*.html 会直接抛
FileNotFoundError —— 卡片、详情、编译错误面板全部渲染失败。

sublime.load_resource() 在 zip 内同样可用，所以所有资源读取都走这里，
并按资源名做进程内缓存（原来每张卡片每次刷新都重新读盘 + 读一次 CSS）。
缓存由 plugin_init 在插件重载时清空。
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


def package_file_path(rel_path):
	"""A path `edit_settings` / `open_file` can actually resolve.

	`Packages/<pkg>/<file>` is a *resource* path: load_resource accepts it,
	edit_settings does not - it hands the string to the OS and answers "The
	settings file ... could not be opened". Sublime's own packages therefore
	pass an absolute path under the Packages directory, and that form resolves
	for both layouts, provided the package name carries no .sublime-package
	suffix (see package_name_for).
	"""
	try:
		return path.join(sublime.packages_path(), resource_dir(), rel_path)
	except Exception:
		return package_resource_path(rel_path)


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

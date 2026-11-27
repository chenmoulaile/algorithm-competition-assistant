"""
Algorithm Competition Assistant - 模板片段插入 (Ctrl+Alt+T)

背景：四个 keymap 一直把 `tab` 绑到 `olympic_funcs` —— 那是从
FastOlympicCoding 抄来时留下的绑定，本插件从未定义该命令，所以 README
宣传的「Tab 插入模板」实际不可用，还会和同时安装 FOC 的用户抢按键。
现在改为独立的 `cph_insert_template` 命令 + 安全快捷键。

查找顺序：
1. `algorithms_base` 目录下的 `<关键字>.cpp/.cc/.cxx/.txt`（FOC 兼容用法）
2. 设置 `templates` 字典：{ "关键字": "片段内容" }
3. 内置常用片段

片段里可以用 {cursor} 标记插入后光标落点。
"""

import sublime
import sublime_plugin
from os import path

from .core.cph_settings import get_settings
from .core.cph_i18n import t


BUILTIN_TEMPLATES = {
	'fastio': (
		'ios::sync_with_stdio(false);\n'
		'cin.tie(nullptr);'
	),
	'main': (
		'#include <bits/stdc++.h>\n'
		'using namespace std;\n\n'
		'int main() {\n'
		'    ios::sync_with_stdio(false);\n'
		'    cin.tie(nullptr);\n\n'
		'    {cursor}\n\n'
		'    return 0;\n'
		'}'
	),
	'bf': (
		'for (int i = 0; i < n; ++i) {\n'
		'    {cursor}\n'
		'}'
	),
	'debug': (
		'#ifdef LOCAL\n'
		'#define dbg(x) cerr << #x << " = " << (x) << "\\n"\n'
		'#else\n'
		'#define dbg(x) ((void)0)\n'
		'#endif'
	),
	'testlib': '#include "testlib.h"',
}


class CphInsertTemplateCommand(sublime_plugin.TextCommand):
	"""Expand the word before the cursor into a code template."""

	def run(self, edit):
		v = self.view
		if len(v.sel()) != 1:
			return

		sel = v.sel()[0]
		region = sel if not sel.empty() else v.word(sel.begin())
		word = v.substr(region).strip()
		if not word:
			sublime.status_message(t('template_no_keyword'))
			return

		snippet = self._lookup(word)
		if not snippet:
			sublime.status_message(t('template_not_found', name=word))
			return

		cursor = snippet.find('{cursor}')
		if cursor >= 0:
			snippet = snippet.replace('{cursor}', '', 1)

		v.replace(edit, region, snippet)

		if cursor >= 0:
			pt = region.begin() + cursor
			v.sel().clear()
			v.sel().add(sublime.Region(pt, pt))

	def _lookup(self, word):
		settings = get_settings()

		# 1. algorithms_base directory (FastOlympicCoding compatible)
		base = settings.get('algorithms_base')
		if base:
			for ext in ('.cpp', '.cc', '.cxx', '.txt'):
				candidate = path.join(base, word + ext)
				try:
					if path.exists(candidate):
						with open(candidate, 'r', encoding='utf-8', errors='replace') as f:
							return f.read().rstrip('\n')
				except Exception:
					pass

		# 2. user templates from the settings file
		templates = settings.get('templates') or {}
		if isinstance(templates, dict) and word in templates:
			return templates[word]

		# 3. built-ins
		return BUILTIN_TEMPLATES.get(word)

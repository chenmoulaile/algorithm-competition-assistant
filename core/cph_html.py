"""Algorithm Competition Assistant - HTML helpers / HTML 处理助手（零依赖）

把题目页面（Codeforces / 洛谷 / AtCoder / 多数评测站）的 HTML 转成能在
Sublime 里阅读的 Markdown，并尽量抽出样例输入输出。

只依赖标准库的 ``re``：不 import 任何插件模块，也不联网。宿主是 Python 3.3，
所以这里不用 f-string / 海象运算符，只用 ``%`` 格式化，并且自己实现 HTML
实体解码（``html.unescape`` 要 3.4+，3.3 上没有）。
"""

import re


# --------------------------------------------------------------------------
# HTML 实体解码
# --------------------------------------------------------------------------

_NAMED_ENTITIES = {
	'amp': '&', 'lt': '<', 'gt': '>', 'quot': '"', 'apos': "'",
	'nbsp': '\xa0', 'shy': '\xad', 'copy': '\xa9', 'reg': '\xae',
	'trade': '\u2122', 'hellip': '\u2026', 'mdash': '\u2014',
	'ndash': '\u2013', 'lsquo': '\u2018', 'rsquo': '\u2019',
	'ldquo': '\u201c', 'rdquo': '\u201d', 'laquo': '\xab', 'raquo': '\xbb',
	'times': '\xd7', 'divide': '\xf7', 'plusmn': '\xb1', 'middot': '\xb7',
	'bull': '\u2022', 'deg': '\xb0', 'sect': '\xa7', 'para': '\xb6',
	'micro': '\xb5', 'sup2': '\xb2', 'sup3': '\xb3', 'frac12': '\xbd',
	'frac14': '\xbc', 'frac34': '\xbe', 'pound': '\xa3', 'yen': '\xa5',
	'cent': '\xa2', 'euro': '\u20ac', 'le': '\u2264', 'ge': '\u2265',
	'ne': '\u2260', 'infin': '\u221e', 'larr': '\u2190', 'rarr': '\u2192',
	'uarr': '\u2191', 'darr': '\u2193', 'prime': '\u2032',
}

_ENTITY_RE = re.compile(
	r'&(#(?:[xX][0-9a-fA-F]+|[0-9]+)|[a-zA-Z][a-zA-Z0-9]*);')


def _replace_entity(match):
	body = match.group(1)
	if body[0] == '#':
		try:
			if body[1] in 'xX':
				code = int(body[2:], 16)
			else:
				code = int(body[1:], 10)
		except (ValueError, IndexError):
			return match.group(0)
		if 0 < code <= 0x10FFFF:
			try:
				return chr(code)
			except (ValueError, OverflowError):
				return match.group(0)
		return match.group(0)
	return _NAMED_ENTITIES.get(body, match.group(0))


def unescape(text):
	"""Decode HTML entities (&amp; &lt; &gt; &quot; &#39; &nbsp; &#NNN; &#xHH;)."""
	if not isinstance(text, str):
		return ''
	if '&' not in text:
		return text
	return _ENTITY_RE.sub(_replace_entity, text)


# --------------------------------------------------------------------------
# 纯文本
# --------------------------------------------------------------------------

_RE_SCRIPT = re.compile(r'(?is)<script\b.*?</script\s*>')
_RE_STYLE = re.compile(r'(?is)<style\b.*?</style\s*>')
_RE_COMMENT = re.compile(r'(?s)<!--.*?-->')
_RE_BR = re.compile(r'(?is)<br\b[^>]*>')
_RE_P_CLOSE = re.compile(r'(?is)</p\s*>')
_RE_BLOCK_CLOSE = re.compile(
	r'(?is)</(?:div|li|tr|td|th|h[1-6]|section|article|blockquote|ul|ol|'
	r'table|dl|dt|dd|pre|p)\s*>')
_RE_ANY_TAG = re.compile(r'<[^>]*>')


def strip_tags(html):
	"""Remove all tags, unescape entities, collapse runs of whitespace.

	``<br>`` and ``</p>`` (and other block ends) become a newline first, so the
	result keeps one line per source line instead of gluing everything together.
	"""
	if not isinstance(html, str) or not html:
		return ''
	text = _RE_COMMENT.sub('', html)
	text = _RE_SCRIPT.sub('', text)
	text = _RE_STYLE.sub('', text)
	text = _RE_BR.sub('\n', text)
	text = _RE_P_CLOSE.sub('\n', text)
	text = _RE_BLOCK_CLOSE.sub('\n', text)
	text = _RE_ANY_TAG.sub('', text)
	text = unescape(text)
	text = text.replace('\r\n', '\n').replace('\r', '\n')
	text = re.sub(r'[ \t\f\v]+', ' ', text)
	text = re.sub(r' *\n *', '\n', text)
	text = re.sub(r'\n{2,}', '\n', text)
	return text.strip()


# --------------------------------------------------------------------------
# HTML -> Markdown
# --------------------------------------------------------------------------

# 一次匹配整个 <pre>/<table>/<code> 块（内容原样保留），否则匹配单个标签。
_TOKEN_RE = re.compile(
	r'(?is)<pre\b[^>]*>.*?</pre\s*>'
	r'|<table\b[^>]*>.*?</table\s*>'
	r'|<code\b[^>]*>.*?</code\s*>'
	r'|<[^>]+>')

_PRE_OPEN_RE = re.compile(r'(?is)^<pre\b[^>]*>')
_PRE_CLOSE_RE = re.compile(r'(?is)</pre\s*>$')
_CODE_OPEN_RE = re.compile(r'(?is)^<code\b[^>]*>')
_CODE_CLOSE_RE = re.compile(r'(?is)</code\s*>$')

_TAG_NAME_RE = re.compile(r'</?\s*([a-zA-Z][a-zA-Z0-9]*)')


def _tag_name(tag):
	match = _TAG_NAME_RE.match(tag)
	return match.group(1).lower() if match else ''


def _attr(tag, name):
	match = re.search(
		r'(?is)\b' + re.escape(name) +
		r'\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))', tag)
	if not match:
		return ''
	return match.group(1) or match.group(2) or match.group(3) or ''


def _trim_one_newline(text):
	"""Strip a single leading and trailing newline (keeps inner whitespace)."""
	if text.startswith('\r\n'):
		text = text[2:]
	elif text.startswith('\n') or text.startswith('\r'):
		text = text[1:]
	if text.endswith('\r\n'):
		text = text[:-2]
	elif text.endswith('\n') or text.endswith('\r'):
		text = text[:-1]
	return text


def _code_block(inner):
	inner = _trim_one_newline(unescape(inner))
	return '```\n' + inner + '\n```'


def _cell_text(html):
	text = re.sub(r'(?is)<br\b[^>]*>', ' ', html)
	text = _RE_ANY_TAG.sub('', text)
	text = unescape(text)
	text = re.sub(r'\s+', ' ', text).strip()
	return text.replace('|', '\\|')


def _table_to_markdown(table_html):
	rows = re.findall(r'(?is)<tr\b[^>]*>(.*?)</tr\s*>', table_html)
	if not rows:
		return ''
	grid = []
	for row in rows:
		cells = re.findall(r'(?is)<t[dh]\b[^>]*>(.*?)</t[dh]\s*>', row)
		grid.append([_cell_text(c) for c in cells])
	if not grid:
		return ''
	ncol = max(len(r) for r in grid)
	if ncol == 0:
		return ''
	for row in grid:
		while len(row) < ncol:
			row.append('')
	lines = ['| ' + ' | '.join(grid[0]) + ' |',
			 '| ' + ' | '.join(['---'] * ncol) + ' |']
	for row in grid[1:]:
		lines.append('| ' + ' | '.join(row) + ' |')
	return '\n'.join(lines)


def _handle_tag(tag, parts, state):
	"""Convert one ordinary tag into markdown fragments."""
	closing = tag.startswith('</')
	name = _tag_name(tag)
	if not name:
		return
	if name == 'br':
		parts.append('\n')
	elif name == 'hr':
		parts.append('\n\n---\n\n')
	elif name == 'img':
		src = _attr(tag, 'src')
		alt = _attr(tag, 'alt')
		if src:
			parts.append('![%s](%s)' % (alt, src))
		elif alt:
			parts.append(alt)
	elif name in ('strong', 'b'):
		parts.append('**')
	elif name in ('em', 'i'):
		parts.append('*')
	elif name == 'a':
		if closing:
			href = state['links'].pop() if state['links'] else ''
			parts.append('](%s)' % href)
		else:
			state['links'].append(_attr(tag, 'href'))
			parts.append('[')
	elif name in ('ul', 'ol'):
		if closing:
			if state['lists']:
				state['lists'].pop()
			parts.append('\n\n')
		else:
			state['lists'].append({'ordered': name == 'ol', 'count': 0})
			parts.append('\n\n')
	elif name == 'li':
		if not closing:
			if state['lists']:
				entry = state['lists'][-1]
			else:
				entry = {'ordered': False, 'count': 0}
			entry['count'] = entry.get('count', 0) + 1
			bullet = ('%d. ' % entry['count']) if entry['ordered'] else '- '
			parts.append('\n' + bullet)
	elif name == 'p':
		parts.append('\n\n')
	elif len(name) == 2 and name[0] == 'h' and name[1] in '123456':
		if closing:
			parts.append('\n\n')
		else:
			parts.append('\n\n' + '#' * int(name[1]) + ' ')
	# every other tag is simply dropped, its text is kept


#: Where a <pre>/<code>/<table> waits while the surrounding text is still
#: being cleaned up. The index is written without padding, so the digits are
#: greedy and the pattern can never match a longer index than the one stored.
_PLACEHOLDER_RE = re.compile(r'\x00B(\d+)\x00')


def html_to_markdown(html):
	"""Best-effort HTML -> Markdown for reading a problem statement in Sublime."""
	if not isinstance(html, str) or not html:
		return ''
	html = _RE_COMMENT.sub('', html)
	html = _RE_SCRIPT.sub('', html)
	html = _RE_STYLE.sub('', html)

	parts = []
	blocks = []
	state = {'links': [], 'lists': []}
	pos = 0
	for match in _TOKEN_RE.finditer(html):
		chunk = html[pos:match.start()]
		if chunk:
			parts.append(unescape(chunk))
		pos = match.end()
		token = match.group(0)
		low = token.lower()
		if low.startswith('<pre'):
			inner = _PRE_CLOSE_RE.sub('', _PRE_OPEN_RE.sub('', token, count=1),
									  count=1)
			blocks.append(_code_block(inner))
			parts.append('\n\n\x00B%d\x00\n\n' % (len(blocks) - 1))
		elif low.startswith('<table'):
			md = _table_to_markdown(token)
			if md:
				blocks.append(md)
				parts.append('\n\n\x00B%d\x00\n\n' % (len(blocks) - 1))
		elif low.startswith('<code'):
			inner = _CODE_CLOSE_RE.sub(
				'', _CODE_OPEN_RE.sub('', token, count=1), count=1)
			blocks.append(_code_block(inner))
			parts.append('\n\n\x00B%d\x00\n\n' % (len(blocks) - 1))
		else:
			_handle_tag(token, parts, state)
	tail = html[pos:]
	if tail:
		parts.append(unescape(tail))

	text = ''.join(parts)
	text = text.replace('\r\n', '\n').replace('\r', '\n')
	text = re.sub(r'[ \t\f\v]+', ' ', text)
	text = re.sub(r'[ \t]*\n[ \t]*', '\n', text)
	text = re.sub(r'\n{3,}', '\n\n', text)
	text = text.strip()
	# One pass, not one pass per block. The old loop scanned the whole text
	# once for every <pre>/<code>/<table>, which is quadratic: a page with
	# 8000 blocks spent 2.4s here alone (measured), and it runs on the UI
	# thread whenever the statement is opened. re.sub walks the text once and
	# the callback picks the block by index, so the result is identical.
	if blocks:
		text = _PLACEHOLDER_RE.sub(
			lambda m: blocks[int(m.group(1))], text)
	return text


# --------------------------------------------------------------------------
# 样例抽取
# --------------------------------------------------------------------------

_PRE_BLOCK_RE = re.compile(r'(?is)<pre\b[^>]*>(.*?)</pre\s*>')
_CLASS_RE = re.compile(
	r'(?is)<[a-zA-Z][^>]*\bclass\s*=\s*["\']([^"\']*)["\'][^>]*>')

_INPUT_KEYS = ['样例输入', '输入样例', 'sample input', 'input', '输入']
_OUTPUT_KEYS = ['样例输出', '输出样例', 'sample output', 'output', '输出']

# 约 300 个字符的前文里找分类关键字（更长的关键字排在前面优先命中）
_REGION_LEN = 300


def _div_class_kind(region):
	"""Look at the class of the last wrapping div/section before the <pre>."""
	kind = None
	for match in _CLASS_RE.finditer(region):
		tokens = re.split(r'[\s\-_]+', match.group(1).lower())
		for token in tokens:
			if token in ('input', 'in'):
				kind = 'input'
			elif token in ('output', 'out'):
				kind = 'output'
	return kind


def _text_kind(region):
	text = strip_tags(region).lower()
	for key in _INPUT_KEYS:
		if key in text:
			return 'input'
	for key in _OUTPUT_KEYS:
		if key in text:
			return 'output'
	return None


def _classify(region):
	kind = _div_class_kind(region)
	if kind is None:
		kind = _text_kind(region)
	return kind


def extract_samples(html):
	"""Find the sample input/output pairs of a problem statement.

	Returns a list of ``(input_text, output_text)`` tuples in page order.
	"""
	if not isinstance(html, str) or not html:
		return []
	try:
		blocks = []
		prev_end = 0
		for match in _PRE_BLOCK_RE.finditer(html):
			start = match.start()
			region = html[prev_end:start]
			if len(region) > _REGION_LEN:
				region = region[-_REGION_LEN:]
			inner = _trim_one_newline(unescape(match.group(1)))
			blocks.append({
				'kind': _classify(region),
				'text': inner,
			})
			prev_end = match.end()

		if not blocks:
			return []

		pairs = []
		pending_input = None
		classified_any = False
		for block in blocks:
			kind = block['kind']
			if kind is not None:
				classified_any = True
			if kind == 'input':
				pending_input = block['text']
			elif kind == 'output' and pending_input is not None:
				pairs.append((pending_input, block['text']))
				pending_input = None

		if not classified_any and len(blocks) >= 2:
			index = 0
			while index + 1 < len(blocks):
				pairs.append((blocks[index]['text'], blocks[index + 1]['text']))
				index += 2
		return pairs
	except Exception:
		return []


# --------------------------------------------------------------------------
# 标题
# --------------------------------------------------------------------------

_TITLE_RE = re.compile(r'(?is)<title\b[^>]*>(.*?)</title\s*>')
_H1_RE = re.compile(r'(?is)<h1\b[^>]*>(.*?)</h1\s*>')

_SITE_WORDS = (
	'codeforces', 'luogu', 'atcoder', 'codechef', 'vjudge', 'nowcoder',
	'topcoder', 'spoj', 'e-olymp', 'uva', 'hdu', 'poj', 'acm', 'oi',
	'洛谷', '牛客', '计蒜客', '题库',
)
_DOMAIN_RE = re.compile(r'^[A-Za-z0-9._-]+\.(?:com|org|net|cn|io|me|dev)$')


def _looks_like_site(text):
	stripped = text.strip()
	if not stripped:
		return False
	low = stripped.lower()
	for word in _SITE_WORDS:
		if word in low:
			return True
	return bool(_DOMAIN_RE.match(stripped))


def problem_title(html):
	"""Best-effort title: <title>, else the first <h1>, else ''."""
	if not isinstance(html, str) or not html:
		return ''
	title = ''
	match = _TITLE_RE.search(html)
	if match:
		title = strip_tags(match.group(1)).strip()
	if not title:
		match = _H1_RE.search(html)
		if match:
			title = strip_tags(match.group(1)).strip()
	if not title:
		return ''
	index = max(title.rfind(' - '), title.rfind(' | '))
	if index != -1:
		head = title[:index].strip()
		tail = title[index + 3:]
		if head and _looks_like_site(tail):
			title = head
	return title.strip()

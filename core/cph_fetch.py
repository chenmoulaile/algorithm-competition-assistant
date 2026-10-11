"""Algorithm Competition Assistant - 从题目 URL 抓题面与样例（没有 Competitive Companion 时用）

用标准库抓取网页，交给 core/cph_html 解析：样例变成测试点，题面转成 Markdown
可以在 Sublime 里读。

没有针对每个站点的硬编码选择器：样例的识别靠 `<pre>` 块 + 前面的标签文字
（样例输入/样例输出/Sample Input/...），所以洛谷、Codeforces、AtCoder 以及
大多数自建 OJ 都能用；抓不到时如实返回空，而不是猜。

放在 core/ 是因为根级插件模块之间不允许互相 import（Package Control 审查规则）。
"""

import re
import urllib.request
import urllib.error

from .cph_html import extract_samples, html_to_markdown, problem_title, strip_tags
from .cph_i18n import t
from .cph_settings import get_settings

#: Some judges (Codeforces in particular) serve a challenge page to unknown
#: clients, so send a normal browser UA.
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
			  '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')

URL_RE = re.compile(r'^https?://\S+$', re.I)

#: Pages heavier than this are refused instead of buffered (the
#: max_fetch_bytes setting, in bytes; the shipped settings file carries the
#: same default). It bounds how big a PROBLEM PAGE may be - it is unrelated
#: to max_output_bytes, which bounds how much of a program's own output the
#: run panel keeps.
DEFAULT_MAX_FETCH_BYTES = 8388608

#: Seconds before a problem page is declared unreachable (the
#: fetch_timeout_seconds setting; the shipped settings file carries the same
#: default). A slow judge used to be indistinguishable from a dead one: the
#: value was a hard-coded 25 in the signature, so a page that legitimately
#: needs 40s on a bad connection could never be fetched at all.
DEFAULT_FETCH_TIMEOUT = 25

#: The body is read in chunks of this size and discarded (with an error) as
#: soon as the next chunk would push the total past the cap, so an oversized
#: page is never fully downloaded, let alone silently truncated.
CHUNK_SIZE = 65536


class _ResponseTooLarge(Exception):
	"""Raised by _read_capped() when the body outgrows max_fetch_bytes."""
	pass


def _max_fetch_bytes():
	"""The configured page-size cap, always a sane positive int."""
	try:
		value = get_settings().get('max_fetch_bytes', DEFAULT_MAX_FETCH_BYTES)
	except Exception:
		return DEFAULT_MAX_FETCH_BYTES
	try:
		value = int(value)
	except (TypeError, ValueError):
		return DEFAULT_MAX_FETCH_BYTES
	return value if value > 0 else DEFAULT_MAX_FETCH_BYTES


def _fetch_timeout():
	"""The configured per-request timeout in seconds, always a sane positive int.

	A slow judge is not a dead judge, so this is a setting rather than the
	hard-coded 25 the signature used to carry. Values that are not a positive
	number fall back to the default instead of raising inside a download.
	"""
	try:
		value = get_settings().get('fetch_timeout_seconds',
								   DEFAULT_FETCH_TIMEOUT)
	except Exception:
		return DEFAULT_FETCH_TIMEOUT
	try:
		value = int(value)
	except (TypeError, ValueError):
		return DEFAULT_FETCH_TIMEOUT
	return value if value > 0 else DEFAULT_FETCH_TIMEOUT


def _read_capped(response, limit):
	"""Read the body in chunks, refusing pages heavier than limit bytes.

	Raises _ResponseTooLarge as soon as one more byte would blow the cap:
	nothing past it is downloaded or buffered, and the caller reports a
	failure instead of handing back a truncated page.
	"""
	chunks = []
	total = 0
	while True:
		chunk = response.read(CHUNK_SIZE)
		if not chunk:
			break
		total += len(chunk)
		if total > limit:
			raise _ResponseTooLarge()
		chunks.append(chunk)
	return b''.join(chunks)


def _format_bytes(n):
	if n >= 1048576:
		return '%.1f MB' % (n / 1048576.0)
	if n >= 1024:
		return '%.1f KB' % (n / 1024.0)
	return '%d B' % n


def looks_like_url(text):
	return bool(text) and bool(URL_RE.match(text.strip()))


def fetch(url, timeout=None):
	"""Download a page. Returns (html, error_message).

	``timeout`` overrides the fetch_timeout_seconds setting; callers that do
	not care pass nothing and get the configured value.
	"""
	url = (url or '').strip()
	if not looks_like_url(url):
		return '', t('fetch_bad_url', url=url)
	request = urllib.request.Request(url)
	request.add_header('User-Agent', USER_AGENT)
	request.add_header('Accept-Language', 'zh-CN,zh;q=0.9,en;q=0.8')
	limit = _max_fetch_bytes()
	if timeout is None:
		timeout = _fetch_timeout()
	try:
		with urllib.request.urlopen(request, timeout=timeout) as response:
			raw = _read_capped(response, limit)
	except _ResponseTooLarge:
		return '', t('fetch_response_too_large', limit=_format_bytes(limit))
	except urllib.error.HTTPError as e:
		return '', t('fetch_http_error', code=e.code, url=url)
	except Exception as e:
		return '', t('fetch_failed', url=url, error=str(e))

	for encoding in ('utf-8', 'gb18030', 'latin-1'):
		try:
			return raw.decode(encoding), ''
		except Exception:
			continue
	return raw.decode('utf-8', 'replace'), ''


def samples(html):
	"""[(input, output)] pairs found in the page (may be empty)."""
	try:
		return extract_samples(html or '')
	except Exception:
		return []


def statement(html, url=''):
	"""Markdown version of the statement, with the source URL at the top."""
	title = ''
	try:
		title = problem_title(html or '')
	except Exception:
		title = ''
	body = ''
	try:
		body = html_to_markdown(html or '')
	except Exception:
		body = ''
	header = ''
	if title:
		header += '# %s\n\n' % title
	if url:
		header += t('statement_source', url=url) + '\n\n'
	return header + body


def title_of(html):
	try:
		return problem_title(html or '')
	except Exception:
		return ''


def describe_samples(pairs):
	"""One-line summary for the status bar."""
	if not pairs:
		return t('fetch_no_samples')
	return t('fetch_found_samples', n=len(pairs))


def summarize(html, limit=160):
	"""One line of prose from the statement, for the status bar.

	A page without a <title> and without an <h1> leaves `problem_title()`
	empty, and the status bar then shows the bare URL - which says nothing
	about whether the right problem was fetched. The longest visible line is
	a decent stand-in: navigation and sample blocks are short, problem
	statements are not. Returns '' when the page has no text at all.
	"""
	try:
		text = strip_tags(html or '')
	except Exception:
		return ''
	best = ''
	for line in text.split('\n'):
		line = line.strip()
		if len(line) > len(best):
			best = line
	if len(best) > limit:
		best = best[:limit - 3].rstrip() + '...'
	return best

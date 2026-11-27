"""UTF-8 byte accounting shared by every output cap.

The `max_output_bytes` setting is documented in bytes, but the serial panel,
the pipe reader in ProcessManager and the parallel worker all used to count
Python characters. For ASCII they agree; for Chinese or any non-ASCII output
one character is up to three bytes, so an 8 MB "byte" cap could hold 24 MB
of text and freeze the view.

Both helpers are deliberately tiny and dependency-free: the plugin host is
Python 3.3, and this module is imported from both root-level plugins and
`Modules/`.
"""


def utf8_len(text):
	"""Length of `text` in UTF-8 bytes (unpaired surrogates become '?').

	`errors='replace'` is used instead of 'strict' because a program can emit
	lone surrogates through the pipe; the cap must never raise on output.
	"""
	if not text:
		return 0
	return len(str(text).encode('utf-8', 'replace'))


def truncate_utf8(text, limit):
	"""Longest prefix of `text` that fits in `limit` UTF-8 bytes.

	Never splits a multi-byte character: the result is always valid text.
	`limit <= 0` means unlimited and returns `text` unchanged.
	"""
	if limit <= 0:
		return text
	text = str(text)
	if utf8_len(text) <= limit:
		return text
	# Slicing the encoded bytes and dropping an incomplete tail is both
	# faster and simpler than walking characters one by one.
	encoded = text.encode('utf-8', 'replace')[:limit]
	return encoded.decode('utf-8', 'ignore')
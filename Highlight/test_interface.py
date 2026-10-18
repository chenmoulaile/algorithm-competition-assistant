import sublime

from ..core.cph_resources import read_resource


def get_test_styles(view):
	"""CSS for the test cards. Read through the zip-safe resource loader
	(and cached) instead of opening files on disk every refresh.

	Matched on the theme's own name rather than the exact settings string: the
	value differs by case, by path and by the name a fork gave it ("Spacegray
	Light Custom.sublime-theme"), and every one of those silently lost the
	variant stylesheet and got the default.
	"""
	theme = str(view.settings().get('theme') or '').lower()
	name = theme.rsplit('/', 1)[-1]
	if 'spacegray' in name and 'light' in name:
		return read_resource('Highlight/test_styles_spacegraylight.css')
	if 'spacegray' in name:
		return read_resource('Highlight/test_styles_spacegray.css')
	return read_resource('Highlight/test_styles.css')

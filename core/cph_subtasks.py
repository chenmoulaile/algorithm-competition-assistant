"""Algorithm Competition Assistant - Subtask（子任务）分组计分

NOI / OI 赛制里测试点是分组的：**组内全部 AC 才拿到这一组的分**，否则该组
0 分（哪怕组里只有一个点挂）。汇总行因此要显示"部分分"，而不是单纯的
通过数。

配置（settings 里的 ``subtasks``）::

    [
      {"name": "Sub1", "from": 1, "to": 3, "score": 30},
      {"name": "Sub2", "from": 4, "to": 10, "score": 70}
    ]

``from`` / ``to`` 是测试点序号（从 1 开始，含两端）。没有配置时所有函数都
返回空结果，调用方照旧显示原来的通过数。

放在 core/ 是因为根级插件模块之间不允许互相 import（Package Control 审查规则）。
"""


def parse(raw):
	"""Normalise the ``subtasks`` setting into a list of clean dicts.

	Bad entries are dropped rather than raising: a typo in the settings must
	not break the summary bar.
	"""
	groups = []
	if not isinstance(raw, list):
		return groups
	for index, entry in enumerate(raw):
		if not isinstance(entry, dict):
			continue
		try:
			start = int(entry.get('from', 0))
			end = int(entry.get('to', 0))
		except (TypeError, ValueError):
			continue
		if start <= 0 or end < start:
			continue
		try:
			score = int(entry.get('score', 0))
		except (TypeError, ValueError):
			score = 0
		name = entry.get('name') or ('Sub%d' % (index + 1))
		groups.append({'name': str(name), 'from': start, 'to': end,
					   'score': max(0, score)})
	groups.sort(key=lambda item: item['from'])
	return groups


def evaluate(verdicts, groups):
	"""Score a run.

	``verdicts`` is a list of verdict short names ('AC', 'WA', ...) or None
	for tests that were not judged, indexed from test 1.
	Returns {'total': int, 'max': int, 'groups': [...]}, or None when there
	are no groups configured.
	"""
	if not groups:
		return None
	result = {'total': 0, 'max': 0, 'groups': []}
	for group in groups:
		collected = []
		for index in range(group['from'] - 1, group['to']):
			if 0 <= index < len(verdicts):
				collected.append(verdicts[index])
			else:
				collected.append(None)
		judged = [v for v in collected if v]
		# A group counts as passed only when every test in it was judged and
		# judged AC - an unjudged test is not a pass.
		passed = bool(collected) and all(v == 'AC' for v in collected)
		score = group['score'] if passed else 0
		result['total'] += score
		result['max'] += group['score']
		result['groups'].append({
			'name': group['name'],
			'from': group['from'],
			'to': group['to'],
			'passed': passed,
			'score': score,
			'max': group['score'],
			'judged': len(judged),
			'total': len(collected),
		})
	return result


def summary_text(result):
	"""One short line like 'Subtask 30/100 - Sub1 0/30, Sub2 30/70'."""
	if not result:
		return ''
	parts = []
	for group in result['groups']:
		parts.append('%s %d/%d%s' % (group['name'], group['score'], group['max'],
									 '' if group['passed'] else ' x'))
	return '%d/%d  %s' % (result['total'], result['max'], ' · '.join(parts))


def html(result, ok_color='#49cd32', bad_color='#d3140d'):
	"""minihtml fragment for the summary bar (empty when no groups)."""
	if not result:
		return ''
	pieces = []
	for group in result['groups']:
		color = ok_color if group['passed'] else bad_color
		pieces.append('<span style="color: %s;">%s %d/%d</span>'
					  % (color, group['name'], group['score'], group['max']))
	return ('<span style="color: var(--foreground); opacity: 0.75;">'
			'Subtask %d/%d</span>&nbsp;&nbsp;%s'
			% (result['total'], result['max'], '&nbsp;·&nbsp;'.join(pieces)))

"""Algorithm Competition Assistant - 测试数据合并策略（唯一实现）

导入 / 剪贴板 / Competitive Companion / 对拍反例四条路径以前各写一份合并逻辑，
语义还不一致：有的按 (输入, 答案) 双键去重，有的按输入单键。结果是同一份输入
可能并存两条，或者浏览器重发时把用户手动 accept 的答案顶掉。

统一策略（改这里就够了）：

- **按输入去重**：同一份输入永远只保留一个测试点；
- **答案只补不覆盖**：原有答案为空时用新答案补上；两边都有且不同则记为冲突，
  保留原有值并由调用方提示用户。用户手动标记的答案优先于自动来源。
"""


def merge_tests(existing, incoming):
	"""Merge two lists of test dicts.

	Returns (merged, conflicts):
	  merged    - new list of dicts, deduplicated by input
	  conflicts - inputs whose answers existed on both sides and differed
	"""
	merged = []
	index = {}
	conflicts = []
	for item in list(existing or []) + list(incoming or []):
		key = item.get('test', '')
		answers = list(item.get('correct_answers') or [])
		if key in index:
			kept = merged[index[key]]
			current = list(kept.get('correct_answers') or [])
			if answers and not current:
				kept['correct_answers'] = answers
			elif answers and sorted(answers) != sorted(current):
				conflicts.append(key)
			continue
		index[key] = len(merged)
		merged.append(dict(item))
	return merged, conflicts


def merge_into_file(file_name, incoming):
	"""Merge new tests into the stored ones and save.

	Returns (saved, total, conflicts). The load+merge+save is one critical
	section (`cph_settings.tests_lock()`): otherwise another writer could
	save between our read and write and its tests would be overwritten by the
	stale snapshot.
	"""
	from .cph_settings import load_all_tests, save_tests, tests_lock
	with tests_lock():
		existing = load_all_tests(file_name) or []
		merged, conflicts = merge_tests(existing, incoming)
		saved = bool(save_tests(file_name, merged))
	return saved, len(merged), conflicts

"""Algorithm Competition Assistant - 本地练习统计存储

纯标准库（os / sys / json / time / tempfile），不 import 任何其它插件模块，
可以被根级插件、core 子包或独立脚本安全加载。宿主是 Python 3.3，所以这里
只用 % 格式化、time.strftime / time.mktime(time.strptime(...)) 做日期运算，
不用 f-string、海象运算符、os.scandir、datetime.fromisoformat 等新特性。

存储位置（见 stats_path）：
  1. 环境变量 CPH_STATS_PATH 覆盖（测试 / 自定义用，文档化入口）；
  2. sublime.packages_path() + '/User/cph-by-chenkx-stats.json'，
     放在 User 目录里，插件升级不丢、用户也能直接看到；
  3. 上面拿不到时退回系统临时目录。
sublime 在运行时才存在，缺失或抛异常时自动降级，绝不因此报错。

数据格式：
  {
    "problems": {
      "<key>": {
        "file":         "<原始路径>",
        "runs":         已判题运行总次数,
        "attempts":     未完全通过（非 all-AC）的运行次数,
        "first_ac":     第一次 all-AC 的 ISO 时间戳（保留，后续不覆盖）,
        "solved":       是否至少 all-AC 过一次,
        "last":         最近一次运行的 ISO 时间戳,
        "last_verdict": 最近一次运行里最严重的判题结果,
        "ac_runs":      all-AC 的运行次数,
        "by_verdict":   {判题短名: 累计次数},
        "stress_rounds": 对拍累计轮数
      }
    },
    "history": [
      {"key":..,"file":..,"time":ISO,"verdict":..,"all_ac":bool}
    ]
  }

history 只保留最新 _HISTORY_LIMIT 条（见该常量）；problems 里的累计字段
（runs / attempts / ac_runs / first_ac / solved / by_verdict / stress_rounds）
是永久账本，从不丢弃。
"""

import json
import os
import sys
import tempfile
import time

# ISO 时间戳格式，strftime / strptime 两端保持一致（本地时区）。
_ISO_FMT = '%Y-%m-%dT%H:%M:%S'

# 判题结果优先级：越靠前越"严重"。worst_verdict 返回其中优先级最高的一个。
# CE 最高（根本编译不过），其后依次是 RE / TLE / MLE / OLE / WA / PE / UKE。
_VERDICT_PRIORITY = ['CE', 'RE', 'TLE', 'MLE', 'OLE', 'WA', 'PE', 'UKE']

_STATS_NAME = 'cph-by-chenkx-stats.json'

# 历史日志的条数上限。record_run() 每次运行都要"读全量 + 追加一条 + 写全量"，
# 而这条记录是**永久增长**的：实测 4000 条时文件 636 KB、单次调用 18.4 ms
# （500 条 3.4 ms、1000 条 5.6 ms、2000 条 9.8 ms，总成本是二次的，20000 条
# 两分钟都跑不完）。这是本地练习统计，不是审计日志：problems 里的累计字段
# （runs / attempts / ac_runs / first_ac / solved / by_verdict）才是真正的账本，
# history 只是"最近发生了什么"的日志，供日报表和窗口统计使用，所以可以截断。
# 保留最新 5000 条 ≈ 重度使用数周，远超默认 7 天的报表窗口。
_HISTORY_LIMIT = 5000

# 最近一次 save() 失败的原因，由 take_save_error() 取走（见 save 的 docstring）。
_last_save_error = ''


def stats_path():
	"""返回 JSON 存储的绝对路径。

	优先读环境变量 CPH_STATS_PATH（测试和高级用户可用它把数据放到别处）；
	否则放到 Sublime 的 User 包目录（升级不丢、用户可见）；sublime 不可用
	或 packages_path() 抛异常时退回临时目录。此函数本身不抛异常。
	"""
	override = os.environ.get('CPH_STATS_PATH')
	if override:
		return override

	base = None
	try:
		import sublime
		base = sublime.packages_path()
	except Exception:
		base = None

	if base:
		return os.path.join(base, 'User', _STATS_NAME)

	try:
		tmp = tempfile.gettempdir()
	except Exception:
		tmp = os.getcwd()
	return os.path.join(tmp, _STATS_NAME)


def load():
	"""读取并解析存储；文件缺失或损坏时返回 {}，永不抛异常。"""
	try:
		f = open(stats_path(), 'r', encoding='utf-8')
	except Exception:
		return {}
	try:
		data = json.load(f)
	except Exception:
		return {}
	finally:
		try:
			f.close()
		except Exception:
			pass
	if not isinstance(data, dict):
		return {}
	return data


def save(data):
	"""原子写入存储：先写同目录下的 .tmp，再 os.replace 覆盖。

	返回 True/False，永不抛异常。父目录不存在时会尽力创建；创建失败
	（权限等）则返回 False，调用方不会因此崩溃。

	失败原因另外存进 _last_save_error，调用方用 take_save_error() 取走
	并显示。本模块是纯标准库、不 import 其它插件模块，所以没法自己弹
	状态栏：不留下这个原因，"统计没存下来"就完全静默。
	"""
	global _last_save_error
	path = stats_path()
	tmp = path + '.tmp'

	try:
		parent = os.path.dirname(path)
		if parent and not os.path.isdir(parent):
			os.makedirs(parent)
	except Exception:
		pass

	try:
		f = open(tmp, 'w', encoding='utf-8')
		try:
			json.dump(data, f, ensure_ascii=False, indent=2)
		finally:
			try:
				f.close()
			except Exception:
				pass
		os.replace(tmp, path)
		_last_save_error = ''
		return True
	except Exception as e:
		_last_save_error = '%s: %s' % (path, e)
		try:
			if os.path.exists(tmp):
				os.remove(tmp)
		except Exception:
			pass
		return False


def take_save_error():
	"""Return the last save() failure and forget it ('' when there was none).

	Reading clears the value so a broken store is reported once per failure
	instead of on every run.
	"""
	global _last_save_error
	error = _last_save_error
	_last_save_error = ''
	return error


def key_for(file):
	"""问题的稳定 key。

	用**规范化后的完整路径**，而不是文件名：不同目录下的同名文件是不同题目
	（例如 a/main.cpp 和 b/main.cpp）。反斜杠统一成 '/'，Windows 上再转小写
	（Windows 文件系统大小写不敏感，避免同一文件两种写法产生两条记录）。
	调用方应传入绝对路径，本函数只做分隔符 / 大小写的规范化。
	"""
	if file is None:
		return ''
	p = str(file).replace('\\', '/')
	if sys.platform.startswith('win'):
		p = p.lower()
	return p


def worst_verdict(verdicts):
	"""返回一组判题结果里最严重的一个。

	全部为 'AC'（含空列表）时返回 'AC'；否则按 _VERDICT_PRIORITY
	（CE > RE > TLE > MLE > OLE > WA > PE > UKE）返回优先级最高者；
	出现未知短名时，返回其中第一个非 'AC' 的项。
	"""
	items = list(verdicts or [])
	if not items:
		return 'AC'
	all_ac = True
	for v in items:
		if v != 'AC':
			all_ac = False
			break
	if all_ac:
		return 'AC'
	present = set(items)
	for v in _VERDICT_PRIORITY:
		if v in present:
			return v
	for v in items:
		if v != 'AC':
			return v
	return 'AC'


def _to_epoch(now):
	"""把 now（None / epoch 数字 / ISO 字符串）统一成 epoch 秒。"""
	if now is None:
		return time.time()
	if isinstance(now, str):
		e = _iso_to_epoch(now)
		if e is not None:
			return e
		return time.time()
	try:
		return float(now)
	except Exception:
		return time.time()


def _iso(ts):
	"""epoch 秒 -> 本地时区 ISO 字符串。"""
	try:
		return time.strftime(_ISO_FMT, time.localtime(ts))
	except Exception:
		return time.strftime(_ISO_FMT, time.localtime(time.time()))


def _iso_to_epoch(s):
	"""ISO 字符串 -> epoch 秒；解析失败返回 None。"""
	if not isinstance(s, str):
		return None
	try:
		return time.mktime(time.strptime(s[:19], _ISO_FMT))
	except Exception:
		return None


def _short(path):
	"""只取路径最后一段，让报告行保持简短。"""
	if not path:
		return '(unknown)'
	p = str(path).replace('\\', '/')
	return p.rsplit('/', 1)[-1]


def _fmt_duration(seconds):
	"""把秒数格式化成 '1h 20m' / '5m 3s' / '42s'。"""
	try:
		seconds = int(seconds)
	except Exception:
		seconds = 0
	if seconds < 0:
		seconds = 0
	h = seconds // 3600
	m = (seconds % 3600) // 60
	s = seconds % 60
	if h:
		return '%dh %dm' % (h, m)
	if m:
		return '%dm %ds' % (m, s)
	return '%ds' % s


def record_run(file, verdicts, now=None):
	"""记录 file 的一次判题运行，返回更新后的问题记录 dict。

	verdicts 是判题短名列表（'AC' / 'WA' / 'TLE' ...），顺序无关。
	attempts 的定义：**未完全通过（非 all-AC）的运行次数**。第一次 AC 之前
	的每次运行都是非 AC，第一次 AC 之后只有非 all-AC 的运行才计入，合起来
	正好等于"非 all-AC 的运行数"，实现上就按后者简单计数。
	first_ac 只写一次，之后的运行永不覆盖。
	"""
	items = list(verdicts or [])
	ts = _to_epoch(now)
	iso = _iso(ts)
	key = key_for(file)
	all_ac = bool(items)
	for v in items:
		if v != 'AC':
			all_ac = False
			break
	worst = 'UKE' if not items else worst_verdict(items)

	data = load()
	problems = data.get('problems')
	if not isinstance(problems, dict):
		problems = {}
	rec = problems.get(key)
	if not isinstance(rec, dict):
		rec = {}

	rec['file'] = file
	rec['runs'] = int(rec.get('runs') or 0) + 1
	rec['attempts'] = int(rec.get('attempts') or 0)
	rec['ac_runs'] = int(rec.get('ac_runs') or 0)

	if all_ac:
		rec['ac_runs'] += 1
		rec['solved'] = True
		if not rec.get('first_ac'):
			rec['first_ac'] = iso
	else:
		rec['attempts'] += 1
		if 'solved' not in rec:
			rec['solved'] = False
		if 'first_ac' not in rec:
			rec['first_ac'] = None

	rec['last'] = iso
	rec['last_verdict'] = worst

	by_verdict = rec.get('by_verdict')
	if not isinstance(by_verdict, dict):
		by_verdict = {}
	for v in items:
		by_verdict[v] = int(by_verdict.get(v) or 0) + 1
	rec['by_verdict'] = by_verdict
	if 'stress_rounds' not in rec:
		rec['stress_rounds'] = 0

	problems[key] = rec
	data['problems'] = problems

	history = data.get('history')
	if not isinstance(history, list):
		history = []
	history.append({
		'key': key,
		'file': file,
		'time': iso,
		'verdict': worst,
		'all_ac': all_ac,
	})
	# 只保留最新 _HISTORY_LIMIT 条（见该常量的注释）：problems 里的累计字段
	# 才是账本，history 只是"最近发生了什么"。丢掉的都是最老的记录，而报表
	# 窗口默认只有 7 天。"已解决"因此不能只靠 history 判断——summary_data
	# 会先读 problems 里的 first_ac，再拿 history 补齐。
	if len(history) > _HISTORY_LIMIT:
		del history[:len(history) - _HISTORY_LIMIT]
	data['history'] = history

	save(data)
	return rec


def record_stress(file, rounds, now=None):
	"""给问题的 'stress_rounds' 累加 rounds 轮对拍。返回问题记录。"""
	key = key_for(file)
	try:
		rounds = int(rounds)
	except Exception:
		return None
	if rounds < 0:
		rounds = 0

	data = load()
	problems = data.get('problems')
	if not isinstance(problems, dict):
		problems = {}
	rec = problems.get(key)
	if not isinstance(rec, dict):
		rec = {}
	rec['file'] = file
	rec['stress_rounds'] = int(rec.get('stress_rounds') or 0) + rounds
	problems[key] = rec
	data['problems'] = problems
	save(data)
	return rec


def summary_data(days=7, now=None):
	"""Return the practice report as data instead of prose.

	``summary()`` prints English lines because this module is deliberately
	dependency-free (pure stdlib, also loadable from a plain script). The UI
	wants the same numbers in the active language, so the aggregation lives
	here once and the rendering happens in two thin layers: ``summary()`` for
	the default English text and ``render_summary()`` for the caller that has
	``cph_i18n`` available. Both consume this one function, so they can never
	drift apart.

	Never raises.
	"""
	try:
		days = int(days)
	except Exception:
		days = 7
	if days < 0:
		days = 0
	now_ts = _to_epoch(now)
	cutoff = now_ts - days * 86400

	data = load()
	history = data.get('history')
	if not isinstance(history, list):
		history = []

	parsed = []
	for e in history:
		if not isinstance(e, dict):
			continue
		t = _iso_to_epoch(e.get('time'))
		if t is None:
			continue
		parsed.append((t, e))

	window = [(t, e) for (t, e) in parsed if t >= cutoff]

	touched = []
	seen = set()
	for t, e in window:
		k = e.get('key')
		if k not in seen:
			seen.add(k)
			touched.append(k)

	# 首次 AC：优先读 problems 里的 first_ac（真正的账本，见 _HISTORY_LIMIT 的
	# 注释），history 只作兜底。history 会被截断，最老的那条 AC 可能已经不在
	# 里面了，而"一旦解决过就永远是已解决"不能因此退回。
	first_ac = {}
	probs = data.get('problems')
	if isinstance(probs, dict):
		for k, rec in probs.items():
			if not isinstance(rec, dict):
				continue
			t = _iso_to_epoch(rec.get('first_ac'))
			if t is not None:
				first_ac[k] = t
	for t, e in parsed:
		k = e.get('key')
		if e.get('all_ac'):
			if k not in first_ac or t < first_ac[k]:
				first_ac[k] = t

	solved = 0
	for k in touched:
		if k in first_ac:
			solved += 1

	# 每日表格
	day_runs = {}
	day_order = []
	for t, e in window:
		d = time.strftime('%Y-%m-%d', time.localtime(t))
		if d not in day_runs:
			day_runs[d] = 0
			day_order.append(d)
		day_runs[d] += 1
	day_solved = {}
	for k in first_ac:
		d = time.strftime('%Y-%m-%d', time.localtime(first_ac[k]))
		day_solved[d] = day_solved.get(d, 0) + 1

	per_day = []
	for d in sorted(day_order):
		per_day.append({
			'date': d,
			'runs': day_runs[d],
			'solved': day_solved.get(d, 0),
		})

	# 最近一次运行 + 尝试次数
	last_by_key = {}
	for t, e in parsed:
		k = e.get('key')
		if k not in last_by_key or t >= last_by_key[k][0]:
			last_by_key[k] = (t, e)

	# Attempt counts come from the permanent per-problem ledger first. The
	# history log is capped at _HISTORY_LIMIT and only describes "recently",
	# so counting its rows understated a hard problem as soon as its older
	# attempts scrolled out - and the report then contradicted the very ledger
	# record_run() had just written. max() keeps the number honest for stores
	# written before the ledger carried the field.
	ledger = {}
	problems = data.get('problems')
	if isinstance(problems, dict):
		for k, rec in problems.items():
			if isinstance(rec, dict):
				ledger[k] = rec

	attempts_by_key = {}
	for t, e in parsed:
		k = e.get('key')
		if not e.get('all_ac'):
			attempts_by_key[k] = attempts_by_key.get(k, 0) + 1
	for k, rec in ledger.items():
		stored = rec.get('attempts')
		if stored is None:
			continue
		try:
			attempts_by_key[k] = max(int(stored), attempts_by_key.get(k, 0))
		except (TypeError, ValueError):
			pass

	# Same reason: by_verdict has been recorded per problem since it was added
	# and shown nowhere. Summing the ledger is exact, unlike sampling history.
	verdicts = {}
	for rec in ledger.values():
		by_verdict = rec.get('by_verdict')
		if not isinstance(by_verdict, dict):
			continue
		for name, count in by_verdict.items():
			try:
				verdicts[name] = verdicts.get(name, 0) + int(count)
			except (TypeError, ValueError):
				pass

	needs = []
	for k in touched:
		entry = last_by_key.get(k)
		if entry is None:
			continue
		if not entry[1].get('all_ac'):
			needs.append({
				'file': _short(entry[1].get('file')),
				'attempts': attempts_by_key.get(k, 0),
			})
	needs.sort(key=lambda item: (-item['attempts'], item['file']))
	needs = needs[:10]

	# Stress rounds were written since the stress runner was wired to
	# record_stress(); older stores simply have no field, which reads as 0.
	stress_rounds = 0
	if isinstance(problems, dict):
		for rec in problems.values():
			if isinstance(rec, dict):
				try:
					stress_rounds += int(rec.get('stress_rounds') or 0)
				except Exception:
					pass

	# Span, NOT time on task. This is max(timestamp) - min(timestamp) over the
	# runs in the window: a session from 09:00 to 12:00 with one run in it
	# reports three hours even though the user worked for ten minutes. It is
	# deliberately not called "focused time" anywhere in the UI - the label
	# says "总用时 / Total time on task" and this comment is the definition.
	# Measuring real time on task would need idle detection, which this plugin
	# has no way to observe.
	span = 0
	if window:
		times = [t for (t, e) in window]
		span = max(times) - min(times)

	return {
		'days': days,
		'problems': len(touched),
		'runs': len(window),
		'solved': solved,
		'per_day': per_day,
		'needs': needs,
		'verdicts': verdicts,
		'stress_rounds': stress_rounds,
		'span': span,
	}


def _fmt_duration_public(seconds):
	"""``_fmt_duration`` with a name callers outside this module may use."""
	return _fmt_duration(seconds)


def render_summary(info, translate=None):
	"""Render ``summary_data()`` output, optionally through ``translate()``.

	``translate`` is a callable ``(key, **kwargs) -> str``; the caller passes
	``cph_i18n.t``. Without it the same English strings ``summary()`` produces
	are used, so the report is identical on both paths except for the labels.
	Never raises.
	"""
	def say(key, **kwargs):
		if translate is not None:
			try:
				return (translate(key, **kwargs) or '').strip()
			except Exception:
				pass
		return _ENGLISH_LABELS.get(key, '').format(**{k: kwargs.get(k, '')
													  for k in _LABEL_FIELDS.get(key, ())})

	if not info:
		return ''
	lines = []
	lines.append(say('stats_summary_header', days=info['days'],
					 problems=info['problems'], runs=info['runs']))
	lines.append(say('stats_summary_solved', count=info['solved']))
	if info.get('verdicts'):
		_items = sorted(info['verdicts'].items(), key=lambda kv: (-kv[1], kv[0]))
		lines.append(say('stats_summary_verdicts',
						 list='  '.join('%s %d' % (name, count)
										for name, count in _items)))
	lines.append(say('stats_summary_per_day'))
	if info['per_day']:
		for row in info['per_day']:
			lines.append(say('stats_summary_day_row', date=row['date'],
							 runs=row['runs'], solved=row['solved']))
	else:
		lines.append(say('stats_summary_no_runs'))
	lines.append(say('stats_summary_needs_work', count=len(info['needs'])))
	if info['needs']:
		for index, row in enumerate(info['needs']):
			lines.append(say('stats_summary_needs_row', index=index + 1,
							 file=row['file'], attempts=row['attempts']))
	else:
		lines.append(say('stats_summary_needs_none'))
	if info.get('stress_rounds'):
		lines.append(say('stats_summary_stress', count=info['stress_rounds']))
	if info.get('span'):
		lines.append(say('stats_summary_total_time',
						 duration=_fmt_duration(info['span'])))
	return '\n'.join(line for line in lines if line is not None)


#: English fallback labels, keyed by the cph_i18n key used by render_summary().
_ENGLISH_LABELS = {
	'stats_summary_header': 'CPH practice stats (last {days} days): '
							'{problems} problems, {runs} runs',
	'stats_summary_solved': 'Solved: {count}',
	'stats_summary_verdicts': 'Verdicts: {list}',
	'stats_summary_per_day': 'Per-day:',
	'stats_summary_day_row': '  {date}  {runs} runs  {solved} solved',
	'stats_summary_no_runs': '  (no runs)',
	'stats_summary_needs_work': 'Needs work ({count}):',
	'stats_summary_needs_row': '  {index}. {file}  attempts={attempts}',
	'stats_summary_needs_none': '  none',
	'stats_summary_stress': 'Stress rounds: {count}',
	'stats_summary_total_time': 'Total time on task: {duration}',
}

#: Which kwargs each label formats with; keeps a missing key from blowing up
#: ``str.format`` (a KeyError here would take the whole report down).
_LABEL_FIELDS = {
	'stats_summary_header': ('days', 'problems', 'runs'),
	'stats_summary_solved': ('count',),
	'stats_summary_verdicts': ('list',),
	'stats_summary_per_day': (),
	'stats_summary_day_row': ('date', 'runs', 'solved'),
	'stats_summary_no_runs': (),
	'stats_summary_needs_work': ('count',),
	'stats_summary_needs_row': ('index', 'file', 'attempts'),
	'stats_summary_needs_none': (),
	'stats_summary_stress': ('count',),
	'stats_summary_total_time': ('duration',),
}


def summary(days=7, now=None):
	"""返回最近 days 天的纯文本练习报告（英文，无 i18n）。永不抛异常。

	内容顺序：标题行（题目数 / 运行数）-> 'Solved: N' -> 每日表格
	（日期 / 运行数 / 当日首次 AC 数）-> 'Needs work' 列表（最近一次非 AC，
	按尝试次数降序，最多 10 条）-> 若有时间戳则给出 'Total time on task'。
	每行都不带行尾空格。
	"""
	try:
		return render_summary(summary_data(days, now))
	except Exception:
		return ''


def summary_localized(days=7, now=None):
	"""Same numbers as ``summary()`` but rendered through ``cph_i18n.t``.

	The import is inside the function on purpose: this module is imported by
	plain scripts too, and ``cph_i18n`` pulls in the plugin's settings layer.
	Never raises - it falls back to the English text.
	"""
	try:
		from .cph_i18n import t
	except Exception:
		t = None
	try:
		return render_summary(summary_data(days, now), translate=t)
	except Exception:
		return summary(days, now)


def reset():
	"""删除存储文件。文件已不存在或删除成功都返回 True。"""
	path = stats_path()
	try:
		if os.path.exists(path):
			os.remove(path)
		return not os.path.exists(path)
	except Exception:
		return False

"""
Algorithm Competition Assistant - 国际化 / Internationalization
默认中文 (可切换为英文)
Default Chinese, switchable to English
"""

LANG_ZH = 'zh'
LANG_EN = 'en'

_current_lang = LANG_ZH  # default is Chinese

STRINGS = {
    'edit': {
        'zh': '编辑',
        'en': 'edit',
    },
    'run': {
        'zh': '运行',
        'en': 'run',
    },
    'stop': {
        'zh': '停止',
        'en': 'stop',
    },
    'detail': {
        'zh': '详情',
        'en': 'detail',
    },
    'time': {
        'zh': '时间',
        'en': 'time',
    },
    'memory': {
        'zh': '内存',
        'en': 'mem',
    },
    'accept': {
        'zh': '接受',
        'en': 'accept',
    },
    'decline': {
        'zh': '拒绝',
        'en': 'decline',
    },
    'test_label': {
        'zh': '测试',
        'en': 'test',
    },
    'compiling': {
        'zh': '编译中...',
        'en': 'Compiling...',
    },
    'compilation_error': {
        'zh': '编译错误',
        'en': 'Compilation Error',
    },
    'compile_warning': {
        'zh': '编译警告',
        'en': 'Compilation Warning',
    },
    'input': {
        'zh': '输入',
        'en': 'Input',
    },
    'expected_output': {
        'zh': '预期输出',
        'en': 'Expected Output',
    },
    'actual_output': {
        'zh': '实际输出',
        'en': 'Actual Output',
    },
    'error_output': {
        'zh': '标准错误输出 (stderr)',
        'en': 'Standard Error (stderr)',
    },
    'message': {
        'zh': '信息',
        'en': 'Message',
    },
    'correct_answer': {
        'zh': '正确答案',
        'en': 'Correct Answer',
    },
    'edit_answer_hint': {
        'zh': '标准答案',
        'en': 'expected answer',
    },
    'edit_input_hint': {
        'zh': '保存输入和答案',
        'en': 'saves input + answer',
    },
    'test_gone': {
        'zh': '该测试点已被删除，编辑内容未保存',
        'en': 'that test no longer exists, nothing was saved',
    },
    'panel_not_ready': {
        'zh': '测试面板还没准备好（编译失败或尚未编译）',
        'en': 'the test panel is not ready yet (nothing compiled)',
    },
    # ---- 机器速度校准 ----
    'calibrate_write_failed': {
        'zh': '写基准程序失败：{error}',
        'en': 'could not write the benchmark: {error}',
    },
    'calibrate_compile_failed': {
        'zh': '基准程序编译失败（{compiler} 是否在 PATH？）：{error}',
        'en': 'benchmark failed to compile (is {compiler} on PATH?): {error}',
    },
    'calibrate_run_failed': {
        'zh': '基准程序运行失败：{error}',
        'en': 'benchmark failed to run: {error}',
    },
    'calibrate_report': {
        'zh': '本机速度系数 {factor}×（{score} ops/s，耗时 {seconds}s）。'
              '系数 >1 表示本机比参考评测机快：本地 {factor} 毫秒只相当于评测机 1 毫秒。',
        'en': 'machine factor {factor}x ({score} ops/s, {seconds}s). A factor '
              'above 1 means this machine is faster than the reference judge.',
    },
    'calibrate_label': {
        'zh': '本机 {factor}× ≈ OJ {ms}ms',
        'en': 'local {factor}x ~ OJ {ms}ms',
    },
    'calibrate_factor': {
        'zh': '本机 {factor}×',
        'en': 'local {factor}x',
    },
    # ---- 抓题 ----
    'fetch_bad_url': {
        'zh': '不是合法的 http(s) 链接：{url}',
        'en': 'not a valid http(s) URL: {url}',
    },
    'fetch_http_error': {
        'zh': '抓取失败：HTTP {code}（{url}）',
        'en': 'fetch failed: HTTP {code} ({url})',
    },
    'fetch_response_too_large': {
        'zh': '抓取失败：页面超过 max_fetch_bytes 上限（{limit}），已中止下载；'
              '确需更大的页面可在设置里调高 max_fetch_bytes',
        'en': 'fetch failed: the page is larger than the max_fetch_bytes cap '
              '({limit}); raise max_fetch_bytes in the settings if you really '
              'need bigger pages',
    },
    'fetch_failed': {
        'zh': '抓取失败：{error}（{url}）',
        'en': 'fetch failed: {error} ({url})',
    },
    'fetch_no_samples': {
        'zh': '页面上没找到样例（可能需要在浏览器里登录，或该站结构不同）',
        'en': 'no sample found on the page (it may need a login, or the site '
              'uses a different structure)',
    },
    'fetch_found_samples': {
        'zh': '找到 {n} 组样例',
        'en': 'found {n} sample(s)',
    },
    'fetch_added': {
        'zh': '已加入 {n} 个测试点，共 {total} 个',
        'en': 'added {n} test(s), {total} in total',
    },
    'fetch_enter_url': {
        'zh': '题目链接',
        'en': 'problem URL',
    },
    'statement_source': {
        'zh': '> 来源：{url}',
        'en': '> Source: {url}',
    },
    'statement_summary': {
        'zh': '题面摘要：{text}',
        'en': 'statement: {text}',
    },
    'statement_empty': {
        'zh': '题面解析为空（可能需要在浏览器里登录）',
        'en': 'the statement parsed as empty (it may need a login)',
    },
    # ---- 比赛计时 ----
    'contest_enter_minutes': {
        'zh': '比赛时长（分钟）',
        'en': 'contest length (minutes)',
    },
    'contest_started': {
        'zh': '比赛计时开始：{minutes} 分钟',
        'en': 'contest timer started: {minutes} minutes',
    },
    'contest_stopped': {
        'zh': '比赛计时已停止',
        'en': 'contest timer stopped',
    },
    'contest_finished': {
        'zh': '比赛时间到！',
        'en': 'time is up!',
    },
    'contest_warning': {
        'zh': '还剩 {minutes} 分钟',
        'en': '{minutes} minutes left',
    },
    # ---- Benchmark ----
    'benchmark_no_test': {
        'zh': '先跑一次测试点再做 benchmark',
        'en': 'run a test first, then benchmark it',
    },
    'benchmark_running': {
        'zh': 'benchmark：跑 {n} 次…',
        'en': 'benchmark: running {n} times...',
    },
    'benchmark_done': {
        'zh': 'benchmark（{n} 次）：最快 {best}ms / 平均 {avg}ms / 最慢 {worst}ms',
        'en': 'benchmark ({n} runs): best {best}ms / avg {avg}ms / worst {worst}ms',
    },
    # ---- 练习统计 ----
    'stats_empty': {
        'zh': '还没有练习记录',
        'en': 'no practice history yet',
    },
    'stats_reset_done': {
        'zh': '练习统计已清空',
        'en': 'practice statistics cleared',
    },
    # --- 统计报告正文（core/cph_stats.py 的 summary() 输出） -----------------
    # summary() itself stays i18n-free: it is a pure-stdlib module that must
    # also run from a plain script, and importing cph_i18n there would drag
    # the whole plugin in. These keys are looked up by name inside
    # render_summary(), so they are whitelisted in tests/run_tests.py
    # (STATS_I18N_KEYS) instead of being called through t('key').
    # the report body (cph_extras.CphStatsCommand), which already imports t().
    'stats_summary_header': {
        'zh': '练习统计（最近 {days} 天）：{problems} 道题，{runs} 次运行',
        'en': 'practice statistics (last {days} days): {problems} problems, '
              '{runs} runs',
    },
    'stats_summary_solved': {
        'zh': '已解决：{count}',
        'en': 'Solved: {count}',
    },
    'stats_summary_verdicts': {
        'zh': '判题分布：{list}',
        'en': 'Verdicts: {list}',
    },
    'stats_summary_per_day': {
        'zh': '按天：',
        'en': 'Per-day:',
    },
    'stats_summary_day_row': {
        'zh': '  {date}  {runs} 次运行  {solved} 次首 AC',
        'en': '  {date}  {runs} runs  {solved} solved',
    },
    'stats_summary_no_runs': {
        'zh': '  （没有运行记录）',
        'en': '  (no runs)',
    },
    'stats_summary_needs_work': {
        'zh': '待加强（{count}）：',
        'en': 'Needs work ({count}):',
    },
    'stats_summary_needs_row': {
        'zh': '  {index}. {file}  尝试 {attempts} 次',
        'en': '  {index}. {file}  attempts={attempts}',
    },
    'stats_summary_needs_none': {
        'zh': '  无',
        'en': '  none',
    },
    'stats_summary_stress': {
        'zh': '对拍累计：{count} 轮',
        'en': 'Stress rounds: {count}',
    },
    'stats_summary_total_time': {
        'zh': '总用时：{duration}',
        'en': 'Total time on task: {duration}',
    },
    'stress_seed': {
        'zh': '本轮随机种子 seed = {seed}（生成器可通过环境变量 CPH_SEED 复现）',
        'en': 'round seed = {seed} (the generator can read it from CPH_SEED)',
    },
    'stress_replaying': {
        'zh': '用 seed = {seed} 重放上一个反例…',
        'en': 'replaying the last counterexample with seed = {seed}...',
    },
    'stress_no_counterexample': {
        'zh': '还没有记录到反例（或该轮没有种子）',
        'en': 'no counterexample recorded yet (or it has no seed)',
    },
    'stress_replay_same': {
        'zh': '重放结果一致：这轮的差异可能是环境/时序导致的',
        'en': 'replay matched: the earlier difference may have been timing',
    },
    'stress_replay_tle': {
        'zh': '重放时用户程序超时',
        'en': 'the user program timed out during the replay',
    },
    'interactor_missing': {
        'zh': '找不到 interactor 文件：{path}（{error}）',
        'en': 'interactor file not found: {path} ({error})',
    },
    'interactor_compile_failed': {
        'zh': 'interactor 编译失败：{cmd}\n{error}',
        'en': 'interactor failed to compile: {cmd}\n{error}',
    },
    # Reached through the dynamic key t(label) in test_manager.py
    # (label = 'interactive_running' if interactive else 'parallel_running').
    # NOT dead: the static literal scan cannot see it - keep on the whitelist.
    'interactive_running': {
        'zh': '交互题：正在与 interactor 交互（{done}/{total}）',
        'en': 'interactive: talking to the interactor ({done}/{total})',
    },
    'checker_missing': {
        'zh': '找不到 checker 文件：{path}（{error}）',
        'en': 'checker file not found: {path} ({error})',
    },
    'checker_compile_failed': {
        'zh': 'checker 编译失败：{cmd}\n{error}',
        'en': 'checker failed to compile: {cmd}\n{error}',
    },
    'checker_run_failed': {
        'zh': 'checker 运行失败：{error}',
        'en': 'checker failed to run: {error}',
    },
    'checker_judged': {
        'zh': 'SPJ 判定：{verdict}',
        'en': 'judged by the checker: {verdict}',
    },
    # Dynamic key: see the note on interactive_running above.
    'parallel_running': {
        'zh': '并行运行测试点：{done}/{total}',
        'en': 'running tests in parallel: {done}/{total}',
    },
    'parallel_done': {
        'zh': '并行运行完成：{total} 个测试点',
        'en': 'parallel run finished: {total} tests',
    },
    'tle_debug_build': {
        'zh': '编译命令里有 -DLOCAL/-DDEBUG：调试宏通常会把 debug() 输出打到不缓冲的 '
              'stderr，循环里调用时输出量是 O(n²)，本地会比评测机慢几十倍——'
              '代码正确也可能被判 TLE。计时时建议去掉该宏，并确认编译命令带 -O2。',
        'en': 'the compile command has -DLOCAL/-DDEBUG: the debug macro usually '
              'writes debug() to unbuffered stderr, which is O(n^2) output '
              'inside a loop, so a local run can be orders of magnitude slower '
              'than the judge and a correct program is reported TLE. Drop the '
              'macro for timing runs, and make sure the command uses -O2.',
    },
    'tle_no_output': {
        'zh': '超时被终止，且完全没有输出：程序很可能在等待输入（样例是否完整？）'
              '或者死循环。若终端里能正常跑出结果，先确认样例是否粘贴完整。',
        'en': 'killed at the time limit with no output at all: the program is '
              'most likely waiting for input (is the sample complete?) or '
              'looping forever. If it works in a terminal, check that the '
              'whole sample was pasted.',
    },
    'wa_release_hint': {
        'zh': 'Release 模式看不出未定义行为：数组越界读在 -O2 下会读到堆里的垃圾值，'
              '程序照样 exit 0，判定只能是 WA。若这题逻辑上应该对，按 Ctrl+Alt+G '
              '切到 Debug 再跑一次（会加上断言与 sanitizer，越界会直接中止并给出'
              '文件:行号）。本条提示每题只出现一次。',
        'en': 'release mode cannot see undefined behaviour: an out-of-bounds '
              'read at -O2 returns heap garbage, the program still exits 0, and '
              'the verdict can only be WA. If this should be correct, press '
              'Ctrl+Alt+G to switch to Debug and run again (assertions and the '
              'sanitizer are added, so the out-of-bounds read aborts with a '
              'file:line). Shown once per problem.',
    },
    # ---- sanitizer 回退 ----
    'sanitizer_fallback': {
        'zh': '本机工具链缺少 sanitizer 运行库 (ld: cannot find -lubsan/-lasan), '
              '已自动去掉 -fsanitize 参数重新编译一次; debug 模式的 -D_GLIBCXX_ASSERTIONS '
              '不依赖该运行库, 越界等未定义行为仍会中止并判 RE',
        'en': 'The local toolchain lacks the sanitizer runtime '
              '(ld: cannot find -lubsan/-lasan). The -fsanitize flags were '
              'dropped and the build was retried once. debug mode still passes '
              '-D_GLIBCXX_ASSERTIONS, which needs no such runtime: '
              'out-of-bounds and other undefined behaviour still abort and '
              'are judged RE',
    },
    'sanitizer_fallback_hint': {
        'zh': '（这条只会在你自己往 compile_cmd 里写了 -fsanitize=address 之类时才出现；'
              '插件默认用的 -fsanitize=undefined -fsanitize-trap=undefined 不需要运行库）',
        'en': '(This only appears when your own compile_cmd carries a sanitizer '
              'such as -fsanitize=address; the default '
              '-fsanitize=undefined -fsanitize-trap=undefined needs no runtime)',
    },
    'diff': {
        'zh': '差异',
        'en': 'Diff',
    },
    'cannot_action_while_running': {
        'zh': '运行中无法{action}',
        'en': 'can not {action} while process running',
    },
    'process_terminated': {
        'zh': '进程已终止',
        'en': 'Process terminated',
    },
    'stop_before_delete': {
        'zh': '请先停止运行再删除',
        'en': 'stop process before delete action',
    },
    'deleted_tests': {
        'zh': '已删除测试: {ids}',
        'en': 'deleted tests: {ids}',
    },
    'process_already_running': {
        'zh': '进程已在运行',
        'en': 'process already running',
    },
    'cant_restore_session': {
        'zh': '无法恢复会话',
        'en': 'Can\'t restore session',
    },
    'next_test': {
        'zh': '下一个测试',
        'en': 'next test',
    },
    'error_handling_post': {
        'zh': '处理 POST 请求出错 - {error}',
        'en': 'Error handling POST - {error}',
    },
    'new_test_file_path': {
        'zh': '测试用例文件路径: {path}',
        'en': 'New test case path: {path}',
    },
    'compile_error': {
        'zh': '编译错误',
        'en': 'compilation error',
    },
    'save': {
        'zh': '保存',
        'en': 'save',
    },
    'delete': {
        'zh': '删除',
        'en': 'delete',
    },
    'import_from_pair': {
        'zh': '从 in/out 文件对导入 (cph-ng 风格)',
        'en': 'Import from in/out file pair (cph-ng style)',
    },
    'import_from_pair_desc': {
        'zh': '导入与输出文件配对的输入文件',
        'en': 'Import an input file paired with an output file',
    },
    'import_from_single': {
        'zh': '从单个文件导入 (含分隔符)',
        'en': 'Import from a single file (with separator)',
    },
    'import_from_single_desc': {
        'zh': '导入同时包含输入和输出的单个文件',
        'en': 'Import a single file containing both input and output',
    },
    'import_from_cphng': {
        'zh': '从 cph-ng 文件夹导入',
        'en': 'Import from cph-ng folder (in.txt/out.txt)',
    },
    'import_from_cphng_desc': {
        'zh': '自动查找同目录下的 in.txt 和 out.txt',
        'en': 'Auto-detect in.txt and out.txt in the same folder',
    },
    'import_file_path': {
        'zh': '测试文件路径',
        'en': 'Test file path',
    },
    'input_file_path': {
        'zh': '输入文件路径',
        'en': 'Input file path',
    },
    'output_file_not_found': {
        'zh': '未找到对应的输出文件,只导入输入',
        'en': 'Output file not found, importing input only',
    },
    'imported_tests': {
        'zh': '已导入 {count} 个测试 (来源: {source})',
        'en': 'Imported {count} test(s) from {source}',
    },
    'import_save_failed': {
        'zh': '导入失败: 无法保存测试数据',
        'en': 'Import failed: cannot save test data',
    },
    'import_failed': {
        'zh': '导入失败: {error}',
        'en': 'Import failed: {error}',
    },
    'read_failed': {
        'zh': '读取文件失败: {error}',
        'en': 'Failed to read file: {error}',
    },
    'path_is_directory': {
        'zh': '{path} 是文件夹，不是测试数据文件：请指向具体的 .in / .out 文件，或用"从文件夹批量导入"',
        'en': '{path} is a folder, not a test data file: point at a .in/.out file, or use "import from folder"',
    },
    'path_is_file_not_dir': {
        'zh': '{path} 是文件，不是文件夹：请在路径栏填数据所在的文件夹，或改用"从单个文件导入"',
        'en': '{path} is a file, not a folder: put the folder holding the data in the path bar, or use "import from a single file"',
    },
    'import_partially_done': {
        'zh': '已导入 {kept} / {total} 个数据文件，其余读取失败（详见 Sublime 控制台）',
        'en': 'imported {kept} of {total} data files; the rest failed to read (see the Sublime console)',
    },
    'file_not_found': {
        'zh': '文件不存在',
        'en': 'File not found',
    },
    'no_cphng_files_found': {
        'zh': '未找到 cph-ng 样例文件 (in.txt / input.txt)',
        'en': 'No cph-ng sample files found (in.txt / input.txt)',
    },
    'no_test_files_in_dir': {
        'zh': '当前目录未找到测试文件',
        'en': 'No test files found in current directory',
    },
    'use_existing_in_cphng': {
        'zh': '(已找到 cph-ng 文件)',
        'en': '(existing cph-ng file found)',
    },
    'save_file_first': {
        'zh': '请先保存当前文件',
        'en': 'Please save the current file first',
    },
    'stress_running': {
        'zh': '对拍进行中...',
        'en': 'Stress testing...',
    },
    'stress_round': {
        'zh': '第 {round} 轮',
        'en': 'Round {round}',
    },
    'stress_passed': {
        'zh': '对拍通过: {rounds} 轮全部一致',
        'en': 'Stress test passed: {rounds} rounds all consistent',
    },
    'runtime_error_at': {
        'zh': '运行错误 (RE) 位置: {location}',
        'en': 'runtime error (RE) at {location}',
    },
    'stress_all_timeout': {
        'zh': '对拍结束: 所有轮次都超时, 未比较任何输出',
        'en': 'stress test ended: every round timed out, nothing compared',
    },
    'stress_failed': {
        'zh': '对拍失败: 第 {round} 轮输出不一致',
        'en': 'Stress test failed: mismatch at round {round}',
    },
    'stress_checker_message': {
        'zh': 'checker 判定: {message}',
        'en': 'Checker verdict: {message}',
    },
    'stress_diff': {
        'zh': '差异:',
        'en': 'Diff:',
    },
    'stress_input': {
        'zh': '输入',
        'en': 'Input',
    },
    'stress_user_output': {
        'zh': '用户输出',
        'en': 'User Output',
    },
    'stress_std_output': {
        'zh': '标准输出',
        'en': 'Standard Output',
    },
    'stress_generator_tle': {
        'zh': '生成器超时（第 {round} 轮，上限 {limit}s），跳过该轮',
        'en': 'generator timed out at round {round} (limit {limit}s), round skipped',
    },
    'stress_generator_limit_hint': {
        'zh': '生成器连续超时，已停止；可调大 stress_generator_time_limit_seconds'
              '（当前 {limit}s）或让 gen.cpp 更快',
        'en': 'the generator keeps timing out, stopped; raise'
              ' stress_generator_time_limit_seconds (now {limit}s) or make gen.cpp faster',
    },
    'stress_generator_failed': {
        'zh': '生成器运行失败（第 {round} 轮，退出码 {code}）',
        'en': 'generator failed at round {round} (exit code {code})',
    },
    'stress_cannot_run': {
        'zh': '无法运行 {program}: {reason}',
        'en': 'cannot run {program}: {reason}',
    },
    'stress_program_died': {
        'zh': '对拍停止: 第 {round} 轮 {program} 异常退出（退出码 {code}）。'
              '正解/暴力在这里断了，它打出来的东西不能当标准答案，'
              '所以这一轮**没有做任何比较**，屏幕上这个反例是假的。先修好 {program} 再对拍。',
        'en': 'stress stopped: {program} died at round {round} (exit code {code}). '
              'A reference solution that aborts cannot define "correct", so this round '
              'compared **nothing** and any counterexample would be fake. Fix '
              '{program} before stress testing again.',
    },
    'stress_user_died': {
        'zh': '对拍失败: 第 {round} 轮你的程序异常退出（退出码 {code}）',
        'en': 'stress failed: your program died at round {round} (exit code {code})',
    },
    'stress_stderr_for': {
        'zh': '{program} 的错误输出 (stderr)',
        'en': '{program} stderr',
    },
    'stress_tle_hint': {
        'zh': '提示: 若编译命令里定义了 LOCAL 之类的调试宏，程序里留在循环内的'
              'debug 输出会非常慢（stderr 不缓冲），本地很容易超时；'
              '去掉该行或删掉编译命令里的 -DLOCAL 再试',
        'en': 'hint: if the compile command defines a debug macro such as LOCAL,'
              ' a debug() call left inside a loop is extremely slow (stderr is'
              ' unbuffered) and times out locally; remove it or drop -DLOCAL',
    },
    'choose_std_file': {
        'zh': '【第 1/2 步 · 选正解 std】慢但一定对的那个（暴力/bf/sol）',
        'en': '[Step 1/2 - the correct solution] the slow but always-right '
              'program (brute / bf / sol)',
    },
    'choose_generator_file': {
        'zh': '【第 2/2 步 · 选生成器 gen】随机造数据的那个（gen/data/rand）',
        'en': '[Step 2/2 - the generator] the program that prints random test '
              'data (gen / data / rand)',
    },
    'stress_same_file': {
        'zh': '正解和数据生成器选到了同一个文件（{file}）：拿程序和自己比，'
              '每一轮必然一致，这场对拍不会发现任何东西。请重新选。',
        'en': 'std and the generator are the same file ({file}): a program '
              'compared with itself agrees every round, so this stress test '
              'can never find anything. Pick again.',
    },
    'stress_files_mode_refused': {
        'zh': '这一题设成了「文件输入输出」（{input} / {output}），对拍开不了。'
              '对拍是把数据从管道喂进去、再从管道读答案；按文件读写的三个程序都'
              '看不到那份数据，每一轮都会"一致"，那场对拍是自欺欺人。'
              '要本地对拍就把这几份程序改成读写 stdin/stdout（交题前再改回来），'
              '或用菜单里"本题评测方式"改回标准输入。',
        'en': 'this problem is set to file input/output ({input} / {output}), so '
              'a stress test cannot start. Stress feeds every round through a '
              'pipe: programs that read and write files would see nothing, and '
              'both sides would "agree" on empty output every round. For a local '
              'stress run make std, gen and your program use stdin/stdout '
              '(switch back before submitting), or set this problem back to '
              'standard input.',
    },
    'stress_same_dir_missing': {
        'zh': '在 {dir} 里没找到唯一的一对 std / gen（正解期望文件名含 std/brute/bf/sol，'
              '生成器含 gen/data/maker/rand）。改一下文件名，或在设置里把 '
              'stress_file_source 换成 pick_once 让它问你一次。',
        'en': 'No single std / gen pair found in {dir} (the correct solution wants a '
              'name containing std/brute/bf/sol, the generator gen/data/maker/rand). '
              'Rename them, or set stress_file_source to pick_once to be asked once.',
    },
    'stress_time_limit': {
        'zh': '对拍时间限制(秒)',
        'en': 'Stress test time limit (seconds)',
    },
    'stress_max_rounds': {
        'zh': '对拍最大轮数',
        'en': 'Stress test max rounds',
    },
    'stress_finished': {
        'zh': '对拍结束，用时 {seconds} 秒',
        'en': 'stress test finished in {seconds}s',
    },
    'stress_summary': {
        'zh': '本轮统计: 共 {rounds} 轮, 实际比较 {compared} 轮, 生成器超时 {timeouts} 次',
        'en': 'this run: {rounds} rounds, {compared} compared, '
              '{timeouts} generator timeouts',
    },
    'stress_wall_limit': {
        'zh': '已达总时长上限 {limit} 秒，已停止（可调大 stress_max_wall_seconds）',
        'en': 'reached the overall limit of {limit}s and stopped'
              ' (raise stress_max_wall_seconds)',
    },
    'stress_browse_up': {
        'zh': '..（上一层目录）',
        'en': '.. (parent directory)',
    },
    'stress_browse_more': {
        'zh': '浏览其它文件…',
        'en': 'Browse for another file...',
    },
    'stress_type_path': {
        'zh': '输入路径…（已预填源文件所在目录，补个文件名即可）',
        'en': 'Type a path... (pre-filled with the source directory)',
    },
    # ---- Competitive Companion persistent listener ----
    'listener_started': {
        'zh': 'Competitive Companion 监听已启动 (端口 {port}，目标: {file})，浏览器扩展可反复点击发送',
        'en': 'Competitive Companion listener started (port {port}, target: {file})',
    },
    'listener_retarget': {
        'zh': '监听运行中，目标已切换为: {file}',
        'en': 'Listener running, target switched to: {file}',
    },
    'listener_stopped': {
        'zh': 'Competitive Companion 监听已停止',
        'en': 'Competitive Companion listener stopped',
    },
    'listener_not_running': {
        'zh': '监听未在运行',
        'en': 'Listener is not running',
    },
    'install_check_warn': {
        'zh': '安装形态异常：同时存在目录安装和 zip，或目录被清空（详情见控制台）',
        'en': 'install layout is odd: a folder and a zip both exist, or the '
              'folder is empty (see the console)',
    },
    'listener_no_target': {
        'zh': '监听目标文件不存在或未保存',
        'en': 'Listener target missing or unsaved',
    },
    'listener_auto_target': {
        'zh': '按题面自动新建文件',
        'en': 'auto-created from the problem name',
    },
    'listener_mode_new': {
        'zh': '一题一个新文件：只建文件、存样例、记限制，不跑样例。'
              '想把样例收进当前文件并立刻跑一次，按 Ctrl+Alt+F',
        'en': 'One new file per problem: created, samples stored, nothing run. '
              'To fill the file you are in and run it once, press Ctrl+Alt+F',
    },
    'listener_mode_current': {
        'zh': '样例会送进 {file} 并立刻跑一遍。注意：比赛题目列表页一次点击会发来'
              '整包题目，那种页面请用 Ctrl+Alt+L 每题建一个新文件，否则每道题的'
              '样例都并进同一个文件',
        'en': 'Samples will go into {file} and run once. Note: a contest task '
              'list sends every task per click - use Ctrl+Alt+L there, one new '
              'file per problem, or all of them pile into this one file',
    },
    'listener_no_directory': {
        'zh': '没有地方可以新建题目文件：先打开这场题目所在的文件夹，'
              '或在设置里指定 auto_create_dir',
        'en': 'Nowhere to create the problem file: open the folder holding '
              'this contest, or set auto_create_dir',
    },
    'header_problem': {
        'zh': '题目：{value}',
        'en': 'Problem: {value}',
    },
    'header_source': {
        'zh': '来源：{value}',
        'en': 'Source: {value}',
    },
    'header_time_limit': {
        'zh': '时间限制：{value} ms',
        'en': 'Time limit: {value} ms',
    },
    'header_memory_limit': {
        'zh': '空间限制：{value} MB',
        'en': 'Memory limit: {value} MB',
    },
    'auto_create_failed': {
        'zh': '自动建文件失败: {path} ({error})',
        'en': 'Could not auto-create the file: {path} ({error})',
    },
    'listener_port_error': {
        'zh': '监听启动失败 (端口 {port}): {error}',
        'en': 'Failed to start listener on port {port}: {error}',
    },
    'tests_received': {
        'zh': '已接收题目 "{name}": {count} 个样例 (时间限制 {time}ms, '
              '内存限制 {memory}MB)，写入 {file}',
        'en': 'Received "{name}": {count} test(s) (TL {time}ms, ML {memory}MB)'
              ' -> {file}',
    },
    # ---- detail view ----
    'detail_no_result': {
        'zh': '请先运行测试再查看详情',
        'en': 'Run the test first to see details',
    },
    'detail_empty': {
        'zh': '(空)',
        'en': '(empty)',
    },
    'diff_ignore_trailing': {
        'zh': '逐行对比，忽略行末空格与末尾换行',
        'en': 'line-by-line, ignoring trailing spaces',
    },
    'diff_all_match': {
        'zh': '全部 {n} 行一致',
        'en': 'all {n} lines match',
    },
    'diff_lines_differ': {
        'zh': '{n} 处不同 (共 {total} 行)',
        'en': '{n} difference(s) over {total} lines',
    },
    'diff_shown_truncated': {
        'zh': '（只列出前 {shown} 处，其余 {n} 处省略——上方"预期输出/实际输出"是完整的，运行视图里也有全量输出）',
        'en': '(showing the first {shown} of {n} differences - the expected/actual '
              'outputs above are complete, and the run view holds the full output)',
    },
    'diff_no_expected': {
        'zh': '未设置标准答案，无法对比。可在 "test N -answer" 标签页设置后再运行',
        'en': 'No expected answer set. Fill it in the "test N -answer" tab and re-run',
    },
    'expected_short': {
        'zh': '预期',
        'en': 'expected',
    },
    'actual_short': {
        'zh': '实际',
        'en': 'actual',
    },
    # ---- import ----
    'import_from_folder': {
        'zh': '从文件夹批量导入 (.in/.out 文件对)',
        'en': 'Batch import from folder (.in/.out pairs)',
    },
    'import_from_folder_desc': {
        'zh': '输入一个文件夹路径, 导入其中所有 .in/.out 文件对',
        'en': 'Enter a folder path to import all .in/.out pairs inside',
    },
    'import_folder_path': {
        'zh': '文件夹路径 (包含 .in/.out 文件对)',
        'en': 'Folder path containing .in/.out pairs',
    },
    'choose_other_file': {
        'zh': '选择其他文件...',
        'en': 'Choose another file...',
    },

    'compile_cached': {
        'zh': '编译缓存命中，已跳过编译',
        'en': 'compile skipped (cache hit)',
    },
    'std_switched': {
        'zh': '这份代码用到 {to} 才有的语法：已从 {frm} 改用 -std={to} 重编'
              '（记在这道题自己的 .std 里，别的文件不受影响）',
        'en': 'this code needs {to}: recompiled with -std={to} (was {frm}), '
              'remembered in this file\'s .std sidecar only',
    },
    'output_truncated_warn': {
        'zh': '输出过大，已截断显示',
        'en': 'output too large, truncated',
    },
    'near_tle_warn': {
        'zh': '本次用了时限的 {pct}%（{runtime}ms / {limit}ms），'
              '本机若比评测机快，同样的代码在 OJ 上可能 TLE',
        'en': 'this run used {pct}% of the limit ({runtime}ms / {limit}ms); '
              'if your machine is faster than the judge, the same code can TLE there',
    },
    'lang_switched_zh': {
        'zh': '已切换为中文界面',
        'en': 'interface language switched to Chinese',
    },
    'lang_switched_en': {
        'zh': '已切换为英文界面',
        'en': 'interface language switched to English',
    },
    'summary_passed': {
        'zh': '{passed}/{total} 通过',
        'en': '{passed}/{total} passed',
    },
    'summary_first_fail': {
        'zh': '首个失败 test {id}',
        'en': 'first failure: test {id}',
    },
    'summary_total_time': {
        'zh': '总用时 {time}s',
        'en': 'total {time}s',
    },
    'no_running_process': {
        'zh': '当前没有正在运行的程序',
        'en': 'no running process',
    },
    'unsupported_language': {
        'zh': '没有为 .{ext} 配置运行方式（见 run_settings）',
        'en': 'no run_settings entry for .{ext}',
    },
    'template_no_keyword': {
        'zh': '请把光标放在关键字后（如 fastio）再按快捷键',
        'en': 'put the cursor after a keyword (e.g. fastio)',
    },
    'template_not_found': {
        'zh': '没有名为 {name} 的模板（可用 algorithms_base 或 templates 设置）',
        'en': 'no template named {name} (see algorithms_base / templates)',
    },
    'clipboard_empty': {
        'zh': '剪贴板为空',
        'en': 'clipboard is empty',
    },
    'clipboard_test_added': {
        'zh': '已从剪贴板新增测试点，共 {total} 个',
        'en': 'added test from clipboard, {total} total',
    },
    'no_tests': {
        'zh': '当前没有测试数据',
        'en': 'no test data',
    },
    'nothing_to_copy': {
        'zh': '这一项是空的，没有内容可复制',
        'en': 'nothing to copy',
    },
    'copied_test_part': {
        'zh': '已复制 test {id} 的内容到剪贴板',
        'en': 'copied test {id} to clipboard',
    },
    'export_to_dir': {
        'zh': '导出目录',
        'en': 'export folder',
    },
    'export_done': {
        'zh': '已导出 {count} 个测试点到 {dir}',
        'en': 'exported {count} tests to {dir}',
    },
    'stress_counterexample_added': {
        'zh': '已把对拍反例保存为测试点（共 {total} 个）',
        'en': 'saved the counterexample as a test ({total} total)',
    },
    'stress_counterexample_crash': {
        'zh': '这个反例只有输入被存下来：程序是崩掉的，没有可比的标准答案，'
              '跑测试点时会显示成 RE（共 {total} 个测试点）',
        'en': 'only the input of this counterexample was saved: the program '
              'died, so there is no expected output to compare - the test '
              'shows up as RE ({total} tests in total)',
    },
    'stress_counterexample_too_big': {
        'zh': '反例输入 {size} 字节，超过 stress_max_insert_bytes={limit}，'
              '未写入 __tests（完整输入已在上方展示；调大该设置或设为 -1 可照常保存）',
        'en': 'the counterexample input is {size} bytes, above '
              'stress_max_insert_bytes={limit} - not written to __tests '
              '(the full input is printed above; raise the setting or use -1 '
              'to save it anyway)',
    },
    'stress_counterexample_save_failed': {
        'zh': '反例没能写入 __tests：{reason}',
        'en': 'the counterexample could not be saved to __tests: {reason}',
    },
    'tests_save_failed': {
        'zh': '测试点没能写入 {path}（{reason}），这次的改动不会保留',
        'en': 'test points could not be written to {path} ({reason}) '
              '- they will not be kept',
    },
    'tests_read_failed': {
        'zh': '测试点文件读不出来：{path}（{reason}）。'
              '面板这次用的样例可能不全 - 修好那个文件里的 JSON，'
              '或者删掉它重新导入样例',
        'en': 'the tests file could not be read: {path} ({reason}). '
              'The panel may be missing test points - fix the JSON in that '
              'file, or delete it and import the samples again',
    },
    'io_reason_unknown': {
        'zh': '文件只读或目录权限不足',
        'en': 'read-only file or no permission',
    },
    'jump_no_target': {
        'zh': '现在没有可跳转的错误位置：先跑一次（Ctrl+Alt+B），编译失败或 RE '
              '会把位置显示在运行面板上，再按 Ctrl+Alt+J 跳过去',
        'en': 'there is nothing to jump to yet: run the file first (Ctrl+Alt+B) - '
              'a compile error or RE puts the location on the run panel, and '
              'Ctrl+Alt+J takes you there',
    },
    'io_output_missing': {
        'zh': '【文件输入输出】程序没有写出 {name}。这一题按文件评测，答案要从 '
              '{name} 读回来；检查代码里 freopen("{name}", "w", stdout) 写没写、'
              '文件名和题面是否一致（题面写 .ans 就别写 .out）。输入 {input} '
              '已经放到运行目录里了。',
        'en': '[file IO] the program never wrote {name}. This problem is judged '
              'from that file, so the answer had to be read back from it - check '
              'that the code calls freopen("{name}", "w", stdout) and that the '
              'name matches the statement (if it says .ans, do not write .out). '
              'The input {input} was put in the run directory.',
    },
    'stats_save_failed': {
        'zh': '练习统计没能保存: {error}（本次运行不计入统计）',
        'en': 'practice statistics could not be saved: {error} '
              '(this run is not counted)',
    },
    'tests_clear_failed': {
        'zh': '测试点没能清空: {error}（这些文件只读或目录权限不足，'
              '下次运行会把它们重新读回来）',
        'en': 'test points could not be cleared: {error} '
              '(read-only file or no permission - they come back on the next run)',
    },
    'doctor_title': {
        'zh': 'Algorithm Competition Assistant 环境自检',
        'en': 'Algorithm Competition Assistant environment check',
    },
    'doctor_no_run_settings': {
        'zh': 'run_settings 为空，先去设置里配置语言',
        'en': 'run_settings is empty - configure a language first',
    },
    'doctor_languages': {
        'zh': '语言配置',
        'en': 'language entry',
    },
    'doctor_compile': {
        'zh': '编译命令',
        'en': 'compile',
    },
    'doctor_run': {
        'zh': '运行命令',
        'en': 'run',
    },
    'doctor_fix_path': {
        'zh': '找不到 {name}，请确认它在 PATH 中或改用绝对路径',
        'en': '{name} not found - add it to PATH or use an absolute path',
    },
    # ---- 设置开关面板 (cph_options) ---------------------------------------
    'opt_verdict_mode': {
        'zh': '判定口径（浮点容差 / PE 当 AC / 首错即停 / stderr）',
        'en': 'Verdict rules (float tolerance, PE, stop on first failure)',
    },
    'opt_companion_options': {
        'zh': '监听设置：样例送到哪里 (Competitive Companion)',
        'en': 'Competitive Companion options (target mode, click log)',
    },
    'opt_stress_options': {
        'zh': '对拍设置：std/gen 从哪来、跑多少轮',
        'en': 'Stress test options (file source, rounds, limit)',
    },
    'opt_float_tolerance': {
        'zh': '浮点容差（浮点题不开会一直 WA）',
        'en': 'Float tolerance (off means floating point WA forever)',
    },
    'opt_regard_pe_as_ac': {
        'zh': '格式不同算 AC（PE 当 AC）',
        'en': 'Treat presentation error as AC',
    },
    'opt_stop_on_first_failure': {
        'zh': '第一个失败就停',
        'en': 'Stop at the first failure',
    },
    'opt_ignore_stderr': {
        'zh': '判定时忽略 stderr',
        'en': 'Ignore stderr when judging',
    },
    'opt_stress_source': {
        'zh': 'std / gen 从哪来',
        'en': 'Where std and gen come from',
    },
    'opt_stress_picker': {
        'zh': '挑文件用哪种界面',
        'en': 'How to pick a file',
    },
    'opt_stress_rounds': {
        'zh': '最大轮数',
        'en': 'Max rounds',
    },
    'opt_stress_time_limit': {
        'zh': '每轮时限（秒）',
        'en': 'Time limit per round (seconds)',
    },
    'opt_save_counterexample': {
        'zh': '反例自动存成测试点',
        'en': 'Save the counterexample as a test',
    },
    'opt_target_row': {
        'zh': '一次点击把样例送到（点一下切换）',
        'en': 'Where a click sends the samples (click to switch)',
    },
    'opt_target_mode': {
        'zh': '一次点击 = 一题一个新文件',
        'en': 'One click = one new file per problem',
    },
    'opt_target_mode_current': {
        'zh': '一次点击 = 送进当前文件并跑一遍',
        'en': 'One click = fill the current file and run it',
    },
    'opt_auto_listen': {
        'zh': '启动 Sublime 就监听（改动下次启动生效）',
        'en': 'Listen when Sublime starts (applies to the next start)',
    },
    'opt_debug_log': {
        'zh': '记录每次点击到 Data/Log/aca-companion.log',
        'en': 'Log every click to Data/Log/aca-companion.log',
    },
    'opt_open_log': {
        'zh': '打开监听日志',
        'en': 'Open the companion log',
    },
    'opt_log_missing': {
        'zh': '还没有日志：先把上面的记录开关打开，再点一次扩展',
        'en': 'No log yet: turn the switch on above, then click the extension once',
    },
    'opt_on': {
        'zh': '开',
        'en': 'on',
    },
    'opt_off': {
        'zh': '关',
        'en': 'off',
    },
    'opt_io_mode': {
        'zh': '本题评测方式（stdin / 文件）',
        'en': 'This problem reads/writes files (IO mode)',
    },
    'opt_io_files': {
        'zh': '文件输入输出',
        'en': 'File input/output',
    },
    'opt_io_stdin': {
        'zh': '标准输入（管道）',
        'en': 'Standard input (pipe)',
    },
    'opt_io_switch': {
        'zh': '切换评测方式',
        'en': 'Switch how this problem is judged',
    },
    'opt_io_switch_to_files': {
        'zh': '改成「文件输入输出」（NOI / CSP-S：freopen add.in add.out）',
        'en': 'switch to file input/output (NOI / CSP-S: freopen add.in, add.out)',
    },
    'opt_io_switch_to_stdin': {
        'zh': '改回「标准输入」',
        'en': 'switch back to standard input',
    },
    'opt_io_names': {
        'zh': '输入 / 输出文件名',
        'en': 'Input / output file names',
    },
    'opt_io_stdin_no_names': {
        'zh': '标准输入模式不需要文件名：样例直接喂给 stdin。要按文件评测就先切换',
        'en': 'a stdin run needs no file names: the sample goes to stdin directly. '
              'Switch to file input/output first',
    },
    'opt_io_input_prompt': {
        'zh': '输入文件名（题面里写的那个，如 add.in）：',
        'en': 'Input file name (the one in the statement, e.g. add.in):',
    },
    'opt_io_output_prompt': {
        'zh': '输出文件名（题面里写的那个，如 add.out）：',
        'en': 'Output file name (the one in the statement, e.g. add.out):',
    },
    'opt_next_start': {
        'zh': '下次启动生效',
        'en': 'next start',
    },
    'opt_value_prompt': {
        'zh': '新值（{label}）',
        'en': 'New value ({label})',
    },
    'opt_number_kept': {
        'zh': '不是数字，保持 {value}',
        'en': 'not a number, kept {value}',
    },
    'doctor_install': {
        'zh': '安装形态',
        'en': 'install layout',
    },
    'doctor_port': {
        'zh': 'Companion 端口',
        'en': 'Companion port',
    },
    'doctor_port_busy': {
        'zh': '端口被占用：监听器可能已在运行，或换 companion_port 设置',
        'en': 'port busy: a listener may already be running, or change companion_port',
    },
    'doctor_tests_path': {
        'zh': '测试数据路径',
        'en': 'tests path',
    },
    'doctor_extension': {
        'zh': '当前文件扩展名',
        'en': 'current file extension',
    },
    'doctor_no_file': {
        'zh': '当前视图没有文件（无法检查测试路径）',
        'en': 'current view has no file (tests path not checked)',
    },
    'doctor_resources': {
        'zh': 'HTML/CSS 资源加载',
        'en': 'HTML/CSS resources',
    },
    'doctor_settings': {
        'zh': '设置文件可解析',
        'en': 'settings file readable',
    },
    'answer_conflict_kept': {
        'zh': '答案冲突：已保留原有的答案',
        'en': 'answer conflict: the existing answer was kept',
    },
    'process_already_exited': {
        'zh': '程序已退出，输入被丢弃',
        'en': 'the program exited, input dropped',
    },
    'stress_already_running': {
        'zh': '对拍已经在运行中',
        'en': 'a stress test is already running',
    },
    'stress_multi_running': {
        'zh': '有 %d 个对拍正在进行，请切换到对应的对拍页再停止',
        'en': '%d stress tests are running - focus one of the stress pages to stop it',
    },
    'stress_none_running': {
        'zh': '当前没有正在运行的对拍',
        'en': 'no stress test running',
    },
    'doctor_placeholder_unknown': {
        'zh': '命令里有不认识的占位符（会被当成空字符串）：{names}',
        'en': 'unknown placeholder(s) in the command (substituted with ""): {names}',
    },
    'doctor_markdown_hint': {
        'zh': '把下面这段贴到 issue 里（已包含环境与命令）：',
        'en': 'paste this block into an issue (environment and commands included):',
    },
    'doctor_footer': {
        'zh': '提示：把结果贴到 issue 里可以更快定位问题。',
        'en': 'tip: paste this report into an issue to speed up diagnosis.',
    },
    'doctor_probe_title': {
        'zh': '工具链实测（真的执行一次，稍后逐条出现）',
        'en': 'toolchain probes (executed for real; lines appear as they finish)',
    },
    'doctor_checker': {
        'zh': '自定义判定器',
        'en': 'custom checker',
    },
    'doctor_interactor': {
        'zh': '交互器',
        'en': 'interactor',
    },
    'doctor_missing_file': {
        'zh': '文件不存在，该功能会静默失效',
        'en': 'file missing, the feature is silently disabled',
    },
    'doctor_gdb': {
        'zh': '崩溃行定位 (gdb)',
        'en': 'crash line lookup (gdb)',
    },
    'doctor_clang_format': {
        'zh': '自动格式化 (clang-format)',
        'en': 'auto format (clang-format)',
    },
    'doctor_helper_missing': {
        'zh': '{exe} 不在 PATH 上，依赖它的功能会静默降级而不是报错',
        'en': '{exe} is not on PATH; the feature that needs it degrades silently',
    },
}


def set_lang(lang):
    global _current_lang
    if lang in (LANG_ZH, LANG_EN):
        _current_lang = lang


def get_lang():
    return _current_lang


def add_strings(new_strings):
    for key, trans in new_strings.items():
        if key in STRINGS:
            for lang, val in trans.items():
                STRINGS[key][lang] = val
        else:
            STRINGS[key] = trans


def t(key, **kwargs):
    if key not in STRINGS:
        return key
    s = STRINGS[key].get(_current_lang, STRINGS[key].get(LANG_EN, key))
    if kwargs:
        try:
            return s.format(**kwargs)
        except (KeyError, IndexError):
            return s
    return s

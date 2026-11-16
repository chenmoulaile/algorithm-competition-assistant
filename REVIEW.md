# 深度代码审查报告

> 审查日期：2026-11-16
> 审查对象：Algorithm Competition Assistant v3.0.10（Sublime Text 4 算法竞赛本地测评插件）
> 方法：静态通读 + 符号交叉引用 + git 历史核验 + 本人逐条复核关键项；只读，未修改任何代码。
> 基线：`python tests/run_tests.py` → **1078 项检查，0 失败**（说明下列问题均为现有测试未覆盖的潜在缺陷）。

## 结论速览

| 级别 | 数量 | 一句话 |
| --- | --- | --- |
| 🔴 Critical | 2 | POSIX 上 `_reap()` 可能用 `killpg` 杀掉 Sublime 宿主进程组；对拍重放对编译型语言完全失效 |
| 🟠 Major | 9 | 重放假判定、判题口径不一致、进程树清理不彻底、统计竞态、round 上限未生效等 |
| 🟡 Minor | 12 | 死代码、失效的 no-op、UI 命令误启用、过期文档、字符串匹配式测试 |
| ✅ 澄清 | 4 | 三份 keymap 相同是设计；AST guard 有效；命令注册双向完整；settings 无死键 |

优先级建议：**先修 2 个 Critical**（会造成未保存工作丢失 / 核心功能不可用），再修 Major 中影响正确性的 4 项（M1/M2/M4/M5），其余可按迭代安排。

---

## 🔴 Critical

### C-1 `core/cph_checker.py` + `core/cph_interactive.py`：`_reap()` 在 POSIX 上可能杀死整个 Sublime 进程组

**现象**：一个超时/死循环的 SPJ checker 或 interactor，会导致编辑器被 SIGKILL 强制退出、未保存文件丢失。

**根因**：`_reap()`（`cph_checker.py:274-302`）在 POSIX 分支执行：

```python
os.killpg(os.getpgid(pid), signal.SIGKILL)   # cph_checker.py:292
```

它的**前提**是被杀进程通过 `preexec_fn=os.setsid` 建立了独立会话。该前提只在编译路径 `cph_checker.py:186` 成立。但以下调用点的 Popen **没有** `setsid`/`start_new_session`，与宿主同属一个进程组，`os.getpgid(pid)` 拿到的是 Sublime 的组，`killpg` 于是杀死编辑器自己：

- `cph_checker.py:355-359` 启动 checker；`:364-365`、`:370-371` 超时/异常 → `_reap(proc)`
- `cph_interactive.py:153-160` interactor 编译（`shell=True`，无 setsid）→ `:160/166` `_reap`
- `cph_interactive.py:235-245` interactor 进程 → `:356`、`:411-412` `_reap`（含正常结束的 `finally`）

这是 `cph_checker.executable()`（有 setsid）与 `cph_interactive.executable()`（无 setsid）**复制粘贴分歧**的直接后果。

**修复**：所有交给 `_reap()` 的 Popen 统一加 `start_new_session=True`（POSIX）；或让 `_reap()` 对无独立会话的进程退化为 `proc.kill()`，**绝不** `killpg` 宿主组。推荐后者作为兜底（防御性更强）。

### C-2 `cph_stress.py:1906`：对拍重放对编译型语言完全失效

**现象**：重放生成器时永远报 `generator failed: rc=-1 no run command configured`，C/C++/Go/Rust/Pascal 全军覆没，只有 Python 偶然可用。

**根因**：重放分支把编译产物 `gen_exe`（一个 `.exe` 路径字符串）直接传给 `_run_program`：

```python
rc, inp, err, tle = _run_program(
    gen_exe, '', cwd=os.path.dirname(gen_file) or None, ...)   # :1906 原始字符串
```

而 `_run_program`（ `:1225-1230` ）对字符串会走 `_program_argv → _lang_entry`，用扩展名 `exe` 去 `run_settings` 里找运行命令——全部语言的 `extensions` 里都没有 `exe`，返回 `None` → 立即返回 `(-1, '', 'no run command configured', False)`。

主循环和同一函数里的 user/std 分支都正确用了 `_program_command(gen_exe, gen_file)` 把 exe 包成 `[exe]` 列表，**唯独 replay 的 generator 漏了这层包装**。git 提交 `397b4e3`/`4fab9ae` 修的是 cwd，未触及此根因；测试 `tests/run_tests.py:6150-6155` 只做源码文本匹配，故漏检。

**修复**：`_run_program(_program_command(gen_exe, gen_file) or gen_exe, ...)`，与 user/std 保持一致。

---

## 🟠 Major

### M-1 对拍重放忽略返回码与 std 超时 → 假「输出一致」
`cph_stress.py:1930-1940`：`_rc1`、`_rc2`、`_tle2` 全部丢弃，只判断 `tle1`。当两个程序都崩溃/启动失败（`_run_program` 把异常吞成 `(-1,'',...)`）或 std 超时只输出了部分内容时，只要两边输出相同就报告「重放结果一致」。主循环明确区分了 `ret1/ret2 != 0` 与 TLE（`:1505-1574`），replay 没有对应逻辑。提交 `397b4e3` 想消除的「两个空输出 + 假 outputs match」根因（忽略返回码）仍在。**修**：重放同样校验两个 rc 与两个 tle。

### M-2 `is_correct_answer` 用全空白折叠判断，与判题器口径不符 → WA 被当作「已接受」折叠
`test_manager.py:753-760` 用 `_squash_ws`（删除**全部**空白）判断答案是否已被接受；判题器 `cph_verdict.py:331-336` 早已改为分词序列比较。于是程序输出 `1 23`、标准答案 `12 3` 时：判题是 **WA**，但 `on_stop`（`test_manager.py:3984`）调用 `is_correct_answer` 得到 `True`，走 `text: ''` 把这张 WA 卡片当已接受折叠/清空。`_squash_ws` 的注释（`test_manager.py:434`）"get_verdict_by_code compares outputs with all whitespace removed" 是修复前的**过时注释**，与现行实现矛盾。**修**：复用 `outputs_equal(answer, correct, float_tolerance)`。

### M-3 gdb 重放在 POSIX 上超时不杀进程树 → 孤儿被测进程
`core/cph_gdb_trace.py:313-329`：`_kill_tree()` 的 docstring 声称「gdb 被超时打断时，它拉起来的被测程序不能留在后台跑」，但 POSIX 分支只有 `proc.kill()`（只杀 gdb，不杀 gdb 派生的 debuggee）。Windows 有 `taskkill /T`。死循环被测程序会作为孤儿持续占 CPU。**修**：gdb 以 `os.setsid` 启动，超时后 `os.killpg(pgid, SIGKILL)`。

### M-4 统计落盘竞态：序列化活对象 + 写失败不重试
`core/cph_stats.py:149-164`：`flush()` 持锁取 `data = _cache`（**引用而非副本**）后释放锁，再在锁外 `json.dump(data)`；同时 `record_run()` 在锁内就地改同一个 `_cache`。后果：① `json.dump` 与并发写竞争可能抛 `dictionary changed size during iteration`，被 `save()` 的宽泛 `except`（`:243`）吞掉；② 失败分支只置 `_cache_dirty = True`，**未重新安排定时器**（`_cache_timer` 已在 `:154` 置 None），此后若无新判题触发 `_mark_dirty()`，这批统计永不落盘。**修**：`copy.deepcopy(_cache)`；失败分支调用 `_mark_dirty()`。

### M-5 命令式 checker/interactor 带尾部参数时路径解析错误
`core/cph_checker.py:93-107`、`core/cph_interactive.py:74-86`：把命令字符串**最后一个** token 当脚本路径做 `isfile()` 判断，`python checker.py --strict` 会取到 `--strict` → 误报 `checker_missing`。`cph_interactive.py:75` 还用 `.split()` 而非 `shlex.split()`，带引号路径会被拆错（又一处与 checker 版分歧）。**修**：取第一个存在的文件 token，统一 `shlex.split`。

### M-6 `cph_calibrate.measure()` 超时不杀进程、错误路径泄漏临时目录
`core/cph_calibrate.py:93-134`：`communicate(timeout=60)` 超时后直接 return，既不 `kill()` 也不 `_cleanup(workdir)`；编译失败（`:101`/`:110`/`:113`）同样不清理 `mkdtemp`。会话内残骸与失控基准进程并存。另有 `time.time()` 而非 `monotonic()` 计时的小问题。**修**：`finally` 统一 kill + cleanup，改用 `monotonic()`。

### M-7 对拍「带选项」模式的自定义时限输入是空操作
`cph_stress.py:742-747`：`on_limit()` 计算了 `limit` 却没传给 `on_rounds`，`on_rounds` 仍用闭包捕获的原始 `time_limit` 调 `_launch`。用户输入的时间限制被直接丢弃。**修**：把 `limit` 作为参数传给 on_rounds 工厂。

### M-8 `stress_max_rounds` 完全没有 clamp（提交说明名不副实）
提交 `4250406` 声明「stress round limit clamped」，但实际只 clamp 了 `time_limit`（`cph_stress.py:654-659`）。`max_rounds` 之后直接进入 `range(1, max_rounds + 1)`（`:1443`）。设为 `-1`（`or 1000` 拦不住负数）→ 循环零次 → 错误报告「all timeout」；设极大值 + 默认 `stress_max_wall_seconds = 0`（无总时限）→ 实际可无限运行。**修**：`max_rounds = max(1, min(max_rounds, 合理上限))`。

### M-9 重放在「已结束」session 上编译 → 污染用户源码目录 + 泄漏 live_keys
`cph_stress.py:1901,1915-1916`：正常流程 `finally` 调 `_finish_stress(sess)`（清空 `build_root`/`live_keys`，`:1279-1302`），但 replay 拿到的是**同一个已结束的 session** 并调 `_compile_program(..., sess=sess)`。此时 `_build_dir(sess)` 因 `build_root=None` 返回 None → 不做隔离 → 产物写到用户真实源码目录（可能覆盖正在用的二进制 / `Permission denied`）；且 `_popen_capture` 把新 key 加进 `sess['live_keys']`，replay 结束不调 `_finish_stress` → `register_live` 条目永不释放。**修**：replay 用自己的临时 session，或结束后调用 `_finish_stress`。

---

## 🟡 Minor

### 冗余 / 死代码（用户重点关注）
| 位置 | 符号 | 说明 |
| --- | --- | --- |
| `cph_manual.py:361-365` | `CphManualAnchorCommand` | 完全不可达：未注册任何 command/menu/keymap，全库零 `run_command` 调用。真正设锚点的是模块级 `_anchor()`（`:318`） |
| `cph_manual.py:122-127` | `_Session.pending_input()` | 死方法，已被模块级 `_pending(view)`（`:410-414`）取代，逻辑重复 |
| `Modules/build_artifact.py:196-209` | `_announce()` | 死函数，全库零调用；`_ANNOUNCED` 只被它写入 |
| `Modules/build_artifact.py:165` | `reset_announced()` | 因上述原因退化为 **no-op**；但 `test_manager.py:14` 导入为 `reset_build_announcements` 并在 `:4614` 调用、注释仍宣称「重置上报记录」——行为已不存在。连带使测试 `run_tests.py:4128-4134` 恒真（vacuous test） |
| `core/cph_i18n.py:1389` | `add_strings()` | 仅定义，零引用 |
| `core/cph_settings.py:97` | `get_supported_exts()` | 仅定义，实际用的是 `is_run_supported_ext()` |
| `core/cph_stats.py:667` | `_fmt_duration_public()` | 仅转调 `_fmt_duration`，无人调用 |
| `Modules/ProcessManager.py:319` | `get_run_entry()` | 仅定义，同类由 `get_lang_entry()` 承担，遗留副本 |

### 其它 Minor
- **残留的 `view.window()` 无 None 保护**（与提交 `0856c02` 同类）：`cph_extras.py:98/158/270`、`cph_doctor.py:511-512`、`cph_tests_io.py:189-192`。`cph_options.py:244-247` 的注释把 `CphFetchProblemCommand` 当成加了 None 判断的范本，但它本身恰恰没加——注释与代码矛盾。
- **UI 命令误启用**：`cph_tests_io.py:142-143,165-171` `CphCopyTestActualCommand.is_enabled()` 对任意有测试的源文件返回 True，但源文件视图的 tests 来自磁盘、不含 `stdout`，点击后只会得到 `nothing_to_copy`。docstring 自述「run panel only」却未收紧启用条件。
- **对拍输出解码不一致**：`cph_stress.py:1165-1167` 严格按 UTF-8 解码，而 `ProcessManager._decode_output`、`cph_checker.decode_output` 都有控制台码页回退；中文 Windows 下 GBK 输出变替换字符，按 `std_out` 存的「正确答案」后续可能对不上。
- **checker/interactor 大段重复**：`cph_checker.py:39-209` 与 `cph_interactive.py:32-178` 各自维护一套 `_compiled`/`_failed`/`_source_newer`/`executable()` 等，已产生 C-1、M-5 两处实际分歧。建议抽公共 build/spawn 辅助。
- **可变默认参数**：`test_manager.py:1032` `def __init__(..., tests=[], ...)` 后 `self.tests = tests`，而 `set_tests()`/`next_test()` 会就地 `clear()`/`append()`；当前调用都显式传参故未触发，属潜在共享状态缺陷。
- **身份比较代替内容比较**：`cph_verdict.py:295` `stdout is not expected_output` 应为 `!=`；两者为同一对象时会跳过崩溃检测。
- **陈旧文档字符串**：`cph_template.py:2` 写 `(Ctrl+Alt+T / Cmd+Alt+T)`，但包内无任何 `super+`（Cmd）绑定，三平台都是字面 Control。
- **冗余赋值 / 死参数**：`cph_stress.py:598` 与 `:605` 重复 `src_dir = os.path.dirname(user_file)`；`_native_pick(window, ...)`（`:283`）的 `window` 参数完全未使用。
- **吞异常**：`cph_stress.py:1017-1038` `_drain` 截断标志在线程 finally 设置而主线程紧接着读取，`join(timeout=2)` 超时时可能跨轮串扰（低概率竞态）；`:1055-1065` `_feed` 管道破裂静默吞掉。

### 测试质量（系统性弱点）
`tests/run_tests.py` 中约 **141 处** 采用 `in <xxx>_src` 的字符串匹配断言，例如：
- `:9165-9167`：`"clr_tests = self.session['clr_tests']" not in _tm_src_fix`
- `:9173-9175`：`"in ('RUNNING','COMPILING')" in _guard and 'batch_active' in _guard`

这类断言只验证源码里出现/不出现某个字面量，重构后可轻易绕过，无法防止以其他形式重新引入 bug——正是 C-2 这类功能失效逃过 1078 项测试的原因。建议对关键路径补真正的行为测试。

---

## ✅ 澄清（核实后非缺陷，避免误报）

1. **三份 keymap 字节级相同**（md5 `270690d5...`）是**有意设计**：`messages/2.5.0.txt:67-69`、README 均说明无 `super+` 绑定；`tests/run_tests.py:9217-9230` 还主动断言三平台都绑定了 `ctrl+alt+shift+n`。不是 drift。
2. **AST guard 有效**：`tests/run_tests.py:6093-6124` 可识别直接继承 `TextCommand` 的类，复核当前**零违规**；`CphIoModeCommand` 已正确用 `self.view.window()` 并做 None 判断。
3. **命令注册双向完整**：所有 `cph_xxx` 注册都有对应类，无孤儿命令。另有 4 个未注册命令类（`cph_import_tests_file` 等）均被内部 `run_command` 调用，属正常设计。
4. **settings 无死键**：56 个已定义键全部有引用；代码中读到的「未定义键」均为 view setting 或经 helper 间接读取，非缺陷。无裸 `except:`。

---

## 建议修复顺序

1. **立即（数据丢失/功能不可用）**：C-1、C-2
2. **正确性**：M-1、M-2、M-3、M-7、M-8
3. **健壮性/资源**：M-4、M-5、M-6、M-9
4. **清理**：删除 8 处死代码 / no-op，收紧 UI 启用条件，统一解码与重复模块
5. **测试**：为 C-2、M-1 等补行为测试，逐步替换字符串匹配式断言

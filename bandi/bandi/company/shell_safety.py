# 功能：Shell 安全分类 — 70+ 只读命令 + 危险 flag + git/sed/awk/curl 审计 + fail-closed

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class ShellSafetyLevel(StrEnum):
    SAFE = "safe"
    CAUTION = "caution"
    DANGEROUS = "dangerous"
    DENIED = "denied"


SHELL_CONTROL_TOKENS = {"&&", "||", ";", "|", "&", ">", ">>", "<", "<<", "\n", "\r"}
# 只剥无害重定向：/dev/null、/dev/zero、/dev/fd/N 丢弃与 fd 复制（2>&1）；
# 指向真实文件的重定向（2> err.log / &> out.log）保留，触发分段走 fail-closed。
SAFE_REDIRECTION_RE = re.compile(r"(?:\d*&?>>?\s*/dev/(?:null|zero|fd/\d+)\b|\d*>&\d+)")
_EXPANSION_SAFE_HEADS = {"pwd", "date", "whoami", "echo", "basename", "dirname"}

_TEST_SAFE_FLAGS = {"-n", "-e", "-r", "-w", "-x", "-f", "-d", "-s"}
# find/fd 与 rg/ripgrep 命令族（内联集合提取为模块常量）
_FIND_COMMANDS = frozenset({"find", "fd"})
_RG_COMMANDS = frozenset({"rg", "ripgrep"})

_GENERIC_READ_ONLY = {
    "cat",
    "head",
    "tail",
    "less",
    "more",
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "ripgrep",
    "find",
    "locate",
    "which",
    "whereis",
    "type",
    "file",
    "stat",
    "du",
    "ls",
    "dir",
    "tree",
    "pwd",
    "echo",
    "printf",
    "env",
    "printenv",
    "sort",
    "uniq",
    "wc",
    "cut",
    "tr",
    "fold",
    "nl",
    "od",
    "base64",
    "md5sum",
    "sha1sum",
    "sha256sum",
    "date",
    "cal",
    "ncal",
    "whoami",
    "id",
    "who",
    "w",
    "users",
    "groups",
    "logname",
    "ps",
    "top",
    "htop",
    "uptime",
    "uname",
    "lsblk",
    "blkid",
    "df",
    "free",
    "vmstat",
    "iostat",
    "mpstat",
    "lscpu",
    "lspci",
    "lsusb",
    "lsmod",
    "lsof",
    "ss",
    "netstat",
    "ping",
    "traceroute",
    "nslookup",
    "dig",
    "host",
    "getconf",
    "locale",
    "help",
    "jq",
    "yq",
    "xmlstarlet",
    "csvtool",
    "python",
    "python3",
    "node",
    "deno",
    "bun",
    "ruby",
    "perl",
    "git",
    "sed",
    "awk",
    "diff",
    "comm",
    "cmp",
}

# 执行，argv 层面看是惰性的，绝不能进只读白名单。与 UNGRANTABLE_PREFIX_HEADS
# 已覆盖的 eval/exec/source 去重。
# 整命令进只读白名单 ⇒ 写形态（mount -o remount,rw、sysctl -w、hostnamectl set-hostname
# 注：mount / sysctl / hostnamectl / timedatectl / localectl / hostname / history /
# （`ifconfig eth0 up` / `ip addr add` 均为写形态）。
# 首参数为空（裸命令）或在白名单内才判只读，其余一律拒绝（fail-closed）。
_READ_ONLY_FIRST_ARGS: dict[str, frozenset[str]] = {
    "mount": frozenset(),  # 裸 mount = 列挂载；带参 = 挂载 / 重挂载（写）
    "arp": frozenset({"-n", "-a", "-v"}),
    "ifconfig": frozenset({"-a", "--all", "-s"}),
    "hostname": frozenset(
        {"-f", "--fqdn", "-s", "--short", "-d", "--domain", "-i", "--ip-address", "-a", "--alias"}
    ),
    "hostnamectl": frozenset({"status", "show"}),
    "timedatectl": frozenset(
        {"status", "show", "timesync-status", "show-timesync", "list-timezones"}
    ),
    "localectl": frozenset({"status", "show"}),
    "route": frozenset({"-n", "-e", "-ee", "-v", "--numeric"}),
}
# sysctl 的写形态：`-w/--write` 改内核参数，`-p/--load/--system` 加载配置文件
_SYSCTL_WRITE_FLAGS = frozenset({"-w", "--write", "-p", "--load", "--system"})
# history 的写/清形态：-c 清空、-w 写文件、-r 读入、-a 追加、-n 重读、-p 推送
_HISTORY_WRITE_FLAGS = frozenset({"-c", "-w", "-r", "-a", "-n", "-p"})

_EVAL_LIKE_BUILTINS = frozenset(
    {
        "command",
        "builtin",
        "fc",
        "coproc",
        "trap",
        "enable",
        "mapfile",
        "readarray",
        "hash",
        "bind",
        "complete",
        "compgen",
        "alias",
        "let",
    }
)

_BANNED_FLAGS: dict[str, set[str]] = {
    "sort": {"-o", "--output", "--compress-program"},
    "yq": {"-i", "--inplace", "--in-place"},
    "xmlstarlet": {"-L", "--inplace", "--in-place"},
    "date": {"-s", "--set"},
    "touch": {"-t", "--time"},
    "chmod": {"-R", "--recursive"},
    "sed": {"-i", "--in-place"},
    "git": {
        "push",
        "fetch",
        "pull",
        "merge",
        "rebase",
        "reset",
        "commit",
        "cherry-pick",
        "revert",
        "clean",
        "gc",
        "prune",
    },
}

_GIT_READ_ONLY_SUBCOMMANDS = {
    "status",
    "log",
    "diff",
    "show",
    "branch",
    "tag",
    "describe",
    "rev-parse",
    "rev-list",
    "ls-tree",
    "ls-files",
    "ls-remote",
    "blame",
    "annotate",
    "grep",
    "shortlog",
    "stash list",
    "stash show",
    "config --list",
    "config --get",
    "remote -v",
    "remote show",
    "submodule status",
    "worktree list",
    "notes list",
    "notes show",
}
_GIT_PAGER_INJECTION_FLAGS = frozenset({"-O", "--open-files-in-pager", "--textconv"})
_GIT_LIST_ONLY_SUBCOMMANDS: dict[str, set[str]] = {
    "branch": {"-a", "--all", "-r", "--remote", "--merged", "--no-merged", "-v", "--verbose"},
    "tag": {"-l", "--list", "-n", "--sort", "--contains", "--merged", "--no-merged"},
    "remote": {"-v", "--verbose"},
}

_FIND_BANNED_PREDICATES = {
    "-delete",
    "-exec",
    "-execdir",
    "-ok",
    "-okdir",
    "-fls",
    "-fprint",
    "-fprint0",
    "-fprintf",
}
_FD_BANNED_EXEC_FLAGS = {"-x", "-X", "--exec", "--exec-batch"}

# -f / --file 程序来自文件（内容任意，可含 system("rm -rf /")）
# -i / --include 加载库文件（会执行其中代码）
# -l / --load 加载扩展库（会执行其中代码）
_AWK_FOREIGN_PROGRAM_FLAGS = frozenset({"-f", "--file", "-i", "--include", "-l", "--load"})
_RG_BANNED_FLAGS = {"--pre", "--hostname-bin", "--no-ignore", "--no-ignore-parent"}
# 匹配 ⇒ 被判只读自动批准，而这两个 flag 都会执行外部命令。故匹配时取 `=` 前段
# （与 find 的 `-exec=` 同一处理口径）。
_NETWORK_AUDITED: set[str] = {"curl", "wget"}
_SHELL_AUDITED_WRITE_CANDIDATES: set[str] = {"sed", "awk", "git"}
_INTERPRETERS = {"python", "python3", "node", "deno", "bun", "ruby", "perl", "php", "lua"}
UNGRANTABLE_PREFIX_HEADS = {
    "bash",
    "sh",
    "zsh",
    "dash",
    "ksh",
    "fish",
    # Windows 解释器前缀同样不可授权——`pwsh -Command Remove-Item` / `cmd /c rmdir /s /q`
    # pwsh.exe/powershell.exe 等 .exe 后缀变体同样拦截
    "pwsh",
    "pwsh.exe",
    "powershell",
    "powershell.exe",
    "cmd",
    "cmd.exe",
    "sudo",
    "su",
    "doas",
    "eval",
    "exec",
    "source",
    "time",
    "nohup",
    "nice",
    "stdbuf",
    "taskset",
    "env",
    "nix-shell",
}
_DANGEROUS_SHELL_PATTERNS = {
    "rm -rf",
    "rm -r",
    "rm -fr",
    "rm -rf/",
    "rm -fr/",
    "mkfs",
    "dd if=",
    "dd of=",
    "chmod 777",
    "chown",
    "passwd",
    "shutdown",
    "reboot",
    "init ",
    "halt",
    "poweroff",
    ":(){ :|& };:",
    "> /dev/sd",
    "< /dev/sd",
}


def pipeline_deny_reason(command: str) -> str | None:
    """功能：六-注入形态硬拒检查（单一来源）——管道执行解释器/反引号/
    命令替换不给确认机会，直接 deny。从 permissions_pipeline 迁入此模块
    与 classify_shell_command 同源（全部 shell 判定归 shell_safety 单一模块）。"""
    dangerous_patterns = ("| sh", "| bash", "| python", "`", "$(")
    for p in dangerous_patterns:
        if p in command:
            return f"dangerous shell pattern: {p}"
    return None


def _segments_with_dangerous_pattern(cmd: str) -> str | None:
    """危险模式逐段检查——echo/printf 参数与白名单只读命令
    （grep/cat/head...）的参数是字面量/检索模式，不执行，跳过豁免；
    interpreter（python/node...）与未知命令段的参数会被执行，必须检查。
    引号包裹形态（python -c 'rm -rf /x'）先剥引号再匹配。
    """
    for seg in split_shell_segments(cmd):
        if not seg:
            continue
        head = seg[0].lower()
        if head in ("echo", "printf"):
            continue
        if head in _GENERIC_READ_ONLY and head not in _INTERPRETERS:
            continue
        text = " ".join(seg).replace("'", " ").replace('"', " ").lower()
        for pattern in _DANGEROUS_SHELL_PATTERNS:
            if text.startswith(pattern) or f" {pattern}" in text:
                return pattern
    return None


@dataclass
class ShellSafetyResult:
    level: ShellSafetyLevel
    reason: str
    matched_pattern: str | None = None


def strip_safe_redirections(cmd: str) -> str:
    return SAFE_REDIRECTION_RE.sub("", cmd).strip()


def _expansion_end(text: str, start: int) -> int:
    """返回从 start（'$(' 的 '(' 之后）开始的平衡括号闭合位置（不含 ')'）。"""
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                return i
            depth -= 1
    return len(text)


def _expansion_all_safe(inner: str) -> bool:
    """$() 展开块内部是否全部安全：递归扫描嵌套 $()，任一层首词不在安全白名单即不安全。

    内层含命令分隔符（; / && / || / | / 单个 & 后台符）、重定向（> / <）、反引号
    命令替换时即使首词安全（echo）也可能执行/写入任意目标 → 视为不安全，保留
    原块交下游拒绝。
    """
    if not inner.strip():
        return True
    if re.search(r"(?:;|&&|\|\||(?<!\|)\|(?!\|)|(?<!&)&(?!&)|[<>]|`)", inner):
        return False
    parts: list[str] = []
    i = 0
    n = len(inner)
    while i < n:
        idx = inner.find("$(", i)
        if idx == -1:
            parts.append(inner[i:])
            break
        parts.append(inner[i:idx])
        end = _expansion_end(inner, idx + 2)
        nested_inner = inner[idx + 2 : end].strip()
        if not _expansion_all_safe(nested_inner):
            return False
        parts.append("__SAFE_EXP__")
        i = end + 1
    head_tokens = " ".join(parts).split()
    head = head_tokens[0] if head_tokens else ""
    return head in _EXPANSION_SAFE_HEADS


def sanitize_expansions(cmd: str) -> str:
    # 按平衡括号扫描替换整个 $() 展开块，支持嵌套 $(echo $(date))
    # 外层头安全但内层含任意命令（echo $(rm -rf /)）时必须保留原块
    # 交由下游分类器拒绝——不得因外层 echo 吞掉恶意嵌套。
    parts: list[str] = []
    i = 0
    n = len(cmd)
    while i < n:
        idx = cmd.find("$(", i)
        if idx == -1:
            parts.append(cmd[i:])
            break
        parts.append(cmd[i:idx])
        end = _expansion_end(cmd, idx + 2)
        inner = cmd[idx + 2 : end].strip()
        if _expansion_all_safe(inner):
            parts.append("__SAFE_EXP__")
        else:
            parts.append(cmd[idx : end + 1])
        i = end + 1
    return "".join(parts)


def _control_positions_outside_quotes(cmd: str, token: str) -> list[int]:
    """扫描命令，返回引号外的 token 出现位置——sed 脚本
    `s|a|b|p` 的 `|` 是替换分隔符（引号内字面量），不得当管道符拆分。"""
    positions: list[int] = []
    quote: str | None = None
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if quote:
            if quote == '"' and ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if cmd.startswith(token, i):
            positions.append(i)
            i += len(token)
            continue
        i += 1
    return positions


_CONTINUATION_RE = re.compile(r"\\+(?:\r\n|\n|\r)")


def _join_line_continuations(cmd: str) -> str:
    """续行奇偶判定：奇数个反斜杠+换行是续行
    （折叠，保留配对剩余的反斜杠）；偶数个是转义序列两两配对、换行仍是
    命令分隔符（原样保留）。"""

    def _sub(m: re.Match[str]) -> str:
        n = len(m.group(0)) - 1
        if n % 2 == 1:
            return "\\" * ((n - 1) // 2)
        return m.group(0)

    return _CONTINUATION_RE.sub(_sub, cmd)


def split_shell_segments(cmd: str) -> list[list[str]]:
    # 按长度降序匹配：长控制符（&&/||/>>/<<）优先于其子串（&/|/>/<）
    # 避免集合无序迭代导致 "a && b" 先命中 "&" 拆出空段。
    # 仅拆分引号外的控制符（引号内是字面量/分隔符）。
    cmd = _join_line_continuations(cmd)
    for tok in sorted(SHELL_CONTROL_TOKENS, key=len, reverse=True):
        positions = _control_positions_outside_quotes(cmd, tok)
        if positions:
            parts: list[str] = []
            last = 0
            for pos in positions:
                parts.append(cmd[last:pos])
                last = pos + len(tok)
            parts.append(cmd[last:])
            # 递归切分：单次只按一种控制符切，剩余段可能仍含其他控制符
            # （如 `echo ok;ls\nrm x` 按 ; 切后第二段仍含 \n）——不递归则
            # 后续命令被吞进前段参数漏检
            segments: list[list[str]] = []
            for part in parts:
                if part.strip():
                    segments.extend(split_shell_segments(part))
            return segments
    return [cmd.split()]


def command_has_redirection(cmd: str) -> bool:
    return bool(re.search(r"(?<!\$)[>]<(?!<)|(?<!\$)[>]>(?!>)|(?<!\$)\d*>[>&]?", cmd))


def _gated_read_only(lower_head: str, rest: list[str]) -> tuple[bool, str] | None:
    """功能：对「写/读形态只差一个 flag / 子命令」的命令按首参数白名单判定（同族）。

    为什么需要它：这些命令整命令进只读白名单时，**写形态会被
    当只读自动批准**——`mount -o remount,rw /`、`sysctl -w ...`、`hostnamectl set-hostname ...`
    `route add ...`、`hostname evil` 全部命中。返回 None 表示不走本机制（保持既有行为）。
    """
    allowed = _READ_ONLY_FIRST_ARGS.get(lower_head)
    if allowed is not None:
        first = rest[0] if rest else ""
        if not first:
            return True, f"read-only: {lower_head} (bare)"
        if first in allowed:
            return True, f"read-only: {lower_head} ({first})"
        # localectl 的 `list-*` 子命令族（list-keymaps / list-locales / list-x11-keymap-*）只读
        if lower_head == "localectl" and first.startswith("list-"):
            return True, f"read-only: localectl {first}"
        return False, f"{lower_head} write-capable form: {first}"
    if lower_head == "sysctl":
        for t in rest:
            if t.split("=", 1)[0] in _SYSCTL_WRITE_FLAGS:
                return False, f"sysctl write flag: {t}"
        return True, "sysctl read"
    if lower_head == "history":
        for t in rest:
            if t in _HISTORY_WRITE_FLAGS:
                return False, f"history write flag: {t}"
        return True, "history read"
    return None


def _segment_read_only(tokens: list[str], config_prefixes: set[str]) -> tuple[bool, str]:
    if not tokens:
        return True, "empty"
    head = tokens[0]
    lower_head = head.lower()
    rest = tokens[1:]

    if lower_head in UNGRANTABLE_PREFIX_HEADS:
        return False, f"ungrantable prefix: {lower_head}"
    if lower_head in _EVAL_LIKE_BUILTINS:
        return False, f"eval-like builtin: {lower_head} (arguments execute as code)"
    if lower_head in _INTERPRETERS:
        return False, f"interpreter: {lower_head} (cannot verify safety)"

    banned = _BANNED_FLAGS.get(lower_head)
    if banned:
        for tok in rest:
            for b in banned:
                if tok.startswith(b):
                    return False, f"banned flag for {lower_head}: {tok}"
        remaining = [t for t in rest if not t.startswith("-") or t in _TEST_SAFE_FLAGS]
        if not remaining:
            return True, f"{lower_head} with safe flags"

    if lower_head == "git":
        return _git_segment_read_only(tokens)
    if lower_head == "sed":
        return _sed_segment_read_only(tokens)
    if lower_head == "awk":
        return _awk_segment_read_only(tokens)
    if lower_head in _FIND_COMMANDS:
        return _find_segment_read_only(tokens)
    if lower_head in _RG_COMMANDS:
        return _rg_segment_read_only(tokens)
    if lower_head in _NETWORK_AUDITED:
        return False, f"network audited: {lower_head} (requires whitelist)"

    gated = _gated_read_only(lower_head, rest)
    if gated is not None:
        return gated

    if lower_head in _GENERIC_READ_ONLY:
        return True, f"read-only: {lower_head}"

    if config_prefixes and lower_head in config_prefixes:
        return True, f"config-allowed prefix: {lower_head}"

    return False, f"unknown command: {lower_head}"


def _git_segment_read_only(tokens: list[str]) -> tuple[bool, str]:
    sub = " ".join(tokens[1:]) if len(tokens) > 1 else ""
    if not sub:
        return True, "git (no subcommand)"
    for t in tokens[2:]:
        if t.startswith("--output") or t in ("-o",) or (t.startswith("-o") and len(t) > 2):
            return False, f"git write flag: {t}"
        # 命令**（pager 注入，git 已知危险 flag）；`--textconv` 会执行配置里定义的 textconv
        # 命令。三者都让只读子命令变成执行入口 ⇒ 一律拒绝。
        if t in _GIT_PAGER_INJECTION_FLAGS or t.split("=", 1)[0] in _GIT_PAGER_INJECTION_FLAGS:
            return False, f"git pager-injection flag: {t}"
    for readonly in _GIT_READ_ONLY_SUBCOMMANDS:
        if sub.startswith(readonly):
            if readonly in _GIT_LIST_ONLY_SUBCOMMANDS:
                allowed = _GIT_LIST_ONLY_SUBCOMMANDS[readonly]
                flags = {
                    t.split("=", 1)[0] if t.startswith("--") else t[:2]
                    for t in tokens[2:]
                    if t.startswith("-")
                }
                extra = flags - allowed
                if extra:
                    return False, f"git {readonly} unsafe flags: {' '.join(extra)}"
                # （`git branch foo` / `git tag v1`）；有 list flag 时位置参数是
                # 过滤 pattern（`git tag -l 'v*'` / `git branch --merged master`）
                positional = [t for t in tokens[2:] if not t.startswith("-")]
                if positional and not flags:
                    return False, f"git {readonly} positional arg (write): {' '.join(positional)}"
            return True, f"git {readonly}"
    return False, f"git non-readonly subcommand: {sub.split()[0] if sub else ''}"


def _sed_script_has_write_or_exec(script: str) -> bool:
    """sed 脚本是否含 w（写文件）或 e（执行）命令——二者是写/执行原语。

    w 命令形态：地址或空后跟 `w` + 空格/文件（`1w /tmp/evil`、`/x/w file`、`$w f`）；
    e 命令形态：独立 `e` 后跟命令（`e echo pwned`、`1e cmd`）。脚本可能带单引号
    包裹（shell 分词后 '1w 成为独立 token），先剥离引号再匹配。
    s 替换命令的 w 标志支持任意自定义分隔符（`s|a|b|w FILE` / `s:a:b:w FILE`）。
    """
    cleaned = script.strip("'\"")
    # 地址可缺省（`e cmd` 独立执行原语）；文件名可无空格紧跟（`1w/tmp/evil`）
    # 或分号后跟下一条命令（`/x/w; /y/p`）
    if re.search(r"(?:^|;|\s)(?:[0-9$/,]|/[^;]*/)*w(?=\s|;|$|/)", cleaned):
        return True
    if re.search(r"(?:^|;|\s)(?:[0-9$/,]|/[^;]*/)*e(?=\s|$)", cleaned):
        return True
    m = re.match(r"s(.)", cleaned)
    if m and not m.group(1).isalnum() and m.group(1) != "\\":
        sep = re.escape(m.group(1))
        if re.search(rf"s{sep}[^;]*?{sep}w(?=\s|;|$|/)", cleaned):
            return True
        # `e` 标志把**替换结果当 shell 命令执行**（`s|^|id;touch /tmp/pwned|e`）
        # `w` 标志写文件（`s|a|b|w /tmp/f`）；标志可能是组合形态（`ep` / `we`）。
        flags = re.search(rf"s{sep}[^{sep}]*{sep}[^{sep}]*{sep}([A-Za-z0-9]*)", cleaned)
        if flags and ("w" in flags.group(1) or "e" in flags.group(1)):
            return True
    return False


def _sed_script_has_print(script: str) -> bool:
    """sed 脚本是否仅打印（p 标志）——/p 或自定义分隔符 sX...Xp 形态。"""
    cleaned = script.strip("'\"")
    # `sed -n <引号>s|^|touch /tmp/pwned; id|e<引号> f` 因 `/tmp/pwned` 含 `/p` 而被判
    # 「sed -n /p (print only)」⇒ SAFE，叠加 s 的 e 标志即漏检「可执行任意命令」。
    # 故删除捷径，只保留下方的**结构判定**（地址 + p）。
    m = re.match(r"s(.)", cleaned)
    if m and not m.group(1).isalnum() and m.group(1) != "\\":
        sep = re.escape(m.group(1))
        if re.search(rf"s{sep}[^;]*?{sep}p(?:\s|;|$)", cleaned):
            return True
    # 地址 + p 形态（`p` / `1p` / `2,5p` / `$p` / `/re/p`），地址可缺省。
    # 于是 `s|^|id;touch /tmp/pwned|e` 因 `pwned` 含 p 而被判「sed -n /p (print only)」
    # ⇒ SAFE，叠加 s 的 e 标志即成「可执行任意命令却自动批准」的漏检
    return bool(re.match(r"^(?:[0-9$/,]+|(?:\\?/)[^/]*/)*!?p(?:\s|;|$)", cleaned))


def _regroup_quoted_tokens(tokens: list[str]) -> list[str]:
    """功能：把被 shell 按空白拆开的**引号包裹块**复原为单个字符串（同族）。

    为什么需要：`cmd.split()` 只按空白拆词，引号内的空格也被当分隔符
    于是 `sed -n <引号>s|^|touch /tmp/pwned; id|e<引号> f` 的脚本被拆成 3 片 ⇒ 任何
    「基于完整脚本」的检查（s 的 w/e 标志、w 命令、打印形态）全部失灵，导致「可执行任意
    命令」的载荷被判 SAFE 自动批准。此处按引号首尾配对把碎片重新拼回。
    """
    out: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t and t[0] in ("'", '"') and not t.endswith(t[0]):
            quote = t[0]
            merged = [t]
            j = i + 1
            while j < len(tokens):
                merged.append(tokens[j])
                if tokens[j].endswith(quote):
                    break
                j += 1
            out.append(" ".join(merged))
            i = j + 1
            continue
        out.append(t)
        i += 1
    return out


def _sed_segment_read_only(tokens: list[str]) -> tuple[bool, str]:
    tokens = _regroup_quoted_tokens(tokens)
    has_safe_n = False
    has_print = False
    scripts: list[str] = []
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t == "-n":
            has_safe_n = True
        elif t == "-i" or t.startswith("-i"):
            return False, "sed -i (in-place edit)"
        elif t in ("-e", "--expression"):
            if i + 1 < len(tokens):
                scripts.append(tokens[i + 1])
                i += 1
        elif t.startswith("-e") and len(t) > 2:
            scripts.append(t[2:])
        elif t.startswith("-") and t not in {"-E", "-r", "-s", "--"}:
            return False, f"sed unsafe flag: {t}"
        else:
            scripts.append(t)
        i += 1
    # 脚本内的 w（写文件）与 e（执行）命令尾标是写/执行原语
    for script in scripts:
        if _sed_script_has_write_or_exec(script):
            return False, "sed script contains w/e command"
    if has_safe_n:
        for script in scripts:
            # 打印判定同样分隔符感知（s|a|b|p 合法打印不得误伤）
            if _sed_script_has_print(script):
                has_print = True
                break
        if has_print:
            return True, "sed -n /p (print only)"
    return False, "sed not readonly (use -n with /p for safe usage)"


def _awk_has_pipe_outside_literals(code: str) -> bool:
    """awk 管道原语检测（引号/正则感知）——`|` 可能出现在字符串字面量
    （"a|b"）、正则交替符（foo|bar/）内，那些不是管道；`|`/`|&` 出现在
    字面量外即管道原语（`"cmd"|var`、`"x"|c`、`|&` 双向管道、`cmd|getline`）。"""
    i = 0
    n = len(code)
    state = "normal"  # normal | sq | dq | regex | regex_class
    while i < n:
        ch = code[i]
        if state == "normal":
            if ch == "'":
                state = "sq"
            elif ch == '"':
                state = "dq"
            elif ch == "/":
                state = "regex"
            elif ch == "|":
                return True
            i += 1
            continue
        if state == "sq":
            if ch == "'":
                state = "normal"
            i += 1
            continue
        if state == "dq":
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                state = "normal"
            i += 1
            continue
        if state == "regex":
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == "[":
                state = "regex_class"
            elif ch == "/":
                state = "normal"
            i += 1
            continue
        # regex_class：字符类内 / 不算正则结束
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "]":
            state = "regex"
        i += 1
    return False


def _awk_segment_read_only(tokens: list[str]) -> tuple[bool, str]:
    code = " ".join(tokens[1:])
    # "x"|c}' 后缀保留引号），先剥外层配对引号再扫描——否则整个程序被当
    # 字面量，管道原语被跳过
    if len(code) >= 2 and code[0] in ("'", '"') and code[-1] == code[0]:
        code = code[1:-1]
    # system( 与 system (（带空格）都是执行原语，不得放行
    if re.search(r"system\s*\(", code):
        return False, "awk contains dangerous: system()"
    # 无空格管道形态 ("cmd"|getline) 同样启动子进程执行
    if re.search(r"\|[ \t]*getline", code):
        return False, "awk contains dangerous: pipe getline"
    # `|&`（双向管道）也是执行原语，且**不匹配**上面的 pipe getline 检查（`&` 夹在中间）；
    # 它可能落在 shell 单引号内 ⇒ `_awk_has_pipe_outside_literals` 的引号状态机会把整段
    # 当字面量吞掉（awk 程序尾部常带文件名参数，整串剥引号会失败）。故对 `|&` 直接
    # fail-closed：字符串字面量里出现 `|&` 极罕见，漏检代价远大于误伤。
    if "|&" in code:
        return False, "awk contains dangerous: two-way pipe |&"
    # 程序来自外部文件 / 就地编辑（`-i inplace`）——静态不可见，一律拒绝
    for tok in tokens[1:]:
        if tok.split("=", 1)[0] in _AWK_FOREIGN_PROGRAM_FLAGS:
            return False, f"awk banned flag: {tok}"
    # 字面量外的 | 与 |& 都是管道原语（"cmd"|var / "x"|c / |& 双向管道）
    if _awk_has_pipe_outside_literals(code):
        return False, "awk contains dangerous: pipe to command"
    # print/printf 后任意位置的输出重定向写文件（`print $1 > "file"` 全形态拦截）
    if re.search(r"(?:print|printf)[^;]*>", code):
        return False, "awk contains dangerous: output redirection"
    # `print | "sh"` 管道启动子进程执行原语（无空格/带引号）
    if re.search(r"(?:print|printf)[^;]*\|[ \t]*[\"']", code):
        return False, "awk contains dangerous: pipe to shell"
    for dangerous in (
        ">>",
        "getline <",
        "close(",
        "| ",
        "fflush(",
    ):
        if dangerous in code:
            return False, f"awk contains dangerous: {dangerous}"
    return True, "awk read-only"


def _find_segment_read_only(tokens: list[str]) -> tuple[bool, str]:
    for t in tokens[1:]:
        # 词边界感知：精确匹配 + -flag=value 粘连形态（fail-closed 方向）
        if t in _FIND_BANNED_PREDICATES or any(
            t.startswith(banned + "=") for banned in _FIND_BANNED_PREDICATES
        ):
            return False, f"find banned predicate: {t}"
    if tokens[0] == "fd":
        for t in tokens[1:]:
            if t in _FD_BANNED_EXEC_FLAGS:
                return False, f"fd banned exec flag: {t}"
    return True, "find read-only"


def _rg_segment_read_only(tokens: list[str]) -> tuple[bool, str]:
    for t in tokens[1:]:
        if t.split("=", 1)[0] in _RG_BANNED_FLAGS:  # 含 `--pre=cmd` 粘连形态
            return False, f"rg banned flag: {t}"
    return True, "rg read-only"


def is_read_only_shell_command(
    command: str, config_prefixes: set[str] | None = None
) -> tuple[bool, str]:
    candidates = [strip_safe_redirections(command)]
    if candidates[0] != command:
        candidates.append(command)
    resolved_prefixes = config_prefixes or set()
    for cmd in candidates:
        sanitized = sanitize_expansions(cmd)
        # 不得因外层命令（cat/echo）是只读命令而放行——展开内容会在执行期运行。
        if "$(" in sanitized:
            return False, "command substitution contains unsafe content"
        segments = split_shell_segments(sanitized)
        single = len(segments) <= 1
        if len(segments) > 1 and command_has_redirection(sanitized):
            for s in segments:
                ok, reason = is_read_only_shell_command(" ".join(s), config_prefixes)
                if not ok:
                    return False, f"segment not readonly: {reason}"
            return True, "all segments readonly"
        for seg in segments:
            ok, reason = _segment_read_only(seg, resolved_prefixes)
            if not ok:
                return False, reason
            if single:
                return True, reason
        return True, "all segments readonly"
    return False, "could not classify"


def backtick_substitution_reason(cmd: str) -> str | None:
    """功能：**单引号外**反引号命令替换的内容递归判定（同族）。

    为什么需要：反引号与 `$()` 同为「零静态可见性」的命令替换原语，但分类器此前
    只处理了 `$()`（`sanitize_expansions`）⇒ 反引号形态被判 SAFE 自动批准
    （差分审计：`echo` 后跟反引号 rm、`cat` 后跟反引号 rm 均判 SAFE）。
    单引号内的反引号是字面量（shell 不求值）故跳过；**双引号内不阻止命令替换**
    因此必须检查。内容安全（如反引号 date）视为已求值字面量继续；不安全即返回
    原因（fail-closed）。
    """
    i, n, in_single = 0, len(cmd), False
    while i < n:
        ch = cmd[i]
        if ch == "'":
            in_single = not in_single
            i += 1
            continue
        if ch == "`" and not in_single:
            end = cmd.find("`", i + 1)
            if end == -1:
                return "unterminated backtick substitution"
            ok, reason = is_read_only_shell_command(cmd[i + 1 : end])
            if not ok:
                return f"backtick substitution not readonly: {reason}"
            i = end + 1
            continue
        i += 1
    return None


def ansi_c_escape_reason(cmd: str) -> str | None:
    """功能：`$'...'`（ANSI-C 引用）内含**反斜杠转义**时返回原因（同族）。

    为什么需要：ANSI-C 引用（dollar 后跟单引号包裹）内的转义在**执行期**才展开——
    换行转义变真换行（= 命令分隔符）、十六进制转义可变分号、八进制转义可变单引号。
    静态分类器看到的是「引号内
    字面量」，与实际语义不符（差分审计：把换行藏在 `$'...'` 里再跟
    `rm -rf` 的写法被判 SAFE，但 shell 真会执行 rm）。转义形态无穷 ⇒ 含反斜杠即拒
    （fail-closed；日常只读命令几乎不会在 `$'...'` 里写转义）。
    """
    i, n, backslash = 0, len(cmd), chr(92)
    while i < n:
        if cmd.startswith("$'", i):
            j = i + 2
            while j < n:
                if cmd[j] == backslash:
                    return "ANSI-C quote with escape (execution-time expansion)"
                if cmd[j] == "'":
                    break
                j += 1
            i = j + 1
            continue
        i += 1
    return None


def classify_shell_command(command: str) -> ShellSafetyResult:
    cmd = command.strip()
    if not cmd:
        return ShellSafetyResult(ShellSafetyLevel.SAFE, "empty command")
    # 危险模式粗筛逐段进行（字面/只读段豁免），命中即 DENIED
    matched = _segments_with_dangerous_pattern(cmd)
    if matched:
        return ShellSafetyResult(ShellSafetyLevel.DENIED, f"dangerous: {matched}", matched)

    # ① ANSI-C 引用（dollar 后跟单引号包裹）内的转义在**执行期**才展开（换行转义 →
    # 真换行 = 命令分隔符）
    # 分类器看到的却是「引号内字面量」。
    ansi_reason = ansi_c_escape_reason(cmd)
    if ansi_reason:
        return ShellSafetyResult(ShellSafetyLevel.DENIED, ansi_reason, "ANSI-C quote")
    backtick_reason = backtick_substitution_reason(cmd)
    if backtick_reason:
        return ShellSafetyResult(ShellSafetyLevel.DENIED, backtick_reason, "backtick")
    prefix_tokens = cmd.split()
    prefix = prefix_tokens[0] if prefix_tokens else ""
    if prefix in UNGRANTABLE_PREFIX_HEADS:
        return ShellSafetyResult(ShellSafetyLevel.DENIED, f"ungrantable: {prefix}", prefix)
    ok, reason = is_read_only_shell_command(cmd)
    if ok:
        return ShellSafetyResult(ShellSafetyLevel.SAFE, reason)
    if prefix in _NETWORK_AUDITED:
        return ShellSafetyResult(ShellSafetyLevel.CAUTION, f"{prefix} needs whitelist", prefix)
    has_control = any(_control_positions_outside_quotes(cmd, tok) for tok in SHELL_CONTROL_TOKENS)
    if has_control:
        return ShellSafetyResult(ShellSafetyLevel.DANGEROUS, "control operators")
    # 已知只读分类器命令（sed/awk/git）判定为非只读 → 说明含写/执行形态
    # （sed w/e、awk system、git --output 等）→ DANGEROUS（禁止静默执行）
    if prefix in _SHELL_AUDITED_WRITE_CANDIDATES:
        return ShellSafetyResult(ShellSafetyLevel.DANGEROUS, reason or f"{prefix} not readonly")
    return ShellSafetyResult(ShellSafetyLevel.CAUTION, reason)


def is_safe_to_auto_approve(command: str) -> bool:
    result = classify_shell_command(command)
    return result.level == ShellSafetyLevel.SAFE

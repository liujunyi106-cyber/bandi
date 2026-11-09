#
# 安全原语的全部组合形态（地址 + 命令 + 分隔符 + 引号 + 粘连 + 壳层转义）枚举成载荷
# 喂给生产分类器，找出「被判 SAFE（= is_safe_to_auto_approve 为真 = 会被自动批准执行）
#
# 三条正交要求：
# ① 写/执行原语的**组合形态**一律不得自动批准；
# ② 反向——合法的只读形态**不得被误伤**（防「过度收紧」把只读命令全变需确认）；
# ③ 族级完整性——只读白名单里不得再出现「写/读只差一个 flag」的命令。
#
# 判据一律用**生产入口**（`is_safe_to_auto_approve` / `classify_shell_command`）
# 不测私有实现细节；载荷全部用 raw 字符串以免转义失真。

from __future__ import annotations

import pytest

from bandi.company.shell_safety import (
    ShellSafetyLevel,
    classify_shell_command,
    is_safe_to_auto_approve,
)

# ── ① 写 / 执行原语的组合形态（断言：**不得**自动批准）─────────────────────────
WRITE_OR_EXEC_CASES: list[tuple[str, str, str]] = [
    ("sed.s.e", r"sed -n 's|^|touch /tmp/pwned; id|e' f", "s 的 e 标志执行替换结果"),
    ("sed.s.e", r"sed -n 's|^|id|ep' f", "s 的组合标志 ep"),
    ("sed.s.e", r"sed -n 's|a|b|e' f", "s 的 e 标志（无打印伪装）"),
    ("sed.s.e", r"sed -n 's|a|b|we /tmp/f; p' f", "s 的 we 组合标志"),
    ("sed.s.w", r"sed -n 's|a|b|w /tmp/evil' f", "s 的 w 标志写文件"),
    ("sed.w", r"sed -n '1W /tmp/evil;p' f", "大写 W 写文件"),
    ("sed.w", r"sed -n 'W /tmp/evil' f", "大写 W（无打印）"),
    ("sed.w", r"sed -n 'w /tmp/evil' f", "裸 w 命令"),
    ("sed.e", r"sed -e '1e id' f", "e 命令执行"),
    ("sed.i", r"sed -i 's/a/b/' f", "就地编辑"),
    ("awk.foreign", r"awk -f /tmp/evil.awk f", "程序来自文件（内容不可控）"),
    ("awk.foreign", r"awk --file /tmp/evil.awk f", "程序来自文件（长选项）"),
    ("awk.foreign", r"awk --file=/tmp/evil.awk f", "程序来自文件（= 粘连）"),
    ("awk.inplace", r"awk -i inplace '{print}' f", "GNU awk 就地编辑（写文件）"),
    ("awk.pipe", "awk 'BEGIN{\"id\"|&getline x}' f", "双向管道 |&（引号内被吞）"),
    ("awk.pipe", "awk 'BEGIN{\"id\"|getline x}' f", "命令喂 getline"),
    ("awk.system", "awk 'BEGIN{system (\"id\")}' f", "system ( 带空格执行"),
    ("awk.write", "awk '{print $1 >> \"/tmp/evil\"}' f", "print 追加重定向"),
    ("rg.pre", "rg --pre=sh pat .", "pre 前处理器执行（= 粘连）"),
    ("rg.pre", "rg --hostname-bin=cmd pat", "外部命令（= 粘连）"),
    ("git.pager", "git grep -O 'sh -c id' pat", "grep -O 启动 pager 执行命令"),
    ("git.pager", "git grep --open-files-in-pager='sh -c id' pat", "pager 长选项"),
    ("git.pager", "git log --textconv", "textconv 执行配置命令"),
    ("git.out", "git log --output=/tmp/f", "写文件 flag"),
    # 其他只读白名单工具的写形态
    ("misc.sort", "sort --compress-program='sh -c id' f", "压缩程序执行"),
    ("misc.sort", "sort -o /tmp/f f", "排序输出到文件"),
    ("misc.yq", "yq -i '.a=1' f.yaml", "yq 就地编辑"),
    ("misc.yq", "yq --inplace '.a=1' f.yaml", "yq 就地编辑（长选项）"),
    ("misc.xml", "xmlstarlet ed --inplace -u //a -v b f.xml", "xml 就地编辑"),
    ("misc.xml", "xmlstarlet ed -L -u //a -v b f.xml", "xml 就地编辑（短选项）"),
]


@pytest.mark.parametrize(
    ("family", "cmd", "why"),
    WRITE_OR_EXEC_CASES,
    ids=[f"{f}-{i}" for i, (f, _, _) in enumerate(WRITE_OR_EXEC_CASES)],
)
def test_write_or_exec_primitives_never_auto_approved(family: str, cmd: str, why: str) -> None:
    """契约①：写/执行原语的组合形态**一律不得**自动批准（同族的系统化钉法）。

    这条覆盖 8 个族。任何一条被判 SAFE，就意味着「会写文件/执行命令的载荷被静默
    自动批准」——这是本契约存在的全部理由。
    """
    result = classify_shell_command(cmd)
    assert not is_safe_to_auto_approve(cmd), (
        f"[{family}] 写/执行形态被判 SAFE（会被自动批准）：{cmd!r}\n  危险：{why}\n"
        f"  分类理由：{result.reason}"
    )


# ── ② 「写/读只差一个 flag」的系统命令（断言：**不得**自动批准）──────────────────
FLAG_GATED_WRITE_CASES: list[tuple[str, str]] = [
    ("mount -o remount,rw /", "重挂载为可写"),
    ("mount /dev/sda1 /mnt", "挂载文件系统"),
    ("sysctl -w kernel.shmmax=1", "写内核参数"),
    ("sysctl --write=kernel.shmmax=1", "写内核参数（= 粘连）"),
    ("sysctl -p /etc/sysctl.conf", "加载配置文件"),
    ("hostnamectl set-hostname evil", "改主机名"),
    ("timedatectl set-time '2020-01-01'", "改系统时间"),
    ("localectl set-keymap us", "改键盘布局"),
    ("route add default gw 1.2.3.4", "加路由"),
    ("route del default", "删路由"),
    ("arp -d 1.2.3.4", "删邻居条目"),
    ("arp -s 1.2.3.4 aa:bb:cc:dd:ee:ff", "加静态条目"),
    ("hostname evil", "设置主机名"),
    ("history -c", "清空历史"),
    ("history -w /tmp/f", "写历史文件"),
    ("ifconfig eth0 up", "启用网卡"),
    ("ifconfig eth0 1.2.3.4", "设置地址"),
    ("ip addr add 1.2.3.4/24 dev eth0", "加地址"),
    ("ip link set eth0 down", "停用网卡"),
]


@pytest.mark.parametrize(
    ("cmd", "why"), FLAG_GATED_WRITE_CASES, ids=[c for c, _ in FLAG_GATED_WRITE_CASES]
)
def test_flag_gated_write_forms_not_auto_approved(cmd: str, why: str) -> None:
    """契约①：这些命令的**写形态**此前被整命令只读白名单自动批准。"""
    result = classify_shell_command(cmd)
    assert not is_safe_to_auto_approve(cmd), (
        f"写形态被判 SAFE（会被自动批准）：{cmd!r}\n  危险：{why}\n  分类理由：{result.reason}"
    )


# ── ③ 壳层：静态看不到真实语义的构造（断言：**不得**自动批准，关键项须硬拒）────────
SHELL_LAYER_CASES: list[tuple[str, str, str]] = [
    # (族, 载荷, 说明)
    ("backtick", "echo `rm -rf /tmp/x`", "反引号命令替换（与 $() 同源）"),
    ("backtick", "cat `rm -rf /tmp/x`", "反引号命令替换（cat）"),
    ("backtick", "echo '`id`'", "单引号内的反引号是字面量——**反向**，见下条用例"),
    ("ansic", r"echo $'\nrm -rf /tmp/x'", "ANSI-C 转义展开出换行 ⇒ 真执行 rm"),
    ("ansic", r"echo $'\x3b rm -rf /tmp/x'", "ANSI-C 十六进制转义 ⇒ 分号"),
]


@pytest.mark.parametrize(
    ("family", "cmd", "why"),
    [c for c in SHELL_LAYER_CASES if c[0] != "backtick" or "单引号" not in c[2]],
    ids=["backtick-rm", "backtick-rm-cat", "ansic-newline", "ansic-hex"],
)
def test_shell_layer_escapes_not_auto_approved(family: str, cmd: str, why: str) -> None:
    """契约①：反引号替换与 ANSI-C 转义此前完全未检查（差分审计抓出）。"""
    result = classify_shell_command(cmd)
    assert not is_safe_to_auto_approve(cmd), (
        f"[{family}] 壳层构造被判 SAFE：{cmd!r}\n  危险：{why}\n  分类理由：{result.reason}"
    )


HARD_DENY_CASES: list[tuple[str, str]] = [
    (r"echo $'\nrm -rf /tmp/x'", "ANSI-C 转义（执行期展开，静态不可信）"),
    ("echo `rm -rf /tmp/x`", "反引号内命令非只读"),
]


@pytest.mark.parametrize(("cmd", "why"), HARD_DENY_CASES, ids=["ansic", "backtick"])
def test_shell_layer_hard_denied(cmd: str, why: str) -> None:
    """契约①（更强）：这两类必须**明确硬拒**（DENIED），而不只是「非 SAFE」。

    为什么要求更强：它们不是「未知命令」（CAUTION 允许人工确认放行），而是**静态语义与
    真实语义不符**的构造——人工确认时看到的也是被引号伪装后的文本，放行即等于放行隐藏
    的命令，故必须 DENIED。
    """
    assert classify_shell_command(cmd).level == ShellSafetyLevel.DENIED, why


DANGEROUS_LEVEL_CASES: list[tuple[str, str]] = [
    (r"sed -n 's|^|touch /tmp/pwned; id|e' f", "sed s 的 e 标志"),
    (r"sed -n 's|a|b|e' f", "sed s 的 e 标志（无伪装）"),
    ("awk -f /tmp/evil.awk f", "awk 程序来自文件"),
    ("awk -i inplace '{print}' f", "awk 就地编辑"),
    ("awk 'BEGIN{\"id\"|&getline x}' f", "awk 双向管道"),
    ("git grep -O 'sh -c id' pat", "git pager 注入"),
]


@pytest.mark.parametrize(
    ("cmd", "why"), DANGEROUS_LEVEL_CASES, ids=[f"c{i}" for i in range(len(DANGEROUS_LEVEL_CASES))]
)
def test_recognised_primitives_are_dangerous(cmd: str, why: str) -> None:
    """契约①（分级）：被**明确识别**的写/执行原语须落在 DANGEROUS/DENIED（不只是非 SAFE）。"""
    level = classify_shell_command(cmd).level
    assert level in (ShellSafetyLevel.DANGEROUS, ShellSafetyLevel.DENIED), (
        f"{why}：{cmd!r} 期望 DANGEROUS/DENIED，实际 {level.value}"
    )


# ── ④ 反向：合法只读形态**不得被误伤**（防过度收紧）─────────────────────────────
READ_ONLY_CASES: list[str] = [
    "ls -la",
    "cat f.txt | grep err | head -5",
    "git status",
    "git log --oneline",
    "git diff HEAD",
    "git branch -a",
    "git tag -l 'v*'",
    "git grep pat",
    "sed -n '/pattern/p' f.txt",
    "sed -n 's/foo/bar/p' f.txt",
    "sed -n 's|foo|bar|p' f.txt",
    "sed -n 'p' f.txt",
    "sed -n '2,5p' f.txt",
    "sed -n '$p' f.txt",
    "sed -n '$!p' f.txt",
    "sed -n '/x/!p' f.txt",
    "sed -n '1,5!p' f.txt",
    "awk '{print $1}' f.txt",
    "awk -v x=1 '{print $1}' f.txt",
    "find . -name '*.py'",
    "fd .md",
    "rg pat .",
    "sort f.txt",
    "sysctl kernel.shmmax",
    "sysctl -a",
    "mount",
    "arp",
    "arp -n",
    "ifconfig -a",
    "hostname -f",
    "route -n",
    "history 20",
    "hostnamectl status",
    "timedatectl status",
    "localectl status",
    "localectl list-keymaps",
    "yq '.a' f.yaml",
    "xmlstarlet sel -t -v //a f.xml",
    "echo `date`",
    "echo '`date`'",
    r"echo $'plain text'",
    "cat f.txt > /dev/null",
]


@pytest.mark.parametrize(
    "cmd", READ_ONLY_CASES, ids=[f"ro{i}" for i in range(len(READ_ONLY_CASES))]
)
def test_read_only_forms_still_auto_approved(cmd: str) -> None:
    """契约②（**反向，同等重要**）：合法只读形态必须仍能自动批准。

    只有正向断言（危险形态被拒）是不够的——把整个白名单收紧到「什么都不自动批准」
    也能让正向全绿，却会让产品退化成「每一步都要人确认」。本用例是该退化的破坏者。
    """
    result = classify_shell_command(cmd)
    assert is_safe_to_auto_approve(cmd), (
        f"只读形态被误伤（不再自动批准）：{cmd!r} → {result.level.value}（{result.reason}）"
    )


def test_backtick_inside_single_quotes_is_literal() -> None:
    """契约②（破坏者）：单引号内的反引号是**字面量**（shell 不求值），不得拒。

    反引号检查若不区分引号状态，会把 `echo '`date`'` 这类字面量文本误判为命令替换
    ——这是「过度收紧」最典型的一种。
    """
    assert is_safe_to_auto_approve("echo '`date`'")
    assert is_safe_to_auto_approve("grep '`' f.txt")


def test_backtick_inside_double_quotes_still_rejected() -> None:
    """契约①（破坏者）：**双引号不阻止命令替换** ⇒ 双引号内的反引号仍须拒。

    若实现把「在引号内」一律当字面量（忽略单/双引号语义差异），这条会红。
    """
    assert not is_safe_to_auto_approve('echo "`rm -rf /tmp/x`"')


def test_ansi_c_without_escape_still_allowed() -> None:
    """契约②（破坏者）：`$'...'` 内**无**反斜杠转义时不得误伤。"""
    assert is_safe_to_auto_approve(r"echo $'hello'")


# ── ⑤ 族级完整性：只读白名单里不得有「写/读只差一个 flag」的命令 ──────────────
def test_generic_read_only_excludes_flag_gated_commands() -> None:
    """契约③（族级）：这些命令必须**不**在 `_GENERIC_READ_ONLY`（走 gated 判定）。

    这是「防止将来新增 gated 命令时忘记从白名单移除」的守门用例：两表若有交集
    说明某命令既被当「整命令只读」（自动批准全部形态）又声称按首参数收窄 ⇒ 红。
    """
    from bandi.company.shell_safety import _GENERIC_READ_ONLY, _READ_ONLY_FIRST_ARGS

    overlap = set(_GENERIC_READ_ONLY) & set(_READ_ONLY_FIRST_ARGS)
    assert not overlap, f"命令同时出现在两表（白名单会绕过 gated 收窄）：{sorted(overlap)}"
    for name in ("ifconfig", "ip", "ip addr", "ip link"):
        assert name not in _GENERIC_READ_ONLY, f"{name} 有写形态，不得整命令进只读白名单"


def test_every_gated_command_has_a_rejected_write_form() -> None:
    """契约③（族级）：`_READ_ONLY_FIRST_ARGS` 的**每个**命令都要有一条写形态被拒的用例。

    防「新增 gated 命令但没补载荷」——把 gated 表与写形态用例表联动起来。
    """
    from bandi.company.shell_safety import _READ_ONLY_FIRST_ARGS

    covered = {cmd.split()[0] for cmd, _ in FLAG_GATED_WRITE_CASES}
    gated_without_case = set(_READ_ONLY_FIRST_ARGS) - covered
    assert not gated_without_case, (
        f"这些 gated 命令没有对应的「写形态被拒」用例（请补 FLAG_GATED_WRITE_CASES）："
        f"{sorted(gated_without_case)}"
    )

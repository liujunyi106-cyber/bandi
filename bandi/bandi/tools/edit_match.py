# 功能：Edit 回退匹配链（opencode 10 级链 + cc-haha 引号归一合体，纯函数无 IO）
# （findActualString/preserveQuoteStyle）+ FileEditTool.ts:275-311（外部修改由工具层消费）
from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

LEFT_SINGLE_CURLY_QUOTE = "\u2018"
RIGHT_SINGLE_CURLY_QUOTE = "\u2019"
LEFT_DOUBLE_CURLY_QUOTE = "\u201c"
RIGHT_DOUBLE_CURLY_QUOTE = "\u201d"

_SINGLE_CANDIDATE_SIMILARITY_THRESHOLD = 0.65
_MULTIPLE_CANDIDATES_SIMILARITY_THRESHOLD = 0.65


class OldStringNotFoundError(ValueError):
    """功能：old_string 全部回退级别均未命中。"""


class MultipleMatchesError(ValueError):
    """功能：old_string 匹配多处且未开启 replace_all。"""


class DisproportionateMatchError(ValueError):
    """功能：回退命中跨度远大于 old_string（疑似失配），拒绝改写。"""


@dataclass(frozen=True)
class EditMatch:
    """功能：一次替换结果（text=替换后全文；tier=命中级别；count=次数；actual_old=原文片段）。"""

    text: str
    tier: str
    count: int
    actual_old: str


def normalize_quotes(text: str) -> str:
    # 功能：弯引号 → 直引号（cc-haha normalizeQuotes 移植）
    return (
        text.replace(LEFT_SINGLE_CURLY_QUOTE, "'")
        .replace(RIGHT_SINGLE_CURLY_QUOTE, "'")
        .replace(LEFT_DOUBLE_CURLY_QUOTE, '"')
        .replace(RIGHT_DOUBLE_CURLY_QUOTE, '"')
    )


def normalize_line_endings(text: str) -> str:
    # 功能：CRLF → LF（模型输入统一为 LF 后按文件行尾转换）
    return text.replace("\r\n", "\n")


def detect_line_ending(text: str) -> str:
    # 功能：检测文件行尾（含 CRLF 即视为 CRLF 文件）
    return "\r\n" if "\r\n" in text else "\n"


def convert_line_ending(text: str, ending: str) -> str:
    # 功能：将 LF 文本转换为目标行尾
    if ending == "\n":
        return text
    return text.replace("\n", "\r\n")


def simple_replacer(content: str, find: str) -> Iterator[str]:
    # 功能：第 1 级——精确原文
    yield find


def quote_normalized_replacer(content: str, find: str) -> Iterator[str]:
    # 功能：第 2 级——引号归一（弯/直互认，命中回切原文切片，保排版风格）
    normalized_find = normalize_quotes(find)
    normalized_content = normalize_quotes(content)
    if normalized_find == find and normalized_content == content:
        return
    start = 0
    while True:
        index = normalized_content.find(normalized_find, start)
        if index == -1:
            return
        yield content[index : index + len(find)]
        start = index + 1


def line_trimmed_replacer(content: str, find: str) -> Iterator[str]:
    # 功能：第 3 级——逐行首尾空白容错（命中块取原文）
    original_lines = content.split("\n")
    search_lines = find.split("\n")
    if search_lines and search_lines[-1] == "":
        search_lines.pop()
    if not search_lines:
        return
    for i in range(len(original_lines) - len(search_lines) + 1):
        if all(
            original_lines[i + j].strip() == search_lines[j].strip()
            for j in range(len(search_lines))
        ):
            yield "\n".join(original_lines[i : i + len(search_lines)])


def levenshtein_distance(a: str, b: str) -> int:
    # 功能：Levenshtein 编辑距离（滚动数组）
    if a == "" or b == "":
        return max(len(a), len(b))
    previous = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        current = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
        previous = current
    return previous[len(b)]


def _block_average_similarity(
    original_lines: list[str], search_lines: list[str], start: int, end: int
) -> float:
    # 功能：块锚多候选平均相似度（中间行 Levenshtein）
    actual_block_size = end - start + 1
    search_block_size = len(search_lines)
    lines_to_check = min(search_block_size - 2, actual_block_size - 2)
    if lines_to_check <= 0:
        return 1.0
    similarity = 0.0
    for j in range(1, min(search_block_size - 1, actual_block_size - 1)):
        original_line = original_lines[start + j].strip()
        search_line = search_lines[j].strip()
        max_len = max(len(original_line), len(search_line))
        if max_len == 0:
            continue
        similarity += 1 - levenshtein_distance(original_line, search_line) / max_len
    return similarity / lines_to_check


def block_anchor_replacer(content: str, find: str) -> Iterator[str]:
    # 功能：第 4 级——首尾行锚点 + 中间行相似度 ≥0.65（块行数偏差 ≤25%）
    original_lines = content.split("\n")
    search_lines = find.split("\n")
    if len(search_lines) < 3:
        return
    if search_lines[-1] == "":
        search_lines.pop()
    first_line_search = search_lines[0].strip()
    last_line_search = search_lines[-1].strip()
    search_block_size = len(search_lines)
    max_line_delta = max(1, int(search_block_size * 0.25))

    candidates: list[tuple[int, int]] = []
    for i in range(len(original_lines)):
        if original_lines[i].strip() != first_line_search:
            continue
        for j in range(i + 2, len(original_lines)):
            if original_lines[j].strip() == last_line_search:
                actual_block_size = j - i + 1
                if abs(actual_block_size - search_block_size) <= max_line_delta:
                    candidates.append((i, j))
                break
    if not candidates:
        return

    if len(candidates) == 1:
        start, end = candidates[0]
        actual_block_size = end - start + 1
        similarity = 0.0
        lines_to_check = min(search_block_size - 2, actual_block_size - 2)
        if lines_to_check > 0:
            for j in range(1, min(search_block_size - 1, actual_block_size - 1)):
                original_line = original_lines[start + j].strip()
                search_line = search_lines[j].strip()
                max_len = max(len(original_line), len(search_line))
                if max_len == 0:
                    continue
                similarity += (1 - levenshtein_distance(original_line, search_line) / max_len) / (
                    lines_to_check
                )
                if similarity >= _SINGLE_CANDIDATE_SIMILARITY_THRESHOLD:
                    break
        else:
            similarity = 1.0
        if similarity >= _SINGLE_CANDIDATE_SIMILARITY_THRESHOLD:
            yield "\n".join(original_lines[start : end + 1])
        return

    best: tuple[int, int] | None = None
    max_similarity = -1.0
    for start, end in candidates:
        similarity = _block_average_similarity(original_lines, search_lines, start, end)
        if similarity > max_similarity:
            max_similarity = similarity
            best = (start, end)
    if max_similarity >= _MULTIPLE_CANDIDATES_SIMILARITY_THRESHOLD and best is not None:
        start, end = best
        yield "\n".join(original_lines[start : end + 1])


_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def whitespace_normalized_replacer(content: str, find: str) -> Iterator[str]:
    # 功能：第 5 级——空白压平（tab/多空格/单空格等效）
    normalized_find = _normalize_whitespace(find)
    lines = content.split("\n")
    for line in lines:
        normalized_line = _normalize_whitespace(line)
        if normalized_line == normalized_find:
            yield line
        elif normalized_find and normalized_find in normalized_line:
            words = find.strip().split()
            if words:
                pattern = r"\s+".join(re.escape(word) for word in words)
                try:
                    match = re.search(pattern, line)
                except re.error:
                    match = None
                if match:
                    yield match.group(0)
    find_lines = find.split("\n")
    if len(find_lines) > 1:
        for i in range(len(lines) - len(find_lines) + 1):
            block = "\n".join(lines[i : i + len(find_lines)])
            if _normalize_whitespace(block) == normalized_find:
                yield block


def _remove_indentation(text: str) -> str:
    # 功能：去除块内公共最小缩进（空行原样保留）
    lines = text.split("\n")
    non_empty = [line for line in lines if line.strip()]
    if not non_empty:
        return text
    min_indent = min(len(line) - len(line.lstrip()) for line in non_empty)
    return "\n".join(line if not line.strip() else line[min_indent:] for line in lines)


def indentation_flexible_replacer(content: str, find: str) -> Iterator[str]:
    # 功能：第 6 级——公共缩进差异容错
    normalized_find = _remove_indentation(find)
    content_lines = content.split("\n")
    find_lines = find.split("\n")
    for i in range(len(content_lines) - len(find_lines) + 1):
        block = "\n".join(content_lines[i : i + len(find_lines)])
        if _remove_indentation(block) == normalized_find:
            yield block


_ESCAPE_PATTERN = re.compile(r"\\(n|t|r|'|\"|`|\\|\n|\$)")
_ESCAPE_MAP = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "'": "'",
    '"': '"',
    "`": "`",
    "\\": "\\",
    "\n": "\n",
    "$": "$",
}


def _unescape_string(text: str) -> str:
    return _ESCAPE_PATTERN.sub(lambda m: _ESCAPE_MAP[m.group(1)], text)


def escape_normalized_replacer(content: str, find: str) -> Iterator[str]:
    # 功能：第 7 级——转义写法差异（\n 字面量 vs 真实换行等）
    unescaped_find = _unescape_string(find)
    if unescaped_find in content:
        yield unescaped_find
    lines = content.split("\n")
    find_lines = unescaped_find.split("\n")
    for i in range(len(lines) - len(find_lines) + 1):
        block = "\n".join(lines[i : i + len(find_lines)])
        if _unescape_string(block) == unescaped_find:
            yield block


def trimmed_boundary_replacer(content: str, find: str) -> Iterator[str]:
    # 功能：第 8 级——首尾空白/空行差异
    trimmed_find = find.strip()
    if trimmed_find == find:
        return
    if trimmed_find in content:
        yield trimmed_find
    lines = content.split("\n")
    find_lines = find.split("\n")
    for i in range(len(lines) - len(find_lines) + 1):
        block = "\n".join(lines[i : i + len(find_lines)])
        if block.strip() == trimmed_find:
            yield block


def context_aware_replacer(content: str, find: str) -> Iterator[str]:
    # 功能：第 9 级——上下文锚点（≥3 行，首尾行锚 + 中间行 trim 相等率 ≥50%）
    find_lines = find.split("\n")
    if len(find_lines) < 3:
        return
    if find_lines[-1] == "":
        find_lines.pop()
    content_lines = content.split("\n")
    first_line = find_lines[0].strip()
    last_line = find_lines[-1].strip()
    for i in range(len(content_lines)):
        if content_lines[i].strip() != first_line:
            continue
        for j in range(i + 2, len(content_lines)):
            if content_lines[j].strip() != last_line:
                continue
            block_lines = content_lines[i : j + 1]
            if len(block_lines) == len(find_lines):
                matching = 0
                total = 0
                for k in range(1, len(block_lines) - 1):
                    block_line = block_lines[k].strip()
                    find_line = find_lines[k].strip()
                    if block_line or find_line:
                        total += 1
                        if block_line == find_line:
                            matching += 1
                if total == 0 or matching / total >= 0.5:
                    yield "\n".join(block_lines)
            break


def multi_occurrence_replacer(content: str, find: str) -> Iterator[str]:
    # 功能：第 10 级——枚举全部精确出现（配合 replace_all）
    start = 0
    while True:
        index = content.find(find, start)
        if index == -1:
            return
        yield find
        start = index + len(find)


def is_disproportionate_match(search: str, old: str) -> bool:
    # 功能：命中块远大于 old（行数 ≥max(old+3, old*2) 或多行且字符 >max(old+500, old*4)）
    old_lines = old.count("\n") + 1
    search_lines = search.count("\n") + 1
    if search_lines >= max(old_lines + 3, old_lines * 2):
        return True
    if old_lines == 1:
        return False
    return len(search.strip()) > max(len(old.strip()) + 500, len(old.strip()) * 4)


def _is_opening_context(chars: list[str], index: int) -> bool:
    if index == 0:
        return True
    return chars[index - 1] in (" ", "\t", "\n", "\r", "(", "[", "{", "\u2014", "\u2013")


def _apply_curly_double(text: str) -> str:
    # 功能：直双引号按交替配对套弯引号（中文前导启发式不适用场景的配对修正）
    result: list[str] = []
    opened = False
    for char in text:
        if char == '"':
            result.append(LEFT_DOUBLE_CURLY_QUOTE if not opened else RIGHT_DOUBLE_CURLY_QUOTE)
            opened = not opened
        else:
            result.append(char)
    return "".join(result)


def _apply_curly_single(text: str) -> str:
    # 功能：直单引号套弯引号（缩写撇号保留右弯 + cc-haha 开闭启发式）
    chars = list(text)
    result: list[str] = []
    for i, char in enumerate(chars):
        if char != "'":
            result.append(char)
            continue
        prev = chars[i - 1] if i > 0 else None
        next_char = chars[i + 1] if i < len(chars) - 1 else None
        prev_is_letter = prev is not None and prev.isalpha()
        next_is_letter = next_char is not None and next_char.isalpha()
        if prev_is_letter and next_is_letter:
            result.append(RIGHT_SINGLE_CURLY_QUOTE)
        else:
            result.append(
                LEFT_SINGLE_CURLY_QUOTE
                if _is_opening_context(chars, i)
                else RIGHT_SINGLE_CURLY_QUOTE
            )
    return "".join(result)


def preserve_quote_style(old: str, actual_old: str, new: str) -> str:
    # 功能：命中经引号归一时把 new 套上文件的弯引号风格（cc-haha preserveQuoteStyle）
    if old == actual_old:
        return new
    has_double = LEFT_DOUBLE_CURLY_QUOTE in actual_old or RIGHT_DOUBLE_CURLY_QUOTE in actual_old
    has_single = LEFT_SINGLE_CURLY_QUOTE in actual_old or RIGHT_SINGLE_CURLY_QUOTE in actual_old
    if not has_double and not has_single:
        return new
    result = new
    if has_double:
        result = _apply_curly_double(result)
    if has_single:
        result = _apply_curly_single(result)
    return result


_REPLACER_CHAIN: tuple[tuple[str, Callable[[str, str], Iterator[str]]], ...] = (
    ("simple", simple_replacer),
    ("quote", quote_normalized_replacer),
    ("line_trimmed", line_trimmed_replacer),
    ("block_anchor", block_anchor_replacer),
    ("whitespace", whitespace_normalized_replacer),
    ("indentation", indentation_flexible_replacer),
    ("escape", escape_normalized_replacer),
    ("trimmed_boundary", trimmed_boundary_replacer),
    ("context_aware", context_aware_replacer),
    ("multi_occurrence", multi_occurrence_replacer),
)


def replace_with_fallback(content: str, old: str, new: str, replace_all: bool = False) -> EditMatch:
    """功能：10 级回退匹配替换——命中后唯一性检查 + 不成比例拒绝；
    全部候选耗尽未命中报 OldStringNotFoundError，命中但均多处报 MultipleMatchesError。"""
    if old == new:
        raise ValueError("No changes to apply: old_string and new_string are identical.")
    if old == "":
        raise ValueError(
            "old_string cannot be empty when editing an existing file. "
            "Provide the exact text to replace, or use write for a full-file replacement."
        )
    not_found = True
    for tier, replacer in _REPLACER_CHAIN:
        for search in replacer(content, old):
            index = content.find(search)
            if index == -1:
                continue
            not_found = False
            if is_disproportionate_match(search, old):
                raise DisproportionateMatchError(
                    "Refusing replacement because the matched span is much larger than "
                    "old_string. Re-read the file and provide the full exact old_string "
                    "for the intended replacement."
                )
            replacement = new
            if tier == "quote" and search != old:
                replacement = preserve_quote_style(old, search, new)
            if replace_all:
                return EditMatch(
                    text=content.replace(search, replacement),
                    tier=tier,
                    count=content.count(search),
                    actual_old=search,
                )
            if index != content.rfind(search):
                continue
            return EditMatch(
                text=content[:index] + replacement + content[index + len(search) :],
                tier=tier,
                count=1,
                actual_old=search,
            )
    if not_found:
        raise OldStringNotFoundError(
            "old_string not found in file. It must match exactly, including whitespace, "
            "indentation, and line endings; automatic fallback matching also failed."
        )
    raise MultipleMatchesError(
        "Found multiple matches for old_string. Provide more surrounding context to make "
        "the match unique, or set replace_all=true."
    )


def probe_match(content: str, target: str) -> str | None:
    """功能：**只探测能否命中**（不替换）——返回命中的回退级别名，未命中返回 None。

    为什么需要它：`apply_patch` 的 hunk 匹配是「在 hunk 头标明的
    窗口内找位置」——位置由窗口决定，**不需要**全局唯一性（全局唯一性判断会把它
    变成另一套语义，可能拒掉此前能应用的补丁）。故这里只回答「该块按回退链能否匹配」
    由调用方按自己的定位策略决定用哪一处。

    与 `replace_with_fallback` 的差异：不检查唯一性、不检查不成比例、不产生结果文本
    （因此也没有 `MultipleMatchesError` / `DisproportionateMatchError`）。
    """
    if not target:
        return None
    for tier, replacer in _REPLACER_CHAIN:
        for search in replacer(content, target):
            if search and search in content:
                return tier
    return None

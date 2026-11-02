# 功能：derive 表达式 ast 白名单求值器——仅解释有限语法，永不 eval
# 安全模型：列引用(裸标识符) + 字面量 + 算术/比较/逻辑 + 白名单函数调用；
# 属性访问/下标/推导式/lambda/海象/import 等可逃逸构造一律拒绝（fail-closed）。
# 向量语义：列名为 pandas Series，BinOp/Call 直接作用于 Series（pandas 广播）。
from __future__ import annotations

import ast
import operator
from typing import Any

# 白名单：BinOp 算术、UnaryOp、BoolOp、Compare 比较器
_ALLOWED_BIN_OPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a**b,
}
_ALLOWED_UNARY_OPS = {
    ast.UAdd: lambda a: +a,
    ast.USub: lambda a: -a,
    ast.Not: lambda a: not a,
}
_ALLOWED_CMP_OPS = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
}
_ALLOWED_BOOL_OPS = {ast.And, ast.Or}


def _is_series_like(v: Any) -> bool:
    """duck-typing 判定向量（pandas Series/ndarray 等具逐元素语义对象）
    不 import pandas 保持零强依赖。"""
    return hasattr(v, "iloc") and hasattr(v, "any") and hasattr(v, "all")


class ExpressionSyntaxError(ValueError):
    """derive 表达式含白名单外语法（属性/下标/推导等逃逸面）——fail-closed 拒绝。"""


def _unsupported(node: ast.AST) -> ExpressionSyntaxError:
    kind = type(node).__name__
    return ExpressionSyntaxError(
        f"expression 不支持 {kind} 语法（仅允许列引用/字面量/算术/比较/逻辑/白名单函数）"
    )


def evaluate_expression(
    expression: str,
    env: dict[str, Any],
    safe_funcs: dict[str, Any],
) -> Any:
    """功能：白名单表达式求值——env 提供列/变量（可含 pandas Series）
    safe_funcs 为可调函数白名单。白名单外语法抛 ExpressionSyntaxError。"""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ExpressionSyntaxError(f"expression 语法错误: {exc}") from exc
    return _eval_node(tree.body, env, safe_funcs)


def _eval_node(node: ast.AST, env: dict[str, Any], funcs: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        raise ValueError(f"expression 引用未知列/变量: {node.id!r}")
    if isinstance(node, ast.BinOp):
        op = _ALLOWED_BIN_OPS.get(type(node.op))
        if op is None:
            raise _unsupported(node.op)
        return op(_eval_node(node.left, env, funcs), _eval_node(node.right, env, funcs))
    if isinstance(node, ast.UnaryOp):
        op = _ALLOWED_UNARY_OPS.get(type(node.op))
        if op is None:
            raise _unsupported(node.op)
        return op(_eval_node(node.operand, env, funcs))
    if isinstance(node, ast.BoolOp):
        if type(node.op) not in _ALLOWED_BOOL_OPS:
            raise _unsupported(node.op)
        values = [_eval_node(v, env, funcs) for v in node.values]
        if any(_is_series_like(v) for v in values):
            # 向量语义：逐元素 and/or（all/any 对 Series 触发 pandas 真值歧义——
            combine = operator.and_ if isinstance(node.op, ast.And) else operator.or_
            result = values[0]
            for v in values[1:]:
                result = combine(result, v)
            return result
        # 纯标量：Python 短路语义
        result = values[0]
        for v in values[1:]:
            if isinstance(node.op, ast.And):
                if not result:
                    return result
                result = result and v
            else:
                if result:
                    return result
                result = result or v
        return result
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, env, funcs)
        for op_node, comparator in zip(node.ops, node.comparators, strict=True):
            right = _eval_node(comparator, env, funcs)
            op = _ALLOWED_CMP_OPS.get(type(op_node))
            if op is None:
                raise _unsupported(op_node)
            left = op(left, right)
        return left
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ExpressionSyntaxError("expression 不支持方法调用（仅白名单函数）")
        fn = funcs.get(node.func.id)
        if fn is None:
            raise ValueError(f"expression 调用了白名单外函数: {node.func.id}")
        args = [_eval_node(a, env, funcs) for a in node.args]
        kwargs: dict[str, Any] = {}
        for kw in node.keywords:
            if kw.arg is None:
                raise ExpressionSyntaxError("expression 不支持 **kwargs")
            kwargs[kw.arg] = _eval_node(kw.value, env, funcs)
        return fn(*args, **kwargs)
    if isinstance(node, (ast.Tuple, ast.List)):
        return [_eval_node(e, env, funcs) for e in node.elts]
    if isinstance(node, ast.NamedExpr):
        raise ExpressionSyntaxError("expression 不支持海象 := 赋值")
    # 其余一律 fail-closed：Attribute/Subscript/推导式/Lambda/IfExp/Dict/Set/f-string 等
    raise _unsupported(node)

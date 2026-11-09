# 功能：rule_matcher — 规则匹配/评分/过滤/校验
from __future__ import annotations

import operator
import re
from collections.abc import Callable
from typing import Any

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext

OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "lt": operator.lt,
    "le": operator.le,
    "gt": operator.gt,
    "ge": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
    "contains": lambda a, b: b in a,
    "not_contains": lambda a, b: b not in a,
    "startswith": lambda a, b: a.startswith(b),
    "endswith": lambda a, b: a.endswith(b),
    "matches": lambda a, b: bool(re.search(b, str(a))),
    "is_empty": lambda a, _: a in (None, "", [], {}),
    "is_not_empty": lambda a, _: a not in (None, "", [], {}),
    "between": lambda a, b: b[0] <= a <= b[1],
    "exists": lambda a, _: a is not None,
    "not_exists": lambda a, _: a is None,
}


class RuleMatcherTool(BaseTool):
    """功能：规则求值/汇总/评分/校验/过滤（6 个 op）。"""

    name = "rule_matcher"
    description = "规则匹配与评估：evaluate/match_all/match_any/score/validate/filter"
    category = ToolCategory.UTIL
    feature_group = "util"
    is_read_only = True
    is_concurrency_safe = True
    capability_tags: list[str] = []
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["evaluate", "match_all", "match_any", "score", "validate", "filter"],
                "description": "要执行的操作",
            },
            "data": {"type": "object", "description": "待匹配数据"},
            "data_list": {"type": "array", "description": "filter 的数据列表"},
            "rules": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "field": {"type": "string"},
                        "operator": {"type": "string", "enum": sorted(OPERATORS)},
                        "value": {},
                        "weight": {"type": "number"},
                        "required": {"type": "boolean"},
                    },
                },
                "description": "规则列表",
            },
            "rule_set": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "logic": {"type": "string", "enum": ["and", "or"]},
                    "rules": {"type": "array"},
                    "groups": {"type": "array"},
                },
                "description": "规则组",
            },
        },
        "required": ["operation"],
    }

    def _get_field_value(self, data: dict[str, Any], field: str) -> Any:
        if not field:
            return None
        parts = field.split(".")
        value: Any = data
        for part in parts:
            if isinstance(value, dict) and part in value:
                value = value[part]
            elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
                value = value[int(part)]
            else:
                return None
        return value

    def _evaluate_condition(self, value: Any, op: str, compare_value: Any) -> bool:
        if op not in OPERATORS:
            raise ValueError(f"Unknown operator: {op}")
        try:
            return OPERATORS[op](value, compare_value)
        except (TypeError, ValueError):
            return False

    def _evaluate_single_rule(self, data: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any]:
        field = rule.get("field", "")
        op = rule.get("operator", "eq")
        compare_value = rule.get("value")
        actual_value = self._get_field_value(data, field)
        matched = self._evaluate_condition(actual_value, op, compare_value)
        result: dict[str, Any] = {
            "rule_id": rule.get("id"),
            "field": field,
            "operator": op,
            "expected": compare_value,
            "actual": actual_value,
            "matched": matched,
        }
        if "message" in rule:
            result["message"] = rule["message"]
        if "weight" in rule:
            result["weight"] = rule["weight"]
        return result

    def _evaluate_rules(self, params: dict[str, Any]) -> dict[str, Any]:
        data = params.get("data", {})
        rules = params.get("rules", [])
        rule_set = params.get("rule_set")
        if not rules and rule_set:
            rules = [rule for rule in rule_set.get("rules", [])]
            for group in rule_set.get("groups", []):
                rules.extend(group.get("rules", []))
            return self._evaluate_rule_set(data, rule_set)
        stop_on_first_match = params.get("stop_on_first_match", False)
        stop_on_first_failure = params.get("stop_on_first_failure", False)
        include_unmatched = params.get("include_unmatched", True)
        results = []
        matched_count = 0
        failed_required = False
        for rule in rules:
            evaluation = self._evaluate_single_rule(data, rule)
            if evaluation["matched"]:
                matched_count += 1
                if stop_on_first_match:
                    results.append(evaluation)
                    break
            else:
                if rule.get("required", False):
                    failed_required = True
                if stop_on_first_failure:
                    results.append(evaluation)
                    break
            if include_unmatched or evaluation["matched"]:
                results.append(evaluation)
        return {
            "data": data,
            "results": results,
            "summary": {
                "total_rules": len(rules),
                "matched": matched_count,
                "unmatched": len(rules) - matched_count,
                "all_matched": matched_count == len(rules),
                "any_matched": matched_count > 0,
                "failed_required": failed_required,
            },
        }

    def _evaluate_rule_set(self, data: dict[str, Any], rule_set: dict[str, Any]) -> dict[str, Any]:
        name = rule_set.get("name", "unnamed")
        logic = rule_set.get("logic", "and")
        rules = rule_set.get("rules", [])
        groups = rule_set.get("groups", [])
        results = []
        for rule in rules:
            results.append(self._evaluate_single_rule(data, rule))
        group_results = []
        for group in groups:
            group_logic = group.get("logic", "and")
            group_rules = group.get("rules", [])
            group_evals = [self._evaluate_single_rule(data, r) for r in group_rules]
            if group_logic == "and":
                group_matched = all(e["matched"] for e in group_evals)
            else:
                group_matched = any(e["matched"] for e in group_evals)
            group_results.append(
                {
                    "logic": group_logic,
                    "matched": group_matched,
                    "evaluations": group_evals,
                }
            )
        all_rule_matches = [e["matched"] for e in results]
        group_matches = [g["matched"] for g in group_results]
        if logic == "and":
            matched = all(all_rule_matches) and all(group_matches)
        else:
            matched = any(all_rule_matches) or any(group_matches)
        return {
            "data": data,
            "results": results,
            "group_results": group_results,
            "matched": matched,
            "rule_set": name,
            "summary": {
                "total_rules": len(rules),
                "matched": sum(1 for m in all_rule_matches if m),
                "unmatched": len(rules) - sum(1 for m in all_rule_matches if m),
                "all_matched": matched,
                "any_matched": any(all_rule_matches) or any(group_matches),
            },
        }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        op = params.get("operation", "evaluate")
        if op == "evaluate":
            if params.get("rule_set"):
                return self._evaluate_rule_set(params.get("data", {}), params["rule_set"])
            return self._evaluate_rules(params)
        if op == "match_all":
            result = self._evaluate_rules(params)
            result["all_matched"] = result["summary"]["all_matched"]
            return result
        if op == "match_any":
            result = self._evaluate_rules(params)
            result["any_matched"] = result["summary"]["any_matched"]
            return result
        if op == "score":
            return self._score_rules(params)
        if op == "validate":
            return self._validate_rules(params)
        if op == "filter":
            return self._filter_data(params)
        raise ValueError(f"Unknown operation: {op}")

    def _score_rules(self, params: dict[str, Any]) -> dict[str, Any]:
        data = params.get("data", {})
        rules = params.get("rules", [])
        if not rules:
            raise ValueError("Rules are required for scoring")
        total_weight = 0.0
        earned_weight = 0.0
        results = []
        for rule in rules:
            evaluation = self._evaluate_single_rule(data, rule)
            weight = rule.get("weight", 1.0)
            total_weight += weight
            if evaluation["matched"]:
                earned_weight += weight
            results.append(evaluation)
        score = (earned_weight / total_weight * 100) if total_weight > 0 else 0
        return {
            "data": data,
            "results": results,
            "scoring": {
                "score": round(score, 2),
                "earned_weight": earned_weight,
                "total_weight": total_weight,
                "percentage": round(score, 2),
            },
        }

    def _validate_rules(self, params: dict[str, Any]) -> dict[str, Any]:
        rules = params.get("rules", [])
        rule_set = params.get("rule_set")
        validation_results = []
        valid_count = 0
        rules_to_validate = list(rules)
        if rule_set:
            rules_to_validate = list(rule_set.get("rules", []))
            for group in rule_set.get("groups", []):
                rules_to_validate.extend(group.get("rules", []))
        for i, rule in enumerate(rules_to_validate):
            errors = []
            if "field" not in rule:
                errors.append("Missing required field: 'field'")
            if "operator" not in rule:
                errors.append("Missing required field: 'operator'")
            elif rule["operator"] not in OPERATORS:
                errors.append(f"Invalid operator: {rule['operator']}")
            is_valid = len(errors) == 0
            if is_valid:
                valid_count += 1
            validation_results.append(
                {
                    "index": i,
                    "rule_id": rule.get("id"),
                    "valid": is_valid,
                    "errors": errors,
                }
            )
        return {
            "validation": validation_results,
            "valid_count": valid_count,
            "summary": {
                "total": len(rules_to_validate),
                "valid": valid_count,
                "invalid": len(rules_to_validate) - valid_count,
            },
        }

    def _filter_data(self, params: dict[str, Any]) -> dict[str, Any]:
        data_list = params.get("data_list", [])
        rules = params.get("rules", [])
        rule_set = params.get("rule_set")
        if not data_list:
            return {
                "matched": [],
                "unmatched": [],
                "summary": {"total": 0, "matched": 0, "unmatched": 0},
            }
        matched = []
        unmatched = []
        for item in data_list:
            if rule_set:
                ok = self._evaluate_rule_set(item, rule_set)["matched"]
            else:
                ok = self._evaluate_rules({"data": item, "rules": rules})
                ok = ok["summary"]["all_matched"]
            (matched if ok else unmatched).append(item)
        return {
            "matched": matched,
            "unmatched": unmatched,
            "summary": {
                "total": len(data_list),
                "matched": len(matched),
                "unmatched": len(unmatched),
            },
        }

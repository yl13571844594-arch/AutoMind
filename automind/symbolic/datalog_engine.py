"""轻量 Datalog 引擎 — 事实、规则、查询，不依赖重量级外部推理机。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Fact:
    """Datalog 事实 — 例如 parent(alice, bob)。"""

    predicate: str
    args: tuple[Any, ...]

    def __str__(self) -> str:
        args_str = ", ".join(str(a) for a in self.args)
        return f"{self.predicate}({args_str})"


@dataclass
class Rule:
    """Datalog 规则 — 例如 ancestor(X, Y) :- parent(X, Y)。
                                        ancestor(X, Y) :- parent(X, Z), ancestor(Z, Y)。

    限于合取查询 (无否定、无析取、无递归)。
    """

    head: Fact
    body: list[Fact]  # 合取 (AND)
    variables: set[str] = field(default_factory=set)

    def __str__(self) -> str:
        body_str = ", ".join(str(f) for f in self.body)
        return f"{self.head} :- {body_str}"


class DatalogEngine:
    """轻量级 Datalog 推理引擎。

    支持:
        - 事实断言 (assert_fact)
        - 规则定义 (add_rule)
        - 查询 (query)
        - 基本演绎推理 (derive)

    限制:
        - 仅支持合取查询
        - 无递归优化
        - 无否定 (可扩展)

    使用示例::

        engine = DatalogEngine()
        engine.assert_fact("parent", "alice", "bob")
        engine.assert_fact("parent", "bob", "carol")
        engine.add_rule("ancestor", ["X", "Y"], [
            ("parent", ["X", "Y"]),
        ])
        results = engine.query("ancestor", "X", "Y")
    """

    def __init__(self) -> None:
        self.facts: list[Fact] = []
        self.rules: list[Rule] = []
        self._fact_index: dict[str, list[Fact]] = {}  # predicate → facts

    # ── 事实管理 ──────────────────────────────────────────

    def assert_fact(self, predicate: str, *args: Any) -> Fact:
        """断言一个事实。"""
        fact = Fact(predicate=predicate, args=args)
        self.facts.append(fact)
        self._fact_index.setdefault(predicate, []).append(fact)
        return fact

    def retract_fact(self, predicate: str, *args: Any) -> bool:
        """撤回一个事实。"""
        target = Fact(predicate=predicate, args=args)
        for i, f in enumerate(self.facts):
            if f.predicate == target.predicate and f.args == target.args:
                self.facts.pop(i)
                self._fact_index[predicate].remove(f)
                return True
        return False

    def list_facts(self, predicate: str | None = None) -> list[Fact]:
        """列出事实。"""
        if predicate:
            return list(self._fact_index.get(predicate, []))
        return list(self.facts)

    # ── 规则管理 ──────────────────────────────────────────

    def add_rule(self, head_pred: str, head_vars: list[str], body_specs: list[tuple[str, list[str]]]) -> Rule:
        """添加推理规则。

        Args:
            head_pred: 头部谓词名。
            head_vars: 头部变量列表。
            body_specs: 体部列表 [(谓词, 变量列表), ...]。

        Returns:
            创建的 Rule 对象。
        """
        head = Fact(predicate=head_pred, args=tuple(head_vars))
        body = [Fact(predicate=pred, args=tuple(vars)) for pred, vars in body_specs]
        variables: set[str] = set(head_vars)
        for _, vars_ in body_specs:
            variables.update(vars_)
        rule = Rule(head=head, body=body, variables=variables)
        self.rules.append(rule)
        return rule

    # ── 查询 ──────────────────────────────────────────────

    def query(self, predicate: str, *args: Any) -> list[dict[str, Any]]:
        """查询匹配的事实和推导结果。

        Args:
            predicate: 谓词名。
            *args: 参数 (字符串字面量或用 "?" 的变量如 "?X", "?Y")。

        Returns:
            变量绑定列表 [{var_name: value}, ...]。
        """
        # 检测哪些位置是变量
        variables: dict[int, str] = {}
        for i, arg in enumerate(args):
            arg_str = str(arg)
            if arg_str.startswith("?"):
                variables[i] = arg_str[1:]  # 去掉 "?" 前缀

        results: list[dict[str, Any]] = []

        # 1. 匹配直接事实
        for fact in self._fact_index.get(predicate, []):
            binding = self._match(fact.args, args, variables)
            if binding is not None:
                results.append(binding)

        # 2. 应用规则推导
        for rule in self.rules:
            if rule.head.predicate == predicate:
                derived = self._apply_rule(rule, args, variables)
                results.extend(derived)

        # 3. 去重
        return self._deduplicate(results)

    def ask(self, predicate: str, *args: Any) -> bool:
        """布尔查询 — 是否存在匹配的事实？"""
        return len(self.query(predicate, *args)) > 0

    # ── 推导 ──────────────────────────────────────────────

    def derive(self, max_iterations: int = 10) -> int:
        """运行推理引擎，推导所有可推导的事实。

        Returns:
            新推导的事实数量。
        """
        new_count = 0
        for _ in range(max_iterations):
            iteration_new = 0
            for rule in self.rules:
                derived = self._apply_rule(rule, tuple(f"^{v}" for v in rule.head.args), {})
                pattern_vars = {i: str(v) for i, v in enumerate(rule.head.args)}
                for binding in derived:
                    concrete_args = tuple(
                        binding.get(pattern_vars.get(i, f"var_{i}"), None)
                        for i in range(len(rule.head.args))
                    )
                    if all(a is not None for a in concrete_args):
                        if not self.ask(rule.head.predicate, *concrete_args):
                            self.assert_fact(rule.head.predicate, *concrete_args)
                            iteration_new += 1
            new_count += iteration_new
            if iteration_new == 0:
                break
        return new_count

    # ── 内部方法 ──────────────────────────────────────────

    def _match(
        self,
        fact_args: tuple[Any, ...],
        query_args: tuple[Any, ...],
        variables: dict[int, str],
    ) -> dict[str, Any] | None:
        """尝试将事实参数与查询参数匹配。"""
        if len(fact_args) != len(query_args):
            return None
        binding: dict[str, Any] = {}
        for i, (fact_arg, query_arg) in enumerate(zip(fact_args, query_args)):
            var_name = variables.get(i)
            if var_name:
                if var_name in binding:
                    if binding[var_name] != fact_arg:
                        return None  # 变量一致性检查失败
                else:
                    binding[var_name] = fact_arg
            elif str(query_arg) != str(fact_arg):
                return None  # 常量不匹配
        return binding

    def _apply_rule(
        self,
        rule: Rule,
        query_args: tuple[Any, ...],
        query_vars: dict[int, str],
    ) -> list[dict[str, Any]]:
        """应用规则推导新结果。"""
        # 为规则变量创建映射
        rule_vars: dict[str, str] = {}  # rule var → query var
        for i, arg in enumerate(query_args):
            arg_str = str(arg)
            if i < len(rule.head.args):
                rule_arg = str(rule.head.args[i])
                if arg_str.startswith("?"):
                    rule_vars[rule_arg] = arg_str[1:]
                elif arg_str.startswith("^"):
                    pass  # 推导模式
                else:
                    rule_vars[rule_arg] = arg_str

        # 对每个 body 事实，查找所有匹配
        results: list[dict[str, Any]] = [{}]
        for body_fact in rule.body:
            new_results = []
            body_query_vars: dict[int, str] = {}
            for i, arg in enumerate(body_fact.args):
                arg_str = str(arg)
                if arg_str in rule_vars:
                    body_query_vars[i] = rule_vars[arg_str]

            for fact in self._fact_index.get(body_fact.predicate, []):
                for prev_binding in results:
                    binding = self._match(fact.args, body_fact.args, body_query_vars)
                    if binding is not None:
                        merged = {**prev_binding, **binding}
                        new_results.append(merged)

            results = new_results
            if not results:
                break

        return results

    @staticmethod
    def _deduplicate(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """去重结果列表。"""
        seen = set()
        unique = []
        for r in results:
            key = tuple(sorted(r.items()))
            if key not in seen:
                seen.add(key)
                unique.append(r)
        return unique

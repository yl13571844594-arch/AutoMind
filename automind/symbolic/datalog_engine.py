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
                        # 去重必须只看**已存储的事实**，不能用 ask()：
                        # query() 第 2 步会应用规则，故 ask() 对任何"可推导"的
                        # 事实都返回 True —— 拿它做判断，derive() 永远认为
                        # "已经有了"，一条也不会物化，返回值恒为 0。
                        if not self._has_stored_fact(rule.head.predicate, concrete_args):
                            self.assert_fact(rule.head.predicate, *concrete_args)
                            iteration_new += 1
            new_count += iteration_new
            if iteration_new == 0:
                break
        return new_count

    # ── 内部方法 ──────────────────────────────────────────

    def _has_stored_fact(self, predicate: str, args: tuple[Any, ...]) -> bool:
        """该事实是否**已经落库**（只查事实索引，不走规则推导）。"""
        target = tuple(str(a) for a in args)
        return any(tuple(str(x) for x in f.args) == target
                   for f in self._fact_index.get(predicate, []))

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
        """应用规则推导新结果。

        三种调用形态（由 query_args 的前缀区分）：
          · ``?X``  —— 查询模式，把规则头变量绑定到调用方指定的变量名；
          · ``^X``  —— 推导模式（derive()），绑定到头变量自身的名字；
          · 其它   —— **常量**，用于过滤，不是变量。

        v1.5.1 之前这里有三处缺陷叠在一起：
          1. ``^`` 分支是 ``pass``，推导模式下一个变量都不绑，derive() 永远
             产不出事实；
          2. 只出现在 body 里的变量（如 X,Z :- p(X,Y), p(Y,Z) 中的 Y）不在
             映射表里，被 _match 当常量去比，连接查询必然失配；
          3. **常量和变量名混存在同一个 dict 里**，常量被当成变量名传给
             _match，于是 ask("grandparent","alice","bob") 这种本不该成立的
             查询会返回 True。
        现在把"变量映射"和"常量替换"彻底分开。
        """
        # 规则变量 → 绑定时使用的名字
        rule_vars: dict[str, str] = {}
        # 规则变量 → 调用方指定的常量（用于过滤，不参与绑定）
        rule_consts: dict[str, Any] = {}

        for i, arg in enumerate(query_args):
            if i >= len(rule.head.args):
                break
            arg_str = str(arg)
            rule_arg = str(rule.head.args[i])
            if arg_str.startswith("?"):
                rule_vars[rule_arg] = arg_str[1:]
            elif arg_str.startswith("^"):
                if rule_arg in rule.variables:
                    rule_vars[rule_arg] = rule_arg
            else:
                rule_consts[rule_arg] = arg

        # body 专有变量同样要参与绑定，否则跨 body 的连接做不了
        for var in rule.variables:
            v = str(var)
            if v not in rule_consts:
                rule_vars.setdefault(v, v)

        # 对每个 body 事实，查找所有匹配
        results: list[dict[str, Any]] = [{}]
        for body_fact in rule.body:
            new_results: list[dict[str, Any]] = []
            # 把常量替换进模式里，交给 _match 做常量比较；
            # 其余位置按变量处理。
            pattern = tuple(rule_consts.get(str(a), a) for a in body_fact.args)
            body_query_vars: dict[int, str] = {
                i: rule_vars[str(a)]
                for i, a in enumerate(body_fact.args)
                if str(a) in rule_vars
            }

            for fact in self._fact_index.get(body_fact.predicate, []):
                binding = self._match(fact.args, pattern, body_query_vars)
                if binding is None:
                    continue
                for prev_binding in results:
                    # 跨 body 的变量一致性：原来是 `{**prev, **binding}`，
                    # 共享变量取值冲突时后者直接覆盖前者，等于不校验连接条件。
                    if any(k in prev_binding and prev_binding[k] != v
                           for k, v in binding.items()):
                        continue
                    new_results.append({**prev_binding, **binding})

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

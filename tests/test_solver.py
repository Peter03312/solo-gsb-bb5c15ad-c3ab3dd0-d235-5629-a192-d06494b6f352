"""求解器与 PEP 440 版本模块测试。

无需数据库或第三方依赖：
    python3 tests/test_solver.py
若安装了 pytest，也可直接 ``pytest`` 运行。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.solver import (
    Candidate, CycleError, RuntimeRequirement, Snapshot, change_count,
    installation_order, plan_diff, solve, solve_minimal_change,
    UnsatisfiableError,
)
from app.version import (
    InvalidSpecifier, InvalidVersion, SpecifierSet, Version,
)


def R(name, spec):
    return RuntimeRequirement(name=name, spec=SpecifierSet.parse(spec))


def C(plugin, ver, deps=(), conflicts=(), rts=()):
    return Candidate(
        plugin=plugin,
        version=Version.parse(ver),
        runtimes=tuple(rts),
        dependencies=tuple((d, SpecifierSet.parse(s)) for d, s in deps),
        conflicts=tuple(conflicts),
    )


class VersionTests(unittest.TestCase):
    def test_ordering_pep440(self):
        seq = ["1.0.dev1", "1.0a1.dev1", "1.0a1", "1.0b1", "1.0rc1",
               "1.0", "1.0.post1.dev1", "1.0.post1"]
        parsed = [Version.parse(s) for s in seq]
        self.assertEqual(parsed, sorted(parsed))

    def test_equality_and_epoch(self):
        self.assertEqual(Version.parse("1.4"), Version.parse("1.4.0"))
        self.assertGreater(Version.parse("1!0"), Version.parse("99"))
        self.assertEqual(str(Version.parse("1.0-rc")), "1.0rc0")

    def test_specifiers(self):
        s = SpecifierSet.parse(">=1.0,<2.0")
        self.assertTrue(s.contains(Version.parse("1.5")))
        self.assertFalse(s.contains(Version.parse("2.0")))
        compat = SpecifierSet.parse("~=1.4.2")
        self.assertTrue(compat.contains(Version.parse("1.4.9")))
        self.assertFalse(compat.contains(Version.parse("1.5")))
        self.assertTrue(SpecifierSet.parse("==1.0.*").contains(Version.parse("1.0.9")))
        self.assertTrue(SpecifierSet.parse("3.8.*").contains(Version.parse("3.8.12")))
        self.assertTrue(SpecifierSet.parse("").contains(Version.parse("9.9")))

    def test_invalid(self):
        for bad in ["abc", "1..0"]:
            with self.assertRaises(InvalidVersion):
                Version.parse(bad)
        for bad in [">>", "~=1", "==1.0.*,<x", "<>1.0"]:
            with self.assertRaises(InvalidSpecifier):
                SpecifierSet.parse(bad)


class SolverTests(unittest.TestCase):
    def setUp(self):
        self.basic = Snapshot(1, {
            "alpha": (C("alpha", "1.0", [("beta", ">=1")]),),
            "beta": (C("beta", "1.5", [("gamma", ">=2")]),
                     C("beta", "1.0", [("gamma", ">=1")])),
            "gamma": (C("gamma", "2.0"), C("gamma", "1.0")),
        })

    def plan(self, snap, runtimes=(), **targets):
        return solve(snap, list(runtimes),
                     {k: SpecifierSet.parse(v) for k, v in targets.items()})

    def test_transitive_latest_and_install_order(self):
        plan = self.plan(self.basic, alpha="*")
        self.assertEqual({p: str(c.version) for p, c in plan.items()},
                         {"alpha": "1.0", "beta": "1.5", "gamma": "2.0"})
        self.assertEqual(installation_order(plan), ["gamma", "beta", "alpha"])

    def test_intersection_forces_downgrade(self):
        plan = self.plan(self.basic, alpha="*", gamma="<2")
        self.assertEqual(str(plan["beta"].version), "1.0")
        self.assertEqual(str(plan["gamma"].version), "1.0")

    def test_impossible_target(self):
        with self.assertRaises(UnsatisfiableError) as cm:
            self.plan(self.basic, gamma=">=9")
        reasons = cm.exception.reasons
        self.assertTrue(any(r["type"] == "no_matching_version"
                            and r["plugin"] == "gamma" for r in reasons))

    def test_missing_plugin(self):
        with self.assertRaises(UnsatisfiableError) as cm:
            self.plan(self.basic, ghost="")
        self.assertTrue(any(r["type"] == "missing_plugin"
                            and r["plugin"] == "ghost"
                            for r in cm.exception.reasons))

    def test_mutex_blocks(self):
        snap = Snapshot(1, {
            "a": (C("a", "1.0", [("b", ">=1"), ("d", ">=1")]),),
            "b": (C("b", "1.0", conflicts=["d"]),),
            "d": (C("d", "1.0"),),
        })
        with self.assertRaises(UnsatisfiableError) as cm:
            self.plan(snap, a="")
        self.assertTrue(any(r["type"] == "mutex_conflict"
                            for r in cm.exception.reasons))

    def test_mutex_avoids_via_other_version(self):
        snap = Snapshot(1, {
            "a": (C("a", "1.0", [("b", ">=1"), ("d", ">=1")]),),
            "b": (C("b", "2.0"), C("b", "1.0", conflicts=["d"])),
            "d": (C("d", "1.0"),),
        })
        plan = self.plan(snap, a="")
        self.assertEqual(str(plan["b"].version), "2.0")

    def test_dependency_cycle_explicit_error(self):
        snap = Snapshot(1, {
            "a": (C("a", "1.0", [("b", ">=1")]),),
            "b": (C("b", "1.0", [("a", ">=1")]),),
        })
        with self.assertRaises(CycleError) as cm:
            self.plan(snap, a="")
        cyc = cm.exception.reasons[0]["cycle"]
        self.assertEqual(cyc[0], cyc[-1])

    def test_cycle_on_newer_picks_older_acyclic(self):
        snap = Snapshot(1, {
            "a": (C("a", "2.0", [("b", ">=2")]),
                  C("a", "1.0", [("b", ">=1")])),
            "b": (C("b", "2.0", [("a", ">=2")]), C("b", "1.0")),
        })
        plan = self.plan(snap, a="")
        self.assertEqual({p: str(c.version) for p, c in plan.items()},
                         {"a": "1.0", "b": "1.0"})

    def test_runtime_compatibility_selects_older(self):
        snap = Snapshot(1, {
            "a": (C("a", "2.0", rts=[R("python", ">=3.9")]),
                  C("a", "1.0", rts=[R("python", ">=3.6,<3.9")])),
        })
        plan = self.plan(snap, runtimes=[R("python", "3.8.*")], a="")
        self.assertEqual(str(plan["a"].version), "1.0")

    def test_runtime_no_match_is_unsat_with_reason(self):
        snap = Snapshot(1, {
            "a": (C("a", "1.0", rts=[R("python", ">=3.9")]),),
        })
        with self.assertRaises(UnsatisfiableError) as cm:
            self.plan(snap, runtimes=[R("python", "2.7")], a="")
        self.assertTrue(any("python" in r.get("runtime_blocked", [])
                            for r in cm.exception.reasons))

    def test_diamond_disjoint_ranges(self):
        snap = Snapshot(1, {
            "a": (C("a", "1.0", [("b", ""), ("d", "")]),),
            "b": (C("b", "1.0", [("gamma", ">=2,<3")]),),
            "d": (C("d", "1.0", [("gamma", ">=3,<4")]),),
            "gamma": (C("gamma", "3.5"), C("gamma", "2.5")),
        })
        with self.assertRaises(UnsatisfiableError):
            self.plan(snap, a="")

    def test_deep_backtracking(self):
        # 钉住 gamma<2：a2/b2 链不可行，必须回溯到 a1/b1/gamma1
        snap = Snapshot(1, {
            "a": (C("a", "2.0", [("beta", ">=2")]),
                  C("a", "1.0", [("beta", ">=1")])),
            "beta": (C("beta", "2.0", [("gamma", ">=2")]),
                     C("beta", "1.0", [("gamma", ">=1")])),
            "gamma": (C("gamma", "2.0"), C("gamma", "1.0")),
        })
        plan = self.plan(snap, a="", gamma="<2")
        self.assertEqual({p: str(c.version) for p, c in plan.items()},
                         {"a": "1.0", "beta": "1.0", "gamma": "1.0"})

    def test_deterministic(self):
        first = {p: str(c.version) for p, c in self.plan(self.basic, alpha="*").items()}
        second = {p: str(c.version) for p, c in self.plan(self.basic, alpha="*").items()}
        self.assertEqual(first, second)

    def test_self_dependency_unsat(self):
        snap = Snapshot(1, {"a": (C("a", "1.0", [("a", ">=2")]),)})
        with self.assertRaises(UnsatisfiableError):
            self.plan(snap, a="")

    def test_multiple_runtimes_must_all_match(self):
        snap = Snapshot(1, {
            "a": (C("a", "1.0", rts=[R("python", ">=3.9"), R("cuda", ">=11")]),),
        })
        ok = self.plan(snap, runtimes=[R("python", "3.10"), R("cuda", "12")], a="")
        self.assertEqual(str(ok["a"].version), "1.0")
        with self.assertRaises(UnsatisfiableError):
            self.plan(snap, runtimes=[R("python", "3.10"), R("cuda", "10")], a="")


class MinimalChangeTests(unittest.TestCase):
    def setUp(self):
        # lib 1.0/2.0/3.0；app 1.0 -> lib>=1，app 2.0 -> lib>=2
        self.snap = Snapshot(1, {
            "lib": (C("lib", "3.0"), C("lib", "2.0"), C("lib", "1.0")),
            "app": (C("app", "2.0", [("lib", ">=2")]),
                    C("app", "1.0", [("lib", ">=1")])),
        })

    def mc(self, baseline, **targets):
        return solve_minimal_change(
            self.snap, [],
            {k: SpecifierSet.parse(v) for k, v in targets.items()},
            {k: Version.parse(v) for k, v in baseline.items()})

    def test_baseline_exact_is_zero_changes(self):
        plan = self.mc({"app": "2.0", "lib": "2.0"}, app="")
        self.assertEqual(str(plan["app"].version), "2.0")
        self.assertEqual(str(plan["lib"].version), "2.0")
        diff = plan_diff({k: Version.parse(v) for k, v in
                          {"app": "2.0", "lib": "2.0"}.items()}, plan)
        self.assertEqual(change_count(diff), 0)
        self.assertTrue(all(d["action"] == "keep" for d in diff.values()))

    def test_prefers_older_app_to_keep_lib(self):
        # 新目标不再约束 app 版本：经典解是 app2/lib3（2 次变动），
        # 最小变动应保留 lib2 并回退 app1（1 次变动）。
        baseline = {"app": "1.0", "lib": "2.0"}
        plan = self.mc(baseline, app="")
        self.assertEqual({p: str(c.version) for p, c in plan.items()},
                         {"app": "1.0", "lib": "2.0"})

    def test_force_upgrade_counts_one_change(self):
        plan = self.mc({"app": "1.0", "lib": "1.0"}, app=">=2")
        self.assertEqual(str(plan["app"].version), "2.0")
        # lib>=2：保留 lib 不可能；选与新约束兼容的最高 lib 3.0（平手按确定性）
        self.assertEqual(str(plan["lib"].version), "3.0")
        diff = plan_diff({k: Version.parse(v) for k, v in
                          {"app": "1.0", "lib": "1.0"}.items()}, plan)
        actions = {p: d["action"] for p, d in diff.items()}
        self.assertEqual(actions, {"app": "change", "lib": "change"})
        self.assertEqual(change_count(diff), 2)
        self.assertEqual(diff["lib"], {"action": "change",
                                       "from_version": "1.0",
                                       "to_version": "3.0"})

    def test_removed_plugin_when_target_dropped(self):
        # 原方案含多余插件 extra；新目标仅 app：extra 无法作为依赖出现 -> 移除
        snap = Snapshot(1, dict(self.snap.candidates_by_plugin,
                                extra=(C("extra", "1.0"),)))
        baseline = {"app": "1.0", "lib": "1.0", "extra": "1.0"}
        plan = solve_minimal_change(
            snap, [], {"app": SpecifierSet.parse("")},
            {k: Version.parse(v) for k, v in baseline.items()})
        diff = plan_diff({k: Version.parse(v) for k, v in baseline.items()}, plan)
        self.assertEqual(diff["extra"]["action"], "remove")
        self.assertIsNone(diff["extra"]["to_version"])
        self.assertEqual(change_count(diff), 1)

    def test_added_plugin_counted(self):
        # baseline 只有 lib（历史目标恰好如此）；新目标 app -> 必新增 app
        baseline = {"lib": "2.0"}
        plan = self.mc(baseline, app="")
        diff = plan_diff({k: Version.parse(v) for k, v in baseline.items()}, plan)
        self.assertEqual(diff["app"]["action"], "add")
        self.assertIsNone(diff["app"]["from_version"])
        # lib2 被保留（app2 兼容），故仅新增 app 一次变动
        self.assertEqual(str(plan["lib"].version), "2.0")
        self.assertEqual(change_count(diff), 1)

    def test_tie_falls_back_to_deterministic_solution(self):
        # baseline lib1.0；目标 app>=2 强制 lib>=2，lib2/lib3 都满足：
        # 都只有 1 次变动（换版），平手必须取经典解 lib3.0
        plan = self.mc({"app": "1.0", "lib": "1.0"}, app=">=2")
        classic = solve(self.snap, [], {"app": SpecifierSet.parse(">=2")})
        self.assertEqual({p: str(c.version) for p, c in plan.items()},
                         {p: str(c.version) for p, c in classic.items()})
        self.assertEqual(str(plan["lib"].version), "3.0")

    def test_unsat_raises_structured(self):
        with self.assertRaises(UnsatisfiableError) as cm:
            self.mc({"app": "1.0", "lib": "1.0"}, app=">=9")
        self.assertTrue(any(r["type"] == "no_matching_version"
                            for r in cm.exception.reasons))

    def test_deterministic_minimal_change(self):
        baseline = {"app": "1.0", "lib": "2.0"}
        first = self.mc(baseline, app="")
        second = self.mc(baseline, app="")
        self.assertEqual({p: str(c.version) for p, c in first.items()},
                         {p: str(c.version) for p, c in second.items()})

    def test_baseline_version_invisible_does_not_pin(self):
        # baseline 引用目标快照里不可见的 lib9（已撤销）：自动计为换版
        plan = self.mc({"app": "1.0", "lib": "9.0"}, app="")
        self.assertIn(str(plan["lib"].version), ("1.0", "2.0", "3.0"))
        diff = plan_diff({"app": Version.parse("1.0"), "lib": Version.parse("9.0")},
                         plan)
        self.assertEqual(diff["lib"]["action"], "change")

    def test_all_solutions_cycle_still_raises_cycle_error(self):
        # 唯一可行链含环：最小变动模式也必须报 CycleError（422），
        # 不能因分支定界剪枝而退化成普通无解（409）
        snap = Snapshot(1, {
            "a": (C("a", "1.0", [("b", "")]), C("a", "2.0")),
            "b": (C("b", "1.0", [("a", "")]),),
        })
        baseline = {"a": Version.parse("2.0")}
        with self.assertRaises(CycleError) as cm:
            solve_minimal_change(
                snap, [], {"a": SpecifierSet.parse("==1.0")}, baseline)
        cyc = cm.exception.reasons[0]["cycle"]
        self.assertEqual(cyc[0], cyc[-1])

    def test_valid_solution_beats_cycle_branch(self):
        # a2 独立合法，a1<->b1 含环；存在完整方案时不得误报环
        snap = Snapshot(1, {
            "a": (C("a", "2.0"), C("a", "1.0", [("b", "")])),
            "b": (C("b", "1.0", [("a", "")]),),
        })
        plan = solve_minimal_change(
            snap, [], {"a": SpecifierSet.parse("")}, {})
        self.assertEqual(str(plan["a"].version), "2.0")
        self.assertNotIn("b", plan)


if __name__ == "__main__":
    unittest.main(verbosity=2)

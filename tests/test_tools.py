from __future__ import annotations

import unittest
from pathlib import Path

from codex_agent.tools import (
    apply_retarget_plan,
    build_retarget_plan,
    check_rule_violations,
    compare_specs,
    parse_number_with_units,
)


EXAMPLE_NETLIST = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "source_amplifier_example.scs"
).read_text(encoding="utf-8")


class NumberParsingTests(unittest.TestCase):
    def test_spectre_suffixes(self) -> None:
        self.assertAlmostEqual(parse_number_with_units("22n"), 22e-9)
        self.assertAlmostEqual(parse_number_with_units("10uA"), 10e-6)
        self.assertAlmostEqual(parse_number_with_units("1meg"), 1e6)


class RetargetingTests(unittest.TestCase):
    def test_ptm22_retargeting_maps_models_and_geometry(self) -> None:
        plan = build_retarget_plan(EXAMPLE_NETLIST, "ptm22_lp", {})
        retargeted = apply_retarget_plan(EXAMPLE_NETLIST, plan)

        self.assertIn("nmos w=1u l=22n", retargeted)
        self.assertIn("pmos w=2u l=22n", retargeted)
        self.assertEqual(check_rule_violations(retargeted, "ptm22_lp"), [])

    def test_spec_comparison_reports_failures(self) -> None:
        specs = [
            {
                "metric": "gain_db",
                "target": "40",
                "comparison": ">=",
                "tolerance": "",
            }
        ]
        passed, failed, summary = compare_specs({"gain_db": 35}, specs)

        self.assertFalse(passed)
        self.assertEqual(failed[0]["metric"], "gain_db")
        self.assertFalse(summary["gain_db"]["passed"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from typing import Any, TypedDict


class AuraState(TypedDict, total=False):
    source_scs_path: str
    target_specs_csv_path: str
    target_pdk: str
    user_prompt: str
    run_id: str
    run_dir: str

    source_netlist: str
    current_netlist: str
    target_specs: list[dict[str, Any]]
    parameter_targets: dict[str, str]
    pdk_info: dict[str, Any]

    retrieved_docs: list[dict[str, Any]]
    retrieved_rules: list[str]

    retarget_plan: dict[str, Any]
    retarget_plan_path: str
    draft_scs_path: str
    rule_violations: list[dict[str, Any]]
    rule_pass: bool

    spectre_compile_ok: bool
    spectre_compile_log_path: str
    compile_errors: list[str]
    compile_warnings: list[str]

    measurement_plan: dict[str, Any]
    measurement_plan_path: str
    ocean_script: str
    ocean_script_path: str
    ocean_run_ok: bool
    ocean_log_path: str
    measured_metrics: dict[str, Any]
    measured_csv_path: str

    specs_met: bool
    failed_specs: list[dict[str, Any]]
    performance_summary: dict[str, Any]

    optimization_strategy: str
    optimization_actions: list[dict[str, Any]]
    next_route: str

    iteration: int
    retarget_attempt: int
    compile_debug_attempt: int
    max_iterations: int
    max_retarget_attempts: int
    max_compile_debug_attempts: int

    final_scs_path: str
    final_ocn_path: str
    migration_report: str
    migration_report_path: str

    artifacts: dict[str, str]
    events: list[str]

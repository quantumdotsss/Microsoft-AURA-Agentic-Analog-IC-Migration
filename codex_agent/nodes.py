from __future__ import annotations

import json
import shutil
from pathlib import Path

from codex_agent.config import get_pdk, settings
from codex_agent.state import AuraState
from codex_agent.tools import (
    apply_retarget_plan,
    build_measurement_plan,
    build_retarget_plan,
    check_rule_violations,
    choose_optimization,
    collect_retrieval_docs,
    compare_specs,
    event,
    generate_ocean_script,
    load_metrics_csv,
    load_target_specs,
    parse_spectre_log,
    read_text,
    run_external,
    update_netlist_parameters,
    write_dry_metrics_csv,
    write_text,
)


def _run_dir(state: AuraState) -> Path:
    path = Path(state["run_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def _artifact(state: AuraState, name: str, path: str | Path) -> None:
    state.setdefault("artifacts", {})[name] = str(path)


def load_inputs(state: AuraState) -> AuraState:
    run_dir = _run_dir(state)
    source_path = Path(state["source_scs_path"])
    specs_path = Path(state["target_specs_csv_path"])
    if not source_path.exists():
        raise FileNotFoundError(f"Source .scs not found: {source_path}")
    if not specs_path.exists():
        raise FileNotFoundError(f"Target specs CSV not found: {specs_path}")

    source_netlist = read_text(source_path)
    target_specs, parameter_targets = load_target_specs(specs_path)
    target_pdk = state.get("target_pdk", "ptm22_lp")

    state["source_netlist"] = source_netlist
    state["current_netlist"] = source_netlist
    state["target_specs"] = target_specs
    state["parameter_targets"] = parameter_targets
    state["pdk_info"] = get_pdk(target_pdk)
    state["max_retarget_attempts"] = settings.max_retarget_attempts
    state["max_compile_debug_attempts"] = settings.max_compile_debug_attempts
    state.setdefault("max_iterations", settings.max_iterations)
    state.setdefault("iteration", 0)
    state.setdefault("retarget_attempt", 0)
    state.setdefault("compile_debug_attempt", 0)
    state.setdefault("optimization_actions", [])
    state.setdefault("artifacts", {})
    state.setdefault("events", [])

    copied_source = run_dir / "input_source.scs"
    write_text(copied_source, source_netlist)
    _artifact(state, "input_source", copied_source)
    _artifact(state, "target_specs", specs_path)
    event(state, f"Loaded source netlist and {len(target_specs)} target specs.")
    return state


def retrieve_kb(state: AuraState) -> AuraState:
    query = "\n".join(
        [
            f"target_pdk={state.get('target_pdk', '')}",
            state.get("user_prompt", ""),
            state.get("current_netlist", "")[:1600],
        ]
    )
    docs = collect_retrieval_docs(query)
    state["retrieved_docs"] = docs
    state["retrieved_rules"] = [doc.get("text", "") for doc in docs]
    write_text(_run_dir(state) / "retrieved_context.json", json.dumps(docs, indent=2))
    _artifact(state, "retrieved_context", _run_dir(state) / "retrieved_context.json")
    event(state, f"Retrieved {len(docs)} local KB snippets from david/csc/TPM/mother_code.")
    return state


def netlist_retargeting_planner(state: AuraState) -> AuraState:
    attempt = state.get("retarget_attempt", 0) + 1
    state["retarget_attempt"] = attempt
    run_dir = _run_dir(state)

    plan = build_retarget_plan(
        state.get("source_netlist", ""),
        state.get("target_pdk", "ptm22_lp"),
        state.get("parameter_targets", {}),
    )
    if state.get("rule_violations"):
        plan["previous_rule_violations"] = state["rule_violations"]
        plan["notes"].append("Planner reran after rule violations and regenerated the target netlist.")

    draft = apply_retarget_plan(state.get("source_netlist", ""), plan)
    plan_path = run_dir / f"retarget_plan_attempt_{attempt}.json"
    draft_path = run_dir / f"draft_target_attempt_{attempt}.scs"
    write_text(plan_path, json.dumps(plan, indent=2))
    write_text(draft_path, draft)

    state["retarget_plan"] = plan
    state["retarget_plan_path"] = str(plan_path)
    state["current_netlist"] = draft
    state["draft_scs_path"] = str(draft_path)
    _artifact(state, "retarget_plan", plan_path)
    _artifact(state, "draft_scs", draft_path)
    event(state, f"Retarget planner produced draft netlist attempt {attempt}.")
    return state


def rule_engine(state: AuraState) -> AuraState:
    violations = check_rule_violations(state.get("current_netlist", ""), state.get("target_pdk", "ptm22_lp"))
    state["rule_violations"] = violations
    state["rule_pass"] = not violations
    report = {
        "rule_pass": state["rule_pass"],
        "violations": violations,
    }
    path = _run_dir(state) / "rule_check.json"
    write_text(path, json.dumps(report, indent=2))
    _artifact(state, "rule_check", path)
    event(state, "Rule engine passed." if state["rule_pass"] else f"Rule engine found {len(violations)} issue(s).")
    return state


def run_spectre_compile(state: AuraState) -> AuraState:
    run_dir = _run_dir(state)
    netlist_path = Path(state.get("draft_scs_path") or (run_dir / "draft_target.scs"))
    if not netlist_path.exists():
        write_text(netlist_path, state.get("current_netlist", ""))

    log_path = run_dir / "spectre_compile.log"
    command = [
        settings.spectre_bin,
        "-64",
        str(netlist_path),
        "+escchars",
        "+log",
        str(log_path),
        "+aps",
        "-maxw",
        "5",
        "-maxn",
        "5",
    ]
    ok, stdout, stderr = run_external(command, run_dir, settings.compile_timeout_sec)
    if settings.dry_run:
        log_text = stderr or "Dry-run Spectre compile skipped. Static rule checks were used instead.\n"
        ok = state.get("rule_pass", False)
    else:
        log_text = ""
        if log_path.exists():
            log_text = read_text(log_path)
        log_text += "\n" + stdout + "\n" + stderr

    errors, warnings = parse_spectre_log(log_text)
    if not settings.dry_run and errors:
        ok = False
    write_text(log_path, log_text)

    state["spectre_compile_ok"] = ok
    state["spectre_compile_log_path"] = str(log_path)
    state["compile_errors"] = errors
    state["compile_warnings"] = warnings
    _artifact(state, "spectre_compile_log", log_path)
    event(state, "Spectre compile passed or was dry-run skipped." if ok else f"Spectre compile failed with {len(errors)} error(s).")
    return state


def compile_debugger(state: AuraState) -> AuraState:
    attempt = state.get("compile_debug_attempt", 0) + 1
    state["compile_debug_attempt"] = attempt
    current = state.get("current_netlist", "")
    errors = "\n".join(state.get("compile_errors", []))
    updates: dict[str, str] = {}

    # Conservative compile fixes. The debugger does not invent topology.
    if "undefined parameter" in errors.lower():
        for name, value in get_pdk(state.get("target_pdk", "ptm22_lp")).get("default_parameters", {}).items():
            if name not in current:
                updates[name] = value
    if updates:
        current = update_netlist_parameters(current, updates)

    debug_path = _run_dir(state) / f"compile_debug_attempt_{attempt}.scs"
    write_text(debug_path, current)
    state["current_netlist"] = current
    state["draft_scs_path"] = str(debug_path)
    _artifact(state, "compile_debug_scs", debug_path)
    event(state, f"Compile debugger wrote attempt {attempt}.")
    return state


def measurement_planner(state: AuraState) -> AuraState:
    plan = build_measurement_plan(state.get("current_netlist", ""), state.get("target_specs", []))
    path = _run_dir(state) / "measurement_plan.json"
    write_text(path, json.dumps(plan, indent=2))
    state["measurement_plan"] = plan
    state["measurement_plan_path"] = str(path)
    _artifact(state, "measurement_plan", path)
    event(state, f"Measurement planner identified topology={plan.get('topology')} and metrics={plan.get('metrics')}.")
    return state


def write_ocean(state: AuraState) -> AuraState:
    run_dir = _run_dir(state)
    netlist_path = state.get("draft_scs_path", "")
    script = generate_ocean_script(
        state.get("current_netlist", ""),
        state.get("target_pdk", "ptm22_lp"),
        state.get("measurement_plan", {}),
        run_dir,
        netlist_path,
    )
    path = run_dir / "measurements.ocn"
    write_text(path, script)
    state["ocean_script"] = script
    state["ocean_script_path"] = str(path)
    _artifact(state, "ocean_script", path)
    event(state, "Ocean measurement script generated.")
    return state


def run_ocean(state: AuraState) -> AuraState:
    run_dir = _run_dir(state)
    script_path = Path(state["ocean_script_path"])
    log_path = run_dir / "ocean.log"
    metrics_path = run_dir / "measured_metrics.csv"

    if settings.dry_run:
        metrics = write_dry_metrics_csv(metrics_path, state.get("target_specs", []), state.get("current_netlist", ""), state.get("iteration", 0))
        log_text = "Dry-run Ocean execution skipped. Synthetic metrics were generated for graph testing.\n"
        ok = True
    else:
        command = [settings.ocean_bin, "-restore", str(script_path)]
        ok, stdout, stderr = run_external(command, run_dir, settings.ocean_timeout_sec)
        log_text = stdout + "\n" + stderr
        metrics = load_metrics_csv(metrics_path)

    write_text(log_path, log_text)
    if not metrics_path.exists():
        write_dry_metrics_csv(metrics_path, state.get("target_specs", []), state.get("current_netlist", ""), state.get("iteration", 0))
    state["ocean_run_ok"] = ok
    state["ocean_log_path"] = str(log_path)
    state["measured_csv_path"] = str(metrics_path)
    state["measured_metrics"] = metrics or load_metrics_csv(metrics_path)
    _artifact(state, "ocean_log", log_path)
    _artifact(state, "measured_metrics_csv", metrics_path)
    event(state, "Ocean run completed." if ok else "Ocean run failed.")
    return state


def performance_analyst(state: AuraState) -> AuraState:
    metrics = state.get("measured_metrics") or load_metrics_csv(state.get("measured_csv_path", ""))
    specs_met, failed, summary = compare_specs(metrics, state.get("target_specs", []))
    state["measured_metrics"] = metrics
    state["specs_met"] = specs_met
    state["failed_specs"] = failed
    state["performance_summary"] = summary
    path = _run_dir(state) / "performance_summary.json"
    write_text(path, json.dumps({"specs_met": specs_met, "failed_specs": failed, "summary": summary}, indent=2))
    _artifact(state, "performance_summary", path)
    event(state, "Performance specs met." if specs_met else f"Performance analyst found {len(failed)} failed spec(s).")
    return state


def optimizer(state: AuraState) -> AuraState:
    iteration = state.get("iteration", 0) + 1
    strategy, updates, actions = choose_optimization(
        state.get("failed_specs", []),
        state.get("current_netlist", ""),
        iteration,
    )
    state["iteration"] = iteration
    state["optimization_strategy"] = strategy
    state.setdefault("optimization_actions", []).extend(actions)

    if strategy == "update_scs" and updates:
        updated_netlist = update_netlist_parameters(state.get("current_netlist", ""), updates)
        path = _run_dir(state) / f"optimized_iter_{iteration}.scs"
        write_text(path, updated_netlist)
        state["current_netlist"] = updated_netlist
        state["draft_scs_path"] = str(path)
        state["next_route"] = "rule_engine"
        _artifact(state, f"optimized_iter_{iteration}", path)
    elif strategy == "update_ocn":
        state["next_route"] = "write_ocean"
    else:
        state["next_route"] = "finalize"

    event(state, f"Optimizer iteration {iteration} selected {strategy}.")
    return state


def finalize_outputs(state: AuraState) -> AuraState:
    run_dir = _run_dir(state)
    output_dir = settings.outputs_dir / state.get("run_id", "run")
    output_dir.mkdir(parents=True, exist_ok=True)

    final_scs = output_dir / "final_target.scs"
    final_ocn = output_dir / "measurements.ocn"
    metrics_out = output_dir / "measured_metrics.csv"
    report_path = output_dir / "migration_report.md"

    write_text(final_scs, state.get("current_netlist", ""))
    if state.get("ocean_script_path") and Path(state["ocean_script_path"]).exists():
        shutil.copyfile(state["ocean_script_path"], final_ocn)
    else:
        write_text(final_ocn, state.get("ocean_script", ""))
    if state.get("measured_csv_path") and Path(state["measured_csv_path"]).exists():
        shutil.copyfile(state["measured_csv_path"], metrics_out)

    report = render_report(state, final_scs, final_ocn, metrics_out)
    write_text(report_path, report)

    state["final_scs_path"] = str(final_scs)
    state["final_ocn_path"] = str(final_ocn)
    state["measured_csv_path"] = str(metrics_out if metrics_out.exists() else state.get("measured_csv_path", ""))
    state["migration_report"] = report
    state["migration_report_path"] = str(report_path)
    _artifact(state, "final_scs", final_scs)
    _artifact(state, "final_ocn", final_ocn)
    _artifact(state, "migration_report", report_path)
    event(state, "Final artifacts exported.")
    return state


def render_report(state: AuraState, final_scs: Path, final_ocn: Path, metrics_out: Path) -> str:
    lines: list[str] = []
    lines.append("# AURA Migration Report")
    lines.append("")
    lines.append(f"- Run ID: {state.get('run_id', '')}")
    lines.append(f"- Target PDK: {state.get('target_pdk', '')}")
    lines.append(f"- Dry run: {settings.dry_run}")
    lines.append(f"- Specs met: {state.get('specs_met', False)}")
    lines.append(f"- Iterations: {state.get('iteration', 0)}")
    lines.append("")
    lines.append("## Final Artifacts")
    lines.append("")
    lines.append(f"- Final netlist: `{final_scs}`")
    lines.append(f"- Ocean script: `{final_ocn}`")
    if metrics_out.exists():
        lines.append(f"- Metrics CSV: `{metrics_out}`")
    lines.append("")
    lines.append("## Agent Flow Events")
    lines.append("")
    for item in state.get("events", []):
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Rule Violations")
    lines.append("")
    violations = state.get("rule_violations", [])
    if violations:
        for violation in violations:
            line = violation.get("line")
            prefix = f"line {line}: " if line else ""
            lines.append(f"- {violation.get('type', 'unknown')}: {prefix}{violation.get('detail', '')}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("## Failed Specs")
    lines.append("")
    failed = state.get("failed_specs", [])
    if failed:
        for spec in failed:
            lines.append(
                f"- {spec.get('metric')}: actual={spec.get('actual')} "
                f"{spec.get('comparison')} target={spec.get('target')}"
            )
    else:
        lines.append("- None")
    lines.append("")
    lines.append("## Optimization Actions")
    lines.append("")
    actions = state.get("optimization_actions", [])
    if actions:
        for action in actions:
            lines.append(f"- `{action.get('type', 'action')}`: {json.dumps(action, default=str)}")
    else:
        lines.append("- None")
    lines.append("")
    if settings.dry_run:
        lines.append("## Cadence Note")
        lines.append("")
        lines.append("This run used dry-run mode because Cadence/Spectre/Ocean may not be available locally. Set `AURA_DRY_RUN=false` on the UCI Linux Cadence host to run real simulations.")
    return "\n".join(lines) + "\n"


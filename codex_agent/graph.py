from __future__ import annotations

from langgraph.graph import END, StateGraph

from codex_agent.nodes import (
    compile_debugger,
    finalize_outputs,
    load_inputs,
    measurement_planner,
    netlist_retargeting_planner,
    optimizer,
    performance_analyst,
    retrieve_kb,
    rule_engine,
    run_ocean,
    run_spectre_compile,
    write_ocean,
)
from codex_agent.state import AuraState


def route_after_rules(state: AuraState) -> str:
    if state.get("rule_pass", False):
        return "compile"
    if state.get("retarget_attempt", 0) < state.get("max_retarget_attempts", 3):
        return "retarget"
    return "finalize"


def route_after_compile(state: AuraState) -> str:
    if state.get("spectre_compile_ok", False):
        return "measure"
    if state.get("compile_debug_attempt", 0) < state.get("max_compile_debug_attempts", 3):
        return "debug"
    return "finalize"


def route_after_analysis(state: AuraState) -> str:
    if state.get("specs_met", False):
        return "finalize"
    if state.get("iteration", 0) >= state.get("max_iterations", 3):
        return "finalize"
    return "optimize"


def route_after_optimizer(state: AuraState) -> str:
    route = state.get("next_route", "")
    if route == "rule_engine":
        return "rules"
    if route == "write_ocean":
        return "write_ocean"
    return "finalize"


def build_graph():
    builder = StateGraph(AuraState)

    builder.add_node("load_inputs", load_inputs)
    builder.add_node("retrieve_kb", retrieve_kb)
    builder.add_node("retarget_planner", netlist_retargeting_planner)
    builder.add_node("rule_engine", rule_engine)
    builder.add_node("run_spectre_compile", run_spectre_compile)
    builder.add_node("compile_debugger", compile_debugger)
    builder.add_node("measurement_planner", measurement_planner)
    builder.add_node("write_ocean", write_ocean)
    builder.add_node("run_ocean", run_ocean)
    builder.add_node("performance_analyst", performance_analyst)
    builder.add_node("optimizer", optimizer)
    builder.add_node("finalize_outputs", finalize_outputs)

    builder.set_entry_point("load_inputs")
    builder.add_edge("load_inputs", "retrieve_kb")
    builder.add_edge("retrieve_kb", "retarget_planner")
    builder.add_edge("retarget_planner", "rule_engine")

    builder.add_conditional_edges(
        "rule_engine",
        route_after_rules,
        {
            "compile": "run_spectre_compile",
            "retarget": "retarget_planner",
            "finalize": "finalize_outputs",
        },
    )

    builder.add_conditional_edges(
        "run_spectre_compile",
        route_after_compile,
        {
            "measure": "measurement_planner",
            "debug": "compile_debugger",
            "finalize": "finalize_outputs",
        },
    )

    builder.add_edge("compile_debugger", "rule_engine")
    builder.add_edge("measurement_planner", "write_ocean")
    builder.add_edge("write_ocean", "run_ocean")
    builder.add_edge("run_ocean", "performance_analyst")

    builder.add_conditional_edges(
        "performance_analyst",
        route_after_analysis,
        {
            "optimize": "optimizer",
            "finalize": "finalize_outputs",
        },
    )

    builder.add_conditional_edges(
        "optimizer",
        route_after_optimizer,
        {
            "rules": "rule_engine",
            "write_ocean": "write_ocean",
            "finalize": "finalize_outputs",
        },
    )

    builder.add_edge("finalize_outputs", END)
    return builder.compile()


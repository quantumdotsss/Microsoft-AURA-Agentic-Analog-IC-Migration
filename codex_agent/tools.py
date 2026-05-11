from __future__ import annotations

import csv
import math
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable

from codex_agent.config import get_pdk, settings


VALUE_RE = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*([a-zA-Z]*)\s*$")
PARAM_RE = re.compile(r"([A-Za-z_][\w]*)\s*=\s*([^\s\\]+)")


def event(state: dict[str, Any], message: str) -> None:
    state.setdefault("events", []).append(message)


def read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def write_text(path: str | Path, text: str) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return str(p)


def load_target_specs(csv_path: str | Path) -> tuple[list[dict[str, Any]], dict[str, str]]:
    path = Path(csv_path)
    if not path.exists():
        return [], {}

    specs: list[dict[str, Any]] = []
    parameter_targets: dict[str, str] = {}

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            row = {str(k).strip(): (v.strip() if isinstance(v, str) else v) for k, v in raw.items() if k}
            metric = row.get("metric") or row.get("name") or row.get("spec") or ""
            target = row.get("target") or row.get("value") or row.get("new_value") or ""
            kind = (row.get("kind") or row.get("type") or "").lower()

            if kind == "param" or metric.lower().startswith("param:"):
                param_name = metric.split(":", 1)[-1].strip() if ":" in metric else row.get("parameter", "")
                if param_name and target:
                    parameter_targets[param_name] = target
                continue

            if metric:
                specs.append(
                    {
                        "metric": metric,
                        "target": target,
                        "comparison": row.get("comparison") or row.get("op") or ">=",
                        "tolerance": row.get("tolerance") or "",
                        "unit": row.get("unit") or "",
                        "weight": parse_float(row.get("weight"), default=1.0),
                        "notes": row.get("notes") or "",
                    }
                )

    return specs, parameter_targets


def parse_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    parsed = parse_number_with_units(str(value))
    return parsed if parsed is not None else default


def parse_number_with_units(value: str | None) -> float | None:
    if value is None:
        return None
    s = str(value).strip().strip('"')
    if not s:
        return None

    # Spectre commonly accepts values such as 300mA, 10uA, 45n, 1K, 1meg.
    m = VALUE_RE.match(s)
    if not m:
        try:
            return float(s)
        except ValueError:
            return None

    number = float(m.group(1))
    suffix = m.group(2).lower()
    suffix = suffix.replace("ohm", "")
    suffix = suffix.rstrip("vafhzs")

    multipliers = {
        "": 1.0,
        "f": 1e-15,
        "p": 1e-12,
        "n": 1e-9,
        "u": 1e-6,
        "m": 1e-3,
        "k": 1e3,
        "meg": 1e6,
        "g": 1e9,
        "t": 1e12,
    }
    return number * multipliers.get(suffix, 1.0)


def spectre_value(value: float, suffix: str = "") -> str:
    if suffix:
        return f"{value:g}{suffix}"
    return f"{value:g}"


def ocean_numeric_literal(value: str) -> str:
    """Convert Spectre-ish values to SKILL/Ocean-friendly numeric literals."""
    s = str(value).strip()
    match = VALUE_RE.match(s)
    if not match:
        return s
    number = match.group(1)
    suffix = match.group(2)
    lower = suffix.lower()
    if lower.endswith("hz"):
        suffix = suffix[:-2]
    elif lower.endswith(("v", "a")):
        suffix = suffix[:-1]
    return f"{number}{suffix}"


def continuation_blocks(text: str) -> list[tuple[int, str]]:
    blocks: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 1
    for idx, line in enumerate(text.splitlines(), start=1):
        if not buf:
            start = idx
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buf.append(stripped[:-1].rstrip())
            continue
        buf.append(stripped)
        blocks.append((start, " ".join(part.strip() for part in buf)))
        buf = []
    if buf:
        blocks.append((start, " ".join(part.strip() for part in buf)))
    return blocks


def parse_parameters(netlist: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for _, block in continuation_blocks(netlist):
        if block.strip().lower().startswith("parameters"):
            for name, value in PARAM_RE.findall(block):
                if name.lower() != "parameters":
                    params[name] = value
    return params


def render_parameters(params: dict[str, str]) -> list[str]:
    if not params:
        return []
    items = [f"{name}={value}" for name, value in params.items()]
    lines = ["parameters \\"]
    for idx in range(0, len(items), 4):
        chunk = " ".join(items[idx : idx + 4])
        suffix = " \\" if idx + 4 < len(items) else ""
        lines.append(f"    {chunk}{suffix}")
    return lines


def replace_parameters_block(netlist: str, updates: dict[str, str]) -> str:
    existing = parse_parameters(netlist)
    merged = {**existing, **{k: v for k, v in updates.items() if v not in {None, ""}}}
    if not merged:
        return netlist

    lines = netlist.splitlines()
    out: list[str] = []
    inserted = False
    skip = False

    for line in lines:
        stripped = line.strip()
        if skip:
            if not line.rstrip().endswith("\\"):
                skip = False
            continue

        if stripped.lower().startswith("parameters"):
            if not inserted:
                out.extend(render_parameters(merged))
                inserted = True
            if line.rstrip().endswith("\\"):
                skip = True
            continue
        out.append(line)

    if not inserted:
        insert_at = 0
        for idx, line in enumerate(out):
            if line.strip().lower().startswith(("simulator", "global")):
                insert_at = idx + 1
        rendered = render_parameters(merged)
        out[insert_at:insert_at] = rendered

    return "\n".join(out) + "\n"


def replace_include_lines(netlist: str, include_path: str, include_suffix: str = "") -> str:
    if not include_path:
        return netlist

    include_line = f'include "{include_path}"'
    if include_suffix:
        include_line = f"{include_line} {include_suffix}"

    lines = netlist.splitlines()
    out: list[str] = []
    inserted = False
    saw_include = False

    for line in lines:
        if line.strip().lower().startswith("include "):
            saw_include = True
            if not inserted:
                out.append(include_line)
                inserted = True
            out.append(f"// AURA original include: {line.strip()}")
            continue
        out.append(line)

    if not saw_include:
        insert_at = 0
        for idx, line in enumerate(out):
            if line.strip().lower().startswith(("global", "parameters")):
                insert_at = idx + 1
        out[insert_at:insert_at] = [include_line]

    return "\n".join(out) + "\n"


def model_polarity(model: str, instance: str = "") -> str | None:
    token = f"{instance} {model}".lower()
    if any(mark in token for mark in ["pmos", "pch", "pfet", "g45p", "hp14tbp"]):
        return "pmos"
    if any(mark in token for mark in ["nmos", "nch", "nfet", "g45n", "hp14tbn"]):
        return "nmos"
    if instance.upper().startswith("P"):
        return "pmos"
    if instance.upper().startswith(("N", "M")):
        return "nmos"
    return None


def iter_instances(netlist: str) -> list[dict[str, Any]]:
    instances: list[dict[str, Any]] = []
    pattern = re.compile(r"^\s*([A-Za-z_][\w.-]*)\s*\(([^)]*)\)\s+([^\s]+)(.*)$")
    for line_no, block in continuation_blocks(netlist):
        stripped = block.strip()
        if not stripped or stripped.startswith(("//", "*", ";")):
            continue
        match = pattern.match(block)
        if not match:
            continue
        name, pins, model, rest = match.groups()
        polarity = model_polarity(model, name)
        instances.append(
            {
                "line_no": line_no,
                "name": name,
                "pins": pins.split(),
                "model": model,
                "params": dict(PARAM_RE.findall(rest)),
                "raw": block,
                "polarity": polarity,
                "is_mos": polarity in {"nmos", "pmos"} and len(pins.split()) >= 4,
            }
        )
    return instances


def _replace_param_in_instance(line: str, param: str, value: str) -> str:
    pattern = re.compile(rf"(\b{re.escape(param)}\s*=\s*)([^\s\\]+)", re.IGNORECASE)
    if pattern.search(line):
        return pattern.sub(rf"\g<1>{value}", line)
    return f"{line} {param}={value}"


def rewrite_mos_devices(netlist: str, nmos_model: str, pmos_model: str, min_l: str = "", min_w: str = "") -> str:
    out: list[str] = []
    pattern = re.compile(r"^(\s*([A-Za-z_][\w.-]*)\s*\([^)]*\)\s+)([^\s]+)(.*)$")

    for _, block in continuation_blocks(netlist):
        original = block
        stripped = block.strip()
        if not stripped or stripped.startswith(("//", "*", ";")):
            out.append(original)
            continue
        match = pattern.match(block)
        if not match:
            out.append(original)
            continue
        prefix, inst, model, rest = match.groups()
        polarity = model_polarity(model, inst)
        if polarity not in {"nmos", "pmos"}:
            out.append(original)
            continue

        new_model = pmos_model if polarity == "pmos" else nmos_model
        new_line = f"{prefix}{new_model}{rest}"
        if min_l:
            new_line = _replace_param_in_instance(new_line, "l", min_l)
        if min_w:
            current_w = parse_param_from_line(new_line, "w")
            if current_w is None or current_w < (parse_number_with_units(min_w) or 0):
                new_line = _replace_param_in_instance(new_line, "w", min_w)
        out.append(new_line)

    return "\n".join(out) + "\n"


def parse_param_from_line(line: str, param: str) -> float | None:
    pattern = re.compile(rf"\b{re.escape(param)}\s*=\s*([^\s\\]+)", re.IGNORECASE)
    match = pattern.search(line)
    return parse_number_with_units(match.group(1)) if match else None


def extract_design_metadata(netlist: str) -> dict[str, str]:
    fields = {
        "library": r"Design library name:\s*([^\n\r]+)",
        "cell": r"Design cell name:\s*([^\n\r]+)",
        "view": r"Design view name:\s*([^\n\r]+)",
    }
    meta: dict[str, str] = {}
    for key, pattern in fields.items():
        match = re.search(pattern, netlist)
        if match:
            meta[key] = match.group(1).strip()
    return meta


def build_retarget_plan(netlist: str, target_pdk: str, parameter_targets: dict[str, str]) -> dict[str, Any]:
    pdk = get_pdk(target_pdk)
    instances = iter_instances(netlist)
    mos = [inst for inst in instances if inst["is_mos"]]
    default_params = dict(pdk.get("default_parameters", {}))
    default_params.update(parameter_targets)

    mappings = []
    for inst in mos:
        target_model = pdk["pmos_model"] if inst["polarity"] == "pmos" else pdk["nmos_model"]
        mappings.append(
            {
                "instance": inst["name"],
                "line_no": inst["line_no"],
                "source_model": inst["model"],
                "target_model": target_model,
                "polarity": inst["polarity"],
                "reason": f"Map MOS model to {pdk.get('label', target_pdk)}.",
            }
        )

    return {
        "target_pdk": target_pdk,
        "target_label": pdk.get("label", target_pdk),
        "source_summary": {
            "instance_count": len(instances),
            "mos_count": len(mos),
            "parameters": parse_parameters(netlist),
            "design_metadata": extract_design_metadata(netlist),
        },
        "include_update": {
            "path": pdk.get("default_include", ""),
            "suffix": pdk.get("default_include_suffix", ""),
        },
        "device_mappings": mappings,
        "geometry_updates": {
            "min_l": pdk.get("min_l", ""),
            "min_w": pdk.get("min_w", ""),
            "policy": "set MOS L to target node minimum and clamp W to legal minimum only",
        },
        "parameter_updates": default_params,
        "notes": [
            "Topology is preserved.",
            "Bias defaults come from local David/UCI examples and can be overridden by target specs CSV param rows.",
        ],
    }


def apply_retarget_plan(netlist: str, plan: dict[str, Any]) -> str:
    include = plan.get("include_update", {})
    pdk_name = plan.get("target_pdk", "")
    pdk = get_pdk(pdk_name)
    text = replace_include_lines(netlist, include.get("path", ""), include.get("suffix", ""))
    text = replace_parameters_block(text, plan.get("parameter_updates", {}))
    text = rewrite_mos_devices(
        text,
        nmos_model=pdk.get("nmos_model", "nmos"),
        pmos_model=pdk.get("pmos_model", "pmos"),
        min_l=plan.get("geometry_updates", {}).get("min_l", ""),
        min_w=plan.get("geometry_updates", {}).get("min_w", ""),
    )
    return text


def check_rule_violations(netlist: str, target_pdk: str) -> list[dict[str, Any]]:
    pdk = get_pdk(target_pdk)
    violations: list[dict[str, Any]] = []
    instances = iter_instances(netlist)
    mos = [inst for inst in instances if inst["is_mos"]]

    if not netlist.strip():
        return [{"type": "empty_netlist", "detail": "Netlist is empty."}]
    if not mos:
        violations.append({"type": "no_mos_devices", "detail": "No MOS devices were detected."})

    allowed_models = {pdk.get("nmos_model"), pdk.get("pmos_model")}
    min_l = parse_number_with_units(pdk.get("min_l", ""))
    min_w = parse_number_with_units(pdk.get("min_w", ""))

    for inst in mos:
        if allowed_models and inst["model"] not in allowed_models:
            violations.append(
                {
                    "type": "model_mapping",
                    "line": inst["line_no"],
                    "instance": inst["name"],
                    "detail": f"Model {inst['model']} is not in target model set {sorted(allowed_models)}.",
                }
            )
        if len(inst["pins"]) != 4:
            violations.append(
                {
                    "type": "mos_pin_order",
                    "line": inst["line_no"],
                    "instance": inst["name"],
                    "detail": "MOS instance should have four pins: drain gate source bulk.",
                }
            )
        l_val = parse_number_with_units(inst["params"].get("l"))
        w_val = parse_number_with_units(inst["params"].get("w"))
        if min_l is not None and l_val is not None and l_val < min_l:
            violations.append(
                {
                    "type": "min_length",
                    "line": inst["line_no"],
                    "instance": inst["name"],
                    "detail": f"L={inst['params'].get('l')} is below target min_l={pdk.get('min_l')}.",
                }
            )
        if min_w is not None and w_val is not None and w_val < min_w:
            violations.append(
                {
                    "type": "min_width",
                    "line": inst["line_no"],
                    "instance": inst["name"],
                    "detail": f"W={inst['params'].get('w')} is below target min_w={pdk.get('min_w')}.",
                }
            )

    params = parse_parameters(netlist)
    if "VDD" not in params and "vdd" not in {k.lower(): v for k, v in params.items()}:
        violations.append({"type": "missing_vdd_parameter", "detail": "No VDD design parameter detected."})

    return violations


def collect_retrieval_docs(query: str, roots: Iterable[Path] | None = None, limit: int = 6) -> list[dict[str, Any]]:
    if roots is None:
        roots = [
            settings.repo_root / "david",
            settings.repo_root / "csc",
            settings.repo_root / "TPM",
            settings.repo_root / "rag_data",
            settings.repo_root / "mother_code",
            settings.repo_root / "Design_Project_export",
        ]
    exts = {".scs", ".ocn", ".log", ".txt", ".md", ".csv", ".py", ".cshrc"}
    query_terms = {t.lower() for t in re.findall(r"[A-Za-z0-9_]+", query) if len(t) > 2}
    scored: list[tuple[float, Path, str]] = []

    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in exts:
                continue
            try:
                if path.stat().st_size > 350_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lowered = text.lower()
            score = sum(lowered.count(term) for term in query_terms)
            if "spectre" in lowered:
                score += 2
            if "ocean" in lowered or path.suffix.lower() == ".ocn":
                score += 2
            if score <= 0:
                continue
            snippet = text[:2200]
            scored.append((score, path, snippet))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        {"path": str(path), "score": score, "text": snippet}
        for score, path, snippet in scored[:limit]
    ]


def run_external(command: list[str], cwd: Path, timeout: int) -> tuple[bool, str, str]:
    if settings.dry_run:
        return True, "", f"Dry-run enabled; skipped command: {' '.join(command)}\n"
    if not shutil.which(command[0]):
        return False, "", f"Binary not found: {command[0]}\n"

    if settings.cadence_setup:
        shell = os.getenv("AURA_CADENCE_SHELL") or ("/bin/tcsh" if Path("/bin/tcsh").exists() else "/bin/bash")
        quoted = " ".join(command)
        full_cmd = f"{settings.cadence_setup}; {quoted}"
        proc = subprocess.run(
            full_cmd,
            cwd=str(cwd),
            shell=True,
            executable=shell,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    else:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    return proc.returncode == 0, proc.stdout, proc.stderr


def parse_spectre_log(text: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if re.search(r"\b(ERROR|Error)\b", s):
            errors.append(s)
        elif re.search(r"\b(WARNING|Warning)\b", s):
            warnings.append(s)
    return errors, warnings


def infer_nodes(netlist: str) -> dict[str, Any]:
    names = set()
    for inst in iter_instances(netlist):
        names.update(pin.strip() for pin in inst["pins"])
    lower_map = {n.lower().replace("\\", ""): n for n in names}

    def pick(*candidates: str) -> str:
        for cand in candidates:
            key = cand.lower().replace("\\", "")
            if key in lower_map:
                return lower_map[key]
        return ""

    outputs = [n for n in ["Vout2", "Vout1", "vout", "out"] if n.lower() in lower_map]
    if not outputs:
        outputs = [pick("Vout2", "Vout1", "vout", "out")]
    outputs = [n for n in outputs if n]

    return {
        "vin_p": pick("Vin+", "Vin\\+", "vinp", "inp"),
        "vin_n": pick("Vin-", "Vin\\-", "vinn", "inn"),
        "outputs": outputs or ["Vout2"],
        "supply": pick("VDD", "vdd", "vdd!"),
        "ground": pick("0", "VSS", "gnd"),
    }


def build_measurement_plan(netlist: str, target_specs: list[dict[str, Any]]) -> dict[str, Any]:
    nodes = infer_nodes(netlist)
    metrics = [spec["metric"] for spec in target_specs] or ["vout_dc", "gain_db", "power_w"]
    topology = "differential_amplifier" if nodes.get("vin_p") and nodes.get("vin_n") else "generic_analog"
    analyses = ["dcOp"]
    if any(m in {"gain_db", "bandwidth_hz", "ugb_hz", "phase_margin_deg"} for m in metrics):
        analyses.append("ac")
    if any(m in {"swing_vpp", "settling_time_s", "vout_dc"} for m in metrics):
        analyses.append("tran")
    return {
        "topology": topology,
        "nodes": nodes,
        "metrics": metrics,
        "analyses": analyses,
        "sweep": {},
    }


def ocean_quote_node(node: str) -> str:
    return "/" + node.replace("\\", "")


def generate_ocean_script(
    netlist: str,
    target_pdk: str,
    measurement_plan: dict[str, Any],
    run_dir: Path,
    netlist_path: str,
) -> str:
    pdk = get_pdk(target_pdk)
    params = parse_parameters(netlist)
    metadata = extract_design_metadata(netlist)
    nodes = measurement_plan.get("nodes", {})
    outputs = nodes.get("outputs") or ["Vout2"]
    results_dir = run_dir / "ocean_results"
    raw_csv = run_dir / "waveforms.csv"
    metrics_csv = run_dir / "measured_metrics.csv"

    lines: list[str] = []
    lines.append("; Generated by AURA LangGraph")
    lines.append("simulator( 'spectre )")
    if metadata.get("library") and metadata.get("cell"):
        lines.append(f'design( "{metadata["library"]}" "{metadata["cell"]}" "{metadata.get("view", "schematic")}" )')
    else:
        lines.append(f'; Standalone netlist path for reference: "{netlist_path}"')

    include_path = pdk.get("default_include", "")
    if include_path:
        lines.append(f'modelFile( list( "{include_path}" "{pdk.get("default_include_suffix", "")}" ) )')

    for name, value in params.items():
        lines.append(f'desVar( "{name}" {ocean_numeric_literal(value)} )')

    lines.append("saveOption( 'save \"allpub\" )")
    if "dcOp" in measurement_plan.get("analyses", []):
        lines.append("analysis('dc ?saveOppoint t )")
    if "ac" in measurement_plan.get("analyses", []):
        lines.append("analysis('ac ?start \"1\" ?stop \"10G\" ?dec 20 )")
    if "tran" in measurement_plan.get("analyses", []):
        lines.append("analysis('tran ?stop \"5u\" ?write \"spectre.ic\" ?writefinal \"spectre.fc\" ?annotate \"status\" )")

    lines.append(f'resultsDir( "{results_dir}" )')
    lines.append("run()")
    lines.append("")
    lines.append("; Export waveforms for Python-side metric parsing.")
    lines.append("selectResult( 'tran )")
    ocn_nodes = " ".join(f'v("{ocean_quote_node(n)}")' for n in outputs)
    if ocn_nodes:
        lines.append(f'ocnPrint( {ocn_nodes} ?output "{raw_csv}" ?numberNotation \'scientific )')
    lines.append("")
    lines.append(f'outPort = outfile("{metrics_csv}")')
    lines.append('fprintf(outPort "metric,value,unit,source\\n")')
    for out_node in outputs:
        qnode = ocean_quote_node(out_node)
        lines.append(f'if( v("{qnode}") then fprintf(outPort "vout_dc,%g,V,ocean\\n" average(v("{qnode}"))) )')
        lines.append(f'if( v("{qnode}") then fprintf(outPort "swing_vpp,%g,V,ocean\\n" ymax(v("{qnode}"))-ymin(v("{qnode}"))) )')
    lines.append("close(outPort)")
    lines.append('printf("\\nAURA Ocean measurement script complete.\\n")')
    lines.append("exit()")
    return "\n".join(lines) + "\n"


def write_dry_metrics_csv(path: str | Path, specs: list[dict[str, Any]], netlist: str, iteration: int) -> dict[str, Any]:
    params = parse_parameters(netlist)
    vdd = parse_number_with_units(params.get("VDD") or params.get("vdd") or "1.0") or 1.0
    iss = parse_number_with_units(params.get("ISS") or "0") or 0.0
    ibtail = parse_number_with_units(params.get("Ibtail") or "0") or 0.0
    estimated_power = max(vdd * (iss + 2 * ibtail), 0.0)
    estimated_gain = 20.0 + iteration * 5.0
    estimated_bw = 5e5 + iteration * 2.5e5
    estimated_vout = vdd / 2.0

    metrics = {
        "gain_db": estimated_gain,
        "bandwidth_hz": estimated_bw,
        "ugb_hz": estimated_bw,
        "power_w": estimated_power,
        "vout_dc": estimated_vout,
        "swing_vpp": max(vdd * 0.25, 0.0),
        "simulated": 0.0,
    }
    for spec in specs:
        metric = spec["metric"]
        metrics.setdefault(metric, metrics.get(metric.lower(), 0.0))

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["metric", "value", "unit", "source"])
        for metric, value in metrics.items():
            writer.writerow([metric, value, "", "dry_run_estimate"])
    return metrics


def load_metrics_csv(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    metrics: dict[str, Any] = {}
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames and {"metric", "value"}.issubset(set(reader.fieldnames)):
            for row in reader:
                metric = (row.get("metric") or "").strip()
                if metric:
                    metrics[metric] = parse_float(row.get("value"), default=row.get("value"))
    return metrics


def compare_specs(metrics: dict[str, Any], specs: list[dict[str, Any]]) -> tuple[bool, list[dict[str, Any]], dict[str, Any]]:
    failed: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    if not specs:
        return True, [], {"detail": "No target specs were provided."}

    for spec in specs:
        metric = spec["metric"]
        actual = parse_float(metrics.get(metric), default=None)
        target = parse_float(spec.get("target"), default=None)
        tolerance = parse_float(spec.get("tolerance"), default=0.0) or 0.0
        op = str(spec.get("comparison") or ">=").lower()

        passed = False
        if actual is None or target is None:
            passed = False
        elif op in {">=", "min", "at_least"}:
            passed = actual >= target
        elif op in {"<=", "max", "at_most"}:
            passed = actual <= target
        elif op in {"within", "~", "about"}:
            passed = abs(actual - target) <= tolerance
        elif op in {"==", "="}:
            passed = math.isclose(actual, target, rel_tol=1e-6, abs_tol=tolerance)

        summary[metric] = {"actual": actual, "target": target, "comparison": op, "passed": passed}
        if not passed:
            failed.append({"metric": metric, "actual": actual, "target": target, "comparison": op, "tolerance": tolerance})

    return not failed, failed, summary


def update_netlist_parameters(netlist: str, updates: dict[str, str]) -> str:
    return replace_parameters_block(netlist, updates)


def choose_optimization(failed_specs: list[dict[str, Any]], netlist: str, iteration: int) -> tuple[str, dict[str, str], list[dict[str, Any]]]:
    params = parse_parameters(netlist)
    updates: dict[str, str] = {}
    actions: list[dict[str, Any]] = []

    failed_names = {item["metric"] for item in failed_specs}
    if {"gain_db", "bandwidth_hz", "ugb_hz"} & failed_names:
        rg = parse_number_with_units(params.get("Rg", "1k")) or 1e3
        rm = parse_number_with_units(params.get("Rm", "1k")) or 1e3
        factor = 1.25 if "gain_db" in failed_names else 0.85
        updates["Rg"] = spectre_value(rg * factor)
        updates["Rm"] = spectre_value(rm * factor)
        actions.append({"type": "resistor_tune", "params": {"Rg": updates["Rg"], "Rm": updates["Rm"]}})

    if "power_w" in failed_names:
        for name in ["ISS", "Ibtail"]:
            if name in params:
                current = parse_number_with_units(params[name])
                if current is not None:
                    updates[name] = spectre_value(current * 0.8)
                    actions.append({"type": "bias_current_reduce", "param": name, "value": updates[name]})

    if "vout_dc" in failed_names or not actions:
        vdd = parse_number_with_units(params.get("VDD", "1")) or 1.0
        updates.setdefault("Aref", spectre_value(vdd / 2.0))
        if "Vb3" in params:
            vb3 = parse_number_with_units(params["Vb3"])
            if vb3 is not None:
                updates["Vb3"] = spectre_value(min(max(vb3 + 0.05 * (iteration + 1), 0.05), vdd))
        actions.append({"type": "bias_common_mode_tune", "params": updates.copy()})

    strategy = "update_scs" if updates else "none"
    return strategy, updates, actions

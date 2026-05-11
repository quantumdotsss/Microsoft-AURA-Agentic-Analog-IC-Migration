# AURA LangGraph Agent

This folder implements the three-stage agent flow from `agent_Flow.png`:

1. Phase 1: netlist retargeting, rule checks, Spectre compile/debug loop.
2. Phase 2: topology-aware Ocean measurement planning and script generation.
3. Phase 3: Ocean execution, metric analysis, and parameter optimization loop.

The project is built with LangGraph. It defaults to `AURA_DRY_RUN=true` so it can run before you connect to the UCI Cadence server. In dry-run mode, the Spectre and Ocean nodes write artifacts and synthetic metric CSVs instead of launching Cadence.

## Setup

```bash
cd /home/emc/Documents/rag/codex_agent
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Run Locally Without UCI Server

```bash
python run_agent.py \
  --source /home/emc/Documents/rag/david/45nm/2stageinput_david.scs \
  --target-specs /home/emc/Documents/rag/codex_agent/examples/target_specs_example.csv \
  --target-pdk ptm22_lp \
  --prompt "Retarget the 45nm two-stage amplifier to PTM 22nm LP while preserving topology."
```

Artifacts are written to:

```text
workspace/runs/<run_id>/
outputs/<run_id>/
```

## Run On UCI Cadence Linux

After logging into the EECS server and preparing Cadence, set:

```bash
export AURA_DRY_RUN=false
export AURA_CADENCE_SETUP="source /ecelib/linware/profile/cadence616"
export SPECTRE_BIN=spectre
export OCEAN_BIN=ocean
```

The project does not SSH by itself. Run it directly in the Linux/Cadence environment.

## Presentation

The agent-only progress deck is generated at:

```text
AURA_Agentic_Framework_Yunbo.pptx
```

To regenerate it:

```bash
python scripts/generate_agent_ppt.py
```

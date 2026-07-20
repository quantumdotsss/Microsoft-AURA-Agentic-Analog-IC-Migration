from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BASE_DIR.parent


@dataclass(frozen=True)
class Settings:
    base_dir: Path = BASE_DIR
    repo_root: Path = REPO_ROOT
    workspace_dir: Path = BASE_DIR / "workspace"
    runs_dir: Path = BASE_DIR / "workspace" / "runs"
    outputs_dir: Path = BASE_DIR / "outputs"

    dry_run: bool = os.getenv("AURA_DRY_RUN", "true").lower() in {"1", "true", "yes", "on"}
    spectre_bin: str = os.getenv("SPECTRE_BIN", "spectre")
    ocean_bin: str = os.getenv("OCEAN_BIN", "ocean")
    cadence_setup: str = os.getenv("AURA_CADENCE_SETUP", "")
    compile_timeout_sec: int = int(os.getenv("AURA_COMPILE_TIMEOUT_SEC", "120"))
    ocean_timeout_sec: int = int(os.getenv("AURA_OCEAN_TIMEOUT_SEC", "300"))
    max_iterations: int = int(os.getenv("AURA_MAX_ITERATIONS", "3"))
    max_retarget_attempts: int = int(os.getenv("AURA_MAX_RETARGET_ATTEMPTS", "3"))
    max_compile_debug_attempts: int = int(os.getenv("AURA_MAX_COMPILE_DEBUG_ATTEMPTS", "3"))


settings = Settings()


TARGET_PDKS: dict[str, dict[str, Any]] = {
    "ptm22_lp": {
        "label": "PTM 22nm LP",
        "node_nm": 22.0,
        "min_l": "22n",
        "min_w": "44n",
        "nmos_model": "nmos",
        "pmos_model": "pmos",
        "default_include": os.getenv("AURA_PTM22_MODEL_PATH", ""),
        "default_parameters": {
            "VDD": "0.95V",
            "VinDC": "0.75V",
            "VinAC": "10mV",
            "Ibtail": "10uA",
            "ISS": "10uA",
            "Vbiasn": "0.85V",
            "Vbp": "0.1V",
            "Vb3": "0.8V",
            "Vb2": "0.2V",
            "Aref": "0.5V",
            "Rg": "1K",
            "Rm": "1K",
        },
    },
    "gpdk045": {
        "label": "Cadence GPDK 45nm",
        "node_nm": 45.0,
        "min_l": "50n",
        "min_w": "120n",
        "nmos_model": "nch",
        "pmos_model": "pch",
        "default_include": os.getenv("AURA_GPDK045_MODEL_PATH", ""),
        "default_include_suffix": "section=tt",
        "default_parameters": {
            "VDD": "1.1",
            "VinDC": "0.55",
            "VinAC": "1m",
            "Ibtail": "20u",
            "ISS": "20u",
            "Vbiasn": "0.6",
            "Vbp": "0.5",
            "Vb3": "0.8",
            "Vb2": "300m",
            "Aref": "550m",
            "Rg": "100k",
            "Rm": "100k",
        },
    },
    "asap7": {
        "label": "ASAP7 / PTM 7nm style",
        "node_nm": 7.0,
        "min_l": "7n",
        "min_w": "50n",
        "nmos_model": "nfet",
        "pmos_model": "pfet",
        "default_include": os.getenv("AURA_ASAP7_MODEL_PATH", ""),
        "default_parameters": {
            "VDD": "0.7",
            "VinDC": "0.35",
            "VinAC": "1m",
        },
    },
}


def ensure_directories() -> None:
    for path in [settings.workspace_dir, settings.runs_dir, settings.outputs_dir]:
        path.mkdir(parents=True, exist_ok=True)


def get_pdk(name: str) -> dict[str, Any]:
    key = (name or "").lower()
    if key in TARGET_PDKS:
        return TARGET_PDKS[key]
    return {
        "label": name,
        "node_nm": None,
        "min_l": "",
        "min_w": "",
        "nmos_model": "nmos",
        "pmos_model": "pmos",
        "default_include": "",
        "default_parameters": {},
    }


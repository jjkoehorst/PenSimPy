"""
PenSimPy REST API
=================
Two modes:

  Batch mode  — POST /batch         run a full batch, get back the dataframe
  Step mode   — POST /sessions      create a session (reset env)
                POST /sessions/{id}/step  advance one timestep with your actions
                GET  /sessions/{id}/state current full observation
                DELETE /sessions/{id}     tear down session

Auto-docs at http://localhost:8000/docs
"""

import math
import uuid
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from pensimpy.constants import STEP_IN_HOURS, STEP_IN_MINUTES, NUM_STEPS, MINUTES_PER_HOUR
from pensimpy.peni_env_setup import PenSimEnv
from pensimpy.examples.recipe import Recipe, RecipeCombo
from pensimpy.data.constants import (
    FS, FOIL, FG, PRES, DISCHARGE, WATER, PAA,
    FS_DEFAULT_PROFILE, FOIL_DEFAULT_PROFILE, FG_DEFAULT_PROFILE,
    PRESS_DEFAULT_PROFILE, DISCHARGE_DEFAULT_PROFILE,
    WATER_DEFAULT_PROFILE, PAA_DEFAULT_PROFILE,
)

app = FastAPI(
    title="PenSimPy API",
    description="Step-by-step control of the IndPenSim penicillin fermentation simulator.",
    version="1.0.0",
)

# ---------------------------------------------------------------------------
# In-memory session store  {session_id: {"env": PenSimEnv, "k": int, ...}}
# ---------------------------------------------------------------------------
_sessions: Dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class Setpoint(BaseModel):
    time: float
    value: float


class RecipeInput(BaseModel):
    Fs:        Optional[List[Setpoint]] = Field(None, description="Sugar feed rate profile")
    Foil:      Optional[List[Setpoint]] = Field(None, description="Oil feed rate profile")
    Fg:        Optional[List[Setpoint]] = Field(None, description="Aeration rate profile")
    pressure:  Optional[List[Setpoint]] = Field(None, description="Back pressure profile")
    discharge: Optional[List[Setpoint]] = Field(None, description="Discharge rate profile")
    Fw:        Optional[List[Setpoint]] = Field(None, description="Water injection profile")
    Fpaa:      Optional[List[Setpoint]] = Field(None, description="Phenylacetic acid profile")


class SessionCreateRequest(BaseModel):
    random_seed: int = Field(0, description="Seed for batch-to-batch variation")
    recipe: Optional[RecipeInput] = Field(None, description="Custom recipe; omit to use defaults")


class StepRequest(BaseModel):
    Fs:        Optional[float] = Field(None, description="Sugar feed rate (L/h)")
    Foil:      Optional[float] = Field(None, description="Oil feed rate (L/h)")
    Fg:        Optional[float] = Field(None, description="Aeration rate (L/h)")
    pressure:  Optional[float] = Field(None, description="Back pressure (bar)")
    discharge: Optional[float] = Field(None, description="Discharge rate (L/h)")
    Fw:        Optional[float] = Field(None, description="Water injection rate (L/h)")
    Fpaa:      Optional[float] = Field(None, description="Phenylacetic acid rate (L/h)")


class Observation(BaseModel):
    step:          int
    time_hours:    float
    done:          bool
    yield_delta:   float
    total_yield:   float
    # process state
    pH:            float
    temperature:   float
    dissolved_O2:  float
    substrate:     float
    penicillin:    float
    volume:        float
    vessel_weight: float
    O2_offgas:     float
    CO2_offgas:    float
    # manipulated variables (what was actually applied this step)
    Fs:        float
    Foil:      float
    Fg:        float
    pressure:  float
    discharge: float
    Fw:        float
    Fpaa:      float
    # PID-controlled flows
    Fa:  float
    Fb:  float
    Fc:  float
    Fh:  float


class BatchRequest(BaseModel):
    random_seed:   int = Field(0)
    include_raman: bool = Field(False)
    recipe: Optional[RecipeInput] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_recipe_combo(recipe_input: Optional[RecipeInput]) -> RecipeCombo:
    defaults = {
        FS:       FS_DEFAULT_PROFILE,
        FOIL:     FOIL_DEFAULT_PROFILE,
        FG:       FG_DEFAULT_PROFILE,
        PRES:     PRESS_DEFAULT_PROFILE,
        DISCHARGE: DISCHARGE_DEFAULT_PROFILE,
        WATER:    WATER_DEFAULT_PROFILE,
        PAA:      PAA_DEFAULT_PROFILE,
    }
    if recipe_input is None:
        profiles = defaults
    else:
        raw = recipe_input.model_dump(exclude_none=True)
        # map API field names → internal constant keys
        key_map = {
            "Fs": FS, "Foil": FOIL, "Fg": FG,
            "pressure": PRES, "discharge": DISCHARGE,
            "Fw": WATER, "Fpaa": PAA,
        }
        profiles = dict(defaults)
        for field, setpoints in raw.items():
            profiles[key_map[field]] = [{"time": sp["time"], "value": sp["value"]} for sp in setpoints]

    return RecipeCombo(recipe_dict={k: Recipe(v, k) for k, v in profiles.items()})


def _extract_observation(x, k: int, yield_delta: float, total_yield: float, done: bool) -> Observation:
    i = max(k - 1, 0)
    raw_pH = x.pH.y[i]
    pH = -math.log10(raw_pH) if raw_pH > 0 else 0.0
    return Observation(
        step=k,
        time_hours=round(k * STEP_IN_HOURS, 4),
        done=done,
        yield_delta=round(yield_delta, 6),
        total_yield=round(total_yield, 6),
        pH=round(pH, 4),
        temperature=round(x.T.y[i], 4),
        dissolved_O2=round(x.DO2.y[i], 4),
        substrate=round(x.S.y[i], 6),
        penicillin=round(x.P.y[i], 6),
        volume=round(x.V.y[i], 2),
        vessel_weight=round(x.Wt.y[i], 2),
        O2_offgas=round(x.O2.y[i], 6),
        CO2_offgas=round(x.CO2outgas.y[i], 6),
        Fs=round(x.Fs.y[i], 4),
        Foil=round(x.Foil.y[i], 4),
        Fg=round(x.Fg.y[i], 4),
        pressure=round(x.pressure.y[i], 4),
        discharge=round(x.discharge.y[i], 4),
        Fw=round(x.Fw.y[i], 4),
        Fpaa=round(x.Fpaa.y[i], 4),
        Fa=round(x.Fa.y[i], 4),
        Fb=round(x.Fb.y[i], 4),
        Fc=round(x.Fc.y[i], 4),
        Fh=round(x.Fh.y[i], 4),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", tags=["info"])
def root():
    return {
        "docs": "/docs",
        "endpoints": {
            "batch":   "POST /batch — run a full batch, returns dataframe as JSON",
            "create":  "POST /sessions — start a new interactive session",
            "step":    "POST /sessions/{id}/step — advance one timestep",
            "state":   "GET  /sessions/{id}/state — current observation",
            "delete":  "DELETE /sessions/{id} — end session",
        }
    }


# --- Batch (fire-and-forget) ------------------------------------------------

@app.post("/batch", tags=["batch"])
def run_batch(req: BatchRequest):
    """Run a complete 230-hour batch and return all process variables as JSON."""
    recipe_combo = _build_recipe_combo(req.recipe)
    env = PenSimEnv(recipe_combo=recipe_combo, fast=False)
    (df, _raman), _ = env.get_batches(random_seed=req.random_seed, include_raman=req.include_raman)
    return {
        "num_steps": len(df),
        "columns": list(df.columns),
        "index_hours": list(df.index),
        "data": df.to_dict(orient="list"),
    }


# --- Interactive sessions ---------------------------------------------------

@app.post("/sessions", tags=["interactive"], status_code=201)
def create_session(req: SessionCreateRequest):
    """Reset the environment and return initial observation + a session ID."""
    session_id = str(uuid.uuid4())
    recipe_combo = _build_recipe_combo(req.recipe)
    env = PenSimEnv(recipe_combo=recipe_combo, fast=False)
    env.random_seed_ref = req.random_seed
    _, x = env.reset()

    _sessions[session_id] = {
        "env":          env,
        "recipe_combo": recipe_combo,
        "x":            x,
        "k":            0,
        "total_yield":  0.0,
    }

    obs = _extract_observation(x, 0, 0.0, 0.0, False)
    return {"session_id": session_id, "observation": obs}


@app.post("/sessions/{session_id}/step", tags=["interactive"])
def step(session_id: str, req: StepRequest):
    """
    Advance one timestep (12 min).

    Pass only the variables you want to override — any omitted value falls
    back to the recipe default for this timestep.
    """
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    sess = _sessions[session_id]
    env: PenSimEnv = sess["env"]
    k = sess["k"] + 1

    if k > NUM_STEPS:
        raise HTTPException(status_code=400, detail="Batch already finished — create a new session")

    # Get recipe defaults for this timestep
    t_hours = k * STEP_IN_MINUTES / MINUTES_PER_HOUR
    defaults = sess["recipe_combo"].get_values_dict_at(time=t_hours)

    Fs       = req.Fs       if req.Fs       is not None else defaults[FS]
    Foil     = req.Foil     if req.Foil     is not None else defaults[FOIL]
    Fg       = req.Fg       if req.Fg       is not None else defaults[FG]
    pressure = req.pressure if req.pressure is not None else defaults[PRES]
    discharge= req.discharge if req.discharge is not None else defaults[DISCHARGE]
    Fw       = req.Fw       if req.Fw       is not None else defaults[WATER]
    Fpaa     = req.Fpaa     if req.Fpaa     is not None else defaults[PAA]

    _, x, yield_delta, done = env.step(k, sess["x"], Fs, Foil, Fg, pressure, discharge, Fw, Fpaa)

    sess["x"] = x
    sess["k"] = k
    sess["total_yield"] += yield_delta

    obs = _extract_observation(x, k, yield_delta, sess["total_yield"], done)

    if done:
        del _sessions[session_id]

    return obs


@app.get("/sessions/{session_id}/state", tags=["interactive"])
def get_state(session_id: str):
    """Return the current observation without advancing the simulation."""
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    sess = _sessions[session_id]
    return _extract_observation(sess["x"], sess["k"], 0.0, sess["total_yield"], False)


@app.delete("/sessions/{session_id}", tags=["interactive"], status_code=204)
def delete_session(session_id: str):
    """Tear down a session early."""
    _sessions.pop(session_id, None)


@app.get("/sessions", tags=["interactive"])
def list_sessions():
    return {
        sid: {"step": s["k"], "total_yield": round(s["total_yield"], 4)}
        for sid, s in _sessions.items()
    }

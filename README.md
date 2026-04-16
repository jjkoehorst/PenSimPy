# PenSimPy

Python implementation of [IndPenSim](http://www.industrialpenicillinsimulation.com/) — an industrial-scale Penicillium chrysogenum fermentation simulator, exposed as a REST API.

The simulator runs a **230-hour batch** at **12-minute timesteps (1 150 steps total)**. At each step you supply seven feed variables; the simulator integrates the ODE system and returns the full process state.

---

## Quick start

```bash
docker build -t pensimpy .
docker run -p 8000:8000 pensimpy
```

Interactive API docs → **http://localhost:8000/docs**

---

## The seven control inputs

| Variable   | Description                   | Typical range |
|------------|-------------------------------|---------------|
| `Fs`       | Sugar feed rate (L/h)         | 7 – 151       |
| `Foil`     | Oil feed rate (L/h)           | 21 – 36       |
| `Fg`       | Aeration rate (L/h)           | 29 – 76       |
| `pressure` | Vessel back pressure (bar)    | 0.5 – 1.2     |
| `discharge`| Discharge rate (L/h)          | 0 – 4 100     |
| `Fw`       | Water injection (L/h)         | 0 – 510       |
| `Fpaa`     | Phenylacetic acid rate (L/h)  | 0 – 150       |

Each input is **optional on every step** — omit it to follow the default recipe profile for that timestep.

---

## Recipes

A recipe defines how one control variable changes over time. Each entry says "from this hour onwards, hold this value" — the simulator forward-fills until the next entry.

`time` is in **hours** (0 – 230). You only need to specify the variables you want to override; the rest follow the built-in defaults.

### Default profiles

**`Fs` — Sugar feed rate (L/h)**
```
  3 h →   8 L/h
 12 h →  15 L/h
 24 h → 150 L/h   ← peak growth phase
 28 h →  30 L/h
 80 h → 116 L/h
230 h →  80 L/h
```

**`Foil` — Oil feed rate (L/h)**
```
  4 h → 22 L/h
 16 h → 30 L/h
 56 h → 35 L/h
230 h → 23 L/h
```

**`Fg` — Aeration rate (L/h)**
```
  8 h →  30 L/h
 40 h →  55 L/h
 90 h →  60 L/h
200 h →  75 L/h
230 h →  65 L/h
```

**`pressure` — Back pressure (bar)**
```
 12 h → 0.6 bar
 40 h → 0.9 bar
100 h → 1.1 bar
230 h → 0.9 bar
```

**`discharge` — Discharge rate (L/h)**
```
100 h →    0 L/h
102 h → 4000 L/h   ← pulse out
130 h →    0 L/h
132 h → 4000 L/h   ← pulse out
  … (repeats every ~20 h from h100 to h212)
230 h →    0 L/h
```

**`Fw` — Water injection (L/h)**
```
 50 h →   0 L/h
 75 h → 500 L/h
150 h → 100 L/h
160 h →   0 L/h
170 h → 400 L/h
230 h → 250 L/h
```

**`Fpaa` — Phenylacetic acid feed rate (L/h)**
```
  5 h →  5 L/h
 40 h →  0 L/h
200 h → 10 L/h
230 h →  4 L/h
```

---

## 1 — Simplest possible run

Run a full batch with all defaults. Returns the complete 1 150-row dataframe.

```bash
curl -s -X POST http://localhost:8000/batch \
     -H "Content-Type: application/json" \
     -d '{}' | python3 -m json.tool | head -40
```

---

## 2 — Full batch with a custom recipe

Override only the variables you care about. Everything else stays on the default profile.

```bash
curl -s -X POST http://localhost:8000/batch \
     -H "Content-Type: application/json" \
     -d '{
       "random_seed": 42,
       "recipe": {
         "Fs": [
           {"time": 0,   "value": 20},
           {"time": 50,  "value": 80},
           {"time": 150, "value": 50}
         ],
         "Fg": [
           {"time": 0,   "value": 45},
           {"time": 100, "value": 65}
         ],
         "pressure": [
           {"time": 0, "value": 0.8}
         ]
       }
     }'
```

This holds `Fs` at 20 L/h for the first 50 hours, ramps to 80 L/h, then drops to 50 L/h at hour 150.  
`Fg` starts at 45 L/h and increases to 65 L/h at hour 100.  
`pressure` is fixed at 0.8 bar for the whole batch.  
All other variables (`Foil`, `discharge`, `Fw`, `Fpaa`) follow the default profiles above.

**Response shape**

```json
{
  "num_steps": 1150,
  "columns": ["Volume", "Penicillin Concentration", "Discharge rate", ...],
  "index_hours": [0.2, 0.4, ...],
  "data": {
    "Volume":                   [57976.9, ...],
    "Penicillin Concentration": [0.001,   ...],
    ...
  }
}
```

---

## 3 — Full automation run (step-by-step, external predictor drives every timestep)

Create a session, then call `/step` in a loop. Your predictor sees the process state after each 12-minute step and decides the next action.

```python
import requests

BASE = "http://localhost:8000"

# --- 1. Start a session (optionally pass a custom recipe) ---
resp = requests.post(f"{BASE}/sessions", json={
    "random_seed": 0,
    "recipe": {
        "Fs":  [{"time": 0, "value": 30}, {"time": 100, "value": 60}],
        "Fg":  [{"time": 0, "value": 45}]
    }
})
session_id = resp.json()["session_id"]
obs        = resp.json()["observation"]
print(f"Session {session_id} created — step 0, pH={obs['pH']}")

# --- 2. Run to completion ---
total_yield = 0.0
while not obs["done"]:
    # ── your predictor here ──────────────────────────────────
    action = {
        "Fs":  60.0 if obs["penicillin"] < 0.5 else 40.0,
        "Fg":  50.0 + obs["dissolved_O2"] * 0.5,
        # omit the rest → recipe default is used
    }
    # ─────────────────────────────────────────────────────────

    obs = requests.post(
        f"{BASE}/sessions/{session_id}/step",
        json=action
    ).json()

    total_yield += obs["yield_delta"]
    print(
        f"  step {obs['step']:4d} | "
        f"t={obs['time_hours']:6.1f}h | "
        f"pH={obs['pH']:.2f} | "
        f"T={obs['temperature']:.1f}K | "
        f"pen={obs['penicillin']:.4f} | "
        f"yield_Δ={obs['yield_delta']:.4f}"
    )

print(f"\nFinal yield: {total_yield:.2f}")
```

**Every `/step` response**

```json
{
  "step": 42,
  "time_hours": 8.4,
  "done": false,
  "yield_delta": 0.12,
  "total_yield": 3.84,

  "pH":           6.47,
  "temperature":  298.1,
  "dissolved_O2": 14.8,
  "substrate":    1.12,
  "penicillin":   0.034,
  "volume":       58420.0,
  "vessel_weight":63100.5,
  "O2_offgas":    0.201,
  "CO2_offgas":   0.082,

  "Fs": 60.0,  "Foil": 28.0,  "Fg": 52.3,
  "pressure": 0.8, "discharge": 0.0,
  "Fw": 100.0, "Fpaa": 5.0,
  "Fa": 0.0,   "Fb": 12.4,  "Fc": 0.0001,  "Fh": 140.2
}
```

Session auto-deletes when `done: true`.

---

## 4 — Run the next N minutes only (resume later)

The session persists between calls. You can advance any number of steps, pause, inspect, and resume.

```python
import requests, time

BASE = "http://localhost:8000"
STEP_MINUTES = 12  # one timestep = 12 minutes

def advance_minutes(session_id: str, minutes: int, action: dict = None) -> list:
    """Advance the simulation by `minutes` and return the observations."""
    steps  = round(minutes / STEP_MINUTES)
    result = []
    for _ in range(steps):
        obs = requests.post(
            f"{BASE}/sessions/{session_id}/step",
            json=action or {}   # {} → full recipe defaults for every step
        ).json()
        result.append(obs)
        if obs["done"]:
            break
    return result


# ── Create session ──────────────────────────────────────────
session_id = requests.post(f"{BASE}/sessions", json={"random_seed": 1}).json()["session_id"]
print(f"Created: {session_id}")

# ── Run first 60 minutes ────────────────────────────────────
batch1 = advance_minutes(session_id, 60)
last   = batch1[-1]
print(f"After 60 min  → step {last['step']}, pH={last['pH']:.2f}, pen={last['penicillin']:.4f}")

# ── Pause — inspect, call your model, whatever ──────────────
time.sleep(2)

# ── Run next 120 minutes with a custom Fs ───────────────────
batch2 = advance_minutes(session_id, 120, action={"Fs": 75.0})
last   = batch2[-1]
print(f"After 180 min → step {last['step']}, pH={last['pH']:.2f}, pen={last['penicillin']:.4f}")

# ── Check current state without advancing ───────────────────
state = requests.get(f"{BASE}/sessions/{session_id}/state").json()
print(f"Current state: step={state['step']}, total_yield={state['total_yield']:.4f}")

# ── Run to end ──────────────────────────────────────────────
while not last["done"]:
    batch = advance_minutes(session_id, 60)
    last  = batch[-1]
    print(f"  → step {last['step']:4d} ({last['time_hours']:.0f}h), yield so far: {last['total_yield']:.2f}")

print(f"\nDone. Final yield: {last['total_yield']:.2f}")
```

---

## Other endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET`    | `/`                          | List all endpoints |
| `POST`   | `/batch`                     | Full batch, returns dataframe |
| `POST`   | `/sessions`                  | Create session (reset env) |
| `POST`   | `/sessions/{id}/step`        | Advance one timestep (12 min) |
| `GET`    | `/sessions/{id}/state`       | Read state without stepping |
| `GET`    | `/sessions`                  | List all active sessions |
| `DELETE` | `/sessions/{id}`             | Tear down session early |

---

## Simulator internals

| Parameter | Value |
|-----------|-------|
| Batch length | 230 h |
| Timestep | 12 min (0.2 h) |
| Steps per batch | 1 150 |
| ODE solver | `scipy.integrate.solve_ivp` — Radau (stiff) |
| State variables | 33 (substrate, biomass fractions, penicillin, pH, temperature, DO₂, …) |
| Observed outputs | 18 per step in batch mode; all 21 fields shown in step-mode response |

> **Note on `fastodeint`:** the original C++ ODE backend was removed from GitHub by Quarticai and is no longer available. The simulator uses `scipy`'s Radau solver instead — identical results, roughly 2× slower per batch (~5 min for 1 150 steps).

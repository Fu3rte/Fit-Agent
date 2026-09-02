# Exercises Dataset — Project Research Report

> Research date: 2026 · Repo: `github.com/hasaneyldrm/exercises-dataset` (MIT + media exception)

## 1. What is this project

A **ready-to-use fitness exercise dataset** with **1,324 exercises**, each including:

- A **180×180 thumbnail** (`images/`, 1,324 JPGs)
- A **180×180 animation GIF** (`videos/`, 1,324 GIFs)
- **Step-by-step instructions in 10 languages**: English, Spanish, Italian, Turkish, Russian, Chinese, Hindi, Polish, Korean, French
- Structured metadata: category (body part), target muscle, synergist muscle group, required equipment, etc.

It is the data layer behind **LogPress** (an AI-assisted workout tracker, `github.com/hasaneyldrm/logpress-public`), published separately so it can be dropped straight into your own application.

Repo size: `exercises.json` ≈ 17 MB, images 12 MB, GIFs 126 MB — **~155 MB total**.

## 2. Directory structure

```
exercises-dataset/
├── data/
│   ├── exercises.json        # Main data: 1,324 exercise records (JSON array)
│   └── exercises.schema.json # JSON Schema (Draft 2020-12) for validation
├── images/                   # 1,324 × 180×180 thumbnails (© Gym visual)
├── videos/                   # 1,324 × 180×180 animation GIFs (© Gym visual)
├── index.html                # Interactive exercise browser (client-side, no server)
├── setup.html                # Developer setup guide (DB SQL + API examples + LLM backend generation)
├── NOTICE.md                 # Media attribution & license terms
├── LICENSE                   # MIT (code & data), media excluded
└── README.md                 # Full documentation
```

## 3. Data schema

Each record in `data/exercises.json` has these fields:

| Field | Type | Description |
|---|---|---|
| `id` | string | Unique numeric ID (e.g. `"0001"`) |
| `name` | string | Exercise name (e.g. `"3/4 sit-up"`) |
| `category` / `body_part` | string | Body-part category (both identical) |
| `equipment` | string | Required equipment (`body weight` = none) |
| `instructions.<lang>` | string | Full step-by-step instructions in 10 languages |
| `instruction_steps.<lang>` | array[string] | Same instructions split into ordered steps (10 languages) |
| `muscle_group` | string | Primary synergist muscle group |
| `secondary_muscles` | array[string] | Other muscles involved |
| `target` | string | Primary target muscle (e.g. `"abs"`, `"biceps"`) |
| `media_id` | string | Original media reference ID (e.g. `"2gPfomN"`) |
| `image` / `gif_url` | string | Local thumbnail / GIF path |
| `attribution` | string | Copyright notice `© Gym visual — https://gymvisual.com/` |
| `created_at` | string | ISO 8601 creation timestamp |

Data quality verified: all 1,324 images and 1,324 GIFs referenced by the JSON **exist on disk — zero missing files**.

## 4. Statistics (measured)

- **Total exercises**: 1,324
- **Body parts (category), 10 types**:

| Body part | Count | Body part | Count |
|---|---|---|---|
| Upper Arms | 292 | Shoulders | 143 |
| Upper Legs | 227 | Lower Legs | 59 |
| Back | 203 | Lower Arms | 37 |
| Waist | 169 | Cardio | 29 |
| Chest | 163 | Neck | 2 |

- **Equipment (28 types)**: body weight 325, dumbbell 294, cable 157, barbell 154, leverage machine 81, band 54, smith machine 48, kettlebell 41, weighted 36, stability ball 28, ez barbell 23, plus bosu ball, medicine ball, rope, tire, rower, elliptical, stationary bike, etc. (~25% of exercises need no equipment).
- **Languages**: 10, each with full instructions + step arrays.

## 5. What you can do with it

1. **Backend data layer for fitness / workout-planning apps**: import `exercises.json` directly — images, GIFs, and multilingual copy included.
2. **ML / recommendation systems**: exercise recognition, workout recommendation, health research.
3. **Teaching demos / prototypes**: the interactive browser `index.html` works standalone.
4. **Backend scaffolding**: `setup.html` generates `CREATE TABLE` SQL + all 1,324 INSERT statements for 4 databases (SQL Server / PostgreSQL / MySQL / SQLite), copy-paste API client examples in 7 languages (JS / Python / C# / Java / PHP / Go / cURL — live-updated with your base URL), and a structured prompt to give ChatGPT/Claude/Gemini for generating a complete REST API in one shot (Express.js / FastAPI / ASP.NET Core / Spring Boot / Laravel / Gin).

## 6. How to use

### Way 1: Browse directly (zero setup)

Open `index.html` in a browser: live search, filters by category / equipment / target muscle, infinite-scroll grid, click any card for full details and instructions in 10 languages with the GIF.

Open `setup.html` for DB SQL, API examples, and the LLM prompt (enter your base URL and examples update live).

### Way 2: Python — load and filter

```python
import json

with open("data/exercises.json", "r", encoding="utf-8") as f:
    exercises = json.load(f)

chest = [ex for ex in exercises if ex["category"] == "chest"]  # 163
bodyweight = [ex for ex in exercises if ex["equipment"] == "body weight"]  # 325

ex = exercises[0]
print(ex["name"], ex["instructions"]["zh"])  # Chinese instructions
print(ex["image"], ex["gif_url"])            # local media paths
```

### Way 3: JavaScript / Node.js

```js
const exercises = require("./data/exercises.json");
console.log(exercises.length); // 1324
const bodyweight = exercises.filter(ex => ex.equipment === "body weight");
console.log(exercises[0].instructions.zh); // access any language by key
```

### Way 4: Validate data

Validate the dataset or your own additions against `exercises.schema.json` with any standard JSON Schema validator (e.g. `python -m jsonschema -i data/exercises.json data/exercises.schema.json`).

## 7. License — important

- **Code, dataset structure, instruction text**: MIT — free to use.
- **Media (`images/` and `videos/`)**: owned by **Gym visual** (gymvisual.com); this repo redistributes it at **180×180 only, with the rights holder's written permission**. You must keep the `© Gym visual — https://gymvisual.com/` attribution intact and respect the 180×180 resolution limit; commercial or broader reuse requires your own license per Gym visual's Terms & Conditions (`gymvisual.com/content/3-terms-and-conditions-of-use`). **Cloning this repo is not a media license.**

## 8. Summary

In one line: **a plug-and-play fitness exercise database** — 1,324 exercises × 10 languages, each with a thumbnail and animation GIF, plus two zero-dependency HTML tools (browser + developer setup wizard). Ideal as a ready-made data layer for fitness apps, workout recommendation, or exercise-recognition research; the only caveat is that the media is governed by Gym visual's license terms.
# Lzheng Fitness Skills — Research Report

> Subject: `Lzheng-fitness` repository (v2.3.1, MIT, zh-CN)
> Date: 2026
> Summary: This is a **personal-training Agent Skill suite that runs fully offline**. Seven Skills form a local closed loop — "intake → plan → train → review → return → workbench". Once connected to a Skill-capable agent (Codex / Claude / generic agents), the user only needs to talk in natural language: the AI acts as coach, record-keeper, cycle planner, and system administrator — with no cloud services required at any point.

---

## 1. Project Overview

| Item | Description |
| --- | --- |
| Positioning | Personal-training Agent Skills: program design, strength cycles, workout review, return-to-training, expert knowledge library, system control, offline workbench |
| Runtime requirements | Python ≥ 3.10, **zero third-party dependencies** |
| Install targets | Codex (`~/.codex`), Claude (`~/.claude`), generic agents platforms |
| Deliverable form | 7 Skills (each with `SKILL.md` + references rule docs + scripts + assets templates) + standalone HTML workbench/plan pages |
| Data form | Fully local: Markdown state snapshots/reviews, JSON contracts, single-file HTML; no accounts, no cloud |
| Knowledge base | 6 "source-constrained" distilled training-expert modules + 7 built-in knowledge docs + evidence/source register |
| Code scale | 23 Python scripts, 154 Markdown files, 11 JSON files, including a full pre-release validator |

## 2. Architecture: Three Layers + Single-Source Closed Loop

```
┌─ System layer (control & display)
│   lzheng-training-system         initialize / migrate / upgrade / diagnose / validate / route
│   lzheng-fitness-workbench-builder  build / refresh / migrate / wallpaper swap / publish offline workbench
├─ Professional layer (prescription capabilities, 4)
│   lzheng-fitness-plan            intake → safety screening → classification → full plan → HTML
│   lzheng-strength-cycle-planner  single-lift 8–12 week strength cycle → curve HTML
│   lzheng-strength-training-review single / weekly / rolling / baseline review → next prescription
│   lzheng-training-return          return after 7+ days off / 3+ missed sessions / condition changes
└─ Knowledge layer (shared internal layer)
    lzheng-training-expert-library  6 source-constrained distilled expert modules
                                    + knowledge/ 7 stable-rule docs + source register
```

**Closed-loop mechanism**: every formal artifact (plan JSON/HTML, execution baseline, reviews, state snapshots, return cards) lands in one knowledge-base directory (`个人训练系统/`), with `系统/lzheng-system.json` recording output locations as the **single source of truth**. Prescription Skills emit a `LZHENG_HANDOFF` record on completion; the workbench builder consumes handoffs to refresh the sole `workbench-data` block, guaranteeing that "conversation ↔ plan ↔ baseline ↔ review ↔ workbench" never drift apart.

## 3. What Each of the Seven Skills Does

### 3.1 lzheng-fitness-plan — Personalized Fitness Plan (core entry)
- Two rounds of minimal necessary intake (goal / experience / time / equipment / recovery / limitations), outputting readiness `full / conservative / blocked`;
- **Safety triage**: red-flag symptoms (sharp pain, numbness, radiating pain, syncope, chest discomfort) → halt high-intensity prescription, recommend in-person professional evaluation;
- P0–L3 trainee classification (global + per-lift, two dimensions); machines vs. free weights chosen by suitability, never implying a status hierarchy;
- Generates the full plan plus **30/20/10-minute short versions, off-day version, and 1–2 missed-session return rules** (short versions only de-load, never make up sessions);
- Every lift must have traceable loading: existing records → working weight + progression/regression rules; **no records → written field-calibration steps; "pick your own weight by RPE" is forbidden**;
- Produces a unique `plan_contract` JSON, validated through validate → render → audit before delivering template-fixed HTML;
- Goal-tracking contract: hypertrophy includes double-progression logs, fat loss includes weight/steps/cardio, strength includes cycle verdicts, general fitness includes workout completion.

### 3.2 lzheng-strength-cycle-planner — Single-Lift Strength Cycle
- Builds 8–12 week cycles (accumulation → intensification → realization → deload/validation) for **one** main lift: squat / bench / deadlift / weighted pull-up / overhead press;
- Each exposure has a single duty: top set for calibration + back-off sets for volume + incomplete-session branches (replacement, not extra work);
- Prescriptions use `first-set RPE→last-set RPE` arrow notation; static-RPE writing is banned;
- Multiple main lifts: build cycles separately first, then check same-week fatigue conflicts; a cycle only becomes the current plan after being merged back through the full-plan Skill;
- Delivers a single-file HTML with intensity/volume curves via Fritsch–Carlson monotone cubic interpolation; chart data comes from the session's own data — copying numbers is prohibited.

### 3.3 lzheng-strength-training-review — Training Review
- Precise routing across four modes: in-cycle single session `cycle` / no-cycle rolling progression `rolling` / baseline training `baseline` / weekly phase review `weekly`;
- **Hard gate: ask for subjective feel before finalizing anything**; minimal follow-up on ambiguous external records; recurring problems must be traced to their cause (never just "pay attention next time");
- Single-session review outputs an explicit next prescription; rolling mode changes one variable at a time (increase ≤ smallest increment; decrease 2.5%–7.5% or drop 1 set);
- Cycle/structure changes require a full pending-confirmation proposal first; after confirmation a `-vNN.html` is created — old versions are never overwritten;
- Every review is written to local Markdown, the index is updated, and the workbench refresh is triggered.

### 3.4 lzheng-training-return — Return After a Training Break
- Applies when: ≥7 days off, 3+ consecutive missed sessions, post-illness or approved post-injury recovery, significant condition changes, repeated interruptions within 4 weeks;
- Determines return clearance `normal_return / degraded_return / minimum_return / pause`, then outputs normal/degraded/minimum three-tier tasks + a 48-hour start action + 7-day return card;
- Never copies pre-break weights back directly; when the old plan is missing, marks it `unknown` and outputs a conservative re-evaluation week;
- Medical red flags (chest pain, syncope, unassessed pain, etc.) go through safety triage first, never into ordinary programming.

### 3.5 lzheng-training-expert-library — Expert Library (internal layer)
- Six fully distilled modules: **Alan Aragon** (nutrition/adherence), **Brad Schoenfeld** (hypertrophy), **Brukner & Khan** (approved rehab progression), **Dan John** (back-to-basics), **Eric Helms** (program structure/deload), **Greg Nuckols** (strength plateaus/periodization);
- Each module carries source/version boundaries, coverage matrix, question routing, knowledge cards, duty boundaries, and an honest validation status;
- Selection protocol: 1 expert by default, more only for independent variables or genuine conflicts; experts supply source-constrained judgments only — **they never own current facts, final prescriptions, or write access**;
- Copyright boundaries: no full-book/raw-article reproduction, no impersonation of the experts, no substitution for medical guidelines.

### 3.6 lzheng-training-system — System Control
- Actions: `bootstrap` (empty-dir init) / `doctor` (diagnostics) / `upgrade` (protected upgrade) / `install-skill` / `import-private-pack` / `process-handoffs` / `validate` (full regression);
- First use is guided by the agent in natural language — the user never needs to find a README or remember commands;
- Upgrades only touch managed config; user-modified files are preserved with a conflict report; private data is never overwritten;
- The whole directory auto-migrates old absolute paths after move/rename/cross-drive relocation.

### 3.7 lzheng-fitness-workbench-builder — Fitness Workbench
- Assembles a **single-file responsive offline workbench** (desktop/tablet/phone, zero online dependencies) from plan JSON + execution baseline + reviews + optional Notion export;
- One view template + one data block (schema 6); the AI can only refresh data, never hand-write a second page;
- Week transitions are atomically synced: real 7-day dates covering today, uniform Wn, session count matching frequency, a prescription for today, and bodyweight not overwritten by historical loads — five checks that reject on failure;
- Supports wallpaper replacement (static image / image+MP4 dynamic background, with automatic backup & rollback), path-portability repair, release copies, and an optional Obsidian edit entry.

## 4. Core Mechanisms & Guardrails (design highlights)

| Mechanism | Practice |
| --- | --- |
| **No fabrication** | Weights/frequency/records must trace back to user facts, calibration, or documented estimates; anything missing is explicitly marked `unknown / needs_baseline / assumption` |
| **Fact priority** | User's current confirmation > authorized verifiable current records > time snapshot > history; old PRs, sample data, and other people's plans can never masquerade as current facts |
| **Source discipline** | Current official safety standards > multi-source consistent evidence > built-in knowledge; every source actually used is written into `knowledge_sources` |
| **No-overwrite principle** | Snapshots, plans, and reviews are all versioned; substantive revisions use `-v02/-v03`; the installer refuses to overwrite existing Skills, backing up before `--force` |
| **Fixed visual contract** | Only three templates registered repo-wide (plan page / cycle page / workbench); the AI may not change navigation, themes, or hand-write pages |
| **Safety boundary** | No diagnosis, no rehab; red flags halt high-intensity work and recommend in-person evaluation; fitness terms are explained in plain language on first use |
| **Privacy** | The public package contains no personal data, absolute paths, or secrets; `validate_bundle.py` scans for drive-letter paths, `/home/*`, project names, OpenAI keys, etc. |
| **Acceptance flow** | Every formal reply follows a fixed format: current result / system judgment / specifics / file list / sync status / pending confirmations / next step; replying only "done" is forbidden |
| **Portability** | UTF-8 everywhere, Chinese and space-containing paths supported; `doctor`/`upgrade` still pass after move/rename |

## 5. Installation

```bash
# One-line install of all 7 Skills into Codex (also: --platform claude / agents, --target-root for isolated dirs)
python tools/install.py --platform codex --all
```

Pre-release / post-upgrade regression: `python tools/validate_bundle.py` (checks metadata, links, privacy residue, script syntax, expert modules, HTML rendering, and initialization/migration/fault-interception).

## 6. Capabilities After Connecting to an Agent

**End-user experience**: open a fresh AI conversation, say one line —「开始建立我的健身系统」(start building my fitness system) — and everything from there is natural language (beginners never read a README, never learn jargon, never memorize commands):

| Scenario | You say | The AI does |
| --- | --- | --- |
| Intake | "I mainly want to build muscle" | Minimal necessary intake → state snapshot → P0–L3 classification → safety screening |
| Weight calibration | no training records | Per-lift: machine setup, trial-weight steps, pass criteria, stop conditions — **no guessing** |
| Full plan | "make a plan for my schedule" | Full-plan JSON + one-shot delivery of standard / 30 / 20 / 10-minute tiers + offline HTML |
| First / daily session | "what do I train today" | Today's prescription, step-by-step coaching per lift, time-short version, not-to-do list |
| Post-session review | "done — bench 60kg 4×6" | Check against the plan → ask subjective feel → judge lift by lift → **explicit next prescription** → write review, refresh workbench |
| Weekly review | "summarize this week" | Week goals passed/pending, root-cause follow-up on recurring issues, next week's verification focus, structure changes confirmed first |
| Return to training | "I skipped two weeks" | Return-clearance judgment, three-tier tasks, 7-day return card, 48-hour minimal action |
| Strength specialization | "I want a bigger bench" | 8–12 week cycle + top/back-off sets, branches, deload + curve HTML, merged back into the full plan |
| New computer / migration | "move me to a new machine" | bootstrap + doctor full check, automatic path migration, workbench replica |
| System trouble | "a workbench button is broken" | Path inventory → repair → full move regression; no blind patching of a single entry |
| Wallpaper swap | hand over an image / image+MP4 | Automatic backup, page update, rollback on failure |
| Knowledge questions | "what's the most effective nutrition/hypertrophy approach" | Source-constrained judgment from the matching expert module via the selection protocol, with validation status and disagreements disclosed |

**For developers/maintainers**: one-command install with full regression validation; knowledge/expert library updateable independently; all Skills installable standalone (the 4 professional Skills + expert library are `standalone: true`); example prompts, a sample cycle JSON, and a CloudBase free static-hosting tutorial ship in-repo for secondary development and client delivery.

## 7. Boundaries & Limitations (honest notes)

- **Not medicine**: no diagnosis, rehab, or risk assessment; medical issues are routed to in-person professionals;
- **Not a course**: knowledge comes from literature distillation and official guidelines; it does not impersonate expert opinions and never lets old books override current guidelines;
- **Not a real-time coach**: no wearable/sensor integration; training data comes from the user's own reporting (no unauthorized writes to external services — Notion is optional read-only input);
- **Fixed workbench UI**: visuals are strictly bound by the three contract templates; deep customization means editing the source templates and re-generating the desensitized template;
- **Single user**: the public package contains no third-party data; client mode uses only code names and desensitized material.

## 8. Conclusion

The suite's value is not "handing the AI fitness knowledge" — it is **turning training into a verifiable engineering loop**: mandatory intake and calibration solve "AI making up weights"; fact priority and source registration solve "AI talking nonsense from stale data"; versioning and fixed templates solve "files getting messier every edit"; the handoff protocol and single source of truth solve "plan / review / workbench fighting each other"; the installer, validator, and portability solve "falling apart on a new machine". Hooked into any Agent Skills platform, the user gets a **local private coach that speaks plainly, remembers every session, knows exactly what to lift next, proactively asks how you feel, and gets you safely back on track after a break**.

---

*This report is based on the repository's README.md, lzheng-fitness.manifest.json, the 7 SKILL.md files, SYSTEM-FLOW.md, knowledge/00-lzheng-knowledge-map.md, and tools/validate_bundle.py.*
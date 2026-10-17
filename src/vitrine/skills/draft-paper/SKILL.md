---
name: draft-paper
description: Draft a research paper skeleton from a vitrine study's provenance trail — decisions, annotations, scripts, tables, and plots become an IMRAD manuscript with auto-generated Methods and supplementary appendices.
tier: community
category: system
---

# Draft Research Paper

Assemble a paper skeleton from a vitrine study's full provenance trail. The output is an IMRAD manuscript where Methods are auto-generated from the decision trail and Results reference actual study outputs. Sections requiring domain expertise are marked with `[PLACEHOLDER]` tags.

## When to Use This Skill

- Researcher wants to turn a completed study into a manuscript draft
- Researcher wants a structured starting point for a journal submission
- Researcher wants to document the study in publication-ready format

## Setup

```python
from vitrine import list_annotations, register_output_dir, show, study_context
from pathlib import Path

STUDY = "<study-label>"  # the study label given with this task
# The output directory given with this task, or None if it gave none
OUTPUT_DIR = "<output-directory>"
output_dir = Path(OUTPUT_DIR) if OUTPUT_DIR else register_output_dir(study=STUDY)
paper_dir = output_dir / "paper"
```

Call `register_output_dir()` only when the task gave no output directory. Without a path it points the study at a new, empty `output/` folder, cutting it off from the directory that holds its scripts and data.

Write everything this skill creates inside `paper_dir`. If it already exists, ask the researcher whether to overwrite it or use a new folder such as `paper-2/`; it may hold an earlier draft they have edited. Then create it with `paper_dir.mkdir(parents=True, exist_ok=True)`.

## Procedure

### Step 1: Parse Researcher Instructions

The researcher's request may name:
- **Venue**: journal name or conference (affects length, style, formatting)
- **Paper type**: full paper, brief communication, methods paper, letter
- **Framing**: specific angle or narrative emphasis
- **Word limit**: target length constraint

If no venue is specified, default to a standard full-length clinical research paper (~3500 words body).

### Step 2: Gather Context

Call `study_context(STUDY)` and extract:
- `cards` — all cards with `card_id`, `title`, `type`, `preview`
- `decisions_made` — all decisions with outcomes, rationale, timestamps
- `pending_responses` — any unresolved blocking cards (note these in the paper)

Call `list_annotations(study=STUDY)` for the researcher's notes on cards (newest first).

Read these files from `output_dir`:
- `PROTOCOL.md` — study protocol (research question, design, analysis plan)
- `RESULTS.md` — study results and conclusions
- `REPORT.md` — compiled research report (if available)
- All `.py` files in `scripts/` — read each to understand the analysis pipeline
- List all files in `plots/` — these become figures

### Step 3: Write `paper.md`

Create `paper.md` in `paper_dir` with this IMRAD structure:

```markdown
# <Title derived from study description>

**Authors:** [PLACEHOLDER: author names and affiliations]

## Abstract

[Generate from actual results. Include: background (1-2 sentences), objective,
methods summary, key results with real numbers from the study, conclusion.
Target 250 words.]

## 1. Introduction

[PLACEHOLDER: opening paragraph — clinical significance and epidemiology]

[Study motivation — derive from study description and PROTOCOL.md. What gap
does this study address?]

[PLACEHOLDER: literature review — 2-3 paragraphs on prior work, cite with [TO CITE]]

[Study objective — clearly state the research question from the protocol]

## 2. Methods

### 2.1 Study Design and Setting

[Auto-generate from protocol and decision trail: study type (retrospective
cohort, case-control, etc.), database used, time period, IRB/ethics statement]

### 2.2 Study Population

[Auto-generate from cohort definition script and decisions: inclusion criteria,
exclusion criteria, sample size. Reference each decision that shaped the cohort.]

### 2.3 Variables

[Auto-generate from scripts and decisions: primary outcome, exposures,
covariates. Each variable definition should trace to a specific script or
decision card.]

### 2.4 Statistical Analysis

[Auto-generate from analysis scripts: describe each analytical step in the
order the scripts were executed. Reference the actual methods used in code.]

### 2.5 Sensitivity Analyses

[If any scripts or decisions relate to sensitivity/robustness checks, describe
them here. Otherwise: "No pre-specified sensitivity analyses were conducted."]

## 3. Results

### 3.1 Study Population

[Auto-generate from cohort table/script outputs: enrollment flow, final sample
size, baseline characteristics summary with actual numbers.]

### 3.2 Primary Outcome

[Auto-generate from results cards and RESULTS.md: state the primary finding
with actual numbers, confidence intervals, p-values as available.]

### 3.3 Secondary Outcomes

[Auto-generate from additional result cards. If none, omit this section.]

**Tables and Figures:**
- Reference each TABLE card as "Table N" and each plot as "Figure N"
- Include actual numbers from table previews
- For plots, describe what they show and reference `figures/<filename>`

## 4. Discussion

[Key findings — auto-generate 1 paragraph summarizing main results]

[PLACEHOLDER: comparison with prior literature — 2-3 paragraphs]

[PLACEHOLDER: potential mechanisms — 1-2 paragraphs]

### Strengths and Limitations

[Auto-generate strengths from: dataset size, validated scores used, decisions
made. Auto-generate limitations from: retrospective design, single-center,
missing data, any caveats noted in annotations.]

### Conclusions

[Auto-generate from RESULTS.md conclusions or final result cards]

## References

See `references.bib` for citation entries. [TO CITE] markers in text indicate
where citations should be inserted.

---

## Appendix A: Decision Log

| # | Decision | Choice | Rationale | Timestamp |
|---|----------|--------|-----------|-----------|
[Auto-generate from decisions_made — one row per decision card, chronological]

## Appendix B: Analysis Scripts

[For each script in scripts/, include the full source in a fenced code block]

### 01_script_name.py
​```python
<full source>
​```

## Appendix C: Researcher Annotations

| Card | Annotation | Timestamp |
|------|------------|-----------|
[Auto-generate from annotations on cards — chronological order]

## Appendix D: Data Provenance

- **Dataset:** [from study context]
- **Access date:** [from study metadata timestamps]
- **Inclusion/exclusion trail:** [summary of cohort filtering decisions]
- **Derived tables used:** [list any referenced derived tables]
- **Software:** M4, Python, DuckDB
```

### Step 4: Write Supporting Files

Write these next to `paper.md` in `paper_dir`.

**`abstract.md`** — The abstract section extracted as a standalone file (for submission systems that require it separately).

**`references.bib`** — BibTeX entries for:
- The dataset (MIMIC-IV, eICU, or custom dataset — use the canonical citation)
- Any scoring systems used (SOFA, APACHE, SAPS, KDIGO, etc.)
- M4 software citation
- Add `% [TO CITE]` comment entries as placeholders for literature references

**`build.sh`** — Shell script for converting paper.md to publication formats:
```bash
#!/bin/bash
# Build paper from markdown to PDF/Word/LaTeX
# Requires: pandoc, a LaTeX distribution (for PDF)

# PDF (via LaTeX)
pandoc paper.md -o paper.pdf --pdf-engine=xelatex --citeproc --bibliography=references.bib

# Word (for journal submission)
pandoc paper.md -o paper.docx --citeproc --bibliography=references.bib

# LaTeX (for manual editing)
pandoc paper.md -o paper.tex --citeproc --bibliography=references.bib
```

**`README.md`** — Next steps document:
```markdown
# Paper Draft — <Study Title>

## Status
Draft generated from vitrine study `<study-label>`.

## Placeholder Inventory
- [ ] Author names and affiliations
- [ ] Introduction: clinical significance paragraphs
- [ ] Introduction: literature review (N [TO CITE] markers)
- [ ] Discussion: comparison with prior literature
- [ ] Discussion: potential mechanisms
- [ ] All [TO CITE] markers replaced with real citations
- [ ] References.bib completed with literature entries

## Files
- `paper.md` — main manuscript
- `abstract.md` — standalone abstract
- `references.bib` — bibliography (dataset + scoring citations included)
- `figures/` — figures copied from study plots
- `build.sh` — pandoc build script (PDF/Word/LaTeX)

## Building
```bash
chmod +x build.sh && ./build.sh
```
```

### Step 5: Assemble Figures

Copy the study's plot files into `paper_dir / "figures"`. From `paper_dir`:
```bash
mkdir -p figures
cp ../plots/*.png figures/ 2>/dev/null || true
cp ../plots/*.svg figures/ 2>/dev/null || true
```

### Step 6: Report Back

Post a summary card with `show(summary, title="Paper Draft", study=STUDY)` and give the researcher the same summary:

```
## Paper draft complete

**Folder:** <paper_dir>

**Files created:**
- paper.md (N words)
- abstract.md
- references.bib (N entries)
- figures/ (N files)
- build.sh
- README.md

**Placeholder inventory:**
- N [PLACEHOLDER] sections requiring human input
- N [TO CITE] markers requiring citations

**Next steps:**
1. Review paper.md and fill [PLACEHOLDER] sections
2. Complete references.bib with literature citations
3. Run `build.sh` to generate PDF/Word output
```

## Critical Rules

1. **Real numbers only.** Every statistic in the paper must come from actual study outputs (table previews, RESULTS.md, script outputs). Never fabricate numbers.

2. **[PLACEHOLDER] for unknowns.** Anything requiring domain expertise (literature review, mechanism discussion, clinical significance) gets a `[PLACEHOLDER: reason]` tag. The researcher fills these in.

3. **[TO CITE] for references.** Every claim that needs a citation gets `[TO CITE]`. The references.bib file has stubs for these.

4. **Decision trail IS the Methods.** Every `confirm()` or `ask()` decision becomes a justified methods choice. This is the key differentiator — no other tool can auto-generate Methods from structured decision provenance.

5. **Scripts ARE the analysis.** The appendix includes full script source. Methods section describes what scripts do in prose.

6. **Venue adaptation.** Brief communications: shorter intro/discussion, tighter abstract. Methods papers: expanded methods, reproducibility emphasis. Letters: minimal background, focus on novel finding.

7. **Leave the study files alone.** Read from `output_dir`, write only inside `paper_dir`. Never modify the study's scripts, data, plots or markdown files.

8. **Supplementary appendices.** The decision log, full scripts, annotations, and data provenance appendices are a differentiator. They align with STROBE/CONSORT transparency requirements. Always include them.

---
name: create-presentation
description: Create a tailored slide deck (.pptx) from a vitrine study's provenance trail — decisions, annotations, scripts, and plots become presentation slides with speaker notes, adapted for the target audience.
tier: community
category: system
---

# Create Presentation

Build a slide deck from a vitrine study's full provenance trail. The output is a `.pptx` file generated via python-pptx, with embedded plots, speaker notes containing full narrative, and `[PLACEHOLDER]` markers where domain expertise is needed. The deck is tailored to the target audience.

## When to Use This Skill

- Researcher needs slides for a lab meeting, conference talk, thesis defense, or grant review
- Researcher wants a structured starting point for a presentation
- Researcher wants to communicate study findings to a specific audience

## Procedure

### Step 1: Parse Researcher Instructions

The `additional_prompt` (in the dispatch context below) may contain:
- **Audience**: lab meeting, conference talk, thesis committee, grant review, departmental seminar
- **Time limit**: presentation duration (affects slide count)
- **Emphasis**: specific results, methods detail, or narrative angle
- **Conference/venue**: name of conference or meeting

If no audience is specified, default to a **research group / lab meeting** (~15 minutes, 10-15 slides).

### Step 2: Determine Deck Structure

Adapt the slide count and content depth to the audience:

**Lab meeting** (10-15 slides):
- Background (1-2), Research question (1), Methods detail (2-3), Results with all plots (3-5), Decision rationale (1-2), Next steps (1), Discussion points (1)

**Conference talk** (12-18 slides):
- Title (1), Background/motivation (2-3), Research question (1), Methods overview (1-2), Key results (3-5), Implications (1-2), Limitations (1), Conclusions (1), Acknowledgments (1)

**Thesis committee** (15-25 slides):
- Title (1), Overview/aims (1), Background (2-3), Methods comprehensive (3-5), All results (4-8), Decision trail (1-2), Limitations & future work (1-2), Timeline (1), Conclusions (1)

**Grant review** (8-12 slides):
- Title (1), Significance (1-2), Innovation (1-2), Preliminary data / key results (2-4), Approach overview (1-2), Next steps / future aims (1), Summary (1)

### Step 3: Gather Context

Read the study context JSON provided below. Extract:
- `cards` — all cards with `card_id`, `title`, `type`, `preview`
- `decisions_made` — all decisions with outcomes, rationale, timestamps
- `pending_responses` — any unresolved blocking cards (note these in the slides)

Read these files from the output directory (copies are available in the workspace):
- `PROTOCOL.md` — study protocol (research question, design, analysis plan)
- `RESULTS.md` — study results and conclusions
- `REPORT.md` — compiled research report (if available)
- All `.py` files in `scripts/` — read each to understand the analysis pipeline
- List all files in `plots/` — these become slide figures

### Step 4: Write `generate_slides.py`

Create `generate_slides.py` in the workspace. The script must:

1. **Import and install**: Start with `python-pptx` import (installed at runtime)
2. **Professional styling**:
   - Slide dimensions: widescreen 16:9 (13.333 x 7.5 inches)
   - Header font: Calibri Bold, 28pt, dark blue (#1B3A5C)
   - Body font: Calibri, 18pt, dark gray (#333333)
   - White background with subtle accent colors
   - Consistent margins and alignment
3. **Title slide**: Study title, author placeholder, date, institution placeholder
4. **Content slides**: Each with a clear heading, concise bullet points (max 5-6 per slide), and speaker notes
5. **Figure slides**: Embed PNG plots directly from `plots/` directory using `add_picture()`. Size figures prominently (centered, max width ~10 inches). Add a descriptive caption below.
6. **Speaker notes**: Every slide gets detailed speaker notes explaining what to say — this is the full narrative. Include actual numbers from the study, decision rationale, and talking points.
7. **Placeholders**: Use `[PLACEHOLDER: reason]` in slide text or notes where domain expertise is needed (e.g., literature context, clinical significance)
8. **Real numbers only**: Every statistic must come from actual study outputs (RESULTS.md, table previews, script outputs). Never fabricate numbers.
9. **Save** to `presentation.pptx` in the workspace directory

Example script structure:
```python
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pathlib import Path
import os

prs = Presentation()
prs.slide_width = Inches(13.333)
prs.slide_height = Inches(7.5)

# Color constants
DARK_BLUE = RGBColor(0x1B, 0x3A, 0x5C)
DARK_GRAY = RGBColor(0x33, 0x33, 0x33)
LIGHT_GRAY = RGBColor(0x66, 0x66, 0x66)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
ACCENT = RGBColor(0x2E, 0x75, 0xB6)

def add_title_slide(title, subtitle=""):
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
    # ... add shaped text boxes with styling
    return slide

def add_content_slide(title, bullets, notes=""):
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
    # ... header bar, bullet text, speaker notes
    return slide

def add_figure_slide(title, image_path, caption="", notes=""):
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
    # ... header, centered image, caption, speaker notes
    return slide

# Build slides...
# ...

prs.save("presentation.pptx")
print("Saved presentation.pptx")
```

### Step 5: Run the Script

Execute the following:
```bash
pip install python-pptx && python generate_slides.py
```

Verify the output file exists and report its size.

### Step 6: Stream Progress

Output progress to stdout as markdown (standard dispatch behavior). Structure output with clear headings so the researcher sees progress in the agent card:

```
## Analyzing study context...
[summary of cards, decisions, plots found]

## Planning deck structure...
[audience type, slide count, outline]

## Writing slide generator...
[key slides being created]

## Generating presentation...
[running python-pptx script]

## Presentation complete

**File:** presentation.pptx (N slides)
**Audience:** [audience type]
**Plots embedded:** N figures

**Slide outline:**
1. Title
2. Background
...

**Placeholder inventory:**
- N [PLACEHOLDER] items requiring human input

**Next steps:**
1. Open presentation.pptx and review slides
2. Fill in [PLACEHOLDER] sections with domain expertise
3. Adjust styling/branding to your institution
4. Practice with speaker notes
```

## Critical Rules

1. **Real numbers only.** Every statistic on a slide must come from actual study outputs (table previews, RESULTS.md, script outputs). Never fabricate numbers.

2. **[PLACEHOLDER] for unknowns.** Anything requiring domain expertise (literature context, clinical significance, institutional branding) gets a `[PLACEHOLDER: reason]` marker. The researcher fills these in.

3. **Speaker notes are the narrative.** Every slide must have speaker notes that tell the presenter exactly what to say. Include actual numbers, decision rationale, and transitions to the next slide.

4. **Decision trail tells the story.** For lab meetings and thesis committees, the decision trail is a key part of the narrative — why choices were made, what alternatives were considered.

5. **Plots are primary evidence.** Embed every relevant plot as a full figure slide. Plots should be large and legible. Add descriptive captions and detailed speaker notes explaining what the plot shows.

6. **Audience adaptation.** A conference talk emphasizes the story arc and key result. A lab meeting goes deep on methods and decisions. A thesis committee wants comprehensive coverage. A grant review focuses on significance and innovation.

7. **Workspace is a copy.** The output directory contents are copies — you can freely read them. Write new files (generate_slides.py, presentation.pptx) alongside the copies. Do not modify the copied study files.

8. **python-pptx at runtime.** Install python-pptx with pip before running the script. Do not assume it is pre-installed.

# START-HERE.md — Kit ko use karne ka tareeqa

## Setup (5 minutes)

```bash
# 1. Project folder banayein
mkdir bank-recon-app && cd bank-recon-app

# 2. Is kit ke files copy karein is folder mein:
#    CLAUDE.md, REQUIREMENTS.md, .claude/ folder, skills/ folder

# 3. Python environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install streamlit pandas pdfplumber rapidfuzz openpyxl pytest

# 4. Git init (recommended)
git init && git add -A && git commit -m "chore: project kit setup"

# 5. Claude Code start karein
claude
```

## Pehla Prompt (copy-paste karein Claude Code mein)

```
Read CLAUDE.md and REQUIREMENTS.md fully. Then implement Milestone M1:
the StandardTransaction schema, shared parse_amount/parse_date helpers,
the generic CSV/Excel parser with fuzzy column detection, and the ledger
parser with configurable column mapping. Use the parser-agent subagent.
Write pytest tests with synthetic fixtures and run them before finishing.
```

## Milestone-by-Milestone Prompts

**M2 (matching engine):**
```
Implement Milestone M2 per REQUIREMENTS.md using the matcher-agent subagent.
Follow skills/reconciliation-matcher/SKILL.md exactly, including all 8
required test cases. Run pytest -q and show me the results.
```

**M3 (UI):**
```
Implement Milestone M3 using the ui-agent subagent: the full Streamlit
interface per CLAUDE.md's UI Requirements section. Wire it to the existing
parsers and matching engine. Then tell me how to run it locally.
```

**M4 (export):** `Implement M4: the color-coded Excel export via ui-agent.`

**M5 (bank adapters):**
```
Implement M5 using parser-agent: HBL, UBL, and Meezan PDF adapters.
I will provide a real sample statement for each — first create the adapter
skeleton and tests with a synthetic fixture, then I'll test with real files.
```

**M6 (LLM toggle):**
```
Implement M6: the optional LLM description-matching hook described in
skills/reconciliation-matcher/SKILL.md (LLM Hook section). Use the Google
Gemini API (google-generativeai / Google API key). It must only re-rank pairs
already shortlisted by the deterministic engine, and the toggle stays off by
default.
```

## Tips

- Har milestone ke baad `git commit` karwayein — Claude khud kar dega agar aap bolein.
- Real bank statements test karte waqt: pehle account number/naam mask kar lein ya Claude ko bolein fixture anonymize kare.
- Agar koi bank ka format ajeeb ho: Claude ko statement ka screenshot ya first 20 rows dikhayein aur bolein "add a new adapter for this using parser-agent".
- Rapid Tyres ka actual data pilot ke liye best hai — M3 complete hote hi try karein.

"""Compile one Markdown knowledge file for a Claude Co-Work project.

    python scripts/build_cowork_file.py

docs/COWORK_BRIEF.md is hand-written and carries the current state, so refresh it first.
Everything else is copied from the memory folder, CLAUDE.md, playbooks and docs. The build
stops rather than write a file containing anything that looks like a credential.
"""

import ast
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEMORY = Path.home() / ".claude/projects/c--Users-Q-SiftStack/memory"
OUT = ROOT / "output" / f"SiftStack_CoWork_Master_{date.today().isoformat()}.md"

CURATIVE = "Curative Title Playbook"
NEPQ = "NEPQ Sales Transcripts/_MASTER_PLAYBOOK"
PARTS = [
    ("Part 2: Backend and system reference", [("CLAUDE.md", "Main system reference")]),
    ("Part 3: Curative title", [
        (f"{CURATIVE}/DEAL_STRUCTURES_AND_DEFECTS.md", "Six offer structures, six defects (feeds the audit Notes)"),
        (".claude/skills/curative-tax-payer-audit/SKILL.md", "The 3-gate curative audit procedure"),
        (f"{CURATIVE}/CURATIVE_TITLE_PLAYBOOK.md", "Alejandro Mendoza playbook"),
        (f"{CURATIVE}/LOGAN_FULLMER_CURATIVE_TITLE_PLAYBOOK.md", "Logan Fullmer playbook"),
        (f"{CURATIVE}/TX_CURATIVE_LAW_REFERENCE.md", "Texas curative law reference (not legal advice)"),
        (f"{CURATIVE}/CURATIVE_TITLE_GO_TO_MARKET_DATASIFT.md", "Go-to-market spec on DataSift (not built live)"),
        ("docs/TAX_PAYER_HISTORY_FEASIBILITY.md", "Who-paid-the-taxes sources by county"),
    ]),
    ("Part 4: NEPQ sales system", [
        (f"{NEPQ}/NEPQ_INNER_CIRCLE_INTEGRATION.md", "AUTHORITATIVE where NEPQ documents disagree"),
        (f"{NEPQ}/NEPQ_GRADING_RUBRIC.md", "Call grading rubric"),
        (f"{NEPQ}/NEPQ_REP_TRAINING_GUIDE.md", "Rep hiring, onboarding, coaching"),
        (f"{NEPQ}/NEPQ_GAP_CLOSURE_SUPPLEMENT.md", "Gap closure supplement"),
        (f"{NEPQ}/NEPQ_SHORTS_ADDENDUM.md", "Shorts addendum"),
        (f"{NEPQ}/NEPQ_MASTER_PLAYBOOK.md", "Master playbook"),
    ]),
    ("Part 5: Novation", [("Novation King Playbook/NOVATION_PLAYBOOK.md", "Novation playbook")]),
    ("Part 6: Operations and build plans", [
        ("docs/CASE_TRACKING_FEASIBILITY.md", "Case tracking study"),
        ("docs/BELL_PORTAL_MIGRATION.md", "Bell CAD portal migration"),
        ("docs/TY_AGENT_PORT_PLAN.md", "Agent port plan and the API decision"),
        ("docs/AGENT-MAP.md", "UPSTREAM agent org chart, build 1.0.44; not what runs here"),
    ]),
    ("Part 8: Company structure (Kessair)", [
        ("Kessair_Shared_Drive_Blueprint.md", "Holding company and Drive blueprint"),
        ("Kessair_Drive_Organization_Standard.md", "Drive organization standard"),
        ("Kessair_Full-Folder-Structure.md", "Full folder structure"),
        ("Kessair_Naming-Convention_Cards.md", "Naming conventions"),
    ]),
]

REDACTIONS = [
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b(?!\.\d)"), "[operator IP]"),
    (re.compile(r"amex \.\.\.\d{4}", re.I), "saved card"),
    (re.compile(r"pay amex", re.I), "pay by saved card"),
]
SECRET = re.compile(
    r"apify_api_\w{10,}|\bsk-[A-Za-z0-9_-]{20,}|\beyJ[A-Za-z0-9_-]{20,}\.|xox[bp]-\w+"
    r"|hooks\.slack\.com/services/T\w+|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY"
    r"|(?:api[_-]?key|token|password|secret)\s*[:=]\s*[\"']?[A-Za-z0-9_\-/+]{16,}", re.I)
VERSION_NUMBER = re.compile(r"\b1\.\d\.\d{1,3}\b")


def clean(text: str) -> str:
    for pat, repl in REDACTIONS:
        text = pat.sub(lambda m: m.group(0) if VERSION_NUMBER.fullmatch(m.group(0)) else repl, text)
    return text


def strip_frontmatter(text: str) -> tuple[str, dict]:
    meta = {}
    m = re.match(r"---\n(.*?)\n---\n?", text, re.S)
    if m:
        key = ""
        for line in m.group(1).splitlines():
            if line[:1].isspace() and key:   # continuation of a folded value
                meta[key] = f"{meta[key]} {line.strip()}".strip()
                continue
            key, _, v = line.partition(":")
            meta.setdefault(key, v.strip().strip('"'))
        text = text[m.end():]
    return text, meta


def shift_headings(text: str, by: int) -> str:
    """Nest a document's headings under its Part, leaving code blocks alone."""
    out, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and (m := re.match(r"(#{1,6})(\s.*)", line)):
            line = "#" * min(len(m.group(1)) + by, 6) + m.group(2)
        out.append(line)
    return "\n".join(out)


def modified(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()


def document(rel: str, label: str) -> str:
    path = ROOT / rel
    body, _ = strip_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    return f"## {path.stem}\n\n*{label}. Source `{rel}`, last changed {modified(path)}.*\n\n{shift_headings(body, 2)}\n"


def skill_catalog() -> str:
    rows = []
    for skill in sorted([*(ROOT / "skills").glob("*/SKILL.md"), *(ROOT / ".claude/skills").glob("*/SKILL.md")]):
        _, meta = strip_frontmatter(skill.read_text(encoding="utf-8", errors="replace"))
        rows.append(f"- **{skill.parent.name}:** {(meta.get('description') or 'no description').strip('>|- ')[:300]}")
    return ("# Part 7: Skill library\n\nThe skills themselves aren't copied here. They're listed so you know what exists; "
            "the packaged copies in `Skills for REI/improved/` are older than the installed library.\n\n" + "\n".join(rows) + "\n")


def code_map() -> str:
    rows = []
    for py in sorted([*(ROOT / "src").glob("*.py"), *(ROOT / "src/scrapers").glob("*.py")]):
        if py.name.startswith("__"):
            continue
        try:
            doc = ast.get_docstring(ast.parse(py.read_text(encoding="utf-8", errors="replace"))) or ""
        except SyntaxError:
            doc = ""
        rows.append(f"- `{py.relative_to(ROOT / 'src').as_posix()}`: {doc.splitlines()[0][:160] if doc else '(no description)'}")
    return "## Code map\n\nEvery module in `src/`, from its own description. The code itself isn't in this file.\n\n" + "\n".join(rows) + "\n"


def memory_part() -> str:
    order = ("feedback_", "incident_", "user_", "project_", "reference_", "dataset_")
    files = sorted((f for f in MEMORY.glob("*.md") if f.name != "MEMORY.md"),
                   key=lambda f: (next((i for i, p in enumerate(order) if f.name.startswith(p)), 9), f.name))
    out = ["# Part 9: Working notes\n\nThe full memory, one dated note per fact: the owner's rules first, then projects, then "
           "reference. Notes describe the day they were written, so a later note or Part 1 wins. `[[name]]` points to another note.\n"]
    for f in files:
        body, meta = strip_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
        out.append(f"## {meta.get('name') or f.stem}\n\n*{meta.get('description', '')} Last changed {modified(f)}.*\n\n{shift_headings(body.strip(), 2)}\n")
    return "\n".join(out)


def main() -> int:
    today = date.today().isoformat()
    sections = [f"# SiftStack and Kessair: Master Knowledge File\n\nCompiled {today}.\n",
                "# Part 1: Start here\n\n" + shift_headings((ROOT / "docs/COWORK_BRIEF.md").read_text(encoding="utf-8"), 1)]
    for title, docs in PARTS:
        sections.append(f"# {title}\n")
        sections += [document(rel, label) for rel, label in docs]
        if title.startswith("Part 6"):
            sections.append(code_map())
            sections.append(skill_catalog())
    sections.append(memory_part())
    text = clean("\n\n".join(sections))
    hits = sorted({m.group(0)[:24] for m in SECRET.finditer(text)})
    if hits:
        print("STOPPED, possible credentials:", hits)
        return 1
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"{OUT}\n{OUT.stat().st_size / 1e6:.2f} MB, {len(text.split()):,} words")
    return 0


if __name__ == "__main__":
    sys.exit(main())

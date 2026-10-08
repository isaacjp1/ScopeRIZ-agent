"""Check that every scored row of the EXPERIMENTS.md "Milestone runs" table matches its git tag (REL-01).

Usage:
    python3 speedrun-agent/eval/check_tags.py [EXPERIMENTS.md]

A row is scored when its Total cell is a whole number. Its tag must resolve to the row's Commit, and the tag text
(`git cat-file -p`: the tag message, or the commit message for a lightweight tag) must hold "scored <total>" and
"S<n> <correct>/<wrong>" for each set. Read-only: never creates, moves or deletes a tag, never edits the file.
Exits 0 when every scored row matches, 1 on any mismatch or when there are no scored rows.
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SET = re.compile(r"(-?\d+)\s*/\s*(\d+)\s*/\s*(\d+)\s*/\s*(\d+)\s*/\s*(\d+)")
WHOLE = re.compile(r"-?\d+")


def git(*args):
    # cwd is this script's directory, so a copy of EXPERIMENTS.md elsewhere is still checked against this repo's tags
    r = subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def table(path):
    """Cell lists of the Milestone runs table, header and separator rows dropped."""
    rows, inside = [], False
    for line in open(path, encoding="utf-8").read().splitlines():
        line = line.rstrip("\r")
        if line.startswith("## "):
            inside = line.startswith("## Milestone runs")
            continue
        if not inside or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if not cells or cells[0] == "Tag" or cells[0].startswith("---"):
            continue
        rows.append(cells)
    return rows


def check(cells):
    """Mismatch texts for one scored row (empty when it matches)."""
    tag, commit, total = cells[0], cells[1], int(cells[7])
    sets = {}
    for n, cell in enumerate(cells[4:7], 1):
        m = SET.fullmatch(cell)
        if m:   # "not finished" and the like add 0 and are not matched against the tag
            sets[n] = [int(x) for x in m.groups()]
    errs = []
    points = sum(v[0] for v in sets.values())
    if points != total:
        errs.append(f"set points sum to {points}, Total is {total}")
    if commit == "not in repo":
        print(f"out of repo, not tagged: {tag}")
        return errs
    sha = (git("rev-parse", "--verify", "-q", tag + "^{commit}") or "").strip()
    if not sha:
        return errs + [f"no git tag {tag!r}"]
    if not commit or not sha.startswith(commit):
        errs.append(f"tag is on {sha[:7]}, Commit cell is {commit!r}")
    text = git("cat-file", "-p", tag) or ""
    if not re.search(rf"\bscored {total}\b", text):
        errs.append(f"tag text lacks 'scored {total}'")
    for n, (_, correct, wrong, _, _) in sorted(sets.items()):
        if not re.search(rf"\bS{n} {correct}/{wrong}\b", text):
            errs.append(f"S{n}: tag text lacks 'S{n} {correct}/{wrong}'")
    return errs


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "EXPERIMENTS.md")
    scored = [c for c in table(path) if len(c) > 7 and WHOLE.fullmatch(c[7])]
    if not scored:
        print("no scored rows in the Milestone runs table")
        return 1
    bad = 0
    for cells in scored:
        errs = check(cells)
        for e in errs:
            print(f"MISMATCH {cells[0]}: {e}")
        if errs:
            bad += 1
        elif cells[1] != "not in repo":
            print(f"ok {cells[0]} -> {cells[1]}")
    if bad:
        print(f"tags do not match EXPERIMENTS.md: {bad} of {len(scored)} scored row(s) differ")
        return 1
    print(f"tags match EXPERIMENTS.md: {len(scored)} scored row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

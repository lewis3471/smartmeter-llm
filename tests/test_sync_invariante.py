#!/usr/bin/env python3
"""Der Evidence-Sync darf NICHTS ausser training-data anfassen.  Lauf:
    .venv/bin/python tests/test_sync_invariante.py

Hintergrund: Am 6.9.2026 hat ein Lauf von nuc_feedback_sync.py den
kompletten Code aus main geloescht — 68 Dateien, eine Stunde nach dem
Merge von PR #3. Zurueck blieb ein Repository, das nur noch
training-data enthielt, und damit ein Add-on-Repository, aus dem Home
Assistant nichts mehr installieren konnte. Der Mechanismus war
nachtraeglich nicht zu rekonstruieren; diese Invariante macht ihn
gleichgueltig.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import nuc_feedback_sync as sync

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'}   {name}" + (f"   {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          check=True)


def baue_repo() -> Path:
    repo = Path(tempfile.mkdtemp())
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "training-data").mkdir()
    (repo / "training-data" / "a.json").write_text("{}")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "meter_reader.py").write_text("# Code\n")
    (repo / "repository.yaml").write_text("name: x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "start")
    return repo


def test_reine_evidence_ist_erlaubt():
    repo = baue_repo()
    (repo / "training-data" / "b.json").write_text("{}")
    git(repo, "add", "-f", "training-data")
    check("neue_evidence_gilt_nicht_als_fremd",
          sync.fremde_aenderungen(repo) == [],
          str(sync.fremde_aenderungen(repo)))


def test_geloeschter_code_wird_erkannt():
    """Genau der Fall vom 6.9.: der Code steht als geloescht im Index."""
    repo = baue_repo()
    (repo / "scripts" / "meter_reader.py").unlink()
    (repo / "repository.yaml").unlink()
    (repo / "training-data" / "b.json").write_text("{}")
    git(repo, "add", "-A")
    fremd = sync.fremde_aenderungen(repo)
    check("loeschung_ausserhalb_evidence_wird_erkannt", len(fremd) == 2, str(fremd))
    check("nennt_die_betroffenen_dateien",
          "scripts/meter_reader.py" in fremd and "repository.yaml" in fremd,
          str(fremd))


def test_fremde_ergaenzung_wird_erkannt():
    repo = baue_repo()
    (repo / "geheim.txt").write_text("x")
    git(repo, "add", "-A")
    check("auch_neue_fremddateien_stoppen_den_sync",
          sync.fremde_aenderungen(repo) == ["geheim.txt"],
          str(sync.fremde_aenderungen(repo)))


def test_sync_committet_nicht_bei_fremdaenderung():
    """End-to-end: main() darf in diesem Zustand keinen Commit erzeugen."""
    repo = baue_repo()
    proben = Path(tempfile.mkdtemp())
    (proben / "events").mkdir(parents=True)
    (proben / "events" / "20260906_120000.json").write_text('{"ev":"test"}')
    (repo / "scripts" / "meter_reader.py").unlink()
    git(repo, "add", "-A")
    vorher = git(repo, "rev-parse", "HEAD").stdout.strip()
    sys.argv = ["x", "--repo", str(repo), "--samples", str(proben)]
    try:
        sync.main()
    except SystemExit:
        pass
    nachher = git(repo, "rev-parse", "HEAD").stdout.strip()
    check("kein_commit_bei_fremdaenderung", vorher == nachher,
          f"{vorher[:8]} -> {nachher[:8]}")
    check("code_ist_im_letzten_commit_noch_da",
          "scripts/meter_reader.py" in git(repo, "ls-tree", "-r", "--name-only",
                                           "HEAD").stdout,
          "Code fehlt im HEAD-Baum")


def test_reste_der_panne_werden_erkannt():
    """Rueckstand vom 6.9.: Code liegt unversioniert herum und blockiert
    den Pull. Erkannt werden darf NUR, was ausserhalb training-data liegt."""
    repo = baue_repo()
    (repo / "scripts" / "uebrig.py").write_text("# Altlast\n")
    (repo / "neue_datei.txt").write_text("x")
    (repo / "training-data" / "frisch.json").write_text("{}")
    reste = sync.reste_ausserhalb_evidence(repo)
    check("altlasten_ausserhalb_werden_gefunden",
          sorted(reste) == ["neue_datei.txt", "scripts/uebrig.py"], str(reste))
    check("evidence_wird_nie_angefasst",
          not any(r.startswith("training-data/") for r in reste), str(reste))


def test_blockierter_pull_heilt_sich_selbst():
    """End-to-end mit echtem Remote: der Pull scheitert an einer
    unversionierten Altdatei, der Sync raeumt sie weg und zieht durch."""
    fern = baue_repo()
    git(fern, "config", "receive.denyCurrentBranch", "ignore")
    # Auf dem Remote kommt eine Datei dazu, die lokal unversioniert
    # herumliegt — genau die Konstellation aus Papas Log.
    (fern / "scripts" / "ac_guard.py").write_text("# echte Fassung\n")
    git(fern, "add", "-A"); git(fern, "commit", "-q", "-m", "code zurueck")

    lokal = Path(tempfile.mkdtemp()) / "klon"
    subprocess.run(["git", "clone", "-q", str(fern), str(lokal)], check=True)
    git(lokal, "config", "user.email", "t@t"); git(lokal, "config", "user.name", "t")
    git(lokal, "reset", "-q", "--hard", "HEAD~1")          # Stand vor dem Code
    (lokal / "scripts" / "ac_guard.py").write_text("# Altlast\n")   # unversioniert

    vorher = subprocess.run(["git", "pull", "--rebase", "origin", "HEAD"],
                            cwd=lokal, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    check("pull_scheitert_ohne_heilung", vorher.returncode != 0
          and "would be overwritten" in vorher.stdout,
          vorher.stdout.strip()[-120:])

    proben = Path(tempfile.mkdtemp()); (proben / "events").mkdir()
    (proben / "events" / "20260909_120000.json").write_text('{"ev":"t"}')
    sys.argv = ["x", "--repo", str(lokal), "--samples", str(proben), "--push"]
    try:
        sync.main()
    except SystemExit:
        pass
    inhalt = (lokal / "scripts" / "ac_guard.py").read_text()
    check("altlast_ersetzt_durch_echte_fassung", "echte Fassung" in inhalt,
          repr(inhalt))
    # ls-tree, nicht ls-files: der Remote ist nicht bare, sein Index
    # bleibt beim Push unberuehrt — gepusht wurde trotzdem.
    check("evidence_wurde_gepusht",
          "20260909" in git(fern, "ls-tree", "-r", "--name-only", "HEAD").stdout,
          "kein Push angekommen")


for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
    print(f"\n{fn.__name__}:")
    fn()

print()
if FAILS:
    print(f"{len(FAILS)} FEHLGESCHLAGEN: {', '.join(FAILS)}")
    sys.exit(1)
print("Sync-Invariante gruen")

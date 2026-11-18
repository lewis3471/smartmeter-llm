#!/usr/bin/env python3
"""Sync NUC OCR evidence and pull the trained model.

Läuft aus einem beschreibbaren Git-Checkout (auf dem NUC: /data/feedback-repo,
Auth über GIT_SSH_COMMAND mit Deploy-Key). Ablauf pro Lauf:

1. Fortsetzung eines abgewürgten Laufs: gestagte Anderungen beenden
   (gestagte_weiterfuehrung() — ein Add-on-Neustart zwischen git rm/add
   und Commit blockt sonst für immer den Pull)
2. pull --rebase (Modell/Daten anderer Maschinen integrieren)
3. Retention: versionierte events-Altlasten per git rm entfernen (siehe
   retention() — eigener Schritt nach dem Pull, damit Index und
   Arbeitskopie konsistent bleiben)
4. Evidence einsammeln: disagreements/ + seg/ komplett, Routine-Samples
   (YYYYMMDD/) nur jedes N-te (Klassen-Balance ohne Repo-Flut).
   events/ wird NICHT mehr hochgeladen — reine Diagnose-Frames ohne
   Trainingswert; es lebt lokal in samples/ (Retention im Add-on-Run
   auf dem samples-Verzeichnis, ausserhalb des Git-Checkouts)
5. commit/push; NUR nach erfolgreichem Push wird lokal geprunt

Labels entstehen NICHT hier: nur der Konsens-Labeler auf der Trainings-
Maschine schreibt training-data/auto/ (scripts/ocr/consensus_label.py).

Training passiert hier NICHT mehr — Gemini-Labels sind fehlerbehaftet und
werden erst auditiert/relabelt (scripts/ocr/relabel.py). Trainiert wird
bewusst: manuell oder via AUTO_TRAIN_HOUR im meter_reader.
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import zlib
from datetime import date
from pathlib import Path

ROUTINE_KEEP_EVERY = 20   # jedes N-te Routine-Sample committen
PRUNE_AGE_DAYS = 3        # nicht-synced Routine-Reste lokal aufraeumen
EVENTS_DAYS = 10          # versionierte events-Altlasten: Aelteres fliegt raus


def log(msg: str, err: bool = False):
    print(time.strftime("[%m-%d %H:%M:%S]"), msg,
          file=sys.stderr if err else sys.stdout, flush=True)


def run(args, cwd: Path, check=True, *, input=None):
    return subprocess.run(args, cwd=cwd, text=True, check=check, input=input,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def reste_ausserhalb_evidence(repo: Path) -> list:
    """Unversionierte Dateien ausserhalb von training-data.

    DAS IST DER RUECKSTAND DER PANNE VOM 6.9.: Damals verschwand der Code
    aus dem INDEX, blieb aber auf der Platte liegen. Der Sync committete
    die Loeschungen (main verlor 68 Dateien), waehrend der Klon des NUC
    dieselben Dateien weiter unversioniert herumliegen hatte. Nach der
    Wiederherstellung will `git pull` sie schreiben und bricht ab:
    "untracked working tree files would be overwritten by merge".

    Damit steht der Evidence-Sync — stundlich, ohne dass jemand es
    merkt. training-data wird hier NIEMALS angefasst. NUL-getrennt
    gelesen: gequotete Sonderzeichen in Pfaden wuerden sonst beim
    Entfernen als falscher Dateiname landen."""
    roh = run(["git", "ls-files", "--others", "--exclude-standard", "-z"],
              repo).stdout
    return [z for z in roh.split("\0")
            if z and not z.startswith("training-data/")]


def halbfertiger_lauf(repo: Path) -> tuple[list, list]:
    """Unversionierte AENDERUNGEN an versionierten Dateien (geaendert oder
    geloescht, nicht im Index) -> (innerhalb training-data, ausserhalb).

    DAS IST DER STILLSTAND VOM 13.9.: Zwei Add-on-Neustarts im Abstand
    von 71 s (Update auf 1.9.0, dann Konfiguration gespeichert) haben
    einen Lauf zwischen Kopieren und Commit abgewuergt. Die heutige
    Regler-Telemetrie training-data/control/<Tag>.jsonl war schon
    ueberschrieben, aber nicht eingecheckt — und `git pull --rebase`
    verweigert: "cannot pull with rebase: You have unstaged changes."
    Jeder weitere Lauf scheiterte am selben Punkt, wieder ohne dass es
    jemand merkte.

    Beides ist gefahrlos zurueckzusetzen: Evidence wird im selben Lauf
    aus samples/ neu kopiert (dort bleibt sie bis zum erfolgreichen
    Push), und Code hat in diesem Klon nichts zu suchen — die gueltige
    Fassung ist die im Repository."""
    roh = run(["git", "diff", "--no-renames", "--name-only", "-z"], repo).stdout
    innen, aussen = [], []
    for pfad in filter(None, roh.split("\0")):
        (innen if pfad.startswith("training-data/") else aussen).append(pfad)
    return innen, aussen


def gestagte_pfade(repo: Path) -> list:
    """Gestagte Pfade — mit --no-renames und NUL-getrennt (-z).

    Ohne --no-renames liefert --name-only bei einer erkannten
    Umbenennung NUR den Zielpfad: die Loeschung des Ursprungs bleibt
    unsichtbar und ein Fortsetzungs-Commit wuerde Code-Loeschungen
    mitpushen. Ohne -z werden Sonderzeichen in Pfaden gequoted
    (core.quotePath) — `git reset` bekommt dann einen Namen, den es
    nicht gibt, und die Aenderung bleibt gestagt (Review PR #14)."""
    roh = run(["git", "diff", "--cached", "--no-renames", "-z",
               "--name-only"], repo).stdout
    return [p for p in roh.split("\0") if p]


def gestagte_weiterfuehrung(repo: Path) -> bool:
    """Ein zwischen `git rm`/`git add` und Commit abgewuergter Lauf
    (Add-on-Neustart) hinterlaesst gestagte Aenderungen. halbfertiger_lauf()
    sieht reine Index-Aenderungen nicht — die Arbeitskopie ist sauber — und
    `git pull --rebase` verweigert dann fuer immer: "Your index contains
    uncommitted changes". Auch jeder Retry bleibt haengen.

    Behandlung VOR dem Pull:
    - Gestagte Aenderungen unter training-data/ sind die Fortsetzung des
      eigenen Syncs (Retention-Loeschungen, halb kopierte Evidence) — sie
      werden als eigener Commit sofort beendet; der Pull --rebase spielt
      ihn nach, retention()/collect_evidence() komplettieren den Lauf.
    - Alles andere (Code ausserhalb training-data) wird unstaged — nie
      committet, Arbeitskopie unangetastet; halbfertiger_lauf() stellt
      versionierte Dateien danach wieder her. Scheitert das Unstagen,
      wird der Lauf abgebrochen statt ein fremder Pfad zu committen.
    - Unmittelbar vor dem Commit wird der Index erneut geprueft: er darf
      NUR Pfade unter training-data/ enthalten.
    """
    staged = gestagte_pfade(repo)
    if not staged:
        return False
    innen = [p for p in staged if p.startswith("training-data/")]
    aussen = [p for p in staged if not p.startswith("training-data/")]
    if aussen:
        log(f"{len(aussen)} gestagte Aenderung(en) ausserhalb training-data "
            f"aus einem abgewuergten Lauf — unstaged, nichts committet "
            f"({', '.join(aussen[:3])}{' ...' if len(aussen) > 3 else ''})",
            err=True)
        r = run(["git", "--literal-pathspecs", "reset", "--pathspec-from-file=-",
                 "--pathspec-file-nul"], repo, check=False, input="\0".join(aussen) + "\0")
        if r.returncode:
            log(f"Reset der gestagten Code-Aenderungen fehlgeschlagen — "
                f"Lauf uebersprungen, nichts committet: "
                f"{r.stdout.strip()[-200:]}", err=True)
            return False
    if innen:
        log(f"{len(innen)} gestagte Aenderung(en) unter training-data aus "
            f"einem abgewuergten Lauf — Commit wird fortgesetzt "
            f"({', '.join(innen[:3])}{' ...' if len(innen) > 3 else ''})")
        verbleibend = [p for p in gestagte_pfade(repo)
                       if not p.startswith("training-data/")]
        if verbleibend:
            log(f"ABBRUCH: Im Index stehen weiter {len(verbleibend)} "
                f"Pfad(e) ausserhalb training-data "
                f"({', '.join(verbleibend[:3])}"
                f"{' ...' if len(verbleibend) > 3 else ''}) — nichts "
                f"committet, nichts gepusht. Repo-Zustand pruefen.",
                err=True)
            return False
        r = run(["git", "commit", "-m", "ocr: sync evidence (Fortsetzung)"],
                repo, check=False)
        if r.returncode:
            log(f"Fortsetzungs-Commit fehlgeschlagen, Lauf uebersprungen: "
                f"{r.stdout.strip()[-200:]}", err=True)
            return False
    return innen or aussen


def fremde_aenderungen(repo: Path) -> list:
    """Alles im Index, was NICHT unter training-data liegt.

    SICHERUNG (6.9.2026): Ein Lauf dieses Skripts hat den KOMPLETTEN Code
    aus main geloescht — 68 Dateien, eine Stunde nach dem Merge von PR #3.
    Zurueck blieb ein Repository, das nur noch training-data enthielt;
    damit war das Add-on-Repository fuer Home Assistant unbrauchbar. Der
    genaue Mechanismus liess sich nachtraeglich nicht rekonstruieren
    (Verdacht: Rebase auf einem shallow/blobless Klon).

    Statt weiter zu suchen: eine Invariante, die es unmoeglich macht.
    Dieser Sync fasst ausschliesslich training-data an — alles andere ist
    ein Abbruchgrund, nie ein Commit. Geparst wird mit --no-renames und
    NUL-getrennt (siehe gestagte_pfade): ein Rename ueber die Verzeichnis-
    grenze darf die Loeschung des Code-Originals nicht verstecken."""
    stufe = run(["git", "diff", "--cached", "--no-renames", "-z",
                 "--name-only"], repo).stdout
    return [z for z in stufe.split("\0")
            if z and not z.startswith("training-data/")]


def changed(repo: Path, path: str) -> bool:
    return bool(run(["git", "status", "--porcelain", "--", path], repo).stdout.strip())


def keep_routine(name: str) -> bool:
    return zlib.crc32(name.encode()) % ROUTINE_KEEP_EVERY == 0


def retention(repo: Path, sparse_options: list[str]):
    """Versionierte Diagnosebilder nach dem Pull tageweise aus Git entfernen.
    Die Tagesordner kommen aus dem Commit-Baum, auch im Sparse-Checkout.
    Rohdaten fuer das Training bereinigt make retrain nach dem Labeln."""
    today = date.today()
    # Enumerate trees, including days outside the model-only checkout.
    paths = run(["git", "ls-tree", "-d", "-z", "--name-only", "HEAD", "--",
                 "training-data/events/"], repo).stdout
    removed = 0
    for path in paths.split("\0"):
        day = Path(path).name
        if not re.fullmatch(r"20\d{6}", day):
            continue
        try:
            age = (today - date(int(day[:4]), int(day[4:6]), int(day[6:]))).days
        except ValueError:
            continue
        if age <= EVENTS_DAYS:
            continue
        r = run(["git", "rm", *sparse_options, "-r", "-q", "--", path], repo, check=False)
        if r.returncode:
            log(f"git rm {path} fehlgeschlagen: "
                f"{r.stdout.strip()[-200:]}", err=True)
            return
        removed += 1
    if removed:
        log(f"Retention: {removed} events-Tagesordner (>{EVENTS_DAYS} Tage) "
            f"aus dem Repo entfernt")


def valid_label(d: dict) -> bool:
    # kWh hat auf dem ESY11 immer 6 Ziffern (fuehrende 0 -> int 5-6-stellig).
    # Gemini trunkiert gelegentlich auf 4 Stellen — solche Labels vergiften
    # das Training und werden hier verworfen.
    return (isinstance(d.get("kwh"), int) and isinstance(d.get("w"), int)
            and 10_000 <= d["kwh"] < 1_000_000 and abs(d["w"]) <= 20_000
            and d["kwh"] != 888888 and abs(d["w"]) not in (88888, 888888))


def evidence_paths(samples: Path) -> list[Path]:
    paths = []
    for sub in ("disagreements", "seg"):
        paths.extend(f.relative_to(samples) for f in sorted((samples / sub).rglob("*"))
                     if f.is_file())

    for jf in sorted(samples.glob("2*/*.json")):
        jpg = jf.with_suffix(".jpg")
        if not keep_routine(jf.stem) or not jpg.is_file():
            continue
        try:
            if valid_label(json.loads(jf.read_text())):
                paths.extend([jf.relative_to(samples), jpg.relative_to(samples)])
        except (OSError, ValueError):
            continue

    paths.extend(f.relative_to(samples) for f in sorted(samples.glob("control/*.jsonl")))
    return paths


def evidence_blobs(repo: Path, paths: list[Path]) -> dict[Path, str]:
    """Look up only pending names, without fetching any archived images."""
    wanted = {f"training-data/{p.as_posix()}": p for p in paths}
    if not wanted:
        return {}
    # On a large backlog, scan the tree once instead of passing the entire
    # outbox on the command line. Both variants only need tree objects.
    pathspec = list(wanted)
    if sum(len(p.encode()) + 1 for p in pathspec) >= 32768:
        pathspec = ["training-data"]
    entries = run(["git", "--literal-pathspecs", "ls-tree", "-r", "-z", "HEAD", "--", *pathspec],
                  repo).stdout
    blobs = {}
    for entry in entries.split("\0"):
        if entry:
            meta, path = entry.split("\t", 1)
            if path in wanted:
                mode, kind, oid = meta.split()
                if kind != "blob" or mode not in ("100644", "100755"):
                    raise ValueError(f"Expected a regular evidence file at {path}")
                blobs[wanted[path]] = oid
    return blobs


def matches_blob(path: Path, oid: str) -> bool:
    digest = hashlib.new("sha1" if len(oid) == 40 else "sha256")
    digest.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == oid


def collect_evidence(samples: Path, evidence: Path, paths: list[Path], blobs: dict) -> int:
    copied = 0
    for rel in paths:
        src = samples / rel
        # Images/labels are immutable. Only the daily telemetry file grows.
        if rel in blobs and (rel.parent != Path("control") or matches_blob(src, blobs[rel])):
            continue
        dst = evidence / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    return copied


def prune(samples: Path, pending: list[Path], blobs: dict):
    """Acknowledge exact files, also after retrying an interrupted push."""
    active_control = Path("control") / time.strftime("%Y%m%d.jsonl")
    removed = 0
    keep = set()
    for rel in pending:
        if rel == active_control:
            continue
        f = samples / rel
        if rel in blobs and f.is_file() and matches_blob(f, blobs[rel]):
            f.unlink()
            removed += 1
        else:
            keep.add(f)
    cutoff = time.time() - PRUNE_AGE_DAYS * 86400
    for day_dir in samples.glob("2*/"):
        for f in list(day_dir.iterdir()):
            if f not in keep and f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        if not any(day_dir.iterdir()):
            day_dir.rmdir()
    log(f"Prune: {removed} lokale Dateien entfernt")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path,
                        default=Path(__file__).resolve().parents[1])
    parser.add_argument("--samples", type=Path, required=True,
                        help="SAVE_SAMPLES_DIR des meter_reader")
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args()
    repo, samples = args.repo.resolve(), args.samples.resolve()
    sparse = run(["git", "config", "--bool", "core.sparseCheckout"], repo, check=False)
    sparse_options = ["--sparse"] if sparse.stdout.strip() == "true" else []

    if args.push:
        gestagte_weiterfuehrung(repo)
        innen, aussen = halbfertiger_lauf(repo)
        if innen or aussen:
            if innen:
                log(f"Halbfertiger Lauf: {len(innen)} Evidence-Datei(en) "
                    f"veraendert, aber nicht eingecheckt — zurueckgesetzt, "
                    f"wird gleich aus samples/ neu kopiert "
                    f"({', '.join(innen[:3])}{' ...' if len(innen) > 3 else ''})")
            if aussen:
                log(f"{len(aussen)} versionierte Datei(en) ausserhalb "
                    f"training-data lokal veraendert — zurueckgesetzt auf den "
                    f"Stand des Repositories "
                    f"({', '.join(aussen[:3])}{' ...' if len(aussen) > 3 else ''})",
                    err=True)
            run(["git", "--literal-pathspecs", "restore", "--ignore-skip-worktree-bits",
                 "--pathspec-from-file=-", "--pathspec-file-nul"], repo,
                input="\0".join(innen + aussen) + "\0")
        branch = run(["git", "symbolic-ref", "--short", "HEAD"], repo).stdout.strip()
        r = run(["git", "pull", "--rebase", "origin", branch], repo, check=False)
        if r.returncode and "would be overwritten" in r.stdout:
            # Selbstheilung statt Handarbeit auf dem NUC: die Reste der
            # Sync-Panne blockieren den Pull. Sie sind reine Altlast — die
            # gueltige Fassung liegt im Repository und wird gleich
            # ausgecheckt. Evidence bleibt unangetastet.
            reste = reste_ausserhalb_evidence(repo)
            if reste:
                log(f"Pull blockiert von {len(reste)} unversionierten "
                    f"Altdateien ausserhalb training-data — werden entfernt "
                    f"({', '.join(reste[:3])}"
                    f"{' ...' if len(reste) > 3 else ''})")
                for rel in reste:
                    try:
                        (repo / rel).unlink()
                    except OSError as e:
                        log(f"  {rel}: {e}", err=True)
                r = run(["git", "pull", "--rebase", "origin", branch],
                        repo, check=False)
        if r.returncode:
            log(f"git pull fehlgeschlagen, Lauf uebersprungen: "
                f"{r.stdout.strip()[-200:]}", err=True)
            return
        retention(repo, sparse_options)
    evidence = repo / "training-data"
    evidence.mkdir(exist_ok=True)
    pending = evidence_paths(samples)
    blobs = evidence_blobs(repo, pending)
    copied = collect_evidence(samples, evidence, pending, blobs)
    log(f"Evidence: {copied} Dateien kopiert")

    if copied or changed(repo, "training-data"):
        run(["git", "add", *sparse_options, "-f", "--", "training-data"], repo)
        fremd = fremde_aenderungen(repo)
        if fremd:
            log(f"ABBRUCH: Sync wuerde {len(fremd)} Datei(en) ausserhalb "
                f"training-data aendern ({', '.join(fremd[:5])}"
                f"{' ...' if len(fremd) > 5 else ''}) — nichts committet, "
                f"nichts gepusht. Repo-Zustand pruefen.", err=True)
            run(["git", "reset"], repo, check=False)
            return
        run(["git", "commit", "-m", "ocr: sync evidence"], repo)
        log("Commit erstellt")
    if args.push:
        r = run(["git", "push", "origin", "HEAD"], repo, check=False)
        if r.returncode:
            log(f"git push fehlgeschlagen — Prune uebersprungen: "
                f"{r.stdout.strip()[-200:]}", err=True)
            return
        log("Push ok")
        if changed(repo, "training-data"):
            # Sicherheitsgurt: Evidence NICHT vollstaendig committet (z.B.
            # gitignore-Panne) -> niemals lokale Originale loeschen
            log("training-data nicht vollstaendig committet — "
                "Prune uebersprungen", err=True)
            return
        prune(samples, pending, evidence_blobs(repo, pending))
        if sparse_options:
            run(["git", "sparse-checkout", "reapply"], repo)


if __name__ == "__main__":
    main()

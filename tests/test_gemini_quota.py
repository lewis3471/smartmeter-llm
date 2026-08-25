#!/usr/bin/env python3
"""Tests fuer die Quota-Bremse des Gemini-Kreuz-Checks.

Lauf:  python3 tests/test_gemini_quota.py

Am 25.08. lief das Add-on in eine Dauerschleife aus HTTP 429: alle 45 s
rotierte es ueber alle Modelle und Keys, ~800 Fehlversuche pro Stunde,
jeder davon eine HTTP-Anfrage mitten im 0,5-s-Regelzyklus. Ursache war
nicht die Rotation, sondern der Abstand des Kreuz-Checks: er zaehlte
ZYKLEN (20), und ein Zyklus dauert seit INTERVAL_S=0.5 keine 15 s mehr,
sondern unter einer Sekunde. Aus "~5 min" wurden 10-25 s.

  Q1  Der Kreuz-Check haelt einen zeitlichen Mindestabstand ein.
  Q2  Ein einzelner Aufruf verbrennt hoechstens GEMINI_TRIES Kombinationen.
  Q3  Sind alle probierten Kombinationen limitiert, gibt es eine Pause —
      und in der Pause fliegt keine einzige Anfrage mehr raus.
  Q4  Die Pause verdoppelt sich und ein Erfolg setzt alles zurueck.
  Q5  Das lokale OCR liest waehrend der Pause unveraendert weiter.
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("OPENDTU_URL", "http://test.invalid")
os.environ.setdefault("OPENDTU_USER", "t")
os.environ.setdefault("OPENDTU_PASS", "t")
os.environ.setdefault("MQTT_USER", "CHANGE_ME")
os.environ.setdefault("MQTT_PASS", "x")
os.environ.setdefault("READER_MODE", "gemini")
os.environ.setdefault("SAVE_SAMPLES_DIR", "")
os.environ.setdefault("GEMINI_API_KEYS", "AAAAkey_MTPg,BBBBkey_gK4w")
os.environ.setdefault(
    "GEMINI_MODELS",
    "gemini-flash-lite-latest,gemini-flash-latest,gemini-3.1-flash-lite,"
    "gemini-3.5-flash,gemini-2.0-flash-lite")
os.environ["STATE_FILE"] = str(Path(tempfile.mkdtemp()) / "state.json")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import meter_reader as mr  # noqa: E402

FAILED = []


def check(name, cond, info=""):
    print(f"  {'OK  ' if cond else 'FAIL'}  {name}" + (f"  — {info}" if info else ""))
    if not cond:
        FAILED.append(name)


class Antwort:
    def __init__(self, code, payload=None):
        self.status_code = code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} Client Error")


OK_PAYLOAD = {"candidates": [{"content": {"parts": [
    {"text": '{"kwh": 35891, "w": -52}'}]}}]}


class Netz:
    """Zaehlt jede HTTP-Anfrage und antwortet nach Drehbuch."""

    def __init__(self, code=429):
        self.calls = []
        self.code = code

    def post(self, url, **kw):
        self.calls.append(url.rsplit("/", 1)[-1].split(":")[0])
        if self.code == 200:
            return Antwort(200, OK_PAYLOAD)
        return Antwort(self.code)


def reset(netz, now):
    mr.requests.post = netz.post
    mr._gemini_pause_until = 0.0
    mr._gemini_pause_s = 0.0
    mr._gemini_err_since = None
    mr._gemini_err_n = 0
    mr._combo_idx = 0
    mr._dead_models.clear()
    mr.time.time = lambda: now[0]


def t_tries_und_pause():
    print("\nQ2/Q3: begrenzte Rotation, danach Pause")
    now = [1_800_000_000.0]
    netz = Netz(429)
    real_time = mr.time.time
    real_post = mr.requests.post
    try:
        reset(netz, now)
        try:
            mr.gemini_read(b"jpeg")
            check("QuotaExhausted geworfen", False, "keine Exception")
        except mr.QuotaExhausted:
            check("QuotaExhausted geworfen", True)
        except Exception as e:
            check("QuotaExhausted geworfen", False, f"{type(e).__name__}: {e}")
        check(f"hoechstens {mr.GEMINI_TRIES} Anfragen pro Aufruf",
              len(netz.calls) <= mr.GEMINI_TRIES,
              f"{len(netz.calls)} Anfragen: {netz.calls}")
        check("Pause gesetzt", mr._gemini_pause_until > now[0],
              f"noch {(mr._gemini_pause_until - now[0]) / 60:.0f} min")

        # Q3: waehrend der Pause fliegt nichts mehr raus
        vorher = len(netz.calls)
        for _ in range(50):
            now[0] += 1.0
            try:
                mr.gemini_read(b"jpeg")
            except Exception:
                pass
        check("in der Pause keine einzige Anfrage",
              len(netz.calls) == vorher, f"{len(netz.calls) - vorher} Anfragen")

        # Q4: nach Ablauf wird es erneut versucht, Pause verdoppelt sich
        erste = mr._gemini_pause_s
        now[0] = mr._gemini_pause_until + 1
        try:
            mr.gemini_read(b"jpeg")
        except Exception:
            pass
        check("nach Ablauf wird erneut probiert", len(netz.calls) > vorher,
              f"{len(netz.calls) - vorher} Anfragen")
        check("Pause verdoppelt sich",
              mr._gemini_pause_s == min(erste * 2, mr.GEMINI_PAUSE_MAX_S),
              f"{erste / 60:.0f} min -> {mr._gemini_pause_s / 60:.0f} min")

        # Erfolg raeumt alles ab
        netz.code = 200
        now[0] = mr._gemini_pause_until + 1
        gelesen = mr.gemini_read(b"jpeg")
        check("Erfolg setzt die Pause zurueck", mr._gemini_pause_until == 0.0)
        check("Lesung kommt durch", gelesen == {"kwh": 35891, "w": -52},
              f"{gelesen}")
    finally:
        mr.time.time = real_time
        mr.requests.post = real_post


def t_kreuzcheck_abstand():
    print("\nQ1: Kreuz-Check haelt zeitlichen Mindestabstand")
    check("Abstand ist in Sekunden konfiguriert, nicht in Zyklen",
          mr.CROSS_CHECK_S >= 60, f"CROSS_CHECK_S={mr.CROSS_CHECK_S}")
    # Der alte Zaehler bleibt als zusaetzliche Untergrenze bestehen
    check("Zyklen-Untergrenze bleibt erhalten", mr.CROSS_CHECK_EVERY >= 1)
    # Bei 0,5 s Zykluszeit waren 20 Zyklen ~10-25 s — jetzt bindet die Zeit
    zyklen_s = 20 * 0.7
    check("Zeitregel bindet bei 0,5-s-Zyklen", mr.CROSS_CHECK_S > zyklen_s,
          f"{mr.CROSS_CHECK_S:.0f} s statt ~{zyklen_s:.0f} s")
    calls_pro_tag = 86400 / mr.CROSS_CHECK_S
    check("Tagesbudget im dokumentierten Rahmen (~300-500)",
          calls_pro_tag <= 500, f"{calls_pro_tag:.0f} Kreuz-Checks/Tag")


def t_lokal_laeuft_weiter():
    print("\nQ5: lokales OCR liest waehrend der Pause weiter")
    now = [1_800_000_000.0]
    netz = Netz(429)
    real_time, real_post = mr.time.time, mr.requests.post
    saved_mode, saved_reader = mr.READER_MODE, mr._local_reader

    class Lokal:
        def read(self, img):
            return {"kwh": 35891, "w": -52}, 0.99

    try:
        reset(netz, now)
        mr.READER_MODE = "hybrid"
        mr._local_reader = Lokal()
        mr._gemini_pause_until = now[0] + 3600
        mr.image_brightness = lambda img: 100.0
        mr.get_snapshot = lambda: b"jpeg"
        gelesen, quelle = mr.read_meter(cycle=0)   # Zyklus 0 = Kreuz-Check
        check("Lesung kommt aus dem lokalen OCR",
              gelesen == {"kwh": 35891, "w": -52}, f"{gelesen} ({quelle})")
        check("kein Gemini-Aufruf waehrend der Pause", not netz.calls,
              f"{netz.calls}")
    finally:
        mr.time.time, mr.requests.post = real_time, real_post
        mr.READER_MODE, mr._local_reader = saved_mode, saved_reader


if __name__ == "__main__":
    print(f"Modelle: {len(mr.GEMINI_MODELS)}  Keys: {len(mr.GEMINI_API_KEYS)}  "
          f"-> {len(mr.GEMINI_MODELS) * len(mr.GEMINI_API_KEYS)} Kombinationen, "
          f"GEMINI_TRIES={mr.GEMINI_TRIES}")
    t_kreuzcheck_abstand()
    t_tries_und_pause()
    t_lokal_laeuft_weiter()
    print()
    if FAILED:
        print(f"{len(FAILED)} FEHLGESCHLAGEN: {FAILED}")
        sys.exit(1)
    print("alle Tests bestanden")

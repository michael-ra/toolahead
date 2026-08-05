"""Workspace-Watcher — die billige „hat sich was geändert?"-Ebene.

Führt einen monoton steigenden `generation`-Zähler, der bei jeder Änderung
unter dem Workspace hochzählt. Damit muss der teure Content-Hash nur dann neu
berechnet werden, wenn der Zähler seit der letzten Berechnung gewandert ist.

Zwei Erkennungswege, mit Fallback:
  • watchdog (falls installiert): push-basiert über FSEvents/inotify/Windows —
    minimale Latenz, bump bei jedem Datei-Event.
  • mtime/size-Poller (stdlib): scannt in Intervallen eine billige Signatur
    (relpath+mtime+size), aber nur wenn watchdog fehlt.

Der Watcher ist ausschliesslich eine Optimierung fuer Dedupe und Diagnose. Er
kann Events verlieren und mtime/size kann manipuliert werden. Deshalb wird am
Serve-Zeitpunkt unabhaengig vom Zaehler immer ein frischer Content-Hash gebildet.
"""

import hashlib
import os
import threading

IGNORE_DIRS = {"__pycache__", ".git", "node_modules", ".venv"}


def _relevant_file(name: str) -> bool:
    return not (name.endswith(".pyc") or name.startswith(".prefetch"))


class WorkspaceWatcher:
    def __init__(self, root: str, poll_interval: float = 0.25):
        self.root = os.path.abspath(root)
        self.poll_interval = poll_interval
        self._gen = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.backend = "poll"
        self._observer = None
        self._last_sig = None
        self._start()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._gen

    def bump(self):
        with self._lock:
            self._gen += 1

    # -- billige Signatur aus mtime + size (liest KEINE Dateiinhalte) --
    def signature(self) -> str:
        h = hashlib.sha256()
        for dp, dirs, files in sorted(os.walk(self.root)):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            for f in sorted(files):
                if not _relevant_file(f):
                    continue
                p = os.path.join(dp, f)
                try:
                    st = os.stat(p)
                    h.update(os.path.relpath(p, self.root).encode())
                    h.update(f"{st.st_mtime_ns}:{st.st_size}".encode())
                except OSError:
                    pass
        return h.hexdigest()

    def _poll_loop(self):
        self._last_sig = self.signature()
        while not self._stop.wait(self.poll_interval):
            try:
                sig = self.signature()
            except Exception:  # noqa: BLE001 — im Zweifel als geändert behandeln
                self.bump()
                continue
            if sig != self._last_sig:
                self._last_sig = sig
                self.bump()

    def _start(self):
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            watcher = self

            class _Handler(FileSystemEventHandler):
                def on_any_event(self, event):
                    if getattr(event, "is_directory", False):
                        return
                    path = getattr(event, "dest_path", None) or event.src_path
                    parts = set(path.split(os.sep))
                    if parts & IGNORE_DIRS:
                        return
                    if _relevant_file(os.path.basename(path)):
                        watcher.bump()

            self._observer = Observer()
            self._observer.schedule(_Handler(), self.root, recursive=True)
            self._observer.daemon = True
            self._observer.start()
            self.backend = "watchdog"
        except Exception:  # noqa: BLE001 — watchdog nicht installiert → nur Polling
            self._observer = None
            self.backend = "poll"
            threading.Thread(target=self._poll_loop, daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2)
            except Exception:  # noqa: BLE001
                pass

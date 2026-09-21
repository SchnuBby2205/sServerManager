#!/usr/bin/env python3
"""
CMaNGOS / AzerothCore Server Manager
=====================================

Grafisches Werkzeug (Tkinter, keine externen Abhängigkeiten) zur Verwaltung
von drei Docker-basierten WoW-Emulator-Repos:

  - https://github.com/SchnuBby2205/cmangos-tbc-server        (CMaNGOS TBC 2.4.3)
  - https://github.com/SchnuBby2205/cmangos-classic-server    (CMaNGOS Classic 1.12)
  - https://github.com/SchnuBby2205/azerothcore-wotlk-server  (AzerothCore WotLK 3.3.5a)

Optik/Layout sind bewusst an "menu_ui.py" (SchnuBbys Repack) angelehnt:
dunkles Farbschema, linke Sidebar mit Hauptpunkten, rechts ein Detailblock
mit anklickbaren Karten, unten eine Fortschrittsbox und ein farbiges
Protokoll.

Funktionen:
  1. Repository klonen / aktualisieren (git clone / git pull)
  2. Host-Ports pro Server frei konfigurierbar (wird direkt in die
     docker-compose.yml des jeweiligen Projekts geschrieben)
  3. sql-init entpacken (nur CMaNGOS-Profile; bei AzerothCore nicht nötig)
  4. Docker-Image bauen — anschließend werden optional automatisch nicht
     mehr benötigte (dangling) Images sowie der Docker-Builder-Cache
     entfernt
  5. WoW-Daten extrahieren (CMaNGOS: ein interaktives Skript; AzerothCore:
     drei getrennte, nicht-interaktive Schritte), inkl. Rechte-Fix ohne
     sudo und Verschieben nach data/
  6. Server starten (nur DB / komplett) und stoppen
  7. Live-Logs / Status der Container
  8. Ein interaktives Konsolenfeld für Eingaben an laufende Prozesse
     (z. B. bei der CMaNGOS-Extraktion oder "docker attach")

Voraussetzungen: git, docker, docker compose (Plugin V2). Der WoW-Client
muss bereits vorhanden sein und manuell unter "wow-client/" im
Projektordner liegen, bevor extrahiert wird.
"""

import json
import os
import queue
import re
import select
import shlex
import shutil
import signal
import subprocess
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

try:
    import pty   # nur Unix (Linux/macOS) - für 'docker attach' an TTY-Container benötigt
    import tty   # setzt das Pseudo-Terminal in den Raw-Modus (siehe start_pty())
    HAVE_PTY = True
except ImportError:
    HAVE_PTY = False

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


# --------------------------------------------------------------------------
# Farben / Schriften (identisch zu menu_ui.py, "SchnuBbys Repack")
# --------------------------------------------------------------------------

class T:
    """Farbpalette (dunkles Thema)."""

    BG = "#1b1e24"          # Fensterhintergrund
    SIDEBAR = "#161920"     # linke Spalte
    PANEL = "#22262f"       # Detailblock
    CARD = "#2a2f3a"        # Karte
    CARD_HOVER = "#353c4a"
    LINE = "#333947"        # Trennlinien
    FG = "#e8eaed"          # Haupttext
    DIM = "#98a0ae"         # Nebentext
    ACCENT = "#4fc3f7"      # Cyan
    GREEN = "#8bc34a"
    YELLOW = "#ffb74d"
    RED = "#ef5350"


FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_TITLE = ("Segoe UI", 15, "bold")
FONT_SMALL = ("Segoe UI", 9)
FONT_MONO = ("Consolas", 9)

APP_TITLE = "CMaNGOS / AzerothCore Server Manager"


# --------------------------------------------------------------------------
# Server-Profile
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PortDef:
    label: str
    container_port: str
    default_host_port: str


@dataclass(frozen=True)
class ContainerRole:
    key: str            # interner Schlüssel, z.B. "db", "world", "realm", "extra"
    label: str          # Anzeige-Name im Ports/Namen-Bereich
    default_name: str   # Standard-Containername laut docker-compose.yml


@dataclass(frozen=True)
class ServerProfile:
    key: str
    label: str
    repo_url: str
    repo_dirname: str
    image_name: str
    extraction_mode: str          # "cmangos" oder "azerothcore"
    container_roles: tuple        # tuple[ContainerRole, ...]
    ports: tuple
    needs_sql_init_unpack: bool
    data_map: tuple               # (Quellordner unter wow-client/, Zielordner unter data/)
    db_core_repo_url: str = ""    # CMaNGOS-Core-Repo (nur für DB-Updates), leer = nicht unterstützt
    db_name: str = ""             # Name der Content-Datenbank in MariaDB
    console_db_user: str = ""
    console_db_pass: str = ""
    console_db_bin: str = "mysql"  # "mariadb" (CMaNGOS) oder "mysql" (AzerothCore-Image mysql:8.0)
    db_install_repo_url: str = ""  # classic-db.git / tbc-db.git - volle DB-Installation, leer = nicht unterstützt
    db_install_dirname: str = ""   # "classic-db" / "tbc-db"
    playerbots_repo_url: str = ""  # gemeinsames Playerbots-Repo, leer = nicht unterstützt


DB_USER = "mangos"
DB_PASS = "mangos"
DB_CORE_SUBDIR = "cmangos-core"


PROFILES = {
    "tbc": ServerProfile(
        key="tbc", label="CMaNGOS TBC (2.4.3)",
        repo_url="https://github.com/SchnuBby2205/cmangos-tbc-server.git",
        repo_dirname="cmangos-tbc-server", image_name="cmangos-tbc",
        extraction_mode="cmangos",
        container_roles=(
            ContainerRole("db", "Datenbank", "cmangos-db"),
            ContainerRole("world", "Mangosd (Server-/GM-Konsole)", "cmangos-mangosd"),
            ContainerRole("realm", "Realmd", "cmangos-realmd"),
            ContainerRole("extra", "KoboldCPP AI", "koboldcpp"),
        ),
        ports=(PortDef("Realmd / Login", "3724", "3724"),
               PortDef("Worldserver", "8085", "8085"),
               PortDef("KoboldCPP AI", "5001", "5001")),
        needs_sql_init_unpack=True,
        data_map=(("maps", "maps"), ("dbc", "dbc"), ("vmaps", "vmaps"),
                  ("mmaps", "mmaps"), ("cameras", "cameras"), ("buildings", "buildings")),
        db_core_repo_url="https://github.com/cmangos/mangos-tbc.git",
        db_name="tbcmangos",
        console_db_user="mangos", console_db_pass="mangos", console_db_bin="mariadb",
        db_install_repo_url="https://github.com/cmangos/tbc-db.git", db_install_dirname="tbc-db",
        playerbots_repo_url="https://github.com/cmangos/playerbots.git",
    ),
    "classic": ServerProfile(
        key="classic", label="CMaNGOS Classic (1.12)",
        repo_url="https://github.com/SchnuBby2205/cmangos-classic-server.git",
        repo_dirname="cmangos-classic-server", image_name="cmangos-classic",
        extraction_mode="cmangos",
        container_roles=(
            ContainerRole("db", "Datenbank", "cmangos-db"),
            ContainerRole("world", "Mangosd (Server-/GM-Konsole)", "cmangos-mangosd"),
            ContainerRole("realm", "Realmd", "cmangos-realmd"),
            ContainerRole("extra", "KoboldCPP AI", "koboldcpp"),
        ),
        ports=(PortDef("Realmd / Login", "3724", "3724"),
               PortDef("Worldserver", "8085", "8085"),
               PortDef("KoboldCPP AI", "5001", "5001")),
        needs_sql_init_unpack=True,
        data_map=(("maps", "maps"), ("dbc", "dbc"), ("vmaps", "vmaps"),
                  ("mmaps", "mmaps"), ("cameras", "cameras"), ("buildings", "buildings")),
        db_core_repo_url="https://github.com/cmangos/mangos-classic.git",
        db_name="classicmangos",
        console_db_user="mangos", console_db_pass="mangos", console_db_bin="mariadb",
        db_install_repo_url="https://github.com/cmangos/classic-db.git", db_install_dirname="classic-db",
        playerbots_repo_url="https://github.com/cmangos/playerbots.git",
    ),
    "azerothcore": ServerProfile(
        key="azerothcore", label="AzerothCore WotLK (3.3.5a)",
        repo_url="https://github.com/SchnuBby2205/azerothcore-wotlk-server.git",
        repo_dirname="azerothcore-wotlk-server", image_name="azerothcore-wotlk",
        extraction_mode="azerothcore",
        container_roles=(
            ContainerRole("db", "Datenbank", "ac-database"),
            ContainerRole("world", "Worldserver (Server-/GM-Konsole)", "ac-worldserver"),
            ContainerRole("realm", "Authserver", "ac-authserver"),
        ),
        ports=(PortDef("Authserver / Login", "3724", "3724"),
               PortDef("Worldserver", "8085", "8085"),
               PortDef("SOAP (optional)", "7878", "7878")),
        needs_sql_init_unpack=False,
        data_map=(("maps", "maps"), ("dbc", "dbc"), ("Cameras", "cameras"), ("vmaps", "vmaps")),
        # Laut docker-compose.yml: MySQL-Container mit User "root" und Passwort aus
        # DOCKER_DB_ROOT_PASSWORD (.env), Standardwert "password", falls nicht gesetzt.
        # Enthält 3 Datenbanken: acore_auth, acore_characters, acore_world.
        console_db_user="root", console_db_pass="password", console_db_bin="mysql",
        # AzerothCore aktualisiert seine Datenbanken selbst (eigener DB-Updater beim Start) -
        # db_core_repo_url bleibt leer, siehe render_detail()/DB-Updates.
    ),
}

CONFIG_FILE = Path.home() / ".cmangos_manager.json"


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


def apply_ports_to_compose(project_dir: Path, port_map: dict) -> list:
    """Schreibt für jeden {container_port: neuer_host_port} Eintrag den passenden
    Host-Port direkt in die docker-compose.yml. Jeder Container-Port kommt in
    den hier verwalteten Compose-Dateien genau einmal vor, daher reicht ein
    gezielter Regex-Ersatz je Port."""
    compose_path = project_dir / "docker-compose.yml"
    text = compose_path.read_text(encoding="utf-8")
    changes = []
    for container_port, new_host in port_map.items():
        pattern = re.compile(r'"(\d+):' + re.escape(container_port) + r'"')

        def _repl(m, new_host=new_host, container_port=container_port):
            old_host = m.group(1)
            if old_host != new_host:
                changes.append(f"{container_port} (Container) : {old_host} -> {new_host} (Host)")
            return f'"{new_host}:{container_port}"'

        text, n = pattern.subn(_repl, text, count=1)
        if n == 0:
            changes.append(f"[Warnung] Container-Port {container_port} nicht in docker-compose.yml gefunden.")
    compose_path.write_text(text, encoding="utf-8")
    return changes


def apply_container_names_to_compose(project_dir: Path, name_map: dict) -> list:
    """Schreibt für jeden {rolle: (aktueller_name, neuer_name)} Eintrag den neuen
    'container_name:' Wert direkt in die docker-compose.yml. Jeder Containername
    kommt genau einmal vor, daher reicht ein gezielter Ersatz je Name."""
    compose_path = project_dir / "docker-compose.yml"
    text = compose_path.read_text(encoding="utf-8")
    changes = []
    for role_key, (old_name, new_name) in name_map.items():
        if old_name == new_name:
            continue
        pattern = re.compile(r'container_name:\s*' + re.escape(old_name) + r'\b')
        new_text, n = pattern.subn(f"container_name: {new_name}", text, count=1)
        if n == 0:
            changes.append(f"[Warnung] container_name '{old_name}' nicht in docker-compose.yml gefunden.")
        else:
            text = new_text
            changes.append(f"{old_name} -> {new_name}")
    compose_path.write_text(text, encoding="utf-8")
    return changes


def patch_shell_config(path: Path, values: dict) -> list:
    """Setzt KEY="VALUE"-Zeilen in einer einfachen shell-artigen Config-Datei
    (wie InstallFullDB.config). Vorhandene KEY=...-Zeilen werden ersetzt,
    fehlende ans Ende angehängt."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    changes = []
    remaining = dict(values)
    for i, line in enumerate(lines):
        stripped = line.strip()
        for key in list(remaining):
            if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
                lines[i] = f'{key}="{remaining.pop(key)}"'
                changes.append(f"{key} gesetzt")
                break
    for key, val in remaining.items():
        lines.append(f'{key}="{val}"')
        changes.append(f"{key} ergänzt (war nicht vorhanden)")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changes


# --------------------------------------------------------------------------
# Prozess-Runner: führt Kommandos in einem Hintergrundthread aus und
# streamt stdout/stderr zeilenweise über eine Queue an die GUI. Erlaubt
# außerdem, Text an das stdin des laufenden Prozesses zu senden.
# --------------------------------------------------------------------------

class ProcessRunner:
    def __init__(self, on_line, on_finished):
        self._on_line = on_line
        self._on_finished = on_finished
        self._proc = None
        self._master_fd = None   # gesetzt, solange ein Prozess im PTY-Modus läuft
        self._lock = threading.Lock()

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def start(self, cmd, cwd=None):
        if self.busy:
            raise RuntimeError("Es läuft bereits ein Vorgang.")

        def worker():
            try:
                proc = subprocess.Popen(
                    cmd, cwd=cwd,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                )
            except FileNotFoundError as exc:
                self._on_line(f"[FEHLER] Befehl nicht gefunden: {exc}")
                self._on_finished(-1)
                return
            except Exception as exc:
                self._on_line(f"[FEHLER] Konnte Prozess nicht starten: {exc}")
                self._on_finished(-1)
                return

            with self._lock:
                self._proc = proc
                self._master_fd = None

            self._on_line(f"$ {' '.join(str(c) for c in cmd)}")
            try:
                for line in proc.stdout:
                    self._on_line(line.rstrip("\n"))
            except Exception as exc:
                self._on_line(f"[FEHLER beim Lesen der Ausgabe] {exc}")

            rc = proc.wait()
            with self._lock:
                self._proc = None
            self._on_line(f"[Beendet mit Exit-Code {rc}]")
            self._on_finished(rc)

        threading.Thread(target=worker, daemon=True).start()

    def start_pty(self, cmd, cwd=None):
        """Wie start(), aber stdin/stdout/stderr hängen an einem echten Pseudo-Terminal.
        Nötig für 'docker attach' an Container, die mit einem TTY laufen (tty: true in
        der docker-compose.yml) - Docker lehnt eine reine Pipe dafür ab ('cannot attach
        stdin to a TTY-enabled container because stdin is not a terminal')."""
        if self.busy:
            raise RuntimeError("Es läuft bereits ein Vorgang.")
        if not HAVE_PTY:
            self._on_line("[FEHLER] Pseudo-Terminal (pty) ist auf diesem Betriebssystem nicht verfügbar "
                           "(nur Linux/macOS). 'docker attach' kann hier nicht genutzt werden.")
            self._on_finished(-1)
            return

        def worker():
            master_fd, slave_fd = pty.openpty()
            # Sofort selbst in den Raw-Modus setzen (kein Zeilenpuffer, kein lokales Echo,
            # keine Software-Flusskontrolle). Ohne das bleiben Steuerzeichen ohne
            # abschließendes Enter - wie die Detach-Sequenz Strg+P Strg+Q in terminate() -
            # im kanonischen Zeilenpuffer des Kernels hängen und kommen nie beim
            # lesenden Prozess (z.B. 'docker attach') an.
            try:
                tty.setraw(slave_fd)
            except Exception:
                pass
            try:
                proc = subprocess.Popen(
                    cmd, cwd=cwd,
                    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                    preexec_fn=os.setsid, close_fds=True,
                )
            except Exception as exc:
                os.close(master_fd)
                os.close(slave_fd)
                self._on_line(f"[FEHLER] Konnte Prozess nicht starten: {exc}")
                self._on_finished(-1)
                return

            os.close(slave_fd)  # gehört jetzt dem Kindprozess, hier nicht mehr benötigt

            with self._lock:
                self._proc = proc
                self._master_fd = master_fd

            self._on_line(f"$ {' '.join(str(c) for c in cmd)}")
            buf = b""
            try:
                while True:
                    if proc.poll() is not None:
                        break
                    try:
                        ready, _, _ = select.select([master_fd], [], [], 0.2)
                    except (OSError, ValueError):
                        break
                    if master_fd not in ready:
                        continue
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self._on_line(line.decode("utf-8", errors="replace").rstrip("\r"))
            finally:
                if buf:
                    self._on_line(buf.decode("utf-8", errors="replace"))
                try:
                    os.close(master_fd)
                except OSError:
                    pass

            rc = proc.wait()
            with self._lock:
                self._proc = None
                self._master_fd = None
            self._on_line(f"[Beendet mit Exit-Code {rc}]")
            self._on_finished(rc)

        threading.Thread(target=worker, daemon=True).start()

    def send_input(self, text: str):
        with self._lock:
            proc = self._proc
            master_fd = self._master_fd
        if proc is None or proc.poll() is not None:
            self._on_line("[Hinweis] Kein laufender Prozess, an den Eingaben gesendet werden können.")
            return
        try:
            if master_fd is not None:
                os.write(master_fd, (text + "\n").encode("utf-8"))
            else:
                proc.stdin.write(text + "\n")
                proc.stdin.flush()
            self._on_line(f"> {text}")
        except Exception as exc:
            self._on_line(f"[FEHLER beim Senden der Eingabe] {exc}")

    def terminate(self):
        """Beendet den laufenden Vorgang sauber. Läuft gerade eine PTY-Sitzung (z.B.
        'docker attach', siehe start_pty()), wird die von Docker vorgesehene
        Detach-Tastenkombination Strg+P Strg+Q gesendet - das trennt nur die lokale
        Sitzung und lässt den Container/Server unangetastet weiterlaufen. Reagiert der
        Prozess darauf nicht, wird nach kurzer Zeit auf ein Signal eskaliert.
        Bei normalen Prozessen wird direkt SIGINT (entspricht Strg+C) gesendet und bei
        Bedarf auf SIGTERM/SIGKILL eskaliert - viele Prozesse (u.a. 'docker compose
        logs -f', interaktive Extraktions-Skripte) reagieren nur auf ein echtes
        Strg+C-Signal sauber und beenden sich nicht bei SIGTERM allein."""
        with self._lock:
            proc = self._proc
            master_fd = self._master_fd
        if proc is None or proc.poll() is not None:
            return

        if master_fd is not None:
            try:
                os.write(master_fd, b"\x10\x11")  # Strg+P, Strg+Q
            except Exception:
                pass

            def escalate_after_detach():
                time.sleep(2)
                with self._lock:
                    p = self._proc
                if p is None or p.poll() is not None:
                    return
                try:
                    p.send_signal(signal.SIGINT)
                except Exception:
                    pass
                time.sleep(2)
                with self._lock:
                    p2 = self._proc
                if p2 is not None and p2.poll() is None:
                    try:
                        p2.terminate()
                    except Exception:
                        pass

            threading.Thread(target=escalate_after_detach, daemon=True).start()
            return

        try:
            proc.send_signal(signal.SIGINT)
        except Exception:
            pass

        def escalate():
            time.sleep(3)
            with self._lock:
                p = self._proc
            if p is None or p.poll() is not None:
                return
            try:
                p.terminate()
            except Exception:
                pass
            time.sleep(2)
            with self._lock:
                p = self._proc
            if p is not None and p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass

        threading.Thread(target=escalate, daemon=True).start()


# --------------------------------------------------------------------------
# Anklickbare Karte im Detailblock (Optik/Verhalten wie in menu_ui.py)
# --------------------------------------------------------------------------

class ActionCard(tk.Frame):
    def __init__(self, master, index, name: str, desc: str, command, enabled_when_idle=True):
        super().__init__(master, bg=T.CARD, cursor="hand2",
                          highlightthickness=1, highlightbackground=T.LINE)
        self.command = command
        self.enabled = True
        self.enabled_when_idle = enabled_when_idle

        self.num = tk.Label(self, text=str(index), bg=T.CARD, fg=T.ACCENT, font=FONT_BOLD, width=3)
        self.title = tk.Label(self, text=name, bg=T.CARD, fg=T.FG, font=FONT_BOLD, anchor="w", width=26)
        self.desc = tk.Label(self, text=desc, bg=T.CARD, fg=T.DIM, font=FONT, anchor="w")
        self.arrow = tk.Label(self, text="▸", bg=T.CARD, fg=T.DIM, font=("Segoe UI", 14))

        self.num.pack(side="left", padx=(10, 0), pady=11)
        self.title.pack(side="left", padx=(4, 10), pady=11)
        self.desc.pack(side="left", fill="x", expand=True, pady=11)
        self.arrow.pack(side="right", padx=12)

        for widget in self._parts():
            widget.bind("<Button-1>", self._click)
            widget.bind("<Enter>", lambda _e: self._paint(T.CARD_HOVER))
            widget.bind("<Leave>", lambda _e: self._paint(T.CARD))

    def _parts(self):
        return (self, self.num, self.title, self.desc, self.arrow)

    def _paint(self, color: str) -> None:
        if not self.enabled:
            return
        for widget in self._parts():
            widget.configure(bg=color)

    def _click(self, _event=None) -> None:
        if self.enabled:
            self.command()

    def set_enabled(self, enabled: bool) -> None:
        if not self.enabled_when_idle:
            return
        self.enabled = enabled
        self.configure(cursor="hand2" if enabled else "watch")
        self.title.configure(fg=T.FG if enabled else T.DIM)
        self._paint(T.CARD)


def dark_entry(master, textvariable=None, width=None, **kw):
    return tk.Entry(master, textvariable=textvariable, width=width, bg=T.CARD, fg=T.FG,
                     insertbackground=T.FG, relief="flat", highlightthickness=1,
                     highlightbackground=T.LINE, highlightcolor=T.ACCENT, font=FONT, **kw)


def dark_button(master, text, command, danger=False, **kw):
    return tk.Button(master, text=text, command=command, font=FONT_SMALL,
                      bg=T.CARD, fg=T.FG,
                      activebackground=T.RED if danger else T.ACCENT,
                      activeforeground="#ffffff", relief="flat", bd=0, padx=10, pady=4, **kw)


def dark_check(master, text, variable, **kw):
    return tk.Checkbutton(master, text=text, variable=variable, bg=T.PANEL, fg=T.FG,
                           selectcolor=T.CARD, activebackground=T.PANEL, activeforeground=T.FG,
                           font=FONT_SMALL, anchor="w", **kw)


# --------------------------------------------------------------------------
# Hauptanwendung
# --------------------------------------------------------------------------

#DB-Installation vorerst auskommentiert
#CATEGORIES = ("Setup", "Extraktion", "Ports", "DB-Installation", "DB-Updates", "Server")
CATEGORIES = ("Setup", "Extraktion", "Ports", "DB-Updates", "Server")


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()

        self.workspace_dir = tk.StringVar(value=self.cfg.get("workspace_dir", str(Path.home() / "cmangos-servers")))
        self.profile_key = tk.StringVar(value=self.cfg.get("profile_key", "tbc"))
        self.nocache_var = tk.BooleanVar(value=False)
        self.cleanup_var = tk.BooleanVar(value=True)
        self.cmangos_threads_var = tk.StringVar(value="")
        self.ac_quick_mmaps_var = tk.BooleanVar(value=True)
        self.port_vars: dict = {}
        self.console_vars: dict = {}
        self._console_vars_profile = None

        self.runner = ProcessRunner(self._on_line, self._on_finished)
        self._log_queue: "queue.Queue" = queue.Queue()
        self._chain: list = []
        self._ui_busy = False
        self.active_category = 0
        self.side_buttons = []
        self.cards: list = []

        root.title(APP_TITLE)
        root.geometry("1080x820")
        root.minsize(900, 660)
        root.configure(bg=T.BG)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_header()
        self._build_warning()

        # Vertikal verschiebbarer Trenner zwischen Navigation/Karten (oben) und
        # Fortschritt/Protokoll (unten) - per Maus ziehbar, damit z.B. das Protokoll
        # bei Bedarf größer gezogen werden kann.
        self.paned = tk.PanedWindow(self.root, orient=tk.VERTICAL, sashwidth=6,
                                     sashrelief="raised", bg=T.LINE, bd=0,
                                     showhandle=False, opaqueresize=True)
        self.paned.pack(side="top", fill="both", expand=True)
        body_frame = tk.Frame(self.paned, bg=T.BG)
        foot_frame = tk.Frame(self.paned, bg=T.BG)
        self.paned.add(body_frame, stretch="always", minsize=220)
        self.paned.add(foot_frame, stretch="always", minsize=140)

        self._build_body(body_frame)
        self._build_footer(foot_frame)

        self.select_category(0)
        self.root.after(100, self._pump)

    # -- Aufbau ----------------------------------------------------------

    def _build_header(self):
        head = tk.Frame(self.root, bg=T.SIDEBAR, height=56)
        head.pack(side="top", fill="x")
        head.pack_propagate(False)
        tk.Label(head, text=APP_TITLE, bg=T.SIDEBAR, fg=T.FG, font=FONT_TITLE).pack(side="left", padx=18)
        self.busy_label = tk.Label(head, text="", bg=T.SIDEBAR, fg=T.ACCENT, font=FONT_SMALL)
        self.busy_label.pack(side="right", padx=18)
        tk.Frame(self.root, bg=T.LINE, height=1).pack(side="top", fill="x")

    def _build_warning(self):
        tk.Label(
            self.root,
            text=("⚠ Alle drei Server verwenden standardmäßig überlappende Host-Ports (3724 / 8085, "
                  "zusätzlich 5001 bei CMaNGOS). CMaNGOS TBC und Classic teilen sich zudem identische "
                  "Container-Namen. Nacheinander betreiben oder unter 'Ports' eindeutige Host-Ports vergeben."),
            bg=T.BG, fg=T.YELLOW, font=FONT_SMALL, wraplength=1040, justify="left", anchor="w",
        ).pack(fill="x", padx=18, pady=(8, 4))

    def _build_body(self, parent):
        body = tk.Frame(parent, bg=T.BG)
        body.pack(side="top", fill="both", expand=True)

        # linke Spalte
        sidebar = tk.Frame(body, bg=T.SIDEBAR, width=210)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="HAUPTMENÜ", bg=T.SIDEBAR, fg=T.DIM,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=16, pady=(16, 8))
        for index, name in enumerate(CATEGORIES):
            self._add_side_button(sidebar, index, name)

        tk.Frame(body, bg=T.LINE, width=1).pack(side="left", fill="y")

        # rechte Spalte
        detail = tk.Frame(body, bg=T.PANEL)
        detail.pack(side="left", fill="both", expand=True)

        # Server-/Arbeitsverzeichnis-Auswahl (immer sichtbar, egal welche Kategorie)
        config_bar = tk.Frame(detail, bg=T.PANEL)
        config_bar.pack(fill="x", padx=22, pady=(14, 0))

        tk.Label(config_bar, text="Server:", bg=T.PANEL, fg=T.DIM, font=FONT_SMALL).grid(row=0, column=0, sticky="w")
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Dark.TCombobox", fieldbackground=T.CARD, background=T.CARD,
                        foreground=T.FG, arrowcolor=T.FG, bordercolor=T.LINE)
        style.map("Dark.TCombobox", fieldbackground=[("readonly", T.CARD)],
                  foreground=[("readonly", T.FG)])
        self.profile_box = ttk.Combobox(config_bar, state="readonly", width=28, style="Dark.TCombobox",
                                        values=[p.label for p in PROFILES.values()])
        self.profile_box.set(PROFILES[self.profile_key.get()].label)
        self.profile_box.grid(row=0, column=1, sticky="w", padx=(6, 18))
        self.profile_box.bind("<<ComboboxSelected>>", lambda e: self._on_profile_change(self.profile_box.get()))

        tk.Label(config_bar, text="Arbeitsverzeichnis:", bg=T.PANEL, fg=T.DIM, font=FONT_SMALL).grid(row=0, column=2, sticky="w")
        self.workspace_entry = dark_entry(config_bar, textvariable=self.workspace_dir, width=44)
        self.workspace_entry.grid(row=0, column=3, sticky="we", padx=6)
        dark_button(config_bar, "Durchsuchen…", self._choose_workspace).grid(row=0, column=4)
        config_bar.columnconfigure(3, weight=1)

        self.project_path_label = tk.Label(detail, text="", bg=T.PANEL, fg=T.DIM, font=FONT_SMALL, anchor="w")
        self.project_path_label.pack(fill="x", padx=22, pady=(4, 0))

        tk.Frame(detail, bg=T.LINE, height=1).pack(fill="x", padx=22, pady=(10, 0))

        self.detail_title = tk.Label(detail, text="", bg=T.PANEL, fg=T.FG, font=FONT_TITLE, anchor="w")
        self.detail_title.pack(fill="x", padx=22, pady=(10, 0))
        self.detail_desc = tk.Label(detail, text="", bg=T.PANEL, fg=T.DIM, font=FONT, anchor="w")
        self.detail_desc.pack(fill="x", padx=22, pady=(2, 10))
        tk.Frame(detail, bg=T.LINE, height=1).pack(fill="x", padx=22)

        self.options_area = tk.Frame(detail, bg=T.PANEL)
        self.options_area.pack(fill="x", padx=16, pady=(10, 0))

        item_container = tk.Frame(detail, bg=T.PANEL)
        item_container.pack(fill="both", expand=True, padx=16, pady=10)

        item_canvas = tk.Canvas(item_container, bg=T.PANEL, highlightthickness=0, bd=0)
        item_scroll = ttk.Scrollbar(item_container, orient="vertical", command=item_canvas.yview)
        item_canvas.configure(yscrollcommand=item_scroll.set)
        item_canvas.pack(side="left", fill="both", expand=True)
        item_scroll.pack(side="right", fill="y")

        self.item_area = tk.Frame(item_canvas, bg=T.PANEL)
        item_window = item_canvas.create_window((0, 0), window=self.item_area, anchor="nw")

        def _on_item_area_configure(_event):
            item_canvas.configure(scrollregion=item_canvas.bbox("all"))

        def _on_canvas_configure(event):
            item_canvas.itemconfig(item_window, width=event.width)

        self.item_area.bind("<Configure>", _on_item_area_configure)
        item_canvas.bind("<Configure>", _on_canvas_configure)

        def _wheel(event):
            delta = event.delta
            if delta:
                item_canvas.yview_scroll(int(-1 * (delta / 120)), "units")

        def _bind_wheel(_e):
            item_canvas.bind_all("<MouseWheel>", _wheel)
            item_canvas.bind_all("<Button-4>", lambda e: item_canvas.yview_scroll(-3, "units"))
            item_canvas.bind_all("<Button-5>", lambda e: item_canvas.yview_scroll(3, "units"))

        def _unbind_wheel(_e):
            item_canvas.unbind_all("<MouseWheel>")
            item_canvas.unbind_all("<Button-4>")
            item_canvas.unbind_all("<Button-5>")

        item_canvas.bind("<Enter>", _bind_wheel)
        item_canvas.bind("<Leave>", _unbind_wheel)

    def _add_side_button(self, parent, index, name):
        row = tk.Frame(parent, bg=T.SIDEBAR, cursor="hand2")
        row.pack(fill="x")
        marker = tk.Frame(row, bg=T.SIDEBAR, width=3)
        marker.pack(side="left", fill="y")
        text = tk.Label(row, text=name, bg=T.SIDEBAR, fg=T.DIM, font=FONT_BOLD, anchor="w", padx=14, pady=10)
        text.pack(side="left", fill="x", expand=True)
        for widget in (row, text):
            widget.bind("<Button-1>", lambda _e, i=index: self.select_category(i))
        self.side_buttons.append((row, marker, text))

    def _build_footer(self, parent):
        tk.Frame(parent, bg=T.LINE, height=1).pack(side="top", fill="x")
        foot = tk.Frame(parent, bg=T.BG)
        foot.pack(side="top", fill="both", expand=True)

        box = tk.Frame(foot, bg=T.PANEL, highlightthickness=1, highlightbackground=T.LINE)
        box.pack(fill="x", padx=14, pady=(12, 8))

        top = tk.Frame(box, bg=T.PANEL)
        top.pack(fill="x", padx=14, pady=(10, 2))
        self.prog_title = tk.Label(top, text="Bereit", bg=T.PANEL, fg=T.FG, font=FONT_BOLD, anchor="w")
        self.prog_title.pack(side="left")

        style = ttk.Style(self.root)
        style.configure("Bar.Horizontal.TProgressbar", troughcolor=T.CARD, bordercolor=T.CARD,
                        background=T.ACCENT, lightcolor=T.ACCENT, darkcolor=T.ACCENT, thickness=14)
        self.prog_var = tk.DoubleVar(value=0.0)
        self.prog_bar = ttk.Progressbar(box, style="Bar.Horizontal.TProgressbar",
                                        variable=self.prog_var, maximum=100.0)
        self.prog_bar.pack(fill="x", padx=14, pady=4)

        bottom = tk.Frame(box, bg=T.PANEL)
        bottom.pack(fill="x", padx=14, pady=(2, 10))
        self.prog_status = tk.Label(bottom, text="", bg=T.PANEL, fg=T.DIM, font=FONT_SMALL, anchor="w")
        self.prog_status.pack(side="left", fill="x", expand=True)
        self.cancel_btn = dark_button(bottom, "Abbrechen", self.action_terminate, danger=True, state="disabled")
        self.cancel_btn.pack(side="right")

        console = tk.Frame(foot, bg=T.BG)
        console.pack(side="bottom", fill="x", padx=14, pady=(0, 12))
        tk.Label(console, text="Eingabe an laufenden Prozess:", bg=T.BG, fg=T.DIM, font=FONT_SMALL).pack(side="left")
        self.console_var = tk.StringVar()
        self.console_entry = dark_entry(console, textvariable=self.console_var)
        self.console_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.console_entry.bind("<Return>", lambda e: self._send_console_input())
        dark_button(console, "Senden", self._send_console_input).pack(side="left")
        dark_button(console, "⏎ Enter", lambda: self.runner.send_input("")).pack(side="left", padx=(6, 0))
        dark_button(console, "y", lambda: self.runner.send_input("y")).pack(side="left", padx=2)
        dark_button(console, "n", lambda: self.runner.send_input("n")).pack(side="left", padx=2)

        logbox = tk.Frame(foot, bg=T.BG)
        logbox.pack(side="top", fill="both", expand=True, padx=14, pady=(0, 6))
        self.log = tk.Text(logbox, height=10, bg="#14171d", fg=T.DIM, font=FONT_MONO, relief="flat",
                            wrap="word", insertbackground=T.FG, state="disabled",
                            highlightthickness=1, highlightbackground=T.LINE)
        scroll = ttk.Scrollbar(logbox, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_configure("info", foreground=T.DIM)
        self.log.tag_configure("ok", foreground=T.GREEN)
        self.log.tag_configure("warn", foreground=T.YELLOW)
        self.log.tag_configure("err", foreground=T.RED)

    # -- Kategorien / Karten ------------------------------------------------

    def select_category(self, index: int):
        self.active_category = index
        for i, (row, marker, text) in enumerate(self.side_buttons):
            chosen = i == index
            bg = T.PANEL if chosen else T.SIDEBAR
            row.configure(bg=bg)
            text.configure(bg=bg, fg=T.FG if chosen else T.DIM)
            marker.configure(bg=T.ACCENT if chosen else bg)
        self.render_detail()

    def render_detail(self):
        name = CATEGORIES[self.active_category]
        descriptions = {
            "Setup": "Repository holen, sql-init entpacken, Docker-Image bauen.",
            "Extraktion": "WoW-Daten (Maps/VMaps/MMaps/DBC) aus dem Client extrahieren.",
            "Ports": "Host-Ports und Container-Namen dieses Servers anpassen (wird in die docker-compose.yml geschrieben).",
            "DB-Installation": "Datenbank komplett neu installieren/befüllen (classic-db/tbc-db + mangos-Core + Playerbots).",
            "DB-Updates": "CMaNGOS-Core-Repo klonen und fehlende SQL-Updates in die laufende Datenbank einspielen.",
            "Server": "Server starten, stoppen, Status und Logs ansehen.",
        }
        self.detail_title.configure(text=name)
        self.detail_desc.configure(text=descriptions[name])
        self._update_project_path_label()

        for w in self.options_area.winfo_children():
            w.destroy()
        for w in self.item_area.winfo_children():
            w.destroy()
        self.cards = []

        profile = self.current_profile()

        if name == "Setup":
            opts = tk.Frame(self.options_area, bg=T.PANEL)
            opts.pack(fill="x", pady=(0, 8))
            dark_check(opts, "ohne Cache bauen (--no-cache)", self.nocache_var).pack(side="left", padx=(0, 16))
            dark_check(opts, "danach aufräumen (dangling Images + Builder-Cache)", self.cleanup_var).pack(side="left")

            entries = [
                ("Klonen / Aktualisieren", "git clone bzw. git pull des ausgewählten Repos", self.action_clone_or_update),
            ]
            if profile.needs_sql_init_unpack:
                entries.append(("sql-init entpacken", "Archive unter sql-init/ vor dem ersten Start entpacken", self.action_unpack_sql_init))
            entries.append(("Docker Image bauen", f"docker build -t {profile.image_name} .", self.action_build_image))
            self._render_cards(entries)

        elif name == "Extraktion":
            if profile.extraction_mode == "cmangos":
                opts = tk.Frame(self.options_area, bg=T.PANEL)
                opts.pack(fill="x", pady=(0, 8))
                tk.Label(opts, text="CPU-Threads für MMaps (leer = alle verfügbaren):",
                         bg=T.PANEL, fg=T.DIM, font=FONT_SMALL).pack(side="left", padx=(0, 6))
                dark_entry(opts, textvariable=self.cmangos_threads_var, width=6).pack(side="left")
                entries = [
                    ("Extraktion starten", "Maps/VMaps/MMaps/DBC (ein interaktives Skript, Fragen werden automatisch beantwortet)",
                     self.action_extract_cmangos),
                    ("Rechte korrigieren + verschieben", "Besitzrechte fixen (ohne sudo) und Ordner nach data/ verschieben",
                     self.action_finalize_data),
                    ("wow-client aufräumen", "Übrig gebliebene maps/dbc/vmaps/mmaps/cameras-Ordner aus einem vorherigen "
                     "Lauf entfernen (behebt 'output directory seems to be polluted' bei erneuter Extraktion)",
                     self.action_clean_wow_client),
                ]
            else:
                info = tk.Label(
                    self.options_area,
                    text=("Hinweis: Der Server benötigt enUS-DBCs (Bot-Spellsystem). Bei einem deDE-Client "
                          "vorher enUS-DBCs zusätzlich nach wow-client/Data/enUS legen."),
                    bg=T.PANEL, fg=T.DIM, font=FONT_SMALL, wraplength=760, justify="left", anchor="w",
                )
                info.pack(fill="x", pady=(0, 8))
                dark_check(self.options_area, "MMaps: nur Map 0 + 1 (schneller Teststart)",
                           self.ac_quick_mmaps_var).pack(anchor="w", pady=(0, 8))
                entries = [
                    ("1) Maps + DBC + Cameras", "map_extractor ausführen", self.action_extract_ac_maps),
                    ("2) VMaps", "vmap4_extractor + vmap4_assembler ausführen (30-60 Min.)", self.action_extract_ac_vmaps),
                    ("3) MMaps", "mmaps_generator ausführen — schreibt direkt nach data/mmaps", self.action_extract_ac_mmaps),
                    ("Rechte korrigieren + verschieben", "Besitzrechte fixen (ohne sudo) und maps/dbc/vmaps/cameras nach data/ verschieben",
                     self.action_finalize_data),
                    ("wow-client aufräumen", "Übrig gebliebene maps/dbc/vmaps/mmaps/Cameras/Buildings-Ordner aus einem "
                     "vorherigen Lauf entfernen (behebt 'output directory seems to be polluted' bei erneuter Extraktion)",
                     self.action_clean_wow_client),
                ]
            self._render_cards(entries)

        elif name == "Ports":
            self._render_ports_panel()

        elif name == "DB-Installation":
            self._render_db_install_panel()

        elif name == "DB-Updates":
            self._render_db_updates_panel()

        elif name == "Server":
            self._ensure_console_vars(profile)
            row = tk.Frame(self.options_area, bg=T.PANEL)
            row.pack(fill="x", pady=(0, 8))
            for key, label, width in (("container", "DB-Container", 12), ("user", "DB-User", 10),
                                       ("password", "DB-Passwort", 10), ("bin", "DB-Client", 8)):
                cell = tk.Frame(row, bg=T.PANEL)
                cell.pack(side="left", padx=(0, 14))
                tk.Label(cell, text=label, bg=T.PANEL, fg=T.DIM, font=FONT_SMALL).pack(anchor="w")
                dark_entry(cell, textvariable=self.console_vars[key], width=width).pack(anchor="w")

            entries = [
                ("Nur Datenbank starten", "docker compose up <db-service> -d", self.action_start_db),
                ("Server starten (komplett)", "docker compose up -d", self.action_start_all),
                ("Server stoppen", "docker compose down", self.action_stop),
                ("Status anzeigen", "docker compose ps", self.action_status),
                ("Logs (Worldserver) live", "docker compose logs -f (mit 'Abbrechen' beenden)", self.action_logs_worldserver),
                ("DB-Konsole öffnen", "Interaktive SQL-Konsole im Datenbank-Container (Accounts/Charaktere per SQL bearbeiten)",
                 self.action_db_console),
                ("Server-Konsole (docker attach)", f"Live-Konsole von '{self._container_name(profile, 'world')}' — "
                 "GM-/Account-Befehle eingeben (z.B. 'account create name pass email')", self.action_world_console),
            ]
            self._render_cards(entries)

        self._refresh_busy_state()

    def _render_cards(self, entries):
        for number, (title, desc, command) in enumerate(entries, start=1):
            card = ActionCard(self.item_area, number, title, desc, command)
            card.pack(fill="x", pady=4, padx=6)
            self.cards.append(card)

    def _render_ports_panel(self):
        profile = self.current_profile()
        saved = self.cfg.get("ports", {}).get(profile.key, {})
        self.port_vars = {}

        panel = tk.Frame(self.item_area, bg=T.PANEL)
        panel.pack(fill="x", pady=4, padx=6)

        for pdef in profile.ports:
            row = tk.Frame(panel, bg=T.CARD, highlightthickness=1, highlightbackground=T.LINE)
            row.pack(fill="x", pady=4)
            tk.Label(row, text=f"{pdef.label}", bg=T.CARD, fg=T.FG, font=FONT_BOLD,
                     anchor="w", width=22).pack(side="left", padx=(12, 4), pady=10)
            tk.Label(row, text=f"Container-Port {pdef.container_port}", bg=T.CARD, fg=T.DIM,
                     font=FONT_SMALL, anchor="w").pack(side="left", padx=4)
            value = saved.get(pdef.container_port, pdef.default_host_port)
            var = tk.StringVar(value=value)
            dark_entry(row, textvariable=var, width=8).pack(side="right", padx=12, pady=10)
            tk.Label(row, text="Host-Port:", bg=T.CARD, fg=T.DIM, font=FONT_SMALL).pack(side="right")
            self.port_vars[pdef.container_port] = var

        dark_button(panel, "Ports in docker-compose.yml übernehmen", self.action_apply_ports).pack(
            anchor="w", pady=(10, 4))

        hint = tk.Label(
            panel,
            text=("Ändert die docker-compose.yml direkt im geklonten Projektordner. "
                  "'git checkout -- docker-compose.yml' macht das rückgängig. Server danach neu starten, "
                  "damit die neuen Ports greifen."),
            bg=T.PANEL, fg=T.DIM, font=FONT_SMALL, wraplength=760, justify="left", anchor="w",
        )
        hint.pack(fill="x", pady=(4, 0))

        # --- Container-Namen ---
        tk.Frame(self.item_area, bg=T.LINE, height=1).pack(fill="x", padx=6, pady=(16, 12))
        tk.Label(self.item_area, text="Container-Namen", bg=T.PANEL, fg=T.FG,
                 font=FONT_BOLD, anchor="w").pack(fill="x", padx=6)

        names_panel = tk.Frame(self.item_area, bg=T.PANEL)
        names_panel.pack(fill="x", pady=(6, 4), padx=6)

        saved_names = self.cfg.get("containers", {}).get(profile.key, {})
        self.container_name_vars = {}
        for role in profile.container_roles:
            row = tk.Frame(names_panel, bg=T.CARD, highlightthickness=1, highlightbackground=T.LINE)
            row.pack(fill="x", pady=4)
            tk.Label(row, text=role.label, bg=T.CARD, fg=T.FG, font=FONT_BOLD,
                     anchor="w", width=26).pack(side="left", padx=(12, 4), pady=10)
            tk.Label(row, text=f"Standard: {role.default_name}", bg=T.CARD, fg=T.DIM,
                     font=FONT_SMALL, anchor="w").pack(side="left", padx=4)
            value = saved_names.get(role.key, role.default_name)
            var = tk.StringVar(value=value)
            dark_entry(row, textvariable=var, width=24).pack(side="right", padx=12, pady=10)
            tk.Label(row, text="Name:", bg=T.CARD, fg=T.DIM, font=FONT_SMALL).pack(side="right")
            self.container_name_vars[role.key] = var

        dark_button(names_panel, "Container-Namen in docker-compose.yml übernehmen",
                    self.action_apply_container_names).pack(anchor="w", pady=(10, 4))

        hint2 = tk.Label(
            names_panel,
            text=("Ändert 'container_name:' direkt in der docker-compose.yml. Bereits laufende Container "
                  "behalten ihren alten Namen, bis der Server neu erstellt wird ('Server stoppen' + "
                  "'Server starten'). CMaNGOS TBC und Classic verwenden standardmäßig identische Namen — "
                  "hier eindeutige Namen vergeben, um beide gleichzeitig zu betreiben (zusammen mit "
                  "eindeutigen Ports oben)."),
            bg=T.PANEL, fg=T.DIM, font=FONT_SMALL, wraplength=760, justify="left", anchor="w",
        )
        hint2.pack(fill="x", pady=(4, 0))

    def _ensure_db_install_vars(self, profile: ServerProfile):
        if getattr(self, "_db_install_vars_profile", None) == profile.key and getattr(self, "db_install_vars", None):
            return
        saved = self.cfg.get("db_install", {}).get(profile.key, {})
        self.db_install_vars = {
            "root_user": tk.StringVar(value=saved.get("root_user", "root")),
            "root_pass": tk.StringVar(value=saved.get("root_pass", "root")),
        }
        self._db_install_vars_profile = profile.key

    def _render_db_install_panel(self):
        profile = self.current_profile()

        if not profile.db_install_repo_url:
            tk.Label(
                self.item_area,
                text=(f"{profile.label} nutzt hierfür einen eigenen DB-Import beim Start — hier ist keine "
                      "Aktion nötig."),
                bg=T.PANEL, fg=T.DIM, font=FONT, wraplength=760, justify="left", anchor="w",
            ).pack(fill="x", pady=8, padx=6)
            return

        self._ensure_db_install_vars(profile)
        db_container = self._container_name(profile, "db")
        characters_db = f"{profile.key}characters"
        realmd_db = f"{profile.key}realmd"
        logs_db = f"{profile.key}logs"

        panel = tk.Frame(self.item_area, bg=T.PANEL)
        panel.pack(fill="x", pady=4, padx=6)

        info = tk.Label(
            panel,
            text=(f"DB-Repo: {profile.db_install_repo_url}   |   Core-Repo: {profile.db_core_repo_url}   |   "
                  f"Playerbots: {profile.playerbots_repo_url}\n"
                  f"Datenbanken: {profile.db_name} (World), {characters_db}, {realmd_db}, {logs_db}   "
                  f"(Container {db_container}, Nutzer {DB_USER})"),
            bg=T.PANEL, fg=T.DIM, font=FONT_SMALL, justify="left", anchor="w",
        )
        info.pack(fill="x", pady=(0, 8))

        root_row = tk.Frame(panel, bg=T.PANEL)
        root_row.pack(fill="x", pady=(0, 10))
        tk.Label(root_row, text="Root-User (für GRANT):", bg=T.PANEL, fg=T.FG, font=FONT_SMALL).pack(side="left")
        dark_entry(root_row, textvariable=self.db_install_vars["root_user"], width=10).pack(side="left", padx=(4, 14))
        tk.Label(root_row, text="Root-Passwort:", bg=T.PANEL, fg=T.FG, font=FONT_SMALL).pack(side="left")
        dark_entry(root_row, textvariable=self.db_install_vars["root_pass"], width=10).pack(side="left", padx=4)

        entries = [
            ("1) Datenbank starten", "docker compose up db -d", self.action_start_db),
            ("2) Repos klonen", f"{profile.db_install_dirname} + mangos-Core (cmangos-core/) + playerbots klonen/aktualisieren",
             self.action_db_install_clone),
            ("3) InstallFullDB.config anpassen", f"MYSQL_HOST/USERNAME/PASSWORD/USERIP/PATH/CORE_PATH in "
             f"{profile.db_install_dirname}/InstallFullDB.config setzen",
             self.action_db_install_configure),
            ("Root-Passwort suchen", "MYSQL_ROOT_PASSWORD/MARIADB_ROOT_PASSWORD aus docker-compose.yml/.env "
             "auflösen und oben automatisch eintragen (falls 'DB-User Rechte vergeben' mit Access denied fehlschlägt)",
             self.action_db_install_find_root_pass),
            ("4) DB-User Rechte vergeben", "GRANT ALL PRIVILEGES für 'mangos'@'%' (per Root-Zugang oben)",
             self.action_db_install_grant),
            ("5) Hauptdatenbank installieren", "InstallFullDB.sh interaktiv ausführen — Eingaben unten im "
             "Konsolenfeld tätigen (z.B. 4, 1, DeleteAll). ACHTUNG: kann bestehende Daten löschen!",
             self.action_db_install_run),
            ("6) DB-Updates einspielen (Core-Fallback)", f"Nur falls Schritt 5 bei Option 3 mit Fehler abbricht: "
             f"characters/realmd/logs Basis-Schema + Updates aus dem Core-Repo manuell einspielen",
             self.action_db_install_core_updates),
            ("7) Playerbots-Tabellen einspielen", "Playerbots-SQL für characters- und world-Datenbank importieren "
             "(Pflicht für Random-Bots/AddAItem)",
             self.action_db_install_playerbots),
            ("Tabellen prüfen", f"SHOW TABLES LIKE 'ai_playerbot%' in {characters_db} (sollte ~10 Tabellen zeigen)",
             self.action_db_install_verify),
            ("8) Alles starten", "docker compose up -d", self.action_start_all),
        ]
        self._render_cards(entries)

    def _render_db_updates_panel(self):
        profile = self.current_profile()

        if not profile.db_core_repo_url:
            tk.Label(
                self.item_area,
                text=(f"{profile.label} aktualisiert seine Datenbanken beim Start automatisch über einen "
                      "eigenen DB-Updater — hier ist keine Aktion nötig."),
                bg=T.PANEL, fg=T.DIM, font=FONT, wraplength=760, justify="left", anchor="w",
            ).pack(fill="x", pady=8, padx=6)
            return

        saved = self.cfg.get("db_update_range", {}).get(profile.key, {})
        default_from = "z2831" if profile.key == "classic" else ""
        default_to = "z2837" if profile.key == "classic" else ""

        panel = tk.Frame(self.item_area, bg=T.PANEL)
        panel.pack(fill="x", pady=4, padx=6)

        info = tk.Label(
            panel,
            text=(f"Core-Repo: {profile.db_core_repo_url}\n"
                  f"Datenbank: {profile.db_name}  (Container {self._container_name(profile, 'db')}, Nutzer {DB_USER})"),
            bg=T.PANEL, fg=T.DIM, font=FONT_SMALL, justify="left", anchor="w",
        )
        info.pack(fill="x", pady=(0, 8))

        range_row = tk.Frame(panel, bg=T.PANEL)
        range_row.pack(fill="x", pady=(0, 10))
        tk.Label(range_row, text="SQL-Updates von", bg=T.PANEL, fg=T.FG, font=FONT_SMALL).pack(side="left")
        self.db_from_var = tk.StringVar(value=saved.get("from", default_from))
        dark_entry(range_row, textvariable=self.db_from_var, width=8).pack(side="left", padx=4)
        tk.Label(range_row, text="bis", bg=T.PANEL, fg=T.FG, font=FONT_SMALL).pack(side="left", padx=(10, 0))
        self.db_to_var = tk.StringVar(value=saved.get("to", default_to))
        dark_entry(range_row, textvariable=self.db_to_var, width=8).pack(side="left", padx=4)
        tk.Label(range_row, text="(Buchstabe(n) + Nummer, z.B. z2831 bis z2837 oder s2000 bis s2010 — "
                 "TBC nutzt sowohl 's' als auch 'z'; bei Bedarf beide Präfixe nacheinander einspielen)",
                 bg=T.PANEL, fg=T.DIM, font=FONT_SMALL).pack(side="left", padx=(10, 0))

        entries = [
            ("Core-Repo klonen / aktualisieren", f"git clone/pull von {profile.db_core_repo_url} nach cmangos-core/",
             self.action_db_clone_core),
            ("SQL-Updates einspielen", f"Passende <Präfix><Version>_*.sql (z.B. z2831_*.sql oder s2000_*.sql) aus "
             f"sql/updates/mangos/ nacheinander in '{profile.db_name}' einspielen (bricht bei erstem Fehler ab)",
             self.action_db_apply_updates),
        ]
        self._render_cards(entries)

    def _current_container_names(self, profile: ServerProfile) -> dict:
        """Aktuelle (ggf. vom Nutzer angepasste) Containernamen für ein Profil,
        role_key -> Name. Fällt auf die Compose-Standardnamen zurück."""
        saved = self.cfg.get("containers", {}).get(profile.key, {})
        return {role.key: saved.get(role.key, role.default_name) for role in profile.container_roles}

    def _container_name(self, profile: ServerProfile, role_key: str) -> str:
        return self._current_container_names(profile).get(role_key, "")

    def _ensure_console_vars(self, profile: ServerProfile):
        if self._console_vars_profile == profile.key and self.console_vars:
            return
        saved = self.cfg.get("console", {}).get(profile.key, {})
        defaults = {
            "container": self._container_name(profile, "db"),
            "user": profile.console_db_user,
            "password": profile.console_db_pass,
            "bin": profile.console_db_bin,
        }
        self.console_vars = {k: tk.StringVar(value=saved.get(k, v)) for k, v in defaults.items()}
        self._console_vars_profile = profile.key

    def _refresh_busy_state(self):
        busy = self._ui_busy
        self.busy_label.configure(text="Vorgang läuft …" if busy else "")
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        for card in self.cards:
            card.set_enabled(not busy)

    # -- Profil / Arbeitsverzeichnis -----------------------------------

    def current_profile(self) -> ServerProfile:
        return PROFILES[self.profile_key.get()]

    def project_dir(self) -> Path:
        return Path(self.workspace_dir.get()).expanduser() / self.current_profile().repo_dirname

    def _choose_workspace(self):
        chosen = filedialog.askdirectory(title="Arbeitsverzeichnis wählen", parent=self.root)
        if chosen:
            self.workspace_dir.set(chosen)
            self._update_project_path_label()
            self._persist_config()

    def _on_profile_change(self, label: str):
        for key, profile in PROFILES.items():
            if profile.label == label:
                self.profile_key.set(key)
                break
        self._persist_config()
        self.render_detail()

    def _update_project_path_label(self):
        self.project_path_label.configure(text=f"Projektordner: {self.project_dir()}")

    def _persist_config(self):
        self.cfg["workspace_dir"] = self.workspace_dir.get()
        self.cfg["profile_key"] = self.profile_key.get()
        save_config(self.cfg)

    def _send_console_input(self):
        # Auch eine leere Eingabe wird gesendet (reines Enter/Return) - manche
        # interaktiven Skripte erwarten genau das, z.B. "Press Enter to continue".
        text = self.console_var.get()
        self.console_var.set("")
        self.runner.send_input(text)

    # -- Log / Fortschritt ------------------------------------------------

    def _on_line(self, line: str):
        self._log_queue.put(("line", line))

    def _on_finished(self, rc: int):
        self._log_queue.put(("finished", rc))

    def _pump(self):
        try:
            while True:
                kind, payload = self._log_queue.get_nowait()
                if kind == "line":
                    self._append_log(payload)
                elif kind == "finished":
                    self._finish_progress(payload)
                    self._advance_chain(payload)
        except queue.Empty:
            pass
        try:
            self.root.after(100, self._pump)
        except tk.TclError:
            pass

    def _append_log(self, line: str):
        kind = "info"
        if line.startswith("[FEHLER") or line.startswith("[Ablauf abgebrochen"):
            kind = "err"
        elif line.startswith("[Warnung") or line.startswith("[Hinweis"):
            kind = "warn"
        elif line.startswith("[Beendet mit Exit-Code 0]"):
            kind = "ok"
        elif line.startswith("[Beendet mit Exit-Code"):
            kind = "err"
        elif line.startswith("$ "):
            kind = "info"
        elif line.startswith("> "):
            kind = "ok"
        stamp = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"{stamp}  {line}\n", kind)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _start_progress(self, title: str):
        self._ui_busy = True
        self.prog_title.configure(text=title, fg=T.FG)
        self.prog_status.configure(text="")
        self.prog_bar.configure(mode="indeterminate")
        self.prog_bar.start(14)
        self._refresh_busy_state()

    def _finish_progress(self, rc: int):
        self._ui_busy = False
        self.prog_bar.stop()
        self.prog_bar.configure(mode="determinate")
        self.prog_var.set(100.0)
        color = T.GREEN if rc == 0 else T.RED
        self.prog_title.configure(fg=color)
        self._set_bar_color(color)
        self.prog_status.configure(text="Fertig." if rc == 0 else f"Fehlgeschlagen (Exit-Code {rc}).")
        self._refresh_busy_state()

    def _set_bar_color(self, color: str):
        ttk.Style(self.root).configure("Bar.Horizontal.TProgressbar", background=color,
                                        lightcolor=color, darkcolor=color)

    # -- Befehlsketten ----------------------------------------------------

    def _run(self, cmd, cwd=None, title=None):
        if title:
            self._append_log(f"--- {title} ---")
            self._start_progress(title)
        self.runner.start(cmd, cwd=cwd)

    def _run_pty(self, cmd, cwd=None, title=None):
        if title:
            self._append_log(f"--- {title} ---")
            self._start_progress(title)
        self.runner.start_pty(cmd, cwd=cwd)

    def _run_chain(self, steps):
        self._chain = list(steps)
        self._advance_chain(0)

    def _advance_chain(self, rc):
        if rc != 0:
            if self._chain:
                self._append_log(f"[Ablauf abgebrochen wegen Exit-Code {rc}]")
            self._chain = []
            return
        if not self._chain:
            return
        step = self._chain.pop(0)
        step()

    def _chain_cmd(self, cmd, cwd=None, title=None):
        def step():
            self._run(cmd, cwd=cwd, title=title)
        return step

    def _chain_sync(self, fn, title=None):
        def step():
            if title:
                self._append_log(f"--- {title} ---")
            try:
                fn()
            except Exception as exc:
                self._append_log(f"[FEHLER] {exc}")
            self._advance_chain(0)
        return step

    # -- Vorbedingungen ----------------------------------------------------

    def _guard_busy(self) -> bool:
        if self._ui_busy or self.runner.busy:
            messagebox.showwarning("Beschäftigt", "Es läuft bereits ein Vorgang. Bitte warten oder stoppen.",
                                    parent=self.root)
            return True
        return False

    def _require_project_dir(self) -> bool:
        if not self.project_dir().exists():
            messagebox.showerror("Fehlt", f"Projektordner existiert nicht:\n{self.project_dir()}\n\n"
                                           "Zuerst 'Klonen / Aktualisieren' ausführen.", parent=self.root)
            return False
        return True

    def _tool_available(self, name: str) -> bool:
        return shutil.which(name) is not None

    def _require_compose_file(self) -> bool:
        if not (self.project_dir() / "docker-compose.yml").exists():
            messagebox.showerror("Fehlt", "Keine docker-compose.yml im Projektordner gefunden.", parent=self.root)
            return False
        return True

    def _wow_client_dir(self):
        wow_client = self.project_dir() / "wow-client"
        if not wow_client.exists():
            messagebox.showerror(
                "wow-client fehlt",
                f"Es wurde kein Ordner 'wow-client' unter\n{self.project_dir()}\ngefunden.\n\n"
                "Bitte den vorhandenen WoW-Client zunächst manuell dorthin kopieren.", parent=self.root)
            return None
        return wow_client

    # -- Aktionen: Setup ----------------------------------------------------

    def action_clone_or_update(self):
        if self._guard_busy():
            return
        if not self._tool_available("git"):
            messagebox.showerror("git fehlt", "git wurde nicht gefunden. Bitte installieren.", parent=self.root)
            return
        profile = self.current_profile()
        workspace = Path(self.workspace_dir.get()).expanduser()
        workspace.mkdir(parents=True, exist_ok=True)
        project = self.project_dir()
        self._clear_log()
        if (project / ".git").exists():
            self._run(["git", "pull"], cwd=str(project), title=f"{profile.label}: Repository aktualisieren")
        else:
            self._run(["git", "clone", profile.repo_url, str(project)], cwd=str(workspace),
                       title=f"{profile.label}: Repository klonen")

    def action_apply_ports(self):
        if self._guard_busy():
            return
        if not self._require_project_dir() or not self._require_compose_file():
            return
        profile = self.current_profile()

        port_map = {}
        for pdef in profile.ports:
            value = self.port_vars[pdef.container_port].get().strip()
            if not value.isdigit() or not (1 <= int(value) <= 65535):
                messagebox.showerror("Ungültiger Port", f"'{value}' ist kein gültiger Port (1-65535) für {pdef.label}.",
                                      parent=self.root)
                return
            port_map[pdef.container_port] = value

        seen = {}
        for cport, hport in port_map.items():
            if hport in seen:
                messagebox.showerror("Doppelter Port", f"Host-Port {hport} ist mehrfach vergeben.", parent=self.root)
                return
            seen[hport] = cport

        self._clear_log()
        self._append_log(f"--- {profile.label}: Ports übernehmen ---")
        try:
            changes = apply_ports_to_compose(self.project_dir(), port_map)
        except Exception as exc:
            self._append_log(f"[FEHLER] Konnte docker-compose.yml nicht anpassen: {exc}")
            return

        for c in changes or ["Keine Änderungen — Ports entsprachen bereits den eingetragenen Werten."]:
            self._append_log(c)

        self.cfg.setdefault("ports", {})[profile.key] = port_map
        save_config(self.cfg)
        self._append_log(
            "[Hinweis] docker-compose.yml wurde direkt im Projektordner geändert (git checkout -- "
            "docker-compose.yml macht das rückgängig). Server ggf. neu starten."
        )

    def action_apply_container_names(self):
        if self._guard_busy():
            return
        if not self._require_project_dir() or not self._require_compose_file():
            return
        profile = self.current_profile()
        current = self._current_container_names(profile)

        name_re = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*$')
        name_map = {}
        seen = {}
        for role in profile.container_roles:
            new_name = self.container_name_vars[role.key].get().strip()
            if not new_name:
                messagebox.showerror("Ungültiger Name", f"Der Container-Name für '{role.label}' darf nicht leer sein.",
                                      parent=self.root)
                return
            if not name_re.match(new_name):
                messagebox.showerror("Ungültiger Name",
                                      f"'{new_name}' ist kein gültiger Docker-Container-Name (erlaubt: Buchstaben, "
                                      "Ziffern, '_', '.', '-', darf nicht mit Sonderzeichen beginnen).",
                                      parent=self.root)
                return
            if new_name in seen:
                messagebox.showerror("Doppelter Name", f"Der Container-Name '{new_name}' ist mehrfach vergeben.",
                                      parent=self.root)
                return
            seen[new_name] = role.key
            name_map[role.key] = (current.get(role.key, role.default_name), new_name)

        self._clear_log()
        self._append_log(f"--- {profile.label}: Container-Namen übernehmen ---")
        try:
            changes = apply_container_names_to_compose(self.project_dir(), name_map)
        except Exception as exc:
            self._append_log(f"[FEHLER] Konnte docker-compose.yml nicht anpassen: {exc}")
            return

        for c in changes or ["Keine Änderungen — Namen entsprachen bereits den eingetragenen Werten."]:
            self._append_log(c)

        self.cfg.setdefault("containers", {})[profile.key] = {role_key: new for role_key, (_, new) in name_map.items()}
        save_config(self.cfg)
        # Erzwingt beim nächsten Öffnen von 'Server' einen Neuaufbau der DB-Konsolen-Felder
        # mit dem jetzt aktuellen Container-Namen.
        self._console_vars_profile = None
        self._append_log(
            "[Hinweis] docker-compose.yml wurde direkt im Projektordner geändert (git checkout -- "
            "docker-compose.yml macht das rückgängig). Für bereits laufende Container: 'Server stoppen' und "
            "danach neu starten, damit die neuen Namen greifen."
        )

    # -- Aktionen: DB-Updates (Core-Repo + fehlende SQL-Updates einspielen) ----

    def action_db_install_clone(self):
        if self._guard_busy():
            return
        if not self._require_project_dir():
            return
        if not self._tool_available("git"):
            messagebox.showerror("git fehlt", "git wurde nicht gefunden. Bitte installieren.", parent=self.root)
            return
        profile = self.current_profile()
        if not profile.db_install_repo_url:
            return
        project = self.project_dir()

        def clone_or_skip(repo_url: str, dirname: str) -> str:
            d = shlex.quote(dirname)
            u = shlex.quote(repo_url)
            return (
                f'if [ -d {d}/.git ]; then echo "{dirname}: bereits vorhanden, klonen übersprungen."; '
                f'else echo "Klone {repo_url} nach {dirname} ..."; git clone {u} {d} || exit 1; fi'
            )

        script = "set -u\n" + "\n".join([
            clone_or_skip(profile.db_install_repo_url, profile.db_install_dirname),
            clone_or_skip(profile.db_core_repo_url, DB_CORE_SUBDIR),
            clone_or_skip(profile.playerbots_repo_url, "playerbots"),
        ])
        self._clear_log()
        self._run(["bash", "-c", script], cwd=str(project), title=f"{profile.label}: DB-Repos klonen")

    def action_db_install_configure(self):
        if self._guard_busy():
            return
        if not self._require_project_dir():
            return
        profile = self.current_profile()
        if not profile.db_install_repo_url:
            return
        config_path = self.project_dir() / profile.db_install_dirname / "InstallFullDB.config"
        if not config_path.exists():
            messagebox.showerror("Fehlt", f"{config_path} nicht gefunden. Zuerst Schritt 2 (Repos klonen) "
                                           "ausführen.", parent=self.root)
            return

        values = {
            "MYSQL_HOST": "db",   # Compose-Servicename - im Projekt-Netzwerk immer erreichbar,
                                  # unabhängig von einem ggf. individuell vergebenen Container-Namen.
            "MYSQL_USERNAME": DB_USER,
            "MYSQL_PASSWORD": DB_PASS,
            "MYSQL_USERIP": "%",
            "MYSQL_PATH": "/usr/bin/mariadb",
            "MYSQL_DUMP_PATH": "/usr/bin/mariadb-dump",
            "CORE_PATH": "/work/mangos-src",
        }
        self._clear_log()
        self._append_log(f"--- {profile.label}: InstallFullDB.config anpassen ---")
        try:
            changes = patch_shell_config(config_path, values)
        except Exception as exc:
            self._append_log(f"[FEHLER] {exc}")
            return
        for c in changes:
            self._append_log(c)
        self._append_log(f"Fertig: {config_path}")

    def action_db_install_find_root_pass(self):
        if not self._require_project_dir() or not self._require_compose_file():
            return
        profile = self.current_profile()
        self._ensure_db_install_vars(profile)

        compose_text = (self.project_dir() / "docker-compose.yml").read_text(encoding="utf-8")
        m = re.search(r'(MYSQL_ROOT_PASSWORD|MARIADB_ROOT_PASSWORD)\s*[:=]?\s*["\']?([^"\'\n#]+)', compose_text)

        self._clear_log()
        self._append_log(f"--- {profile.label}: Root-Passwort suchen ---")
        if not m:
            self._append_log(
                "Keine MYSQL_ROOT_PASSWORD/MARIADB_ROOT_PASSWORD-Zeile in der docker-compose.yml gefunden. "
                "Entweder ist kein Root-Passwort gesetzt (dann evtl. leeres Passwort probieren), oder es "
                "steht unter anderem Namen in einer .env-Datei - dort bitte manuell nachsehen."
            )
            return

        raw_value = m.group(2).strip()
        value = raw_value
        var_match = re.match(r'^\$\{([A-Za-z_][A-Za-z0-9_]*)(:-(.*))?\}$', raw_value)
        if var_match:
            var_name, default_val = var_match.group(1), var_match.group(3)
            env_path = self.project_dir() / ".env"
            found = None
            if env_path.exists():
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith(f"{var_name}="):
                        found = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
            if found is not None:
                value = found
                self._append_log(f"Variable {var_name} aus .env aufgelöst.")
            elif default_val is not None:
                value = default_val
                self._append_log(f"Variable {var_name} nicht in .env gefunden — Vorgabewert aus "
                                  "docker-compose.yml verwendet.")
            else:
                self._append_log(f"[Warnung] Variable {var_name} referenziert, aber weder in .env noch als "
                                  "Vorgabewert gefunden — bitte manuell in .env/Umgebung nachsehen.")
                return

        self._append_log(f"Root-Passwort gefunden: {value}")
        self.db_install_vars["root_user"].set("root")
        self.db_install_vars["root_pass"].set(value)
        self._append_log("Root-User/-Passwort-Felder oben wurden automatisch ausgefüllt.")

    def action_db_install_grant(self):
        if self._guard_busy():
            return
        if not self._require_project_dir():
            return
        if not self._tool_available("docker"):
            messagebox.showerror("docker fehlt", "docker wurde nicht gefunden. Bitte installieren.", parent=self.root)
            return
        profile = self.current_profile()
        if not profile.db_install_repo_url:
            return
        self._ensure_db_install_vars(profile)
        root_user = self.db_install_vars["root_user"].get().strip() or "root"
        root_pass = self.db_install_vars["root_pass"].get()

        self.cfg.setdefault("db_install", {})[profile.key] = {"root_user": root_user, "root_pass": root_pass}
        save_config(self.cfg)

        db_container = self._container_name(profile, "db")
        sql = "GRANT ALL PRIVILEGES ON *.* TO 'mangos'@'%' WITH GRANT OPTION; FLUSH PRIVILEGES;"
        cmd = ["docker", "exec", db_container, "mariadb", f"-u{root_user}", f"-p{root_pass}", "-e", sql]
        self._clear_log()
        self._run(cmd, title=f"{profile.label}: DB-User Rechte vergeben")

    def action_db_install_run(self):
        if self._guard_busy():
            return
        if not self._require_project_dir() or not self._require_compose_file():
            return
        if not self._tool_available("docker"):
            messagebox.showerror("docker fehlt", "docker wurde nicht gefunden. Bitte installieren.", parent=self.root)
            return
        profile = self.current_profile()
        if not profile.db_install_repo_url:
            return
        project = self.project_dir()
        db_dir = project / profile.db_install_dirname
        core_dir = project / DB_CORE_SUBDIR
        if not db_dir.exists() or not core_dir.exists():
            messagebox.showerror("Fehlt", "Bitte zuerst Schritt 2 (Repos klonen) und Schritt 3 (Config anpassen) "
                                           "ausführen.", parent=self.root)
            return

        if not messagebox.askyesno(
            "Achtung — Datenbank wird (neu) installiert",
            f"Startet InstallFullDB.sh für {profile.label} interaktiv. Im Menü unten im Eingabefeld "
            "nacheinander eingeben und jeweils mit Enter/Senden bestätigen:\n\n"
            "  4\n  1\n  DeleteAll\n\n"
            "('DeleteAll' bestätigt das Löschen evtl. bestehender Daten — das kann VORHANDENE "
            "Datenbankinhalte unwiderruflich löschen!)\n\nNur fortfahren, wenn das gewünscht ist.",
            parent=self.root,
        ):
            return

        cmd = ["docker", "compose", "run", "--rm", "--no-deps",
               "-v", f"{db_dir}:/work/{profile.db_install_dirname}",
               "-v", f"{core_dir}:/work/mangos-src",
               "mangosd", "bash", "-c",
               f"cd /work/{profile.db_install_dirname} && bash ./InstallFullDB.sh"]
        self._clear_log()
        self._append_log(
            "InstallFullDB.sh läuft interaktiv. Unten im Eingabefeld nacheinander eintippen und mit Enter/"
            "Senden bestätigen: '4', dann '1', dann 'DeleteAll'. Bricht Option 3 (Core-Updates einspielen) "
            "mit einem Fehler ab (bekannter Bash-Bug), Schritt 6 unten ('DB-Updates einspielen') danach "
            "verwenden. 'Abbrechen' detacht sauber (Strg+P Strg+Q), der DB-Container läuft weiter."
        )
        self._run_pty(cmd, cwd=str(project), title=f"{profile.label}: InstallFullDB.sh")

    def action_db_install_core_updates(self):
        if self._guard_busy():
            return
        if not self._require_project_dir():
            return
        if not self._tool_available("docker"):
            messagebox.showerror("docker fehlt", "docker wurde nicht gefunden. Bitte installieren.", parent=self.root)
            return
        profile = self.current_profile()
        if not profile.db_install_repo_url:
            return
        core_dir = self.project_dir() / DB_CORE_SUBDIR
        if not core_dir.exists():
            messagebox.showerror("Fehlt", "Core-Repo wurde noch nicht geklont (Schritt 2).", parent=self.root)
            return

        db_container = self._container_name(profile, "db")
        categories = (("characters", f"{profile.key}characters"),
                      ("realmd", f"{profile.key}realmd"),
                      ("logs", f"{profile.key}logs"))

        parts = ["set -u"]
        for cat, db in categories:
            base_file = core_dir / "sql" / "base" / f"{cat}.sql"
            parts.append(f'echo "--- {cat} base schema ---"')
            if base_file.exists():
                parts.append(
                    f"docker exec -i {db_container} mariadb -u{DB_USER} -p{DB_PASS} {shlex.quote(db)} "
                    f"< {shlex.quote(str(base_file))} || true"
                )
            else:
                parts.append(f'echo "  (kein sql/base/{cat}.sql gefunden, übersprungen)"')
            parts.append(f'echo "--- {cat} updates ---"')
            updates_dir = core_dir / "sql" / "updates" / cat
            parts.append(
                f'find {shlex.quote(str(updates_dir))} -name "*.sql" 2>/dev/null | sort | while read -r f; do '
                f'echo "  $f"; docker exec -i {db_container} mariadb -u{DB_USER} -p{DB_PASS} '
                f'{shlex.quote(db)} < "$f" 2>/dev/null; done'
            )
        parts.append('echo "Alle DB-Updates eingespielt (Fehler zu bereits vorhandenen Strukturen wurden ignoriert)."')
        script = "\n".join(parts)

        self._clear_log()
        self._append_log(f"--- {profile.label}: DB-Updates (Core-Fallback) für characters/realmd/logs ---")
        self._run(["bash", "-c", script], title=f"{profile.label}: DB-Updates (Core-Fallback)")

    def action_db_install_playerbots(self):
        if self._guard_busy():
            return
        if not self._require_project_dir():
            return
        if not self._tool_available("docker"):
            messagebox.showerror("docker fehlt", "docker wurde nicht gefunden. Bitte installieren.", parent=self.root)
            return
        profile = self.current_profile()
        if not profile.db_install_repo_url:
            return
        pb_dir = self.project_dir() / "playerbots"
        if not pb_dir.exists():
            messagebox.showerror("Fehlt", "Playerbots-Repo wurde noch nicht geklont (Schritt 2).", parent=self.root)
            return

        db_container = self._container_name(profile, "db")
        characters_db = f"{profile.key}characters"
        world_db = profile.db_name
        expansion_folder = profile.key  # "classic" bzw. "tbc" - passt exakt zum Ordnernamen im Playerbots-Repo

        chars_glob = shlex.quote(str(pb_dir / "sql" / "characters"))
        world_dir = pb_dir / "sql" / "world"
        script = f'''set -u
echo "--- Playerbots: characters-Tabellen ---"
find {chars_glob} -name "*.sql" 2>/dev/null | sort | while read -r f; do
    echo "Importing: $f"
    docker exec -i {db_container} mariadb -u{DB_USER} -p{DB_PASS} {shlex.quote(characters_db)} < "$f"
done
echo "--- Playerbots: world-Tabellen ---"
for f in {shlex.quote(str(world_dir / "ai_playerbot_rpg_races.sql"))} \\
         {shlex.quote(str(world_dir / "ai_playerbot_indexes.sql"))} \\
         {shlex.quote(str(world_dir / expansion_folder))}/*.sql \\
         {shlex.quote(str(world_dir))}/*.sql; do
    if [ -f "$f" ]; then
        docker exec -i {db_container} mariadb -u{DB_USER} -p{DB_PASS} {shlex.quote(world_db)} < "$f" \\
            2>/dev/null && echo "OK: $f"
    fi
done
echo "Fertig."
'''
        self._clear_log()
        self._append_log(f"--- {profile.label}: Playerbots-Tabellen einspielen ---")
        self._run(["bash", "-c", script], title=f"{profile.label}: Playerbots-Tabellen einspielen")

    def action_db_install_verify(self):
        if self._guard_busy():
            return
        if not self._require_project_dir():
            return
        if not self._tool_available("docker"):
            messagebox.showerror("docker fehlt", "docker wurde nicht gefunden. Bitte installieren.", parent=self.root)
            return
        profile = self.current_profile()
        if not profile.db_install_repo_url:
            return
        self._ensure_db_install_vars(profile)
        root_user = self.db_install_vars["root_user"].get().strip() or "root"
        root_pass = self.db_install_vars["root_pass"].get()
        db_container = self._container_name(profile, "db")
        characters_db = f"{profile.key}characters"

        cmd = ["docker", "exec", db_container, "mariadb", f"-u{root_user}", f"-p{root_pass}",
               characters_db, "-e", "SHOW TABLES LIKE 'ai_playerbot%';"]
        self._clear_log()
        self._run(cmd, title=f"{profile.label}: Playerbot-Tabellen prüfen ({characters_db})")

    def action_db_clone_core(self):
        if self._guard_busy():
            return
        if not self._require_project_dir():
            return
        if not self._tool_available("git"):
            messagebox.showerror("git fehlt", "git wurde nicht gefunden. Bitte installieren.", parent=self.root)
            return
        profile = self.current_profile()
        if not profile.db_core_repo_url:
            return

        core_dir = self.project_dir() / DB_CORE_SUBDIR
        self._clear_log()
        if (core_dir / ".git").exists():
            self._run(["git", "pull"], cwd=str(core_dir), title=f"{profile.label}: Core-Repo aktualisieren")
        else:
            self._run(["git", "clone", profile.db_core_repo_url, str(core_dir)], cwd=str(self.project_dir()),
                       title=f"{profile.label}: Core-Repo klonen")

    def action_db_apply_updates(self):
        if self._guard_busy():
            return
        if not self._require_project_dir():
            return
        if not self._tool_available("docker"):
            messagebox.showerror("docker fehlt", "docker wurde nicht gefunden. Bitte installieren.", parent=self.root)
            return
        profile = self.current_profile()
        if not profile.db_core_repo_url:
            return

        core_dir = self.project_dir() / DB_CORE_SUBDIR
        if not core_dir.exists():
            messagebox.showerror("Fehlt", "Core-Repo wurde noch nicht geklont. Zuerst 'Core-Repo klonen / "
                                           "aktualisieren' ausführen.", parent=self.root)
            return

        from_text = self.db_from_var.get().strip()
        to_text = self.db_to_var.get().strip()
        token_re = re.compile(r'^([A-Za-z]+)(\d+)$')
        m_from, m_to = token_re.match(from_text), token_re.match(to_text)
        if not m_from or not m_to:
            messagebox.showerror("Ungültiger Bereich",
                                  "Bitte 'von' und 'bis' als Buchstabe(n) + Nummer angeben (z.B. z2831 oder "
                                  "s2000).", parent=self.root)
            return
        prefix_from, from_n = m_from.group(1), int(m_from.group(2))
        prefix_to, to_n = m_to.group(1), int(m_to.group(2))
        if prefix_from.lower() != prefix_to.lower():
            messagebox.showerror("Ungültiger Bereich",
                                  f"'von' ({prefix_from}...) und 'bis' ({prefix_to}...) müssen denselben "
                                  "Buchstaben-Präfix verwenden. Für mehrere Präfixe (z.B. TBC: 's' und 'z') "
                                  "diese Aktion nacheinander mit jeweils passendem Präfix ausführen.",
                                  parent=self.root)
            return
        prefix = prefix_from
        if from_n > to_n:
            messagebox.showerror("Ungültiger Bereich", "'von' darf nicht größer als 'bis' sein.", parent=self.root)
            return

        updates_dir = core_dir / "sql" / "updates" / "mangos"
        if not updates_dir.exists():
            messagebox.showerror("Fehlt", f"Ordner nicht gefunden: {updates_dir}", parent=self.root)
            return

        files = []
        for n in range(from_n, to_n + 1):
            files.extend(sorted(updates_dir.glob(f"{prefix}{n}_*.sql")))

        self._clear_log()
        self._append_log(f"--- {profile.label}: SQL-Updates {prefix}{from_n} bis {prefix}{to_n} ---")
        if not files:
            self._append_log("Keine passenden SQL-Dateien in diesem Versionsbereich gefunden.")
            return
        for f in files:
            self._append_log(f"  gefunden: {f.name}")

        self.cfg.setdefault("db_update_range", {})[profile.key] = {"from": from_text, "to": to_text}
        save_config(self.cfg)

        steps = []
        db_container = self._container_name(profile, "db")
        for f in files:
            inner = (f"docker exec -i {db_container} mariadb -u{DB_USER} -p{DB_PASS} "
                     f"{shlex.quote(profile.db_name)} < {shlex.quote(str(f))}")
            steps.append(self._chain_cmd(["bash", "-c", inner], title=f"SQL-Update anwenden: {f.name}"))
        self._run_chain(steps)

    def action_unpack_sql_init(self):
        if self._guard_busy():
            return
        if not self._require_project_dir():
            return
        profile = self.current_profile()
        if not profile.needs_sql_init_unpack:
            messagebox.showinfo("Nicht nötig", f"Für '{profile.label}' ist kein sql-init-Entpacken nötig.",
                                 parent=self.root)
            return

        sql_init = self.project_dir() / "sql-init"
        if not sql_init.exists():
            messagebox.showerror("Fehlt", f"Ordner nicht gefunden: {sql_init}", parent=self.root)
            return

        self._clear_log()
        self._append_log(f"--- {profile.label}: sql-init entpacken ---")
        archives = list(sql_init.glob("*.zip"))
        other_archives = [p for p in sql_init.iterdir() if p.suffix.lower() in (".rar", ".7z")]

        if not archives and not other_archives:
            self._append_log("Keine Archive in sql-init/ gefunden — vermutlich bereits entpackt.")
            return

        archive_backup = sql_init / "_archived"
        for zpath in archives:
            self._append_log(f"Entpacke {zpath.name} …")
            try:
                with zipfile.ZipFile(zpath) as zf:
                    zf.extractall(sql_init)
                archive_backup.mkdir(exist_ok=True)
                shutil.move(str(zpath), str(archive_backup / zpath.name))
                self._append_log(f"  -> entpackt, Archiv nach {archive_backup} verschoben.")
            except Exception as exc:
                self._append_log(f"[FEHLER] {zpath.name}: {exc}")

        for opath in other_archives:
            self._append_log(
                f"[Hinweis] {opath.name} ist ein .{opath.suffix.lstrip('.')}-Archiv und kann nicht automatisch "
                f"entpackt werden. Bitte manuell mit 'unrar' bzw. '7z' entpacken."
            )

        sql_files = list(sql_init.glob("*.sql"))
        self._append_log(f"sql-init enthält jetzt {len(sql_files)} .sql-Datei(en).")

    def action_build_image(self):
        if self._guard_busy():
            return
        if not self._require_project_dir():
            return
        if not self._tool_available("docker"):
            messagebox.showerror("docker fehlt", "docker wurde nicht gefunden. Bitte installieren.", parent=self.root)
            return
        profile = self.current_profile()
        cmd = ["docker", "build"]
        if self.nocache_var.get():
            cmd.append("--no-cache")
        cmd += ["-t", profile.image_name, "."]

        self._clear_log()
        steps = [self._chain_cmd(cmd, cwd=str(self.project_dir()), title=f"{profile.label}: Image bauen")]
        if self.cleanup_var.get():
            steps.append(self._chain_cmd(["docker", "image", "prune", "-f"], title="Dangling Images entfernen"))
            steps.append(self._chain_cmd(["docker", "builder", "prune", "-f"], title="Docker Builder-Cache leeren"))
        self._run_chain(steps)

    # -- Aktionen: Extraktion CMaNGOS ---------------------------------------

    def action_extract_cmangos(self):
        if self._guard_busy():
            return
        if not self._require_project_dir():
            return
        if not self._tool_available("docker"):
            messagebox.showerror("docker fehlt", "docker wurde nicht gefunden. Bitte installieren.", parent=self.root)
            return
        wow_client = self._wow_client_dir()
        if wow_client is None:
            return

        profile = self.current_profile()
        cmd = [
            "docker", "run", "--rm", "-i",
            "-v", f"{wow_client}:/wow",
            profile.image_name,
            "bash", "-c",
            "cp /opt/mangos/bin/tools/* /wow/ && "
            "cd /wow && "
            "sed -i 's/\\r//' ExtractResources.sh && "
            "bash ./ExtractResources.sh",
        ]

        self._clear_log()
        self._append_log(
            "Starte Ressourcen-Extraktion. Automatisch beantwortet werden: 'Should all data be extracted?' "
            "mit 'y', die Thread-Anzahl mit dem oben eingetragenen Wert (leer = alle verfügbaren Threads) "
            "und die abschließende 'Press Enter to continue'-Aufforderung mit Enter. Bei unerwarteten "
            "weiteren Fragen unten manuell antworten (auch reines Enter über den '⏎ Enter'-Button möglich)."
        )
        self._run(cmd, title=f"{profile.label}: Extraktion")

        answers = ["y", self.cmangos_threads_var.get().strip(), ""]

        def send_answers():
            time.sleep(3)
            for a in answers:
                if not self.runner.busy:
                    return
                self.runner.send_input(a)
                time.sleep(2.0)

        threading.Thread(target=send_answers, daemon=True).start()

    # -- Aktionen: Extraktion AzerothCore ------------------------------------

    def action_extract_ac_maps(self):
        if self._guard_busy():
            return
        if not self._require_project_dir():
            return
        if not self._tool_available("docker"):
            messagebox.showerror("docker fehlt", "docker wurde nicht gefunden. Bitte installieren.", parent=self.root)
            return
        wow_client = self._wow_client_dir()
        if wow_client is None:
            return
        profile = self.current_profile()

        cmd = [
            "docker", "run", "--rm", "-v", f"{wow_client}:/wow", profile.image_name,
            "bash", "-c",
            "cp /azerothcore/env/dist/bin/map_extractor /wow/ && cd /wow && chmod +x map_extractor && ./map_extractor",
        ]
        self._clear_log()
        self._run(cmd, title=f"{profile.label}: Maps/DBC/Cameras extrahieren")

    def action_extract_ac_vmaps(self):
        if self._guard_busy():
            return
        if not self._require_project_dir():
            return
        if not self._tool_available("docker"):
            messagebox.showerror("docker fehlt", "docker wurde nicht gefunden. Bitte installieren.", parent=self.root)
            return
        wow_client = self._wow_client_dir()
        if wow_client is None:
            return
        profile = self.current_profile()

        cmd = [
            "docker", "run", "--rm", "-v", f"{wow_client}:/wow", profile.image_name,
            "bash", "-c",
            "cp /azerothcore/env/dist/bin/vmap4_extractor /wow/ && "
            "cp /azerothcore/env/dist/bin/vmap4_assembler /wow/ && "
            "cd /wow && chmod +x vmap4_extractor vmap4_assembler && "
            "./vmap4_extractor && mkdir -p vmaps && ./vmap4_assembler Buildings vmaps",
        ]
        self._clear_log()
        self._run(cmd, title=f"{profile.label}: VMaps extrahieren (30-60 Min.)")

    def action_extract_ac_mmaps(self):
        if self._guard_busy():
            return
        if not self._require_project_dir():
            return
        if not self._tool_available("docker"):
            messagebox.showerror("docker fehlt", "docker wurde nicht gefunden. Bitte installieren.", parent=self.root)
            return
        wow_client = self._wow_client_dir()
        if wow_client is None:
            return
        profile = self.current_profile()
        data_dir = self.project_dir() / "data"
        maps_dir, vmaps_dir, mmaps_dir = data_dir / "maps", data_dir / "vmaps", data_dir / "mmaps"
        if not maps_dir.exists() or not vmaps_dir.exists():
            messagebox.showerror(
                "Voraussetzung fehlt",
                "data/maps und data/vmaps müssen vorher extrahiert und mit "
                "'Rechte korrigieren + verschieben' hierher verschoben worden sein.", parent=self.root)
            return
        mmaps_dir.mkdir(parents=True, exist_ok=True)

        # data/mmaps wird direkt als Volume gemountet, der Generator schreibt seine
        # Ausgabe damit ohne Zwischenschritt unmittelbar dorthin.
        if self.ac_quick_mmaps_var.get():
            inner = "./mmaps_generator 0 --threads $(nproc) && ./mmaps_generator 1 --threads $(nproc)"
            title = f"{profile.label}: MMaps (nur Map 0+1)"
        else:
            inner = "./mmaps_generator --threads $(nproc)"
            title = f"{profile.label}: MMaps (alle Maps, kann Stunden dauern)"

        cmd = [
            "docker", "run", "--rm",
            "-v", f"{wow_client}:/wow",
            "-v", f"{maps_dir}:/wow/maps",
            "-v", f"{vmaps_dir}:/wow/vmaps",
            "-v", f"{mmaps_dir}:/wow/mmaps",
            profile.image_name,
            "bash", "-c",
            "cp /azerothcore/env/dist/bin/mmaps_generator /wow/ && cd /wow && chmod +x mmaps_generator && " + inner,
        ]
        self._clear_log()
        self._run(cmd, title=title)

    # -- Aktion: wow-client für einen erneuten Extraktions-Lauf bereinigen ---

    def action_clean_wow_client(self):
        if self._guard_busy():
            return
        if not self._require_project_dir():
            return
        if not self._tool_available("docker"):
            messagebox.showerror("docker fehlt", "docker wurde nicht gefunden. Bitte installieren.", parent=self.root)
            return
        wow_client = self._wow_client_dir()
        if wow_client is None:
            return
        profile = self.current_profile()

        # Nur die Ausgabe-Ordner der Extraktoren, NIE den eigentlichen Client (Data/ usw.).
        leftover_names = ["maps", "dbc", "vmaps", "mmaps", "cameras", "Cameras", "Buildings", "logs"]
        existing = [name for name in leftover_names if (wow_client / name).exists()]
        if not existing:
            messagebox.showinfo("Nichts zu tun",
                                 "Keine übrig gebliebenen Extraktions-Ordner in wow-client/ gefunden.",
                                 parent=self.root)
            return

        listing = "\n".join(f"  - {n}" for n in existing)
        if not messagebox.askyesno(
            "Bestätigen",
            f"Folgende Ordner werden aus\n{wow_client}\nentfernt (der WoW-Client unter Data/ bleibt "
            f"unberührt):\n\n{listing}\n\n"
            "Das behebt typischerweise 'Your output directory seems to be polluted' bei einem erneuten "
            "Extraktions-Lauf, der wegen bereits vorhandener Ausgabeordner abbricht. Fortfahren?",
            parent=self.root,
        ):
            return

        # Die Ordner wurden vom Extraktions-Container als root angelegt und gehören daher
        # meist root - normales shutil.rmtree() scheitert dann mit "Permission denied".
        # Ein kurzlebiger Container mit demselben Mount hat root-Rechte im Volume und kann
        # sie problemlos entfernen.
        rm_targets = " ".join(f"/wow/{name}" for name in existing)
        cmd = ["docker", "run", "--rm", "-v", f"{wow_client}:/wow", "alpine", "sh", "-c", f"rm -rf {rm_targets}"]

        self._clear_log()
        self._run(cmd, title=f"{profile.label}: wow-client aufräumen")

    # -- Aktionen: Rechte + Verschieben --------------------------------------

    def action_finalize_data(self):
        if self._guard_busy():
            return
        if not self._require_project_dir():
            return
        if not self._tool_available("docker"):
            messagebox.showerror("docker fehlt", "docker wurde nicht gefunden. Bitte installieren.", parent=self.root)
            return
        wow_client = self._wow_client_dir()
        if wow_client is None:
            return
        profile = self.current_profile()

        if not messagebox.askyesno(
            "Bestätigen",
            "Dies korrigiert die Dateirechte (per Docker/Alpine, ohne sudo) und verschiebt die "
            f"extrahierten Ordner aus\n{wow_client}\nnach\n{self.project_dir() / 'data'}\n\n"
            "Vorhandene Zielordner werden dabei überschrieben. Fortfahren?", parent=self.root
        ):
            return

        self._clear_log()
        uid, gid = os.getuid(), os.getgid()
        chown_cmd = ["docker", "run", "--rm", "-v", f"{wow_client}:/wow", "alpine",
                     "chown", "-R", f"{uid}:{gid}", "/wow"]
        self._run_chain([
            self._chain_cmd(chown_cmd, title=f"{profile.label}: Besitzrechte korrigieren"),
            self._chain_sync(lambda: self._move_extracted_data(profile), title="Daten nach data/ verschieben"),
        ])

    def _move_extracted_data(self, profile: ServerProfile):
        wow_client = self.project_dir() / "wow-client"
        data_dir = self.project_dir() / "data"
        data_dir.mkdir(exist_ok=True)
        for src_name, dst_name in profile.data_map:
            src = wow_client / src_name
            dst = data_dir / dst_name
            if not src.exists():
                self._append_log(f"  {src_name}: nicht vorhanden, übersprungen.")
                continue
            if dst.exists():
                shutil.rmtree(dst)
            shutil.move(str(src), str(dst))
            self._append_log(f"  {src_name} -> {dst}")
        self._append_log("Fertig. data/ enthält jetzt die extrahierten Ressourcen.")

    # -- Aktionen: Server ----------------------------------------------------

    def action_start_db(self):
        if self._guard_busy():
            return
        if not self._require_project_dir() or not self._require_compose_file():
            return
        profile = self.current_profile()
        db_service = "db" if profile.extraction_mode == "cmangos" else "ac-database"
        self._clear_log()
        self._run(["docker", "compose", "up", db_service, "-d"], cwd=str(self.project_dir()),
                   title=f"{profile.label}: nur Datenbank starten")

    def action_start_all(self):
        if self._guard_busy():
            return
        if not self._require_project_dir() or not self._require_compose_file():
            return
        profile = self.current_profile()
        other_names = set()
        for key, p in PROFILES.items():
            if key != profile.key:
                other_names.update(self._current_container_names(p).values())
        running = self._running_containers(other_names)
        if running:
            proceed = messagebox.askyesno(
                "Container bereits aktiv",
                f"Es laufen bereits Container eines anderen Profils ({', '.join(running)}).\n"
                "Die Server-Setups teilen sich standardmäßig Ports und können sich beim Start "
                "gegenseitig blockieren, sofern keine eindeutigen Ports vergeben wurden.\n\n"
                "Trotzdem versuchen zu starten?", parent=self.root)
            if not proceed:
                return
        self._clear_log()
        self._run(["docker", "compose", "up", "-d"], cwd=str(self.project_dir()),
                   title=f"{profile.label}: Server starten")

    def action_stop(self):
        if self._guard_busy():
            return
        if not self._require_project_dir() or not self._require_compose_file():
            return
        profile = self.current_profile()
        self._clear_log()
        self._run(["docker", "compose", "down"], cwd=str(self.project_dir()), title=f"{profile.label}: Server stoppen")

    def action_status(self):
        if self._guard_busy():
            return
        if not self._require_project_dir() or not self._require_compose_file():
            return
        self._clear_log()
        self._run(["docker", "compose", "ps"], cwd=str(self.project_dir()), title="Status")

    def action_logs_worldserver(self):
        if self._guard_busy():
            return
        if not self._require_project_dir() or not self._require_compose_file():
            return
        profile = self.current_profile()
        service = "mangosd" if profile.extraction_mode == "cmangos" else "ac-worldserver"
        self._clear_log()
        self._run(["docker", "compose", "logs", "-f", service], cwd=str(self.project_dir()),
                   title=f"Live-Logs: {service} (mit 'Abbrechen' beenden)")

    def action_db_console(self):
        if self._guard_busy():
            return
        if not self._tool_available("docker"):
            messagebox.showerror("docker fehlt", "docker wurde nicht gefunden. Bitte installieren.", parent=self.root)
            return
        profile = self.current_profile()
        self._ensure_console_vars(profile)
        container = self.console_vars["container"].get().strip()
        user = self.console_vars["user"].get().strip()
        password = self.console_vars["password"].get()
        binname = self.console_vars["bin"].get().strip() or "mysql"
        if not container or not user:
            messagebox.showerror("Fehlende Angaben", "DB-Container und DB-User dürfen nicht leer sein.", parent=self.root)
            return

        self.cfg.setdefault("console", {})[profile.key] = {
            "container": container, "user": user, "password": password, "bin": binname,
        }
        save_config(self.cfg)

        cmd = ["docker", "exec", "-i", container, binname, f"-u{user}", f"-p{password}"]
        self._clear_log()
        self._append_log(
            f"Verbunden mit der DB-Konsole von '{container}' ({binname}). SQL-Befehle unten eingeben (mit ';' "
            "abschließen) und mit Enter/Senden abschicken, z.B. 'USE meinedb;' und dann 'SELECT ...;'. "
            "'Abbrechen' beendet nur diese Sitzung — der Datenbank-Container läuft weiter."
        )
        self._run(cmd, title=f"{profile.label}: DB-Konsole ({container})")

    def action_world_console(self):
        if self._guard_busy():
            return
        if not self._tool_available("docker"):
            messagebox.showerror("docker fehlt", "docker wurde nicht gefunden. Bitte installieren.", parent=self.root)
            return
        profile = self.current_profile()
        container = self._container_name(profile, "world")

        if not messagebox.askyesno(
            "Server-Konsole öffnen",
            f"Verbindet interaktiv mit der laufenden Konsole von '{container}' (docker attach).\n\n"
            "GM-/Account-Befehle können unten im Eingabefeld eingegeben werden, z.B.:\n"
            "  account create <name> <passwort> <email>\n"
            "  account set gmlevel <name> 3\n\n"
            "'Abbrechen' sendet die von Docker vorgesehene Detach-Tastenkombination (Strg+P Strg+Q) und "
            "trennt damit nur diese Ansicht - der Server läuft weiter. "
            "Der Container muss dafür bereits laufen. Fortfahren?",
            parent=self.root,
        ):
            return

        cmd = ["docker", "attach", "--sig-proxy=false", container]
        self._clear_log()
        self._append_log(
            f"Verbunden mit der Konsole von '{container}'. Befehle unten eingeben (Enter/Senden). "
            "'Abbrechen' detacht sauber (Strg+P Strg+Q), der Server läuft danach weiter."
        )
        self._run_pty(cmd, title=f"{profile.label}: Server-Konsole ({container})")

    def action_terminate(self):
        self.runner.terminate()
        self._append_log("[Abbruch gesendet - bei einer Terminal-Sitzung (z.B. Server-Konsole) als Detach "
                          "(Strg+P Strg+Q), sonst als Strg+C (SIGINT); falls nötig wird automatisch nachgefasst]")

    def _running_containers(self, names: set):
        try:
            out = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                                  capture_output=True, text=True, timeout=10)
            running = set(out.stdout.split())
            return [n for n in names if n in running]
        except Exception:
            return []

    # -- Ende ---------------------------------------------------------------

    def on_close(self):
        if self.runner.busy:
            if not messagebox.askyesno("Beenden", "Es läuft noch ein Vorgang.\nTrotzdem beenden?", parent=self.root):
                return
        self.runner.terminate()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()


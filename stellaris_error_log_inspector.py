"""
Stellaris Error Log Inspector v1
Python/PyQt6 GUI tool for parsing Stellaris error.log and estimating likely mod culprits.

Run:
    pip install PyQt6
    python stellaris_error_log_inspector.py
"""
from __future__ import annotations

import csv
import os
import re
import sys
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, QSettings, QThread, pyqtSignal, QObject
from PyQt6.QtGui import QAction, QColor
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "Stellaris Error Log Inspector"
APP_VERSION = "1.4"

COMMON_RELATIVE_ROOTS = (
    "common/",
    "events/",
    "localisation/",
    "localization/",
    "interface/",
    "gfx/",
    "sound/",
    "music/",
    "prescripted_countries/",
    "map/",
    "mod/",
    "dlc/",
)

ERROR_PATTERNS = [
    # Specific, high-signal categories first. These beat generic "Invalid" / "Error" matches.
    ("Version / descriptor issue", re.compile(r"supported_version|remote_file_id|descriptor|\.mod(?:\s|$)|mod/ugc_\d+\.mod", re.I)),
    ("Component issue", re.compile(r"component template|component set|component slot|component_tag|ship design.*(?:component|slot|set)|invalid component", re.I)),
    ("Ship design issue", re.compile(r"ship design|design \"|section template|ship_size|fleet_slot_size", re.I)),
    ("Technology issue", re.compile(r"technology|tech_[A-Za-z0-9_]+|invalid tech|research", re.I)),
    ("Localisation", re.compile(r"locali[sz]ation|missing key|invalid key|yml|l_[a-z_]+\.yml", re.I)),
    ("Syntax / Unexpected token", re.compile(r"Unexpected token|Unexpected end|Expected token|Parser error", re.I)),
    ("Invalid scope/context", re.compile(r"Invalid context switch|invalid scope|Scope:|wrong scope|event target", re.I)),
    ("Missing asset/reference", re.compile(r"Could not find|not found|Missing|Failed to find|Unable to find|could not open|texture|sprite|dds|mesh|asset", re.I)),
    ("Unknown key/modifier/effect", re.compile(r"Unknown|Invalid .*modifier|Invalid .*effect|Invalid .*trigger|Invalid .*key|Invalid .*value", re.I)),
    ("Duplicate/conflict", re.compile(r"duplicate|already exists|overriding|redefined|conflict", re.I)),
    ("Script error", re.compile(r"Script Error|Error:", re.I)),
]

@dataclass
class ModInfo:
    name: str
    descriptor_path: Path
    content_path: Optional[Path] = None
    remote_file_id: str = ""
    enabled_hint: bool = True

@dataclass
class ParsedError:
    severity: str
    error_type: str
    message: str
    file_path: str = ""
    relative_path: str = ""
    line: str = ""
    likely_mod: str = "Base game / unknown"
    mod_path: str = ""


def default_stellaris_user_dir() -> Path:
    return Path.home() / "Documents" / "Paradox Interactive" / "Stellaris"


def default_log_dir() -> Path:
    return default_stellaris_user_dir() / "logs"


def default_mod_dir() -> Path:
    return default_stellaris_user_dir() / "mod"


def parse_quoted_value(text: str, key: str) -> str:
    m = re.search(rf"^\s*{re.escape(key)}\s*=\s*\"([^\"]*)\"", text, re.M)
    if m:
        return m.group(1).strip()
    m = re.search(rf"^\s*{re.escape(key)}\s*=\s*([^\s#]+)", text, re.M)
    return m.group(1).strip() if m else ""


def load_mods(mod_dir: Path) -> List[ModInfo]:
    mods: List[ModInfo] = []
    if not mod_dir.exists():
        return mods
    for descriptor in sorted(mod_dir.glob("*.mod")):
        try:
            text = descriptor.read_text(encoding="utf-8-sig", errors="replace")
        except Exception:
            continue
        name = parse_quoted_value(text, "name") or descriptor.stem
        path_value = parse_quoted_value(text, "path")
        archive_value = parse_quoted_value(text, "archive")
        remote_file_id = parse_quoted_value(text, "remote_file_id")
        content_path = None
        if path_value:
            content_path = Path(os.path.expandvars(os.path.expanduser(path_value)))
            if not content_path.is_absolute():
                content_path = (descriptor.parent / content_path).resolve()
        elif archive_value:
            content_path = Path(os.path.expandvars(os.path.expanduser(archive_value)))
            if not content_path.is_absolute():
                content_path = (descriptor.parent / content_path).resolve()
        mods.append(ModInfo(name=name, descriptor_path=descriptor, content_path=content_path, remote_file_id=remote_file_id))
    return mods


def normalize_slashes(value: str) -> str:
    return value.replace("\\", "/")


def extract_file_and_line(line: str) -> Tuple[str, str]:
    file_path = ""
    line_no = ""

    patterns = [
        r'in file:\s*"([^"]+)"',
        r'file:\s*"([^"]+)"',
        r'File:\s*"([^"]+)"',
        r'file:\s*([^,\s]+)',
        r'((?:mod|common|events|locali[sz]ation|interface|gfx|sound|music|map|prescripted_countries|dlc)[\\/][^\"]+?\.(?:txt|gui|gfx|asset|yml|mod|dds|ogg|wav))',
        r'([A-Za-z]:[\\/][^\"]+?\.(?:txt|gui|gfx|asset|yml|mod|dds|ogg|wav))',
    ]
    for pat in patterns:
        m = re.search(pat, line, re.I)
        if m:
            file_path = m.group(1).strip().strip(',')
            break

    mline = re.search(r'(?:line|near line):\s*(\d+)', line, re.I)
    if mline:
        line_no = mline.group(1)
    return file_path, line_no


def extract_relative_path(file_path: str) -> str:
    if not file_path:
        return ""
    cleaned = normalize_slashes(file_path).strip('"')
    lower = cleaned.lower()
    best_idx = -1
    for root in COMMON_RELATIVE_ROOTS:
        idx = lower.find(root)
        if idx >= 0 and (best_idx < 0 or idx < best_idx):
            best_idx = idx
    if best_idx >= 0:
        return cleaned[best_idx:]
    return cleaned


def extract_workshop_id(*values: str) -> str:
    """Return a Steam Workshop ID found in common Stellaris log path formats."""
    joined = " ".join(v for v in values if v)
    norm = normalize_slashes(joined).lower()
    patterns = [
        r"mod/ugc_(\d+)\.mod",
        r"ugc_(\d+)\.mod",
        r"(?:workshop/content/281990/|steamapps/workshop/content/281990/)(\d+)",
        r"remote_file_id\s*=\s*\"?(\d+)\"?",
    ]
    for pat in patterns:
        m = re.search(pat, norm, re.I)
        if m:
            return m.group(1)
    return ""


def classify_error(line: str) -> str:
    # Use the descriptive text in the log line whenever possible.
    for label, pattern in ERROR_PATTERNS:
        if pattern.search(line):
            return label
    return "Other"


def severity_for(error_type: str, line: str) -> str:
    if error_type in {"Syntax / Unexpected token", "Invalid scope/context", "Component issue"}:
        return "High"
    if error_type in {"Version / descriptor issue", "Unknown key/modifier/effect", "Missing asset/reference", "Ship design issue", "Technology issue"}:
        return "Medium"
    if "warning" in line.lower():
        return "Low"
    return "Medium"


def map_error_to_mod(file_path: str, relative_path: str, mods: List[ModInfo], raw_line: str = "") -> Tuple[str, str]:
    if not file_path and not relative_path and not raw_line:
        return "Base game / unknown", ""
    norm_file = normalize_slashes(file_path).lower()
    rel = normalize_slashes(relative_path).lower()

    # Direct mod descriptor reference, e.g. mod/ugc_1199002146.mod.
    wid = extract_workshop_id(file_path, relative_path, raw_line)
    if wid:
        for mod in mods:
            descriptor_id = extract_workshop_id(str(mod.descriptor_path))
            if mod.remote_file_id == wid or descriptor_id == wid:
                return f"{mod.name} (Workshop ID {wid})", str(mod.content_path or mod.descriptor_path)
        return f"Workshop item {wid}", ""

    # Direct path containment.
    for mod in mods:
        if mod.content_path:
            mp = normalize_slashes(str(mod.content_path.resolve())).lower()
            if norm_file and norm_file.startswith(mp):
                return mod.name, str(mod.content_path)

    # Relative file exists inside mod folder.
    if rel:
        for mod in mods:
            if mod.content_path and mod.content_path.exists() and mod.content_path.is_dir():
                candidate = mod.content_path / relative_path.replace("/", os.sep)
                if candidate.exists():
                    return mod.name, str(mod.content_path)

    # Workshop ID hint.
    m = re.search(r'(?:workshop/content/281990/|steamapps/workshop/content/281990/)(\d+)', norm_file)
    if m:
        wid = m.group(1)
        for mod in mods:
            if mod.remote_file_id == wid:
                return mod.name, str(mod.content_path or mod.descriptor_path)
        return f"Workshop item {wid}", ""

    return "Base game / unknown", ""


def parse_error_log(error_log: Path, mods: List[ModInfo]) -> List[ParsedError]:
    if not error_log.exists():
        return []
    try:
        lines = error_log.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except Exception:
        lines = error_log.read_text(errors="replace").splitlines()

    results: List[ParsedError] = []
    interesting = re.compile(r"\b(Error|Exception|Warning|Invalid|Unexpected|Could not|Missing|Unknown|Failed)\b", re.I)
    for raw in lines:
        if not interesting.search(raw):
            continue
        file_path, line_no = extract_file_and_line(raw)
        rel = extract_relative_path(file_path)
        etype = classify_error(raw)
        sev = severity_for(etype, raw)
        mod_name, mod_path = map_error_to_mod(file_path, rel, mods, raw)
        results.append(ParsedError(sev, etype, raw.strip(), file_path, rel, line_no, mod_name, mod_path))
    return results


def find_duplicate_relative_files(mods: List[ModInfo]) -> Dict[str, List[str]]:
    seen: Dict[str, List[str]] = {}
    wanted_ext = {".txt", ".gui", ".gfx", ".asset", ".yml"}
    roots = [r.strip("/") for r in COMMON_RELATIVE_ROOTS if r not in {"mod/", "dlc/"}]
    for mod in mods:
        root = mod.content_path
        if not root or not root.exists() or not root.is_dir():
            continue
        for sub in roots:
            start = root / sub
            if not start.exists():
                continue
            for p in start.rglob("*"):
                if p.is_file() and p.suffix.lower() in wanted_ext:
                    rel = normalize_slashes(str(p.relative_to(root)))
                    seen.setdefault(rel, []).append(mod.name)
    return {k: v for k, v in seen.items() if len(set(v)) > 1}


class LogLoadWorker(QObject):
    finished = pyqtSignal(object, object, object, object)  # mods, errors, duplicates, error_message
    status = pyqtSignal(str)

    def __init__(self, error_log: Path, mod_dir: Path, scan_duplicates: bool):
        super().__init__()
        self.error_log = error_log
        self.mod_dir = mod_dir
        self.scan_duplicates = scan_duplicates

    def run(self):
        try:
            self.status.emit("Loading mod descriptors...")
            mods = load_mods(self.mod_dir)
            self.status.emit("Reading and parsing error.log...")
            errors = parse_error_log(self.error_log, mods)
            duplicates = {}
            if self.scan_duplicates:
                self.status.emit("Scanning duplicate mod files...")
                duplicates = find_duplicate_relative_files(mods)
            self.finished.emit(mods, errors, duplicates, None)
        except Exception as exc:
            self.finished.emit([], [], {}, str(exc))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("JTTools", "StellarisErrorLogInspector")
        self.mods: List[ModInfo] = []
        self.errors: List[ParsedError] = []
        self.duplicates: Dict[str, List[str]] = {}
        self.worker_thread: Optional[QThread] = None
        self.worker: Optional[LogLoadWorker] = None
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.resize(1250, 760)
        self._build_ui()
        self._load_settings()
        self.summary.setPlainText("Ready. No log has been loaded yet. Choose the default log folder or browse to a custom error.log, then click SHOW ERROR LOG.")
        self.conflicts.setPlainText("No scan has been run yet.")

    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)

        settings_box = QGroupBox("Paths")
        grid = QGridLayout(settings_box)
        self.use_default_logs = QCheckBox("Use default Stellaris log folder")
        self.scan_duplicates = QCheckBox("Scan duplicate mod files / possible overwrite conflicts (slower)")
        self.log_path = QLineEdit()
        self.log_path.setPlaceholderText("Select folder containing error.log, or select error.log directly")
        self.mod_path = QLineEdit()
        self.mod_path.setPlaceholderText("Select Documents/Paradox Interactive/Stellaris/mod folder")
        browse_log = QPushButton("Browse Log Folder/File")
        browse_mod = QPushButton("Browse Mod Folder")
        open_log = QPushButton("Open Log Folder")
        self.show_button = QPushButton("SHOW ERROR LOG")
        browse_log.clicked.connect(self.browse_log)
        browse_mod.clicked.connect(self.browse_mod)
        open_log.clicked.connect(self.open_log_folder)
        self.show_button.clicked.connect(self.show_error_log)
        self.use_default_logs.stateChanged.connect(self.default_logs_changed)

        grid.addWidget(self.use_default_logs, 0, 0, 1, 2)
        grid.addWidget(self.scan_duplicates, 0, 2, 1, 2)
        grid.addWidget(QLabel("Log source:"), 1, 0)
        grid.addWidget(self.log_path, 1, 1)
        grid.addWidget(browse_log, 1, 2)
        grid.addWidget(open_log, 1, 3)
        grid.addWidget(QLabel("Mod descriptors:"), 2, 0)
        grid.addWidget(self.mod_path, 2, 1)
        grid.addWidget(browse_mod, 2, 2)
        grid.addWidget(self.show_button, 2, 3)
        root.addWidget(settings_box)

        self.status_label = QLabel("Ready")
        root.addWidget(self.status_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.summary = QTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setMinimumWidth(360)
        left_layout.addWidget(QLabel("Summary / likely culprits"))
        left_layout.addWidget(self.summary)
        self.conflicts = QTextEdit()
        self.conflicts.setReadOnly(True)
        left_layout.addWidget(QLabel("Potential file conflicts"))
        left_layout.addWidget(self.conflicts)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Severity", "Type", "Likely Mod", "File", "Line", "Relative Path", "Message"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.table.setSortingEnabled(True)
        right_layout.addWidget(QLabel("Parsed error.log entries"))
        right_layout.addWidget(self.table)

        buttons = QHBoxLayout()
        export_txt = QPushButton("Export TXT")
        export_csv = QPushButton("Export CSV")
        copy_report = QPushButton("Copy Summary")
        export_txt.clicked.connect(self.export_txt)
        export_csv.clicked.connect(self.export_csv)
        copy_report.clicked.connect(self.copy_summary)
        buttons.addWidget(export_txt)
        buttons.addWidget(export_csv)
        buttons.addWidget(copy_report)
        buttons.addStretch()
        right_layout.addLayout(buttons)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 3)
        root.addWidget(splitter, 1)
        self.setCentralWidget(central)

        about = QAction("About", self)
        about.triggered.connect(lambda: QMessageBox.information(self, "About", f"{APP_NAME} v{APP_VERSION}\n\nParses Stellaris error.log and estimates likely mod culprits."))
        self.menuBar().addMenu("Help").addAction(about)

    def _load_settings(self):
        use_default = self.settings.value("use_default_logs", True, type=bool)
        self.use_default_logs.setChecked(use_default)
        self.scan_duplicates.setChecked(self.settings.value("scan_duplicates", False, type=bool))
        self.log_path.setText(self.settings.value("log_path", str(default_log_dir())))
        self.mod_path.setText(self.settings.value("mod_path", str(default_mod_dir())))
        self.default_logs_changed()

    def _save_settings(self):
        self.settings.setValue("use_default_logs", self.use_default_logs.isChecked())
        self.settings.setValue("scan_duplicates", self.scan_duplicates.isChecked())
        self.settings.setValue("log_path", self.log_path.text())
        self.settings.setValue("mod_path", self.mod_path.text())

    def default_logs_changed(self):
        if self.use_default_logs.isChecked():
            self.log_path.setText(str(default_log_dir()))
            self.log_path.setEnabled(False)
        else:
            self.log_path.setEnabled(True)

    def current_error_log(self) -> Path:
        p = Path(self.log_path.text().strip())
        if p.is_dir():
            return p / "error.log"
        return p

    def browse_log(self):
        self.use_default_logs.setChecked(False)
        start = str(Path(self.log_path.text()).parent if self.log_path.text() else default_log_dir())
        file_path, _ = QFileDialog.getOpenFileName(self, "Select error.log file", start, "Log files (*.log *.txt);;All files (*.*)")
        if file_path:
            self.log_path.setText(file_path)
            return
        folder = QFileDialog.getExistingDirectory(self, "Select folder containing error.log", start)
        if folder:
            self.log_path.setText(folder)

    def browse_mod(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Stellaris mod descriptor folder", self.mod_path.text() or str(default_mod_dir()))
        if folder:
            self.mod_path.setText(folder)

    def open_log_folder(self):
        p = self.current_error_log()
        folder = p.parent if p.suffix else p
        if not folder.exists():
            QMessageBox.warning(self, "Folder not found", f"Folder does not exist:\n{folder}")
            return
        if sys.platform.startswith("win"):
            os.startfile(str(folder))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])

    def show_error_log(self):
        self._save_settings()
        error_log = self.current_error_log()
        mod_dir = Path(self.mod_path.text().strip())

        if self.worker_thread is not None and self.worker_thread.isRunning():
            QMessageBox.information(self, "Busy", "The log is already being loaded.")
            return

        if not error_log.exists():
            QMessageBox.warning(self, "error.log not found", f"No error.log was found here:\n{error_log}\n\nChoose a different log folder/file or use the default path.")
            self.errors = []
            self.populate_table()
            self.summary.setPlainText(self.build_summary_text(error_log, mod_dir))
            self.status_label.setText("error.log not found")
            return

        # Non-blocking load: the UI remains responsive while parsing/scanning runs.
        self.show_button.setEnabled(False)
        self.status_label.setText("Starting log load...")
        self.summary.setPlainText("Loading error.log... The window will remain responsive.")

        self.worker_thread = QThread(self)
        self.worker = LogLoadWorker(error_log, mod_dir, self.scan_duplicates.isChecked())
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.status.connect(self.status_label.setText)
        self.worker.finished.connect(lambda mods, errors, duplicates, err: self._log_load_finished(error_log, mod_dir, mods, errors, duplicates, err))
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker_thread.finished.connect(self._thread_cleanup)
        self.worker_thread.start()

    def _thread_cleanup(self):
        self.worker_thread = None
        self.worker = None

    def _log_load_finished(self, error_log: Path, mod_dir: Path, mods, errors, duplicates, error_message):
        self.show_button.setEnabled(True)
        if error_message:
            self.status_label.setText("Error while loading log")
            QMessageBox.critical(self, "Load error", f"Could not load/parse the log:\n\n{error_message}")
            return
        self.mods = mods
        self.errors = errors
        self.duplicates = duplicates
        self.populate_table()
        self.populate_summary(error_log, mod_dir)
        self.populate_conflicts()
        self.status_label.setText(f"Loaded {len(errors)} error/warning entries from error.log")

    # Backwards-compatible alias for older docs/scripts.
    refresh_all = show_error_log

    def populate_table(self):
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.errors))
        for row, err in enumerate(self.errors):
            vals = [err.severity, err.error_type, err.likely_mod, err.file_path, err.line, err.relative_path, err.message]
            for col, val in enumerate(vals):
                item = QTableWidgetItem(val)
                if col == 0:
                    if val == "High":
                        item.setBackground(QColor(255, 210, 210))
                    elif val == "Medium":
                        item.setBackground(QColor(255, 238, 200))
                    else:
                        item.setBackground(QColor(225, 240, 255))
                self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()
        self.table.setSortingEnabled(True)

    def build_summary_text(self, error_log: Optional[Path] = None, mod_dir: Optional[Path] = None) -> str:
        counts: Dict[str, int] = {}
        highs: Dict[str, int] = {}
        types: Dict[str, Dict[str, int]] = {}
        for e in self.errors:
            counts[e.likely_mod] = counts.get(e.likely_mod, 0) + 1
            if e.severity == "High":
                highs[e.likely_mod] = highs.get(e.likely_mod, 0) + 1
            types.setdefault(e.likely_mod, {})[e.error_type] = types.setdefault(e.likely_mod, {}).get(e.error_type, 0) + 1
        ranked = sorted(counts, key=lambda k: (highs.get(k, 0), counts[k]), reverse=True)
        lines = []
        lines.append(f"{APP_NAME} v{APP_VERSION}")
        if error_log:
            lines.append(f"Log: {error_log}")
        if mod_dir:
            lines.append(f"Mod descriptor folder: {mod_dir}")
        lines.append(f"Loaded mod descriptors: {len(self.mods)}")
        lines.append(f"Parsed error/warning entries: {len(self.errors)}")
        lines.append("")
        if not error_log or not error_log.exists():
            lines.append("WARNING: error.log was not found at the selected path.")
            return "\n".join(lines)
        if not self.errors:
            lines.append("No obvious error/warning entries were found in the selected log.")
            return "\n".join(lines)
        lines.append("Likely culprits ranked by high-severity and total entries:")
        for mod in ranked[:20]:
            lines.append(f"- {mod}: {counts[mod]} entries, {highs.get(mod, 0)} high severity")
            type_bits = ", ".join(f"{k}: {v}" for k, v in sorted(types[mod].items(), key=lambda x: x[1], reverse=True)[:4])
            if type_bits:
                lines.append(f"  Types: {type_bits}")
        lines.append("")
        lines.append("Interpretation note: this tool identifies the file/mod most directly referenced by the log. Some errors are indirect conflicts caused by load order or overwritten vanilla files.")
        return "\n".join(lines)

    def populate_summary(self, error_log: Path, mod_dir: Path):
        self.summary.setPlainText(self.build_summary_text(error_log, mod_dir))

    def populate_conflicts(self):
        if not self.scan_duplicates.isChecked():
            self.conflicts.setPlainText("Duplicate file conflict scanning is disabled. Enable the checkbox in Paths and click SHOW ERROR LOG to scan for possible overwritten files.")
            return
        if not self.duplicates:
            self.conflicts.setPlainText("No duplicate script/interface/localisation files were found across scanned mod folders, or no mod content folders were available.")
            return
        lines = ["Files present in more than one enabled/scanned mod folder:", ""]
        for rel, mods in sorted(self.duplicates.items())[:200]:
            lines.append(f"{rel}")
            lines.append("  " + ", ".join(sorted(set(mods))))
        if len(self.duplicates) > 200:
            lines.append(f"\n...and {len(self.duplicates) - 200} more.")
        self.conflicts.setPlainText("\n".join(lines))

    def export_txt(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export TXT report", "stellaris_error_report.txt", "Text files (*.txt)")
        if not path:
            return
        text = self.build_summary_text(self.current_error_log(), Path(self.mod_path.text().strip()))
        text += "\n\nDetailed entries:\n"
        for e in self.errors:
            text += f"\n[{e.severity}] {e.error_type}\nMod: {e.likely_mod}\nFile: {e.file_path}\nLine: {e.line}\nMessage: {e.message}\n"
        Path(path).write_text(text, encoding="utf-8")

    def export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV report", "stellaris_error_report.csv", "CSV files (*.csv)")
        if not path:
            return
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["severity", "error_type", "likely_mod", "file_path", "line", "relative_path", "message", "mod_path"])
            for e in self.errors:
                writer.writerow([e.severity, e.error_type, e.likely_mod, e.file_path, e.line, e.relative_path, e.message, e.mod_path])

    def copy_summary(self):
        QApplication.clipboard().setText(self.summary.toPlainText())
        QMessageBox.information(self, "Copied", "Summary copied to clipboard.")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    try:
        win = MainWindow()
        win.show()
        sys.exit(app.exec())
    except Exception as exc:
        QMessageBox.critical(None, "Startup error", f"{APP_NAME} could not start:\n\n{exc}")
        raise

if __name__ == "__main__":
    main()

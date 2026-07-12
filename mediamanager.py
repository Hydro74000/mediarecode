import os
import re
import subprocess
import sys
from collections import defaultdict

# Chemin complet optionnel vers l'executable MediaInfo, nom du binaire inclus.
# Exemple : "/usr/bin/mediainfo" ou r"C:\Program Files\MediaInfo\MediaInfo.exe"
# Laisser vide pour conserver l'appel actuel via le PATH : "mediainfo".
MEDIAINFO_EXECUTABLE_PATH = "/var/home/hydromel/dev/MediaInfo/MediaInfo_CLI_CPP/MediaInfo/Project/GNU/CLI/mediainfo"

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTreeWidget, QTreeWidgetItem, QPushButton, QLabel,
    QMessageBox, QSplitter, QFileDialog, QLineEdit, QScrollArea, QFrame, QFormLayout, QSizePolicy
)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont, QColor

# --- LOGIQUE DE NETTOYAGE ET PARSING ---
TRASH_WORDS = [r'\bmulti\b', r'\bfrench\b', r'\bvff\b', r'\bvfq\b', r'\bvfi\b', r'\bhdr\b', r'\btruefrench\b', r'\bfastsub\b', r'\bvostfr\b']

def clean_title(raw_title):
    if not raw_title: return ""
    t = raw_title
    
    # Suppression des tags entre crochets/accolades
    t = re.sub(r'\{.*?\}', '', t)
    t = re.sub(r'\[.*?\]', '', t)
    
    # Séparation si une année est collée au titre
    m_year = re.search(r'(.*?)(\b(19|20)\d{2}\b)', t, re.IGNORECASE)
    if m_year:
        potential = m_year.group(1).strip()
        if potential.replace('.', '').replace('-', '') != "":
            t = potential

    # Nettoyage des résolutions, codecs et mots poubelles
    t = re.split(r'\b(1080p|2160p|720p|bluray|web|vff|multi|vfi|french|dts|x264|x265|hdr|dv|hevc|uhd)\b', t, flags=re.IGNORECASE)[0]
    t = t.replace('.', ' ').replace('-', ' ').replace('_', ' ')
    
    for word in TRASH_WORDS:
        t = re.sub(word, '', t, flags=re.IGNORECASE)
        
    return re.sub(r'\s+', ' ', t).strip().title()

def parse_media_file(filepath):
    filename = os.path.basename(filepath)
    name = os.path.splitext(filename)[0]

    if "sample" in name.lower() or (len(name) > 30 and " " not in name and "." not in name):
        return None

    # Motifs de détection de séries (Ordre de priorité important)
    series_patterns = [
        r'(?P<show>.*?)[ \.\-_]*[Ss](?P<season>\d+)[ \.\-_]*[Ee](?P<ep>\d+)',              # S01E01, S01.E01
        r'(?P<show>.*?)[ \.\-_]*(?<!\d)(?P<season>\d+)[xX](?P<ep>\d+)(?!\d)',               # 1x01
        r'(?P<show>.*?)[ \.\-_]*[Ss]aison\s*(?P<season>\d+).*?[Ee]pisode\s*(?P<ep>\d+)',    # Saison 1 Episode 2
        r'(?P<show>.*?)[ \.\-_]*[Ss](?P<season>\d+)-(?P<ep>\d+)',                           # S06-01
    ]

    is_series = False
    season_num = 0
    ep_num = 0
    show_title = ""

    # 1. Analyse par nom de fichier
    for pattern in series_patterns:
        match = re.search(pattern, name, re.IGNORECASE)
        if match:
            is_series = True
            show_title = match.group('show')
            season_num = int(match.group('season'))
            ep_num = int(match.group('ep'))
            break

    # 2. Analyse par structure de dossier (Fallback)
    if not is_series:
        parent_dir = os.path.basename(os.path.dirname(filepath))
        match_season_dir = re.search(r'^(?:[Ss]eason|[Ss]aison|[Ss])\s*(\d+)$', parent_dir, re.IGNORECASE)
        if match_season_dir:
            is_series = True
            season_num = int(match_season_dir.group(1))
            # On tente de trouver le numéro d'épisode dans le fichier
            match_ep = re.search(r'(?:[Ee]pisode|[Ee]|^\s*|-|_)\s*(\d{1,3})\b', name, re.IGNORECASE)
            ep_num = int(match_ep.group(1)) if match_ep else 0
            show_title = os.path.basename(os.path.dirname(os.path.dirname(filepath)))

    if is_series:
        clean_show = clean_title(show_title)
        # Si le nom de la série est introuvable via la regex, on prend le dossier parent
        if not clean_show or len(clean_show) < 2:
            clean_show = clean_title(os.path.basename(os.path.dirname(filepath)))
            if re.search(r'(?:[Ss]eason|[Ss]aison|[Ss])\s*\d+', clean_show, re.IGNORECASE):
                clean_show = clean_title(os.path.basename(os.path.dirname(os.path.dirname(filepath))))

        if not clean_show: clean_show = "Série Inconnue"
        return {"type": "S", "show": clean_show, "season": season_num, "episode": ep_num, "path": filepath}
    else:
        clean_movie = clean_title(name)
        if clean_movie:
            return {"type": "F", "title": clean_movie, "path": filepath}
        return None

def get_file_size(path) -> str:
    try:
        size_bytes = float(os.path.getsize(path))
        for unit in ['B', 'Ko', 'Mo', 'Go', 'To']:
            if size_bytes < 1024: return f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.2f} To"
    except: return "N/A"

# --- LISTE DES CLÉS MEDIAINFO À CONSERVER ---
TARGET_KEYS = {
    "general": {"Complete name", "File size", "Duration", "Overall bit rate", "Title", "Format"},
    "video": {"Format", "Format profile", "HDR format", "Width", "Height", "Frame rate", "Maximum Content Light Level", "Maximum Frame-Average Light Level", "Bit rate"},
    "audio": {"Format", "Commercial name", "Title", "Language", "Bit rate", "Channel(s)", "Sampling rate"},
    "text": {"Format", "Title", "Language", "Forced"}
}

# --- THREADS ASYNCHRONES ---
class ScannerThread(QThread):
    file_found = Signal(dict) # Émet un dictionnaire structuré F ou S
    finished = Signal()
    
    def __init__(self, directory):
        super().__init__()
        self.directory = directory

    def run(self):
        for root, _, files in os.walk(self.directory):
            for f in files:
                if f.lower().endswith(('.mkv', '.mp4', '.avi', '.mov')):
                    filepath = os.path.join(root, f)
                    meta = parse_media_file(filepath)
                    if meta:
                        self.file_found.emit(meta)
        self.finished.emit()

class MediaInfoThread(QThread):
    info_ready = Signal(list)
    
    def __init__(self, filepath):
        super().__init__()
        self.filepath = filepath

    def parse_content(self, text):
        sections_data = []
        lines = text.split('\n')
        current_sec = None
        current_data = {}

        for line in lines:
            line = line.strip()
            if not line: continue
            if ":" not in line or (line.split(':', 1)[0].strip() in ["General", "Video", "Audio", "Text"]):
                if current_sec and current_data: 
                    sections_data.append({"title": current_sec, "data": current_data})
                current_sec = line
                current_data = {}
                continue
            if ":" in line and current_sec:
                key, val = [x.strip() for x in line.split(':', 1)]
                sec_type = current_sec.lower().split()[0]
                if sec_type in TARGET_KEYS and key in TARGET_KEYS[sec_type]:
                    current_data[key] = val
        if current_sec and current_data: 
            sections_data.append({"title": current_sec, "data": current_data})
        return sections_data

    def run(self):
        base = os.path.splitext(self.filepath)[0]
        nfo_path = base + ".nfo"
        mediainfo_nfo_path = base + "_mediainfo.nfo"

        for candidate, label in [(nfo_path, "📄 Fichier NFO"), (mediainfo_nfo_path, "📄 _mediainfo.nfo")]:
            if os.path.exists(candidate):
                try:
                    with open(candidate, 'r', encoding='utf-8', errors='ignore') as f:
                        data = self.parse_content(f.read())
                        if data:
                            data[0]["data"]["_Source"] = label
                            self.info_ready.emit(data)
                            return
                except: pass

        try:
            mediainfo_executable = MEDIAINFO_EXECUTABLE_PATH or 'mediainfo'
            res = subprocess.run([mediainfo_executable, self.filepath], capture_output=True, text=True, encoding='utf-8', stdin=subprocess.DEVNULL)
            if res.stdout:
                target = mediainfo_nfo_path if os.path.exists(nfo_path) else nfo_path
                try:
                    with open(target, 'w', encoding='utf-8') as f: f.write(res.stdout)
                except OSError: pass
            data = self.parse_content(res.stdout)
            if data: data[0]["data"]["_Source"] = "⚙️ MediaInfo Engine"
            self.info_ready.emit(data)
        except:
            self.info_ready.emit([])

# --- UI COMPONENTS ---
class InfoCard(QFrame):
    def __init__(self, title, data_dict):
        super().__init__()
        color = "#7aa2f7" 
        if "Video" in title: color = "#bb9af7" 
        elif "Audio" in title: color = "#9ece6a" 
        elif "Text" in title: color = "#e0af68" 

        self.setStyleSheet(f"QFrame {{ background-color: #24283b; border-radius: 8px; border: 1px solid #414868; margin-bottom: 12px; }} QLabel {{ border: none; }}")
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        
        header = QLabel(title.upper())
        header.setStyleSheet(f"color: {color}; font-weight: 900; font-size: 12px; letter-spacing: 1px; margin-bottom: 5px;")
        layout.addWidget(header)
        
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(6)
        
        for k, v in data_dict.items():
            lbl_key = QLabel(k)
            lbl_key.setStyleSheet("color: #565f89; font-weight: bold; font-size: 11px;")
            lbl_val = QLabel(v)
            lbl_val.setStyleSheet("color: #c0caf5; font-size: 11px; font-weight: 600;")
            lbl_val.setWordWrap(True)
            form.addRow(lbl_key, lbl_val)
            
        layout.addLayout(form)

def _startup_directory_from_argv(argv):
    """Retourne le premier dossier positionnel passé au lancement."""
    for raw in argv[1:]:
        if not raw or raw.startswith("-"):
            continue
        path = os.path.abspath(os.path.expanduser(raw))
        if os.path.isdir(path):
            return path
        raise ValueError(f"Le chemin de démarrage n'est pas un dossier valide : {raw}")
    return None


class MediaManager(QMainWindow):
    COL = {"type": 0, "nom": 1, "taille": 2, "action": 3}

    def __init__(self, startup_dir=None):
        super().__init__()
        self.tree_items = {}
        self.full_data = defaultdict(list)
        self.scanned_count = 0
        self._current_path = None
        self._threads = []
        
        self.setWindowTitle("Media Manager")
        self.resize(1400, 900)
        self.apply_styles()
        self.init_ui()

        self.target_dir = startup_dir or QFileDialog.getExistingDirectory(self, "Choisir le dossier de films/séries")
        if self.target_dir:
            self.start_scan()
        else:
            sys.exit(0)

    def apply_styles(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background-color: #1a1b26; color: #c0caf5; font-family: 'Segoe UI', 'Inter', sans-serif; }
            QTreeWidget { background-color: #1f2335; border: 1px solid #292e42; border-radius: 8px; outline: none; }
            QTreeWidget::item { height: 38px; border-bottom: 1px solid #24283b; }
            QTreeWidget::item:selected { background-color: #3d59a1; color: #ffffff; border-radius: 4px; }
            QHeaderView::section { background-color: #1f2335; color: #7aa2f7; padding: 10px; border: none; font-weight: bold; font-size: 11px; text-transform: uppercase; }
            QHeaderView::section:hover { background-color: #252a3a; color: #c0caf5; }
            QHeaderView::section:pressed { background-color: #2d3250; color: #bb9af7; }
            QLineEdit { background-color: #24283b; border: 1px solid #414868; border-radius: 6px; padding: 10px; color: #c0caf5; font-size: 13px; }
            QLineEdit:focus { border: 1px solid #7aa2f7; }
            QPushButton#btn_delete { background-color: #f7768e; color: #1a1b26; border-radius: 4px; font-size: 10px; font-weight: bold; height: 22px; max-width: 80px; }
            QPushButton#btn_delete:hover { background-color: #ff9eaf; }
            QPushButton#btn_refresh { background-color: #24283b; border: 1px solid #414868; color: #7aa2f7; border-radius: 6px; padding: 8px 15px; font-weight: bold;}
            QPushButton#btn_refresh:hover { background-color: #414868; }
            QPushButton#btn_browse { background-color: #24283b; border: 1px solid #bb9af7; color: #bb9af7; border-radius: 6px; padding: 8px 15px; font-weight: bold;}
            QPushButton#btn_browse:hover { background-color: #bb9af7; color: #1a1b26; }
            QScrollArea { border: none; background-color: transparent; }
            QScrollBar:vertical { background: #1a1b26; width: 12px; margin: 0px; }
            QScrollBar::handle:vertical { background: #414868; border-radius: 6px; min-height: 20px; }
        """)

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        top = QHBoxLayout()
        title = QLabel("MEDIA MANAGER")
        title.setStyleSheet("font-weight: 950; font-size: 24px; color: #bb9af7; letter-spacing: -1px;")
        
        self.search = QLineEdit()
        self.search.setPlaceholderText("Rechercher un film ou une série...")
        self.search.setFixedWidth(400)
        self.search.textChanged.connect(self.filter_tree)

        self.btn_refresh = QPushButton("Actualiser")
        self.btn_refresh.setObjectName("btn_refresh")
        self.btn_refresh.clicked.connect(self.start_scan)

        self.btn_browse = QPushButton("Browse...")
        self.btn_browse.setObjectName("btn_browse")
        self.btn_browse.clicked.connect(self.change_directory)
        
        top.addWidget(title)
        top.addStretch()
        top.addWidget(self.search)
        top.addWidget(self.btn_refresh)
        top.addWidget(self.btn_browse)
        layout.addLayout(top)

        self.lbl_status = QLabel("Prêt")
        self.lbl_status.setStyleSheet("color: #9ece6a; font-weight: bold; font-size: 12px;")
        layout.addWidget(self.lbl_status)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        
        self.tree = QTreeWidget()
        self.tree.setSortingEnabled(False)
        self.tree.setHeaderLabels(["Type", "Nom / Fichier", "Taille", "Action"])
        self.tree.setColumnWidth(self.COL["type"],   55)
        self.tree.setColumnWidth(self.COL["nom"],   500)
        self.tree.setColumnWidth(self.COL["taille"], 100)
        self.tree.setColumnWidth(self.COL["action"],  50)
        self.tree.itemSelectionChanged.connect(self.load_info)

        header = self.tree.header()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self.handle_header_click)

        self._sort_col = None
        self._sort_asc = False
        splitter.addWidget(self.tree)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.details_container = QWidget()
        self.details_layout = QVBoxLayout(self.details_container)
        self.details_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.placeholder = self._make_placeholder()
        self.details_layout.addWidget(self.placeholder)

        self.scroll_area.setWidget(self.details_container)
        splitter.addWidget(self.scroll_area)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

    def _make_placeholder(self):
        placeholder = QLabel("Sélectionnez un fichier pour voir les détails")
        placeholder.setStyleSheet("color: #565f89; font-style: italic; font-size: 14px;")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return placeholder

    def _clear_details(self):
        while self.details_layout.count():
            item = self.details_layout.takeAt(0)
            if item:
                w = item.widget()
                if w:
                    w.deleteLater()

    def _show_placeholder(self):
        self._clear_details()
        self.placeholder = self._make_placeholder()
        self.details_layout.addWidget(self.placeholder)

    def handle_header_click(self, index):
        C = self.COL
        sortable = {C["type"], C["nom"], C["taille"]}
        if index not in sortable: return
        if self._sort_col == index: self._sort_asc = not self._sort_asc
        else: self._sort_asc = False
        self._sort_col = index

        order = Qt.SortOrder.AscendingOrder if self._sort_asc else Qt.SortOrder.DescendingOrder
        direction = "▲ Croissant" if self._sort_asc else "▼ Décroissant"
        col_name = {C["type"]: "Type", C["nom"]: "Nom / Fichier", C["taille"]: "Taille"}[index]
        self.lbl_status.setText(f"🔃 Tri par {col_name} {direction}…")

        self.tree.header().setSortIndicator(index, order)
        self._update_header_style(index)

        if index == C["type"]: self.sort_by_type()
        elif index == C["nom"]: self.sort_tree()
        elif index == C["taille"]: self.sort_by_size()

    def _update_header_style(self, active_col):
        C = self.COL
        arrow = " ▲" if self._sort_asc else " ▼"
        labels = {C["type"]: "Type", C["nom"]: "Nom / Fichier", C["taille"]: "Taille", C["action"]: "Action"}
        for col, label in labels.items():
            self.tree.headerItem().setText(col, label + (arrow if col == active_col else ""))

        base = "QHeaderView::section { background-color: #1f2335; color: #7aa2f7; padding: 10px; border: none; font-weight: bold; font-size: 11px; }"
        active = "color: #bb9af7; background-color: #1d1f33; border-bottom: 2px solid #bb9af7;"
        nth = {C["type"]: "first", C["nom"]: "nth-child(2)", C["taille"]: "nth-child(3)"}
        extra = f"QHeaderView::section:{nth[active_col]} {{ {active} }}" if active_col in nth else ""
        self.tree.header().setStyleSheet(base + extra)

    def restore_buttons(self, item):
        path = item.data(self.COL["type"], Qt.ItemDataRole.UserRole)
        if isinstance(path, str) and path.lower().endswith(('.mkv', '.mp4', '.avi')):
            self.create_del_btn(item, path)
        else:
            for i in range(item.childCount()):
                self.restore_buttons(item.child(i))

    def _make_file_item(self, parent: QTreeWidgetItem, path: str, total_bytes: int) -> QTreeWidgetItem:
        child = QTreeWidgetItem(parent, ["", os.path.basename(path), get_file_size(path), ""])
        child.setData(self.COL["type"], Qt.ItemDataRole.UserRole, path)
        child.setData(self.COL["taille"], Qt.ItemDataRole.UserRole, total_bytes)
        self.create_del_btn(child, path)
        return child

    def create_del_btn(self, item, path):
        btn = QPushButton("Supprimer")
        btn.setObjectName("btn_delete")
        btn.clicked.connect(lambda checked, p=path, i=item: self.confirm_delete(p, i))
        c = QWidget()
        l = QHBoxLayout(c)
        l.setContentsMargins(0,0,10,0)
        l.setAlignment(Qt.AlignmentFlag.AlignRight)
        l.addWidget(btn)
        self.tree.setItemWidget(item, self.COL["action"], c)
        
    def sort_by_type(self):
        self.tree.setUpdatesEnabled(False)
        items = []
        while self.tree.topLevelItemCount() > 0:
            items.append(self.tree.takeTopLevelItem(0))

        # F avant S si ascendant, S avant F si descendant
        C = self.COL
        items.sort(key=lambda x: (x.text(C["type"]), x.text(C["nom"]).lower()), reverse=not self._sort_asc)

        for parent in items:
            self.tree.addTopLevelItem(parent)
            self.restore_buttons(parent)

        self.tree.setUpdatesEnabled(True)
        self.lbl_status.setText("🔠 Trié par type (F / S)")

    def sort_by_size(self):
        self.tree.setUpdatesEnabled(False)
        items = []
        while self.tree.topLevelItemCount() > 0:
            items.append(self.tree.takeTopLevelItem(0))

        C = self.COL
        sign = 1 if self._sort_asc else -1
        items.sort(key=lambda item: sign * (item.data(C["taille"], Qt.ItemDataRole.UserRole) or 0))

        for parent in items:
            self.tree.addTopLevelItem(parent)
            self.restore_buttons(parent)

        self.tree.setUpdatesEnabled(True)
        self.lbl_status.setText("⚖️ Trié par taille")

    def sort_tree(self):
        self.tree.setUpdatesEnabled(False)
        items = []
        while self.tree.topLevelItemCount() > 0:
            items.append(self.tree.takeTopLevelItem(0))

        # 1. Sous-tri croissant saisons et épisodes par numéro
        C = self.COL
        def sort_children_by_num(node):
            children = [node.takeChild(0) for _ in range(node.childCount())]
            children.sort(key=lambda x: int(m.group()) if (m := re.search(r'\d+', x.text(C["nom"]))) else 0)
            for child in children:
                node.addChild(child)

        for parent in items:
            sort_children_by_num(parent)          # saisons
            for i in range(parent.childCount()):
                sort_children_by_num(parent.child(i))  # épisodes

        # 2. Tri du niveau parent (Doublons en premier, puis alphabétique)
        items.sort(key=lambda x: x.text(C["nom"]).lower(), reverse=not self._sort_asc)
        items.sort(key=lambda x: (x.data(C["action"], Qt.ItemDataRole.UserRole) or 0), reverse=True)
        
        for parent in items:
            self.tree.addTopLevelItem(parent)
            self.restore_buttons(parent)
        
        self.tree.setUpdatesEnabled(True)
        self.lbl_status.setText(f"✅ Tri terminé : {len(items)} titres/séries organisés.")

    def start_scan(self):
        self.tree.clear()
        self.tree_items.clear()
        self.full_data.clear()
        self.scanned_count = 0
        
        self.lbl_status.setText(f"⏳ Analyse du dossier en cours... ({self.target_dir})")
        self.scanner = ScannerThread(self.target_dir)
        self.scanner.file_found.connect(self.add_to_ui)
        self.scanner.finished.connect(self.on_scan_finished)
        self.scanner.start()

    def on_scan_finished(self):
        self.lbl_status.setText(f"✅ Analyse terminée : {self.tree.topLevelItemCount()} éléments racines en mémoire.")
        self._sort_col = self.COL["nom"]
        self._sort_asc = False
        self.tree.header().setSortIndicator(self.COL["nom"], Qt.SortOrder.DescendingOrder)
        self._update_header_style(self.COL["nom"])
        self.sort_tree()

    def add_to_ui(self, meta):
        path = meta["path"]
        total_bytes = os.path.getsize(path) if os.path.exists(path) else 0
        self.scanned_count += 1
        
        if self.scanned_count % 10 == 0:
            self.lbl_status.setText(f"⏳ Analyse en cours... ({self.scanned_count} fichiers trouvés)")

        C = self.COL
        if meta["type"] == "F":
            title = meta["title"]
            key_f = f"F|{title}"

            self.full_data[key_f].append(path)
            count = len(self.full_data[key_f])

            if key_f not in self.tree_items:
                parent = QTreeWidgetItem(self.tree, ["F", f"{title} ({count})", self.format_size(total_bytes), ""])
                parent.setForeground(C["type"], QColor("#9ece6a"))
                parent.setTextAlignment(C["type"], Qt.AlignmentFlag.AlignCenter)
                parent.setFont(C["nom"], QFont("Segoe UI", 11, QFont.Weight.Bold))
                parent.setData(C["taille"], Qt.ItemDataRole.UserRole, total_bytes)
                parent.setData(C["action"], Qt.ItemDataRole.UserRole, count - 1)
                parent.setData(C["type"], Qt.ItemDataRole.UserRole, key_f)
                self.tree_items[key_f] = parent

                current_search = self.search.text().lower()
                if current_search and current_search not in title.lower():
                    parent.setHidden(True)
            else:
                parent = self.tree_items[key_f]
                parent.setText(C["nom"], f"{title} ({count})")
                new_size = parent.data(C["taille"], Qt.ItemDataRole.UserRole) + total_bytes
                parent.setData(C["taille"], Qt.ItemDataRole.UserRole, new_size)
                parent.setText(C["taille"], self.format_size(new_size))
                parent.setData(C["action"], Qt.ItemDataRole.UserRole, count - 1)
                if count > 1: parent.setForeground(C["nom"], QColor("#e0af68"))

            assert isinstance(parent, QTreeWidgetItem)
            self._make_file_item(parent, path, total_bytes)

        elif meta["type"] == "S":
            show = meta["show"]
            season = meta["season"]
            ep = meta["episode"]

            key_s = f"S|{show}"
            key_season = f"S|{show}|{season}"
            key_ep = f"S|{show}|{season}|{ep}"

            self.full_data[key_ep].append(path)
            count = len(self.full_data[key_ep])

            # 1. SHOW
            if key_s not in self.tree_items:
                n_show = QTreeWidgetItem(self.tree, ["S", show, "0 B", ""])
                n_show.setForeground(C["type"], QColor("#e0af68"))
                n_show.setTextAlignment(C["type"], Qt.AlignmentFlag.AlignCenter)
                n_show.setFont(C["nom"], QFont("Segoe UI", 11, QFont.Weight.Bold))
                n_show.setForeground(C["nom"], QColor("#bb9af7"))
                n_show.setData(C["taille"], Qt.ItemDataRole.UserRole, 0)
                n_show.setData(C["action"], Qt.ItemDataRole.UserRole, 0)
                n_show.setData(C["type"], Qt.ItemDataRole.UserRole, key_s)
                self.tree_items[key_s] = n_show

                current_search = self.search.text().lower()
                if current_search and current_search not in show.lower():
                    n_show.setHidden(True)
            else:
                n_show = self.tree_items[key_s]

            s_size = n_show.data(C["taille"], Qt.ItemDataRole.UserRole) + total_bytes
            n_show.setData(C["taille"], Qt.ItemDataRole.UserRole, s_size)
            n_show.setText(C["taille"], self.format_size(s_size))
            if count > 1:
                n_show.setData(C["action"], Qt.ItemDataRole.UserRole, n_show.data(C["action"], Qt.ItemDataRole.UserRole) + 1)

            # 2. SEASON
            if key_season not in self.tree_items:
                n_season = QTreeWidgetItem(n_show, ["", f"Saison {season:02d} (0 épisode)", "0 B", ""])
                n_season.setFont(C["nom"], QFont("Segoe UI", 10, QFont.Weight.Bold))
                n_season.setForeground(C["nom"], QColor("#7aa2f7"))
                n_season.setData(C["taille"], Qt.ItemDataRole.UserRole, 0)
                n_season.setData(C["type"], Qt.ItemDataRole.UserRole, key_season)
                self.tree_items[key_season] = n_season
            else:
                n_season = self.tree_items[key_season]

            seas_size = n_season.data(C["taille"], Qt.ItemDataRole.UserRole) + total_bytes
            n_season.setData(C["taille"], Qt.ItemDataRole.UserRole, seas_size)
            n_season.setText(C["taille"], self.format_size(seas_size))

            nb_saisons = n_show.childCount()
            n_show.setText(C["nom"], f"{show} ({nb_saisons} {'saison' if nb_saisons <= 1 else 'saisons'})")

            # 3. EPISODE
            if key_ep not in self.tree_items:
                n_ep = QTreeWidgetItem(n_season, ["", f"Épisode {ep:02d} ({count})", "0 B", ""])
                n_ep.setFont(C["nom"], QFont("Segoe UI", 10))
                n_ep.setData(C["taille"], Qt.ItemDataRole.UserRole, 0)
                n_ep.setData(C["type"], Qt.ItemDataRole.UserRole, key_ep)
                self.tree_items[key_ep] = n_ep
            else:
                n_ep = self.tree_items[key_ep]
                n_ep.setText(C["nom"], f"Épisode {ep:02d} ({count})")
                if count > 1: n_ep.setForeground(C["nom"], QColor("#e0af68"))

            nb_eps = n_season.childCount()
            n_season.setText(C["nom"], f"Saison {season:02d} ({nb_eps} {'épisode' if nb_eps <= 1 else 'épisodes'})")

            ep_size = n_ep.data(C["taille"], Qt.ItemDataRole.UserRole) + total_bytes
            n_ep.setData(C["taille"], Qt.ItemDataRole.UserRole, ep_size)
            n_ep.setText(C["taille"], self.format_size(ep_size))

            # 4. FILE
            assert isinstance(n_ep, QTreeWidgetItem)
            self._make_file_item(n_ep, path, total_bytes)

    def format_size(self, size_bytes):
        if size_bytes == 0: return "0 B"
        for unit in ['B', 'Ko', 'Mo', 'Go', 'To']:
            if size_bytes < 1024: return f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.2f} To"

    def change_directory(self):
        new_path = QFileDialog.getExistingDirectory(self, "Sélectionner un nouveau répertoire")
        if new_path and new_path != self.target_dir:
            self.target_dir = new_path
            self.start_scan()
            
    def filter_tree(self, text):
        query = text.lower()
        for i in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(i)
            if parent is None:
                continue
            should_show = query in parent.text(self.COL["nom"]).lower()
            parent.setHidden(not should_show)

    def load_info(self):
        sel = self.tree.selectedItems()
        if not sel or not sel[0].data(self.COL["type"], Qt.ItemDataRole.UserRole): return

        path = sel[0].data(self.COL["type"], Qt.ItemDataRole.UserRole)
        if not isinstance(path, str) or not path.lower().endswith(('.mkv', '.mp4', '.avi')): return

        self._current_path = path

        self._clear_details()

        loader = QLabel("⚡ Extraction des métadonnées MediaInfo...")
        loader.setStyleSheet("color: #7aa2f7; font-weight: bold; font-size: 12px; margin-top: 20px;")
        loader.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.details_layout.addWidget(loader)

        t = MediaInfoThread(path)
        t.info_ready.connect(lambda sections, p=path: self._on_info_ready(sections, p))
        t.finished.connect(lambda thread=t: self._threads.remove(thread))
        self._threads.append(t)
        t.start()

    def _on_info_ready(self, sections, path):
        if path != self._current_path:
            return
        self.draw_info(sections)

    def draw_info(self, sections):
        self._clear_details()
            
        if not sections:
            err = QLabel("Aucune donnée MediaInfo trouvée.")
            err.setStyleSheet("color: #f7768e;")
            self.details_layout.addWidget(err)
            
        for sec in sections:
            if sec["data"]: 
                self.details_layout.addWidget(InfoCard(sec["title"], sec["data"]))
                
        self.details_layout.addStretch()

    def confirm_delete(self, path, item):
        msg = QMessageBox(self)
        msg.setWindowTitle("Suppression")
        msg.setText(f"Voulez-vous vraiment supprimer définitivement ce fichier ?\n\n{os.path.basename(path)}")
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        msg.setDefaultButton(QMessageBox.StandardButton.No)
        msg.setStyleSheet("QLabel{color: #c0caf5; min-width: 400px;} QPushButton{width: 80px;}")

        if msg.exec() == QMessageBox.StandardButton.Yes:
            
            C = self.COL
            file_size_deleted = item.data(C["taille"], Qt.ItemDataRole.UserRole) or 0
            ep_or_movie_node = item.parent()
            if ep_or_movie_node is None:
                return
            data_key = ep_or_movie_node.data(C["type"], Qt.ItemDataRole.UserRole)

            try:
                os.remove(path)
            except FileNotFoundError:
                pass  
            except Exception as e:
                QMessageBox.critical(self, "Erreur", f"Erreur de suppression :\n{e}")
                return

            if data_key in self.full_data and path in self.full_data[data_key]:
                self.full_data[data_key].remove(path)

            season_node = ep_or_movie_node.parent() if ep_or_movie_node.parent() else None
            show_node = season_node.parent() if season_node else None

            # Retirer visuellement le fichier
            ep_or_movie_node.removeChild(item)

            # Mise à jour des tailles en remontant l'arbre
            def reduce_size_upwards(node, amount):
                while node:
                    cur = node.data(C["taille"], Qt.ItemDataRole.UserRole) or 0
                    new_val = max(0, cur - amount)
                    node.setData(C["taille"], Qt.ItemDataRole.UserRole, new_val)
                    node.setText(C["taille"], self.format_size(new_val))
                    node = node.parent()

            reduce_size_upwards(ep_or_movie_node, file_size_deleted)

            # Nettoyage des noeuds vides
            if ep_or_movie_node.childCount() == 0:
                if season_node: # Logique Série
                    season_node.removeChild(ep_or_movie_node)
                    if data_key in self.tree_items: del self.tree_items[data_key]
                    
                    if season_node.childCount() == 0 and show_node is not None:
                        show_node.removeChild(season_node)
                        season_key = season_node.data(C["type"], Qt.ItemDataRole.UserRole)
                        if season_key in self.tree_items: del self.tree_items[season_key]

                        if show_node.childCount() == 0:
                            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(show_node))
                            show_key = show_node.data(C["type"], Qt.ItemDataRole.UserRole)
                            if show_key in self.tree_items: del self.tree_items[show_key]
                else: # Logique Film
                    self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(ep_or_movie_node))
                    if data_key in self.tree_items: del self.tree_items[data_key]
            else:
                # Mise à jour du texte si le noeud survit
                new_count = len(self.full_data[data_key])
                if season_node: # Épisode
                    ep_num = data_key.split('|')[-1]
                    ep_or_movie_node.setText(C["nom"], f"Épisode {int(ep_num):02d} ({new_count})")
                    if new_count == 1: ep_or_movie_node.setForeground(C["nom"], QColor("#c0caf5"))
                    if show_node:
                        show_node.setData(C["action"], Qt.ItemDataRole.UserRole, max(0, show_node.data(C["action"], Qt.ItemDataRole.UserRole) - 1))
                else: # Film
                    title = data_key.split('|')[1]
                    ep_or_movie_node.setText(C["nom"], f"{title} ({new_count})")
                    if new_count == 1: ep_or_movie_node.setForeground(C["nom"], QColor("#c0caf5"))
                    ep_or_movie_node.setData(C["action"], Qt.ItemDataRole.UserRole, new_count - 1)

            # Réinitialiser le panneau de droite
            self._current_path = None
            self._show_placeholder()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    try:
        startup_dir = _startup_directory_from_argv(sys.argv)
    except ValueError as exc:
        QMessageBox.critical(None, "Dossier invalide", str(exc))
        sys.exit(2)
    win = MediaManager(startup_dir)
    win.show()
    sys.exit(app.exec())

"""GGGI ResourceSpace bulk uploader (PySide6 desktop app).

Run:    uv run --with PySide6 --with keyring python app.py
Build:  uv run --with PySide6 --with keyring --with pyinstaller --with pillow \
            pyinstaller --windowed --noconfirm --name "RS Uploader" --icon icon.png --add-data "icon.png:." app.py
        -> dist/RS Uploader.app on macOS. Build on Windows for the .exe (PyInstaller does not cross-compile).

API key is stored in the OS keychain (macOS Keychain / Windows Credential Manager), never in a file.
Settings + presets: ~/.gggi_uploader/config.json. Upload logs: ~/.gggi_uploader/logs/*.csv
"""
import csv, json, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import keyring
from PySide6.QtCore import QFileInfo, QObject, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QCompleter, QDialog, QDialogButtonBox,
    QFileDialog, QFileIconProvider, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListView, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton,
    QScrollArea, QSplitter, QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator, QVBoxLayout, QWidget)

import rs_api

APP_DIR = Path.home() / ".gggi_uploader"
CONFIG = APP_DIR / "config.json"
SCHEMA_CACHE = APP_DIR / "fields_cache_v2.json"  # bump the version when the cached field format changes
KEYRING_SERVICE = "gggi-rs-uploader"
DEFAULT_URL = "https://gggi.resourcespace.com/api/"
PROFILE_URL = "https://gggi.resourcespace.com/pages/user/user_profile_edit.php"
WORKERS = 3  # parallel uploads


def load_config():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    APP_DIR.mkdir(exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=1, ensure_ascii=False))


_keys = {}


def get_key(user):
    """Keychain read, once per run: on macOS every read of an unsigned app's item can prompt."""
    if user and user not in _keys:
        _keys[user] = keyring.get_password(KEYRING_SERVICE, user)
    return _keys.get(user)


def set_key(user, key):
    keyring.set_password(KEYRING_SERVICE, user, key)
    _keys[user] = key


def make_client(cfg):
    key = get_key(cfg.get("user"))
    return rs_api.Client(cfg["url"], cfg["user"], key) if cfg.get("url") and key else None


def thumbnail(path):
    # ponytail: decoded on the UI thread; move to a worker if batches of 1000+ photos feel slow
    r = QImageReader(str(path))
    r.setAutoTransform(True)
    if r.canRead():
        if r.size().isValid():
            r.setScaledSize(r.size().scaled(160, 160, Qt.KeepAspectRatio))
        img = r.read()
        if not img.isNull():
            return QIcon(QPixmap.fromImage(img))
    return QFileIconProvider().icon(QFileInfo(str(path)))


class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ResourceSpace connection")
        self.cfg = cfg
        self.url = QLineEdit(cfg.get("url") or DEFAULT_URL)
        self.user = QLineEdit(cfg.get("user", ""), placeholderText="your GGGI email, e.g. name@gggi.org")
        has_key = bool(get_key(cfg.get("user")))
        self.key = QLineEdit(echoMode=QLineEdit.Password,
                             placeholderText="saved in keychain (type only to change)" if has_key else "")
        help_ = QLabel(f'How to get your API key:<br>1. Log in to ResourceSpace.<br>'
                       f'2. Open <a href="{PROFILE_URL}">{PROFILE_URL}</a><br>'
                       f'3. Next to <b>API key</b>, click <b>Show hidden property</b> and copy the key.')
        help_.setOpenExternalLinks(True)
        help_.setWordWrap(True)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        test = QPushButton("Test connection")
        test.clicked.connect(self.test)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.addRow("URL", self.url)
        form.addRow("Username (GGGI email)", self.user)
        form.addRow("API key", self.key)
        form.addRow(help_)
        form.addRow(test, self.status)
        form.addRow(buttons)
        self.resize(560, 0)

    def _key(self):
        return self.key.text().strip() or get_key(self.user.text().strip()) or ""

    def test(self):
        try:
            types = rs_api.Client(self.url.text(), self.user.text(), self._key(), timeout=30).resource_types()
            self.status.setText(f"OK: {len(types)} resource types")
        except rs_api.RSError as e:
            self.status.setText(f"Failed: {e}")

    def save(self):
        user = self.user.text().strip()
        if not (self.url.text().strip() and user and self._key()):
            self.status.setText("URL, username and API key are all required.")
            return
        if self.key.text().strip():
            set_key(user, self.key.text().strip())
        self.cfg.update(url=self.url.text().strip(), user=user)
        save_config(self.cfg)
        self.accept()


class PickerDialog(QDialog):
    """Searchable multi-select for checkbox / category tree / keyword fields."""

    def __init__(self, title, options, selected, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.search = QLineEdit(placeholderText="Search...")
        self.list = QListWidget()
        for ref, label in sorted(options, key=lambda o: o[1].lower()):
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, ref)
            it.setCheckState(Qt.Checked if ref in selected else Qt.Unchecked)
            self.list.addItem(it)
        self.search.textChanged.connect(self.filter)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        for w in (self.search, self.list, buttons):
            lay.addWidget(w)
        self.resize(380, 480)

    def filter(self, text):
        for i in range(self.list.count()):
            self.list.item(i).setHidden(text.lower() not in self.list.item(i).text().lower())

    def selected(self):
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]


class FieldInput(QWidget):
    """One metadata field. value(): str for text fields, list of node ids for fixed-list fields.
    Dropdowns, radios and small checkbox lists (<= 3 options, e.g. Yes/No) use a single-pick combo box."""
    changed = Signal()

    def __init__(self, field, options):
        super().__init__()
        self.field, self.options, self.labels = field, options, dict(options)
        self.hint = rs_api.auto_hint(field)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)
        t = field["type"]
        self.single = t in rs_api.SINGLE_SELECT or (t == 2 and 0 < len(options) <= 3)
        if self.single:
            self.w = QComboBox(editable=True, insertPolicy=QComboBox.NoInsert)
            self.w.addItem("", None)
            for ref, label in sorted(options, key=lambda o: o[1].lower()):
                self.w.addItem(label, ref)
            self.w.completer().setFilterMode(Qt.MatchContains)
            self.w.completer().setCompletionMode(QCompleter.PopupCompletion)
            self.w.currentIndexChanged.connect(self.changed)
        elif t in rs_api.FIXED_LIST:
            self.chosen = []
            self.w = QPushButton(styleSheet="text-align: left")
            self.w.clicked.connect(self.pick)
            self._show()
        else:
            self.w = QLineEdit(placeholderText="YYYY-MM-DD" if t in rs_api.DATE_TYPES else "")
            self.w.textChanged.connect(self.changed)
        lay.addWidget(self.w)
        if self.hint:
            note = QLabel(self.hint + " if left blank")
            note.setStyleSheet("color: gray; font-size: 11px")
            lay.addWidget(note)

    def pick(self):
        d = PickerDialog(self.field["title"], self.options, self.chosen, self)
        if d.exec():
            self.chosen = d.selected()
            self._show()
            self.changed.emit()

    def _show(self):
        names = [self.labels.get(r, str(r)) for r in self.chosen]
        self.w.setText(", ".join(names) if names else "Choose...")
        self.w.setToolTip("\n".join(names))

    def value(self):
        if self.single:
            i = self.w.findText(self.w.currentText())
            return [self.w.itemData(i)] if i > 0 else []
        if self.field["type"] in rs_api.FIXED_LIST:
            return list(self.chosen)
        return self.w.text().strip()

    def set_value(self, v):
        if self.single:
            self.w.setCurrentIndex(max(self.w.findData(v[0]), 0) if v else 0)
        elif self.field["type"] in rs_api.FIXED_LIST:
            self.chosen = list(v or [])
            self._show()
            self.changed.emit()
        else:
            self.w.setText(v or "")


class DestinationTree(QWidget):
    """Folder sidebar: Favorites, My collections, featured folder tree.
    Categories (folders with sub-folders) cannot be picked; click toggles them open."""
    changed = Signal()
    REF = Qt.UserRole

    def __init__(self, cfg, on_new_collection):
        super().__init__()
        self.cfg, self.dests, self.by_ref = cfg, [], {}
        self.want = cfg.get("last_dest", False)  # ref to re-select after a rebuild; False = nothing picked
        self.search = QLineEdit(placeholderText="Search folders", clearButtonEnabled=True)
        self.search.textChanged.connect(self.filter)
        self.tree = QTreeWidget(headerHidden=True)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.menu)
        self.tree.itemSelectionChanged.connect(self._picked)
        self.tree.itemClicked.connect(lambda it: None if self._pickable(it) else it.setExpanded(not it.isExpanded()))
        self.status = QLabel("Right-click a collection to add it to Favorites.")
        self.status.setWordWrap(True)
        new = QPushButton("New collection...")
        new.clicked.connect(on_new_collection)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        for w in (self.search, self.tree, self.status, new):
            lay.addWidget(w)

    @staticmethod
    def _pickable(it):
        return bool(it.flags() & Qt.ItemIsSelectable)

    def _node(self, parent, text, ref=None, pickable=False, tip="", style=None):
        it = QTreeWidgetItem(parent, [text])
        it.setData(0, self.REF, ref)
        it.setToolTip(0, tip or text)
        if not pickable:
            it.setFlags(it.flags() & ~Qt.ItemIsSelectable)
        if style:  # "bold" section header / "italic" special entry
            font = it.font(0)
            font.setBold(style == "bold")
            font.setItalic(style == "italic")
            it.setFont(0, font)
        return it

    def set_items(self, dests):
        self.dests = dests
        self.by_ref = {d["ref"]: d for d in sorted(dests, key=lambda d: d["kind"] == "folder")}  # folder wins
        self.tree.blockSignals(True)
        self.tree.clear()
        self._node(self.tree, "No collection", None, True, "Upload without adding to any collection", "italic")
        fav = self._node(self.tree, "Favorites", style="bold")
        for ref in self.cfg.get("favorites", []):
            if ref in self.by_ref:
                self._node(fav, rs_api.dest_path(self.by_ref, ref), ref, True)
        mine = self._node(self.tree, "My collections", style="bold")
        root = self._node(self.tree, "Folders", style="bold")
        folder_icon = QFileIconProvider().icon(QFileIconProvider.IconType.Folder)  # native per OS
        blank = QPixmap(16, 16)
        blank.fill(Qt.transparent)
        blank = QIcon(blank)  # keeps collection names aligned with folder names
        kids = {}
        for d in dests:
            if d["kind"] == "mine":
                self._node(mine, d["name"], d["ref"], True)
            else:
                kids.setdefault(d["parent"], []).append(d)

        def add(parent_item, parent_ref, depth=0):
            for d in sorted(kids.get(parent_ref, []), key=lambda d: d["name"].lower()):
                if d["category"]:
                    it = self._node(parent_item, d["name"], d["ref"],
                                    tip=f"{d['name']}: category, pick a collection inside it")
                    it.setIcon(0, folder_icon)
                    if depth < 10:
                        add(it, d["ref"], depth + 1)
                else:
                    self._node(parent_item, d["name"], d["ref"], True).setIcon(0, blank)

        add(root, None)
        for top in (fav, mine, root):
            top.setExpanded(True)
        self.tree.blockSignals(False)
        self.select(self.want)
        self.filter()

    def select(self, ref):
        self.want = ref
        if ref is False:
            return
        it_ = QTreeWidgetItemIterator(self.tree)
        while it := it_.value():
            in_fav = it.parent() and it.parent().text(0) == "Favorites"
            if self._pickable(it) and it.data(0, self.REF) == ref and not in_fav:
                p = it.parent()
                while p:
                    p.setExpanded(True)
                    p = p.parent()
                self.tree.setCurrentItem(it)
                self.tree.scrollToItem(it)
                return
            it_ += 1

    def ref(self):
        """Picked collection ref; None = "No collection"; False = nothing picked yet."""
        items = self.tree.selectedItems()
        return items[0].data(0, self.REF) if items else False

    def path(self, ref):
        return rs_api.dest_path(self.by_ref, ref)

    def _picked(self):
        r = self.ref()
        if r is not False:
            self.want = r
        self.changed.emit()

    def filter(self):
        text = self.search.text().strip().lower()

        def walk(it, shown_by_parent):
            hit = shown_by_parent or (bool(text) and text in it.text(0).lower())
            child_vis = [walk(it.child(i), hit) for i in range(it.childCount())]  # no short-circuit
            vis = not text or hit or any(child_vis)
            it.setHidden(not vis)
            if text and any(child_vis) and not shown_by_parent:
                it.setExpanded(True)
            return vis

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i), False)

    def menu(self, pos):
        it = self.tree.itemAt(pos)
        if not it or not self._pickable(it) or it.data(0, self.REF) is None:
            return
        ref, favs = it.data(0, self.REF), self.cfg.setdefault("favorites", [])
        m = QMenu(self)
        act = m.addAction("Remove from Favorites" if ref in favs else "Add to Favorites")
        if m.exec(self.tree.viewport().mapToGlobal(pos)) is act:
            favs.remove(ref) if ref in favs else favs.append(ref)
            save_config(self.cfg)
            self.set_items(self.dests)


class Bridge(QObject):  # worker threads -> UI thread
    item_done = Signal(str, str)
    all_done = Signal(str)
    dests_loaded = Signal(list)
    schema_loaded = Signal(int, object)  # restype, schema dict or error str


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GGGI ResourceSpace Uploader")
        self.cfg = load_config()
        self.cfg.setdefault("presets", {})
        self.client = None
        self.items = {}         # path -> {"path", "meta", "ref", "stage", "error"}
        self.fields, self.options, self.inputs = [], {}, {}
        try:
            self.cache = {int(k): v for k, v in json.loads(SCHEMA_CACHE.read_text()).items()}
        except (OSError, ValueError):
            self.cache = {}
        self.uploading = False
        self.bridge = Bridge()
        self.bridge.item_done.connect(self.on_item_done)
        self.bridge.all_done.connect(self.on_all_done)
        self.bridge.dests_loaded.connect(self.set_destinations)
        self.bridge.schema_loaded.connect(self.on_schema_loaded)
        self.setAcceptDrops(True)
        self._build()
        self.resize(1360, 800)

    # ---------- layout ----------
    def _build(self):
        top = QHBoxLayout()
        for label, fn in [("Settings", self.open_settings), ("Add files", self.add_files_dialog),
                          ("Add folder", self.add_folder_dialog), ("Remove selected", self.remove_selected),
                          ("Clear all", self.clear_all)]:
            b = QPushButton(label)
            b.clicked.connect(fn)
            top.addWidget(b)
        top.addStretch()
        self.conn = QLabel("Not connected")
        top.addWidget(self.conn)

        self.list = QListWidget(viewMode=QListView.IconMode, iconSize=QSize(120, 120), gridSize=QSize(150, 165),
                                resizeMode=QListView.Adjust, movement=QListView.Static, wordWrap=True,
                                selectionMode=QAbstractItemView.ExtendedSelection)
        self.list.setAcceptDrops(False)
        self.list.viewport().setAcceptDrops(False)
        self.list.itemSelectionChanged.connect(self.on_selection)
        hint = QLabel(f"Drag files or folders here.  {'Cmd' if sys.platform == 'darwin' else 'Ctrl'}/Shift-click to select several, "
                      "then fill the panel and press Apply.   🔴 missing required  🟢 ready  ✅ uploaded  ❌ failed")
        hint.setWordWrap(True)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addWidget(hint)
        ll.addWidget(self.list)

        self.restype = QComboBox()
        self.restype.currentIndexChanged.connect(self.load_fields)
        self.preset = QComboBox()
        self.preset.activated.connect(self.apply_preset)
        save_p, del_p = QPushButton("Save"), QPushButton("Delete")
        save_p.clicked.connect(self.save_preset)
        del_p.clicked.connect(self.delete_preset)
        prow = QHBoxLayout()
        prow.addWidget(self.preset, 1)
        prow.addWidget(save_p)
        prow.addWidget(del_p)
        self.sel_label = QLabel("Selected: 0")
        self.form_host = QWidget()
        self.form = QFormLayout(self.form_host)
        self.form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)  # macOS default keeps fields narrow
        scroll = QScrollArea(widgetResizable=True)
        scroll.setWidget(self.form_host)

        actions = QHBoxLayout()
        for label, fn in [("Apply to selected", lambda: self.apply_panel(self.selected_paths())),
                          ("Apply to all", lambda: self.apply_panel(list(self.items))),
                          ("Load from selected", self.load_from_selected),
                          ("Clear selected", self.clear_selected_meta), ("Reset panel", self.reset_panel)]:
            b = QPushButton(label)
            b.clicked.connect(fn)
            actions.addWidget(b)

        self.right = QWidget()
        rl = QVBoxLayout(self.right)
        head = QFormLayout()
        head.addRow("Resource type", self.restype)
        head.addRow("Preset", prow)
        rl.addLayout(head)
        rl.addWidget(self.sel_label)
        rl.addWidget(scroll, 1)
        rl.addLayout(actions)

        split = QSplitter()
        self.dest = DestinationTree(self.cfg, self.new_collection)
        self.dest.changed.connect(self.on_dest_changed)
        split.addWidget(self.dest)
        split.addWidget(left)
        split.addWidget(self.right)
        split.setSizes([300, 560, 480])

        self.dest_label = QLabel("Destination: pick a collection on the left")
        self.dest_label.setStyleSheet("font-weight: bold")
        self.archive = QComboBox()
        for v, label, _ in rs_api.ARCHIVE_STATES:
            self.archive.addItem(label, v)
        for i, (_, _, tip) in enumerate(rs_api.ARCHIVE_STATES):
            self.archive.setItemData(i, tip, Qt.ToolTipRole)
        self.archive.setToolTip("Workflow state the new resources get in ResourceSpace.")
        self.exif = QCheckBox("Read camera/file metadata")
        self.exif.setToolTip("On: ResourceSpace reads metadata stored inside the file (EXIF/IPTC: date taken, GPS,\n"
                             "caption, photographer) into matching fields. Values you set in the panel still win.\n"
                             "Off: embedded metadata is ignored.")
        self.go = QPushButton("Upload")
        self.go.setStyleSheet("font-weight: bold; padding: 6px 18px")
        self.go.clicked.connect(self.start_upload)
        self.progress = QProgressBar()
        bottom = QHBoxLayout()
        for w in (self.dest_label, QLabel("After upload:"), self.archive, self.exif, self.progress, self.go):
            bottom.addWidget(w)
        bottom.setStretchFactor(self.dest_label, 2)
        bottom.setStretchFactor(self.progress, 1)

        root = QWidget()
        lay = QVBoxLayout(root)
        lay.addLayout(top)
        lay.addWidget(split, 1)
        lay.addLayout(bottom)
        self.setCentralWidget(root)
        self.controls = [w for w in root.findChildren(QPushButton) if w is not self.go] + \
                        [self.restype, self.preset, self.dest, self.archive, self.exif, self.form_host]
        self.refresh_presets()

    # ---------- connection ----------
    def connect_rs(self):
        self.client = make_client(self.cfg)
        if not self.client:
            self.open_settings()
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            types = self.client.resource_types()
        except rs_api.RSError as e:
            self.conn.setText("Connection failed")
            QMessageBox.critical(self, "ResourceSpace", str(e))
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.conn.setText(f"Connected: {self.cfg['user']}")
        # folders: show the cached list now, refresh in the background (~15 s for ~600 folders)
        self.set_destinations(self.cfg.get("dest_tree", []), loading=True)
        client = self.client
        threading.Thread(target=lambda: self.bridge.dests_loaded.emit(
            self._safe(client.destinations)), daemon=True).start()
        last = self.cfg.get("restype")
        self.restype.blockSignals(True)
        self.restype.clear()
        for ref, name in types:
            self.restype.addItem(name, ref)
        self.restype.setCurrentIndex(max(self.restype.findData(last), 0))
        self.restype.blockSignals(False)
        self.load_fields()
        # fields: cached ones show instantly; refresh every type in the background, current one first
        order = sorted((r for r, _ in types), key=lambda r: r != self.restype.currentData())

        def prefetch():
            for rt in order:
                try:
                    self.bridge.schema_loaded.emit(rt, client.schema(rt))
                except rs_api.RSError as e:
                    self.bridge.schema_loaded.emit(rt, str(e))

        threading.Thread(target=prefetch, daemon=True).start()

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except rs_api.RSError:
            return None  # keep the cached list

    def set_destinations(self, dests, loading=False):
        if dests is None:
            self.dest.status.setText("Folder refresh failed; showing the cached list.")
            return
        if not loading:
            made = [d for d in self.dest.dests if d.get("new") and d["ref"] not in {x["ref"] for x in dests}]
            dests = dests + made  # keep collections created while the refresh ran
            self.cfg.pop("dest_cache", None)  # old flat format
            self.cfg["dest_tree"] = dests
            save_config(self.cfg)
        self.dest.set_items(dests)
        n = sum(1 for d in dests if d["kind"] == "folder")
        self.dest.status.setText("Loading folders..." if loading else
                                 f"{n} folders. Right-click a collection to add it to Favorites.")

    def on_dest_changed(self):
        r = self.dest.ref()
        self.dest_label.setText("Destination: " + ("pick a collection on the left" if r is False
                                                   else self.dest.path(r)))
        if r is not False:
            self.cfg["last_dest"] = r
            save_config(self.cfg)

    def open_settings(self):
        if SettingsDialog(self.cfg, self).exec():
            self.cache.clear()
            self.connect_rs()

    def on_schema_loaded(self, rt, data):
        current = rt == self.restype.currentData()
        if isinstance(data, str):  # error; keep any cached schema
            if current and rt not in self.cache:
                self.sel_label.setText(f"Could not load fields: {data}")
            return
        if self.cache.get(rt) == data:
            return
        self.cache[rt] = data
        APP_DIR.mkdir(exist_ok=True)
        SCHEMA_CACHE.write_text(json.dumps(self.cache, ensure_ascii=False))
        if current:
            self.load_fields(keep_panel=True)

    def load_fields(self, keep_panel=False):
        rt = self.restype.currentData()
        if rt is None:
            return
        self.cfg["restype"] = rt
        save_config(self.cfg)
        kept = self.panel_values() if keep_panel else {}
        while self.form.rowCount():
            self.form.removeRow(0)
        self.inputs = {}
        data = self.cache.get(rt)
        self.fields = data["fields"] if data else []
        self.options = data["options"] if data else {}
        self.sel_label.setText(f"Selected: {len(self.list.selectedItems())} of {len(self.items)}" if data
                               else f"Loading fields for {self.restype.currentText()}...")
        for f in self.fields:
            if f["twin_of"]:
                continue  # RS duplicate field: filled from its twin at upload
            inp = FieldInput(f, data["options"].get(str(f["ref"]), []))
            inp.changed.connect(self.update_visibility)
            self.inputs[f["ref"]] = inp
            self.form.addRow(f["title"] + (" *" if f["required"] else ""), inp)
            if f["ref"] in kept:
                inp.set_value(kept[f["ref"]])
        self.refresh_all()

    def update_visibility(self):
        """Show a conditional field (e.g. Project Number when Project related? = Yes) if the panel
        or any selected file meets its condition."""
        metas = [self.panel_values()] + [self.items[p]["meta"] for p in self.selected_paths()]
        for f in self.fields:
            if f["ref"] in self.inputs:
                self.form.setRowVisible(self.inputs[f["ref"]], any(
                    rs_api.shown(f, m, self.fields, self.options) for m in metas))

    # ---------- files ----------
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and not self.uploading:
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.add_paths([Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()])

    def add_files_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Add files")
        self.add_paths([Path(f) for f in files])

    def add_folder_dialog(self):
        d = QFileDialog.getExistingDirectory(self, "Add folder")
        if d:
            self.add_paths([Path(d)])

    def add_paths(self, paths):
        files = []
        for p in paths:
            files += sorted(x for x in p.rglob("*") if x.is_file()) if p.is_dir() else [p]
        for f in files:
            if f.name.startswith(".") or str(f) in self.items:
                continue
            self.items[str(f)] = {"path": str(f), "meta": {}}
            it = QListWidgetItem(thumbnail(f), f.name)
            it.setData(Qt.UserRole, str(f))
            self.list.addItem(it)
            self.refresh_item(it)
        self.on_selection()

    def selected_paths(self):
        return [it.data(Qt.UserRole) for it in self.list.selectedItems()]

    def remove_selected(self):
        for it in self.list.selectedItems():
            self.items.pop(it.data(Qt.UserRole), None)
            self.list.takeItem(self.list.row(it))

    def clear_all(self):
        self.items.clear()
        self.list.clear()
        self.on_selection()

    def on_selection(self):
        self.sel_label.setText(f"Selected: {len(self.list.selectedItems())} of {len(self.items)}")
        self.update_visibility()

    # ---------- metadata ----------
    def panel_values(self):
        return {ref: v for ref, inp in self.inputs.items() if (v := inp.value())}

    def apply_panel(self, paths):
        vals = self.panel_values()
        if not paths or not vals:
            QMessageBox.information(self, "Apply", "Select files and fill at least one field first.")
            return
        for p in paths:
            if self.items[p].get("stage") != rs_api.DONE:
                self.items[p]["meta"].update(vals)
        self.refresh_all()

    def load_from_selected(self):
        paths = self.selected_paths()
        if not paths:
            return
        for ref, inp in self.inputs.items():
            vals = [self.items[p]["meta"].get(ref) for p in paths]
            inp.set_value(vals[0] if all(v == vals[0] for v in vals) else None)  # blank where files differ

    def clear_selected_meta(self):
        for p in self.selected_paths():
            if self.items[p].get("stage") != rs_api.DONE:
                self.items[p]["meta"] = {}
        self.refresh_all()

    def reset_panel(self):
        for inp in self.inputs.values():
            inp.set_value(None)

    def refresh_item(self, it):
        d = self.items[it.data(Qt.UserRole)]
        name = Path(d["path"]).name
        if d.get("stage") == rs_api.DONE:
            badge, tip = "✅", f"Uploaded: ref {d['ref']}"
        elif d.get("error"):
            badge, tip = "❌", d["error"] + (f"\n(resource {d['ref']} exists; retry resumes it)" if d.get("ref") else "")
        else:
            missing = rs_api.missing_required(self.fields, d["meta"], d["path"], self.options)
            badge, tip = ("🔴", "Missing: " + ", ".join(missing)) if missing else ("🟢", "Ready")
        labels = {f["ref"]: f["title"] for f in self.fields}
        filled = [f"{labels[r]}: {v if isinstance(v, str) else len(v)}" for r, v in d["meta"].items() if r in labels]
        eff = rs_api.effective(self.fields, d["meta"], d["path"], self.options)
        names = {r: dict(map(tuple, o)) for r, o in self.options.items()}
        auto = [f"{f['title']}: {v if isinstance(v, str) else ', '.join(str(names.get(str(f['ref']), {}).get(i, i)) for i in v)} (auto)"
                for f in self.fields if not f["twin_of"] and not d["meta"].get(f["ref"]) and rs_api.auto_kind(f)
                and (v := eff.get(f["ref"]))]
        it.setText(f"{badge} {name}")
        it.setToolTip("\n".join([name, tip, *filled, *auto]))

    def refresh_all(self):
        for i in range(self.list.count()):
            self.refresh_item(self.list.item(i))
        self.update_visibility()

    # ---------- presets ----------
    def refresh_presets(self):
        self.preset.clear()
        self.preset.addItem("(choose preset)")
        self.preset.addItems(sorted(self.cfg["presets"]))

    def save_preset(self):
        vals = self.panel_values()
        if not vals:
            QMessageBox.information(self, "Preset", "Fill some fields in the panel first.")
            return
        name, ok = QInputDialog.getText(self, "Save preset", "Preset name:")
        if ok and name.strip():
            self.cfg["presets"][name.strip()] = {"restype": self.restype.currentData(),
                                                 "values": {str(k): v for k, v in vals.items()}}
            save_config(self.cfg)
            self.refresh_presets()
            self.preset.setCurrentText(name.strip())

    def apply_preset(self, index):
        p = self.cfg["presets"].get(self.preset.itemText(index))
        if not p:
            return
        if p["restype"] != self.restype.currentData():
            self.restype.setCurrentIndex(max(self.restype.findData(p["restype"]), 0))
        self.reset_panel()
        for ref, v in p["values"].items():
            if int(ref) in self.inputs:
                self.inputs[int(ref)].set_value(v)

    def delete_preset(self):
        name = self.preset.currentText()
        if name in self.cfg["presets"]:
            del self.cfg["presets"][name]
            save_config(self.cfg)
            self.refresh_presets()

    # ---------- destination ----------
    def new_collection(self):
        if not self.client:
            return
        name, ok = QInputDialog.getText(self, "New collection", "Collection name:")
        if not (ok and name.strip()):
            return
        try:
            ref = self.client.call("create_collection", name=name.strip())
            if not str(ref).isdigit():
                raise rs_api.RSError(f"create_collection returned {ref!r} (no permission?)")
        except rs_api.RSError as e:
            QMessageBox.critical(self, "New collection", str(e))
            return
        self.dest.dests.append({"ref": int(ref), "name": name.strip(), "parent": None, "kind": "mine",
                                "category": False, "new": True})
        self.dest.want = int(ref)
        self.dest.set_items(self.dest.dests)

    # ---------- upload ----------
    def start_upload(self):
        if not self.client or not self.fields:
            QMessageBox.warning(self, "Upload", "Fields are still loading. Try again in a moment." if self.client
                                else "Connect to ResourceSpace first (Settings).")
            return
        todo = [d for d in self.items.values() if d.get("stage") != rs_api.DONE and not d.get("duplicate")]
        if not todo:
            QMessageBox.information(self, "Upload", "Nothing to upload.")
            return
        missing = [d for d in todo if rs_api.missing_required(self.fields, d["meta"], d["path"], self.options)]
        if missing:
            QMessageBox.warning(self, "Upload", f"{len(missing)} file(s) are missing required fields (🔴). "
                                                "Hover a file to see which.")
            return
        dest = self.dest.ref()
        if dest is False:
            QMessageBox.warning(self, "Upload", "Pick a destination in the left panel (or No collection).")
            return
        msg = (f"Upload {len(todo)} file(s)?\n\nType: {self.restype.currentText()}\n"
               f"Destination: {self.dest.path(dest)}\nStatus: {self.archive.currentText()}")
        if QMessageBox.question(self, "Upload", msg) != QMessageBox.Yes:
            return
        self.uploading = True
        for w in self.controls:
            w.setEnabled(False)
        self.go.setEnabled(False)
        self.progress.setRange(0, len(todo))
        self.progress.setValue(0)
        args = (self.fields, self.options, self.restype.currentData(), self.archive.currentData(),
                dest, not self.exif.isChecked())
        threading.Thread(target=self.run_upload, args=(todo, args), daemon=True).start()

    def run_upload(self, todo, args):  # worker thread: no widget access here
        def one(d):
            d["error"] = ""
            try:
                rs_api.upload_item(self.client, d, *args)
            except Exception as e:  # report every failure per file, keep the batch going
                d["error"] = str(e) or type(e).__name__
            self.bridge.item_done.emit(d["path"], d["error"])
            return d

        with ThreadPoolExecutor(WORKERS) as ex:
            done = list(ex.map(one, todo))
        log = APP_DIR / "logs" / time.strftime("upload_%Y%m%d_%H%M%S.csv")
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["file", "path", "ref", "result", "error"])
            for d in done:
                w.writerow([Path(d["path"]).name, d["path"], d.get("ref", ""),
                            "ok" if d.get("stage") == rs_api.DONE else "failed", d["error"]])
        self.bridge.all_done.emit(str(log))

    def on_item_done(self, path, error):
        self.progress.setValue(self.progress.value() + 1)
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == path:
                self.refresh_item(self.list.item(i))
                break

    def on_all_done(self, log):
        self.uploading = False
        for w in self.controls:
            w.setEnabled(True)
        self.go.setEnabled(True)
        dup = sum(1 for d in self.items.values() if d.get("duplicate"))
        failed = sum(1 for d in self.items.values() if d.get("error") and not d.get("duplicate"))
        text = f"Done. {failed} failed; press Upload again to retry them." if failed else "All uploaded."
        if dup:
            text += f"\n{dup} file(s) were already in ResourceSpace and were skipped."
        QMessageBox.information(self, "Upload", f"{text}\n\nLog: {log}")

    def closeEvent(self, e):
        if self.uploading and QMessageBox.question(
                self, "Quit", "Upload in progress. Quit anyway? Unfinished files will be incomplete.") \
                != QMessageBox.Yes:
            e.ignore()
            return
        e.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "icon.png")))
    win = MainWindow()
    win.show()
    win.connect_rs()
    sys.exit(app.exec())

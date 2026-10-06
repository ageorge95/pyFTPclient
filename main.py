import sys
import os
import json
import html
import time
import ftplib
import posixpath
import threading
import itertools
from datetime import datetime
from PySide6.QtCore import (Qt,
                            Signal,
                            Slot,
                            QObject,
                            QThread,
                            QDir,
                            QMimeData,
                            QModelIndex,
                            QByteArray)
from PySide6.QtGui import (QIcon,
                           QFontDatabase,
                           QAction,
                           QKeySequence,
                           QShortcut)
from PySide6.QtWidgets import (QApplication,
                               QWidget,
                               QLabel,
                               QMainWindow,
                               QPushButton,
                               QToolButton,
                               QMessageBox,
                               QHBoxLayout,
                               QVBoxLayout,
                               QGridLayout,
                               QGroupBox,
                               QLineEdit,
                               QSpinBox,
                               QDoubleSpinBox,
                               QCheckBox,
                               QComboBox,
                               QRadioButton,
                               QButtonGroup,
                               QPlainTextEdit,
                               QProgressBar,
                               QSplitter,
                               QTabWidget,
                               QTreeView,
                               QTreeWidget,
                               QTreeWidgetItem,
                               QFileSystemModel,
                               QAbstractItemView,
                               QHeaderView,
                               QFileDialog,
                               QInputDialog,
                               QMenu,
                               QStyle)
from ftp_core import (ConnectionSettings,
                      TransferOptions,
                      TransferEngine,
                      connect,
                      safe_close,
                      list_dir,
                      remote_abspath,
                      remote_rmtree,
                      remote_join,
                      remote_disk_usage,
                      server_supports_df,
                      human_size,
                      human_gb,
                      human_time,
                      MODE_COPY,
                      MODE_MOVE,
                      EXISTS_RESUME,
                      EXISTS_OVERWRITE,
                      EXISTS_SKIP,
                      DIRECTION_UPLOAD,
                      DIRECTION_DOWNLOAD)

SETTINGS_FILE = os.path.abspath('settings.json')
REMOTE_MIME = 'application/x-pyftpclient-remote-items'

LOG_COLORS = {'info': '#d4d4d4',
              'success': '#6a9955',
              'warning': '#dcdcaa',
              'error': '#f44747',
              'progress': '#569cd6'}
LOG_TAGS = {'warning': 'WARN ', 'error': 'ERROR', 'success': 'OK   ', 'progress': 'PROG '}

_session_ids = itertools.count(1)


def get_running_path(relative_path):
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_path)


def read_version():
    try:
        with open(get_running_path('version.txt'), 'r') as f:
            return f.read().strip()
    except OSError:
        return '?'


def load_settings():
    try:
        with open(SETTINGS_FILE, 'r') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(data):
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        print(f'Could not save settings: {e}')


def decode_remote_mime(mime):
    if not mime.hasFormat(REMOTE_MIME):
        return None
    try:
        return json.loads(bytes(mime.data(REMOTE_MIME)).decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return None


class RemoteBrowserWorker(QObject):
    """Owns the browsing FTP connection of one session; lives in its own thread so the GUI never blocks."""
    connected = Signal(str)
    disconnected = Signal()
    listed = Signal(str, object)
    failed = Signal(str)
    log = Signal(str, str)
    busy = Signal(bool)
    disk_usage = Signal(str, object)
    drives = Signal(object)

    def __init__(self):
        super().__init__()
        self.ftp = None
        self.settings = None
        self.df_supported = False

    def _drop(self):
        if self.ftp is not None:
            try:
                self.ftp.close()
            except Exception:
                pass
        self.ftp = None

    def _call(self, description, func):
        self.busy.emit(True)
        try:
            for attempt in range(2):
                try:
                    if self.ftp is None:
                        if self.settings is None:
                            raise RuntimeError('Not connected')
                        self.ftp = connect(self.settings)
                    return func(self.ftp)
                except ftplib.error_perm:
                    raise
                except ftplib.all_errors as e:
                    self._drop()
                    if attempt == 1:
                        raise
                    self.log.emit(f'{description}: connection problem ({e!r}), reconnecting ...', 'warning')
        finally:
            self.busy.emit(False)

    def _list(self, path):
        def do(ftp):
            abs_path = remote_abspath(ftp, path)
            return abs_path, list_dir(ftp, abs_path)
        abs_path, entries = self._call(f'Listing {path}', do)
        self.listed.emit(abs_path, entries)
        self._emit_disk_usage(abs_path)

    def _emit_disk_usage(self, path):
        usage = None
        if self.df_supported:
            try:
                usage = self._call('Disk usage', lambda ftp: remote_disk_usage(ftp, path))
            except Exception as e:
                self.log.emit(f'Cannot read disk usage of {path}: {e}', 'warning')
        self.disk_usage.emit(path, usage)

    @Slot()
    def refresh_drives(self):
        if not self.df_supported or self.settings is None:
            self.drives.emit(None)
            return

        def do(ftp):
            return [(remote_join('/', e.name), remote_disk_usage(ftp, remote_join('/', e.name)))
                    for e in list_dir(ftp, '/') if e.is_dir]
        try:
            self.drives.emit(self._call('Drive usage', do))
        except Exception as e:
            self.log.emit(f'Cannot read drive usage: {e}', 'warning')
            self.drives.emit(None)

    @Slot(object, str)
    def connect_to(self, settings, start_path):
        safe_close(self.ftp)
        self.ftp = None
        self.settings = settings
        self.busy.emit(True)
        try:
            self.log.emit(f'Connecting to {settings.host}:{settings.port} '
                          f'({"FTPS" if settings.use_tls else "FTP"}, '
                          f'{"passive" if settings.passive else "active"}) ...', 'info')
            self.ftp = connect(settings)
            welcome = (self.ftp.getwelcome() or '').strip()
            if welcome:
                self.log.emit(welcome, 'info')
            cwd = self.ftp.pwd()
            self.df_supported = server_supports_df(self.ftp)
            if not self.df_supported:
                self.log.emit('Server does not support SITE DF: disk space info unavailable', 'info')
            self.log.emit(f'Connected. Remote working directory: {cwd}', 'success')
            self.connected.emit(cwd)
        except Exception as e:
            self.settings = None
            self.df_supported = False
            self._drop()
            self.failed.emit(f'Connection failed: {e}')
            return
        finally:
            self.busy.emit(False)
        try:
            self._list(start_path or cwd)
        except Exception as e:
            self.log.emit(f'Cannot open {start_path}: {e}; falling back to {cwd}', 'warning')
            self.list_path(cwd)

    @Slot()
    def disconnect_from(self):
        safe_close(self.ftp)
        self.ftp = None
        self.settings = None
        self.df_supported = False
        self.disconnected.emit()

    @Slot(str)
    def list_path(self, path):
        try:
            self._list(path)
        except Exception as e:
            self.failed.emit(f'Cannot list {path}: {e}')

    @Slot(str, str)
    def make_dir(self, path, refresh_path):
        try:
            self._call('mkdir', lambda ftp: ftp.mkd(path))
            self.log.emit(f'Created remote folder {path}', 'success')
        except Exception as e:
            self.failed.emit(f'Cannot create folder {path}: {e}')
        self.list_path(refresh_path)

    @Slot(str, str, str)
    def rename(self, src, dst, refresh_path):
        try:
            self._call('rename', lambda ftp: ftp.rename(src, dst))
            self.log.emit(f'Renamed {src} -> {dst}', 'success')
        except Exception as e:
            self.failed.emit(f'Cannot rename {src}: {e}')
        self.list_path(refresh_path)

    @Slot(object, str)
    def delete(self, items, refresh_path):
        for path, is_dir in items:
            try:
                if is_dir:
                    self._call('delete', lambda ftp, p=path: remote_rmtree(ftp, p, self.log.emit))
                else:
                    self._call('delete', lambda ftp, p=path: ftp.delete(p))
                    self.log.emit(f'Deleted remote file {path}', 'info')
            except Exception as e:
                self.failed.emit(f'Cannot delete {path}: {e}')
        self.list_path(refresh_path)

    def shutdown(self):
        safe_close(self.ftp)
        self.ftp = None


class TransferWorker(QThread):
    log = Signal(str, str)
    progress = Signal(object)
    done = Signal(object)

    def __init__(self, settings, options, direction, paths, destination):
        super().__init__()
        self.settings = settings
        self.options = options
        self.direction = direction
        self.paths = paths
        self.destination = destination
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        engine = TransferEngine(self.settings, self.options, self.log.emit, self.progress.emit, self.cancel_event)
        self.done.emit(engine.run(self.direction, self.paths, self.destination))


class LocalTree(QTreeView):
    """Local browser; accepts remote items dragged from the remote browser of the same session (=> download)."""
    remote_dropped = Signal(object, str)

    def __init__(self, session_id):
        super().__init__()
        self.session_id = session_id
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)

    def _payload(self, event):
        data = decode_remote_mime(event.mimeData())
        return data if data and data.get('session') == self.session_id else None

    def dragEnterEvent(self, event):
        if self._payload(event):
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        data = self._payload(event)
        if not data:
            event.ignore()
            return
        index = self.indexAt(event.position().toPoint())
        target = self.model().filePath(index) if index.isValid() and self.model().isDir(index) else ''
        event.setDropAction(Qt.CopyAction)
        event.accept()
        self.remote_dropped.emit(data['paths'], target)


class RemoteTree(QTreeWidget):
    """Remote browser; accepts local files/folders from the local browser or the OS file manager (=> upload)."""
    local_dropped = Signal(object, str)

    def __init__(self, session_id):
        super().__init__()
        self.session_id = session_id
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)

    def mimeTypes(self):
        return [REMOTE_MIME]

    def mimeData(self, items):
        paths = [[it.data(0, Qt.UserRole), bool(it.data(0, Qt.UserRole + 1))]
                 for it in items if it.data(0, Qt.UserRole + 2) != '..']
        mime = QMimeData()
        mime.setData(REMOTE_MIME, QByteArray(json.dumps({'session': self.session_id,
                                                         'paths': paths}).encode('utf-8')))
        return mime

    def supportedDropActions(self):
        return Qt.CopyAction

    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if mime.hasUrls() and any(u.isLocalFile() for u in mime.urls()):
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        mime = event.mimeData()
        paths = [QDir.toNativeSeparators(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()]
        if not paths:
            event.ignore()
            return
        item = self.itemAt(event.position().toPoint())
        target = item.data(0, Qt.UserRole) if item is not None and item.data(0, Qt.UserRole + 1) else ''
        event.setDropAction(Qt.CopyAction)
        event.accept()
        self.local_dropped.emit(paths, target)


class SessionTab(QWidget):
    """One self-contained session: connection, local + remote browsers, transfer queue, progress and console."""
    title_changed = Signal()

    req_connect = Signal(object, str)
    req_disconnect = Signal()
    req_list = Signal(str)
    req_mkdir = Signal(str, str)
    req_rename = Signal(str, str, str)
    req_delete = Signal(object, str)
    req_drives = Signal()

    def __init__(self, settings=None):
        super().__init__()
        self.session_id = next(_session_ids)
        self.custom_name = ''
        self.remote_cwd = ''
        self.remote_connected = False
        self.status_suffix = ''
        self.transfer_worker = None
        self.queue = []
        self._last_console_progress = 0.0

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_connection_box())

        vertical = QSplitter(Qt.Vertical)
        browsers = QSplitter(Qt.Horizontal)
        browsers.addWidget(self._build_local_browser())
        browsers.addWidget(self._build_remote_browser())
        browsers.setSizes([600, 600])
        vertical.addWidget(browsers)
        vertical.addWidget(self._build_transfer_panel())
        vertical.addWidget(self._build_console_box())
        vertical.setStretchFactor(0, 4)
        vertical.setStretchFactor(1, 0)
        vertical.setStretchFactor(2, 2)
        layout.addWidget(vertical)

        self._setup_browser_thread()
        self._set_remote_state(False)
        self.apply_settings(settings or {})

    def _build_connection_box(self):
        box = QGroupBox('Connection')
        layout = QHBoxLayout(box)

        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText('ftp.example.com')
        self.host_edit.textChanged.connect(lambda _text: self.title_changed.emit())
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(21)
        self.user_edit = QLineEdit()
        self.user_edit.setPlaceholderText('anonymous')
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.Password)
        self.remember_pass_check = QCheckBox('Remember')
        self.remember_pass_check.setToolTip('Stores the password in plain text in settings.json')
        self.tls_check = QCheckBox('FTPS (explicit TLS)')
        self.passive_check = QCheckBox('Passive')
        self.passive_check.setChecked(True)
        self.encoding_combo = QComboBox()
        self.encoding_combo.addItems(['utf-8', 'latin-1', 'cp1252'])
        self.encoding_combo.setEditable(True)
        self.encoding_combo.setToolTip('Encoding used for file names on the server')
        self.connect_button = QPushButton('Connect')
        self.connect_button.clicked.connect(self.toggle_connection)
        self.host_edit.returnPressed.connect(self.toggle_connection)
        self.pass_edit.returnPressed.connect(self.toggle_connection)

        for label, widget, stretch in (('Host:', self.host_edit, 3), ('Port:', self.port_spin, 0),
                                       ('User:', self.user_edit, 2), ('Password:', self.pass_edit, 2)):
            layout.addWidget(QLabel(label))
            layout.addWidget(widget, stretch)
        layout.addWidget(self.remember_pass_check)
        layout.addWidget(self.tls_check)
        layout.addWidget(self.passive_check)
        layout.addWidget(QLabel('Encoding:'))
        layout.addWidget(self.encoding_combo)
        layout.addWidget(self.connect_button)
        return box

    def _icon_button(self, icon, tooltip, slot):
        button = QPushButton()
        button.setIcon(self.style().standardIcon(icon))
        button.setToolTip(tooltip)
        button.clicked.connect(slot)
        return button

    def _build_local_browser(self):
        box = QGroupBox('Local')
        layout = QVBoxLayout(box)

        nav = QHBoxLayout()
        self.local_path_edit = QLineEdit()
        self.local_path_edit.returnPressed.connect(lambda: self.set_local_dir(self.local_path_edit.text()))
        nav.addWidget(self._icon_button(QStyle.SP_FileDialogToParent, 'Parent folder', self.local_up))
        nav.addWidget(self._icon_button(QStyle.SP_DirHomeIcon, 'Home folder',
                                        lambda: self.set_local_dir(os.path.expanduser('~'))))
        nav.addWidget(self.local_path_edit)
        nav.addWidget(self._icon_button(QStyle.SP_DirOpenIcon, 'Pick folder', self.pick_local_dir))
        layout.addLayout(nav)

        self.local_model = QFileSystemModel()
        self.local_model.setRootPath('')
        self.local_model.setFilter(QDir.AllEntries | QDir.NoDotAndDotDot | QDir.Hidden | QDir.System)
        self.local_view = LocalTree(self.session_id)
        self.local_view.setModel(self.local_model)
        self.local_view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.local_view.setSortingEnabled(True)
        self.local_view.sortByColumn(0, Qt.AscendingOrder)
        self.local_view.setItemsExpandable(False)
        self.local_view.setRootIsDecorated(False)
        self.local_view.hideColumn(2)
        self.local_view.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.local_view.header().setStretchLastSection(False)
        self.local_view.doubleClicked.connect(self.local_double_clicked)
        self.local_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.local_view.customContextMenuRequested.connect(self.local_context_menu)
        self.local_view.remote_dropped.connect(self.on_remote_dropped)
        layout.addWidget(self.local_view)

        upload_button = QPushButton('Upload selected  \u2192')
        upload_button.clicked.connect(lambda: self.upload_selection())
        layout.addWidget(upload_button)
        return box

    def _build_remote_browser(self):
        box = QGroupBox('Remote')
        layout = QVBoxLayout(box)

        nav = QHBoxLayout()
        self.remote_up_button = self._icon_button(QStyle.SP_FileDialogToParent, 'Parent folder', self.remote_up)
        self.remote_refresh_button = self._icon_button(QStyle.SP_BrowserReload, 'Refresh (F5)', self.remote_refresh)
        self.remote_mkdir_button = self._icon_button(QStyle.SP_FileDialogNewFolder, 'New folder', self.remote_mkdir)
        self.remote_path_edit = QLineEdit()
        self.remote_path_edit.returnPressed.connect(lambda: self.request_remote_list(self.remote_path_edit.text()))
        self.remote_busy_label = QLabel('')
        for w in (self.remote_up_button, self.remote_refresh_button, self.remote_mkdir_button,
                  self.remote_path_edit, self.remote_busy_label):
            nav.addWidget(w)
        layout.addLayout(nav)

        self.remote_tree = RemoteTree(self.session_id)
        self.remote_tree.setHeaderLabels(['Name', 'Size', 'Modified'])
        self.remote_tree.setRootIsDecorated(False)
        self.remote_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.remote_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.remote_tree.header().setStretchLastSection(False)
        self.remote_tree.itemDoubleClicked.connect(self.remote_double_clicked)
        self.remote_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.remote_tree.customContextMenuRequested.connect(self.remote_context_menu)
        self.remote_tree.local_dropped.connect(self.on_local_dropped)
        layout.addWidget(self.remote_tree)

        self.remote_disk_bar = self._make_disk_bar()
        self.remote_disk_bar.setToolTip('Disk space of the drive holding the current remote folder')
        layout.addWidget(self.remote_disk_bar)

        self.drives_box = QGroupBox('Drives (root folders)')
        self.drives_box.setCheckable(True)
        self.drives_box.setChecked(True)
        self.drives_container = QWidget()
        self.drives_layout = QGridLayout(self.drives_container)
        self.drives_layout.setContentsMargins(0, 0, 0, 0)
        self.drives_layout.setColumnStretch(1, 1)
        drives_box_layout = QVBoxLayout(self.drives_box)
        drives_box_layout.addWidget(self.drives_container)
        self.drives_box.toggled.connect(self.drives_container.setVisible)
        layout.addWidget(self.drives_box)

        self.remote_download_button = QPushButton('\u2190  Download selected')
        self.remote_download_button.clicked.connect(lambda: self.download_selection())
        layout.addWidget(self.remote_download_button)
        return box

    def _make_disk_bar(self):
        bar = QProgressBar()
        bar.setRange(0, 1000)
        bar.setTextVisible(True)
        self._set_disk_bar(bar, None, 'Disk space: -')
        return bar

    @staticmethod
    def _set_disk_bar(bar, usage, empty_text='Disk space: n/a', prefix=''):
        if usage is None or usage.total <= 0:
            bar.setValue(0)
            bar.setFormat(empty_text)
            bar.setStyleSheet('')
            return
        ratio = usage.used / usage.total
        color = '#d9534f' if ratio >= 0.9 else ('#f0ad4e' if ratio >= 0.75 else '#5cb85c')
        bar.setValue(int(min(1.0, ratio) * 1000))
        bar.setFormat(f'{prefix}{human_gb(usage.used)} / {human_gb(usage.total)} GB used ({ratio * 100:.1f}%)'
                      f'  |  {human_gb(usage.free)} GB free')
        bar.setStyleSheet(f'QProgressBar {{ text-align: center; }} '
                          f'QProgressBar::chunk {{ background-color: {color}; }}')

    def _build_transfer_panel(self):
        panel = QWidget()
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        options_box = QGroupBox('Options')
        grid = QGridLayout(options_box)
        self.copy_radio = QRadioButton('Copy')
        self.move_radio = QRadioButton('Move')
        self.copy_radio.setChecked(True)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.copy_radio)
        self.mode_group.addButton(self.move_radio)
        mode_layout = QHBoxLayout()
        mode_layout.addWidget(self.copy_radio)
        mode_layout.addWidget(self.move_radio)
        mode_layout.addStretch()

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(1, 3600)
        self.timeout_spin.setValue(30)
        self.timeout_spin.setSuffix(' s')
        self.timeout_spin.setToolTip('Socket timeout for connect / read / write operations')
        self.retries_spin = QSpinBox()
        self.retries_spin.setRange(0, 1000)
        self.retries_spin.setValue(3)
        self.retries_spin.setToolTip('How many times a failed file is retried (with resume when possible)')
        self.retry_delay_spin = QDoubleSpinBox()
        self.retry_delay_spin.setRange(0, 3600)
        self.retry_delay_spin.setValue(5)
        self.retry_delay_spin.setSuffix(' s')
        self.exists_combo = QComboBox()
        self.exists_combo.addItem('Resume partial / skip identical', EXISTS_RESUME)
        self.exists_combo.addItem('Overwrite', EXISTS_OVERWRITE)
        self.exists_combo.addItem('Skip', EXISTS_SKIP)
        self.console_interval_spin = QDoubleSpinBox()
        self.console_interval_spin.setRange(0.5, 600)
        self.console_interval_spin.setValue(2)
        self.console_interval_spin.setSuffix(' s')
        self.console_interval_spin.setToolTip('How often progress lines are printed in the console')
        self.verify_check = QCheckBox('Verify size after transfer')
        self.verify_check.setChecked(True)

        cells = (('Mode:', mode_layout), ('If target exists:', self.exists_combo),
                 ('Timeout:', self.timeout_spin), ('Retries:', self.retries_spin),
                 ('Retry delay:', self.retry_delay_spin), ('Console progress every:', self.console_interval_spin))
        for i, (label, widget) in enumerate(cells):
            row, col = divmod(i, 2)
            grid.addWidget(QLabel(label), row, col * 2)
            if isinstance(widget, QHBoxLayout):
                grid.addLayout(widget, row, col * 2 + 1)
            else:
                grid.addWidget(widget, row, col * 2 + 1)
        grid.addWidget(self.verify_check, 3, 0, 1, 4)
        layout.addWidget(options_box, 1)

        progress_box = QGroupBox('Progress')
        progress_layout = QVBoxLayout(progress_box)
        self.file_progress = QProgressBar()
        self.file_progress.setRange(0, 1000)
        self.file_progress.setFormat('File: -')
        self.total_progress = QProgressBar()
        self.total_progress.setRange(0, 1000)
        self.total_progress.setFormat('Total: -')
        self.stats_label = QLabel('Idle')
        self.stats_label.setWordWrap(True)
        bottom = QHBoxLayout()
        self.queue_label = QLabel('Queue: 0')
        self.cancel_button = QPushButton('Cancel')
        self.cancel_button.setToolTip('Cancel the running transfer and clear the queue of this tab')
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_transfer)
        bottom.addWidget(self.queue_label)
        bottom.addStretch()
        bottom.addWidget(self.cancel_button)
        progress_layout.addWidget(self.file_progress)
        progress_layout.addWidget(self.total_progress)
        progress_layout.addWidget(self.stats_label)
        progress_layout.addLayout(bottom)
        layout.addWidget(progress_box, 1)
        return panel

    def _build_console_box(self):
        box = QGroupBox('Console')
        layout = QVBoxLayout(box)
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(20000)
        self.console.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.console.setStyleSheet('QPlainTextEdit { background-color: #1e1e1e; color: #d4d4d4; }')
        layout.addWidget(self.console)
        buttons = QHBoxLayout()
        buttons.addStretch()
        save_button = QPushButton('Save log...')
        save_button.clicked.connect(self.save_log)
        clear_button = QPushButton('Clear console')
        clear_button.clicked.connect(self.console.clear)
        buttons.addWidget(save_button)
        buttons.addWidget(clear_button)
        layout.addLayout(buttons)
        return box

    def _setup_browser_thread(self):
        self.browser_thread = QThread(self)
        self.browser_worker = RemoteBrowserWorker()
        self.browser_worker.moveToThread(self.browser_thread)
        self.req_connect.connect(self.browser_worker.connect_to)
        self.req_disconnect.connect(self.browser_worker.disconnect_from)
        self.req_list.connect(self.browser_worker.list_path)
        self.req_mkdir.connect(self.browser_worker.make_dir)
        self.req_rename.connect(self.browser_worker.rename)
        self.req_delete.connect(self.browser_worker.delete)
        self.req_drives.connect(self.browser_worker.refresh_drives)
        self.browser_worker.disk_usage.connect(self.on_disk_usage)
        self.browser_worker.drives.connect(self.on_drives)
        self.browser_worker.connected.connect(self.on_remote_connected)
        self.browser_worker.disconnected.connect(lambda: self._set_remote_state(False))
        self.browser_worker.listed.connect(self.on_remote_listed)
        self.browser_worker.failed.connect(self.on_remote_failed)
        self.browser_worker.log.connect(self.log)
        self.browser_worker.busy.connect(lambda b: self.remote_busy_label.setText('working...' if b else ''))
        self.browser_thread.start()

    def title(self):
        return (self.custom_name or self.host_edit.text().strip() or 'New session') + self.status_suffix

    def is_busy(self):
        return self.transfer_worker is not None or bool(self.queue)

    def apply_settings(self, s):
        self.custom_name = s.get('tab_name', '')
        self.host_edit.setText(s.get('host', ''))
        self.port_spin.setValue(int(s.get('port', 21)))
        self.user_edit.setText(s.get('user', ''))
        self.remember_pass_check.setChecked(bool(s.get('remember_password', False)))
        self.pass_edit.setText(s.get('password', ''))
        self.tls_check.setChecked(bool(s.get('use_tls', False)))
        self.passive_check.setChecked(bool(s.get('passive', True)))
        self.encoding_combo.setCurrentText(s.get('encoding', 'utf-8'))
        self.timeout_spin.setValue(int(s.get('timeout', 30)))
        self.retries_spin.setValue(int(s.get('retries', 3)))
        self.retry_delay_spin.setValue(float(s.get('retry_delay', 5)))
        self.console_interval_spin.setValue(float(s.get('console_interval', 2)))
        self.verify_check.setChecked(bool(s.get('verify_size', True)))
        (self.move_radio if s.get('mode') == MODE_MOVE else self.copy_radio).setChecked(True)
        idx = self.exists_combo.findData(s.get('exists_policy'))
        if idx >= 0:
            self.exists_combo.setCurrentIndex(idx)
        self.remote_cwd = s.get('remote_dir', '')
        local_dir = s.get('local_dir') or os.path.expanduser('~')
        self.set_local_dir(local_dir if os.path.isdir(local_dir) else os.path.expanduser('~'))
        self.title_changed.emit()

    def collect_settings(self, include_password=False):
        data = {'tab_name': self.custom_name,
                'host': self.host_edit.text().strip(),
                'port': self.port_spin.value(),
                'user': self.user_edit.text(),
                'remember_password': self.remember_pass_check.isChecked(),
                'use_tls': self.tls_check.isChecked(),
                'passive': self.passive_check.isChecked(),
                'encoding': self.encoding_combo.currentText(),
                'timeout': self.timeout_spin.value(),
                'retries': self.retries_spin.value(),
                'retry_delay': self.retry_delay_spin.value(),
                'console_interval': self.console_interval_spin.value(),
                'verify_size': self.verify_check.isChecked(),
                'mode': MODE_MOVE if self.move_radio.isChecked() else MODE_COPY,
                'exists_policy': self.exists_combo.currentData(),
                'local_dir': self.local_path_edit.text(),
                'remote_dir': self.remote_cwd}
        if include_password or data['remember_password']:
            data['password'] = self.pass_edit.text()
        return data

    def connection_settings(self):
        return ConnectionSettings(host=self.host_edit.text().strip(),
                                  port=self.port_spin.value(),
                                  user=self.user_edit.text(),
                                  password=self.pass_edit.text(),
                                  use_tls=self.tls_check.isChecked(),
                                  passive=self.passive_check.isChecked(),
                                  timeout=float(self.timeout_spin.value()),
                                  encoding=self.encoding_combo.currentText() or 'utf-8')

    def transfer_options(self, mode=None):
        return TransferOptions(retries=self.retries_spin.value(),
                               retry_delay=self.retry_delay_spin.value(),
                               mode=mode or (MODE_MOVE if self.move_radio.isChecked() else MODE_COPY),
                               exists_policy=self.exists_combo.currentData(),
                               verify_size=self.verify_check.isChecked())

    @Slot(str, str)
    def log(self, message, level='info'):
        color = LOG_COLORS.get(level, LOG_COLORS['info'])
        stamp = datetime.now().strftime('%H:%M:%S')
        safe = html.escape(message).replace('\n', '<br>')
        self.console.appendHtml(f'<span style="color:#808080">[{stamp}]</span> '
                                f'<span style="color:{color}">{LOG_TAGS.get(level, "INFO ")} {safe}</span>')
        scrollbar = self.console.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def save_log(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Save log',
                                              f'pyFTPclient_{datetime.now():%Y%m%d_%H%M%S}.log',
                                              'Log files (*.log *.txt);;All files (*)')
        if path:
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(self.console.toPlainText())
                self.log(f'Log saved to {path}', 'success')
            except OSError as e:
                self.log(f'Cannot save log: {e}', 'error')

    def toggle_connection(self):
        if self.remote_connected:
            self.req_disconnect.emit()
            self.log('Disconnected', 'info')
            return
        self.connect_remote()

    def connect_remote(self):
        settings = self.connection_settings()
        if not settings.host:
            QMessageBox.warning(self, 'Missing host', 'Please enter the FTP host.')
            return
        self.connect_button.setEnabled(False)
        self.req_connect.emit(settings, self.remote_cwd)

    def _set_remote_state(self, connected):
        self.remote_connected = connected
        self.connect_button.setEnabled(True)
        self.connect_button.setText('Disconnect' if connected else 'Connect')
        for w in (self.remote_up_button, self.remote_refresh_button, self.remote_mkdir_button,
                  self.remote_path_edit, self.remote_download_button):
            w.setEnabled(connected)
        for w in (self.host_edit, self.port_spin, self.user_edit, self.pass_edit, self.tls_check,
                  self.passive_check, self.encoding_combo):
            w.setEnabled(not connected)
        if not connected:
            self.remote_tree.clear()
            self._set_disk_bar(self.remote_disk_bar, None, 'Disk space: -')
            self.on_drives(None)
        self.title_changed.emit()

    @Slot(str)
    def on_remote_connected(self, cwd):
        self._set_remote_state(True)
        if not self.remote_cwd:
            self.remote_cwd = cwd
        self.req_drives.emit()

    @Slot(str, object)
    def on_disk_usage(self, path, usage):
        if path != self.remote_cwd:
            return
        if not self.browser_worker.df_supported:
            empty = 'Disk space: not supported by this server (no SITE DF)'
        else:
            empty = f'Disk space: not reported for {path}'
        self._set_disk_bar(self.remote_disk_bar, usage, empty, prefix=f'{path}:  ')

    @Slot(object)
    def on_drives(self, drives):
        while self.drives_layout.count():
            widget = self.drives_layout.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        if not drives:
            self.drives_box.setVisible(False)
            return
        for row, (path, usage) in enumerate(drives):
            button = QToolButton()
            button.setText(posixpath.basename(path) or path)
            button.setAutoRaise(True)
            button.setToolTip(f'Open {path}')
            button.clicked.connect(lambda _checked=False, p=path: self.request_remote_list(p))
            bar = self._make_disk_bar()
            self._set_disk_bar(bar, usage, 'not reported')
            self.drives_layout.addWidget(button, row, 0)
            self.drives_layout.addWidget(bar, row, 1)
        self.drives_box.setVisible(True)

    @Slot(str)
    def on_remote_failed(self, message):
        self.log(message, 'error')
        if not self.remote_connected:
            self._set_remote_state(False)

    @Slot(str, object)
    def on_remote_listed(self, path, entries):
        self.remote_cwd = path
        self.remote_path_edit.setText(path)
        self.remote_tree.clear()
        style = self.style()
        dir_icon = style.standardIcon(QStyle.SP_DirIcon)
        file_icon = style.standardIcon(QStyle.SP_FileIcon)
        link_icon = style.standardIcon(QStyle.SP_FileLinkIcon)
        if path != '/':
            up = QTreeWidgetItem(['..', '', ''])
            up.setIcon(0, style.standardIcon(QStyle.SP_FileDialogToParent))
            up.setData(0, Qt.UserRole, posixpath.dirname(path.rstrip('/')) or '/')
            up.setData(0, Qt.UserRole + 1, True)
            up.setData(0, Qt.UserRole + 2, '..')
            self.remote_tree.addTopLevelItem(up)
        for entry in entries:
            item = QTreeWidgetItem([entry.name, '' if entry.is_dir else human_size(entry.size), entry.modified])
            item.setIcon(0, dir_icon if entry.is_dir else (link_icon if entry.is_link else file_icon))
            item.setData(0, Qt.UserRole, posixpath.join(path, entry.name))
            item.setData(0, Qt.UserRole + 1, entry.is_dir)
            item.setData(0, Qt.UserRole + 2, entry.name)
            item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            self.remote_tree.addTopLevelItem(item)
        self.remote_tree.resizeColumnToContents(1)
        self.remote_tree.resizeColumnToContents(2)

    def request_remote_list(self, path):
        if self.remote_connected:
            self.req_list.emit(path.strip() or '/')

    def remote_up(self):
        if self.remote_cwd and self.remote_cwd != '/':
            self.request_remote_list(posixpath.dirname(self.remote_cwd.rstrip('/')) or '/')

    def remote_refresh(self):
        self.request_remote_list(self.remote_cwd or '/')
        if self.remote_connected:
            self.req_drives.emit()

    def remote_double_clicked(self, item, _column):
        if item.data(0, Qt.UserRole + 1):
            self.request_remote_list(item.data(0, Qt.UserRole))

    def selected_remote_items(self):
        return [(it.data(0, Qt.UserRole), bool(it.data(0, Qt.UserRole + 1)))
                for it in self.remote_tree.selectedItems() if it.data(0, Qt.UserRole + 2) != '..']

    def remote_mkdir(self):
        if not self.remote_connected:
            return
        name, ok = QInputDialog.getText(self, 'New remote folder', 'Folder name:')
        if ok and name.strip():
            self.req_mkdir.emit(posixpath.join(self.remote_cwd, name.strip()), self.remote_cwd)

    def remote_rename(self):
        items = self.selected_remote_items()
        if len(items) != 1:
            return
        path = items[0][0]
        name, ok = QInputDialog.getText(self, 'Rename', 'New name:', text=posixpath.basename(path))
        if ok and name.strip() and name.strip() != posixpath.basename(path):
            self.req_rename.emit(path, posixpath.join(posixpath.dirname(path), name.strip()), self.remote_cwd)

    def remote_delete(self):
        items = self.selected_remote_items()
        if not items:
            return
        listing = '\n'.join(p + ('/' if d else '') for p, d in items[:15])
        if len(items) > 15:
            listing += f'\n... and {len(items) - 15} more'
        answer = QMessageBox.question(self, 'Delete remote items',
                                      f'Permanently delete {len(items)} item(s) (folders recursively)?\n\n{listing}')
        if answer == QMessageBox.Yes:
            self.req_delete.emit(items, self.remote_cwd)

    def _fill_menu(self, menu, actions):
        for entry in actions:
            if entry is None:
                menu.addSeparator()
                continue
            text, slot, enabled = entry
            action = QAction(text, menu)
            action.setEnabled(enabled)
            action.triggered.connect(slot)
            menu.addAction(action)

    def remote_context_menu(self, pos):
        if not self.remote_connected:
            return
        menu = QMenu(self)
        selected = self.selected_remote_items()
        self._fill_menu(menu, [('Download (copy)', lambda: self.download_selection(MODE_COPY), bool(selected)),
                               ('Download (move)', lambda: self.download_selection(MODE_MOVE), bool(selected)),
                               None,
                               ('New folder...', self.remote_mkdir, True),
                               ('Rename...', self.remote_rename, len(selected) == 1),
                               ('Delete...', self.remote_delete, bool(selected)),
                               None,
                               ('Refresh', self.remote_refresh, True)])
        menu.exec(self.remote_tree.viewport().mapToGlobal(pos))

    def set_local_dir(self, path):
        path = path.strip()
        if not path:
            self.local_view.setRootIndex(QModelIndex())
            self.local_path_edit.setText('')
            return
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(path):
            self.log(f'Local folder not found: {path}', 'error')
            return
        self.local_model.setRootPath(path)
        self.local_view.setRootIndex(self.local_model.index(path))
        self.local_path_edit.setText(QDir.toNativeSeparators(path))

    def local_up(self):
        current = self.local_path_edit.text()
        if not current:
            return
        parent = os.path.dirname(os.path.normpath(current))
        if parent == os.path.normpath(current):
            if sys.platform.startswith('win'):
                self.set_local_dir('')
            return
        self.set_local_dir(parent)

    def pick_local_dir(self):
        path = QFileDialog.getExistingDirectory(self, 'Choose local folder', self.local_path_edit.text())
        if path:
            self.set_local_dir(path)

    def local_double_clicked(self, index):
        if self.local_model.isDir(index):
            self.set_local_dir(self.local_model.filePath(index))

    def selected_local_paths(self):
        return [QDir.toNativeSeparators(self.local_model.filePath(idx))
                for idx in self.local_view.selectionModel().selectedRows(0)]

    def local_context_menu(self, pos):
        menu = QMenu(self)
        selected = bool(self.selected_local_paths())
        self._fill_menu(menu, [('Upload (copy)', lambda: self.upload_selection(MODE_COPY), selected),
                               ('Upload (move)', lambda: self.upload_selection(MODE_MOVE), selected)])
        menu.exec(self.local_view.viewport().mapToGlobal(pos))

    def upload_selection(self, mode=None):
        self.enqueue(DIRECTION_UPLOAD, self.selected_local_paths(), self.remote_cwd or '/', mode)

    def download_selection(self, mode=None):
        self.enqueue(DIRECTION_DOWNLOAD, [p for p, _ in self.selected_remote_items()],
                     self.local_path_edit.text(), mode)

    @Slot(object, str)
    def on_local_dropped(self, paths, target):
        self.enqueue(DIRECTION_UPLOAD, paths, target or self.remote_cwd or '/')

    @Slot(object, str)
    def on_remote_dropped(self, items, target):
        self.enqueue(DIRECTION_DOWNLOAD, [p for p, _ in items], target or self.local_path_edit.text())

    def enqueue(self, direction, paths, destination, mode=None):
        if not paths:
            self.log('Nothing selected', 'warning')
            return
        if direction == DIRECTION_UPLOAD and not self.remote_connected:
            self.log('Connect first: uploads go to the current remote folder', 'warning')
            return
        if direction == DIRECTION_DOWNLOAD and not destination:
            self.log('Choose a local folder first: downloads go to the current local folder', 'warning')
            return
        options = self.transfer_options(mode)
        if options.mode == MODE_MOVE:
            answer = QMessageBox.question(self, 'Confirm move',
                                          f'MOVE {len(paths)} item(s)? Sources are deleted after each successful '
                                          f'transfer.')
            if answer != QMessageBox.Yes:
                return
        settings = self.connection_settings()
        arrow = '->' if direction == DIRECTION_UPLOAD else '<-'
        self.log(f'Queued {direction} ({options.mode}) of {len(paths)} item(s) {arrow} {destination} | '
                 f'timeout {settings.timeout:g}s, retries {options.retries}, retry delay {options.retry_delay:g}s, '
                 f'existing files: {options.exists_policy}', 'info')
        self.queue.append((settings, options, direction, paths, destination))
        self._start_next()

    def _start_next(self):
        self.queue_label.setText(f'Queue: {len(self.queue)}')
        if self.transfer_worker is not None or not self.queue:
            return
        settings, options, direction, paths, destination = self.queue.pop(0)
        self.queue_label.setText(f'Queue: {len(self.queue)}')
        self.transfer_worker = TransferWorker(settings, options, direction, paths, destination)
        self.transfer_worker.log.connect(self.log)
        self.transfer_worker.progress.connect(self.on_progress)
        self.transfer_worker.done.connect(self.on_transfer_done)
        self.transfer_worker.finished.connect(self._on_worker_finished)
        self._last_console_progress = 0.0
        self.cancel_button.setEnabled(True)
        self.file_progress.setValue(0)
        self.total_progress.setValue(0)
        self.stats_label.setText('Starting ...')
        self.status_suffix = ' [0%]'
        self.title_changed.emit()
        self.transfer_worker.start()

    def cancel_transfer(self):
        if self.queue:
            self.log(f'Dropped {len(self.queue)} queued transfer(s)', 'warning')
            self.queue.clear()
            self.queue_label.setText('Queue: 0')
        if self.transfer_worker is not None:
            self.log('Cancelling ...', 'warning')
            self.cancel_button.setEnabled(False)
            self.transfer_worker.cancel()

    @Slot(object)
    def on_progress(self, info):
        file_size, total_size = info['file_size'], info['total_size']
        file_ratio = info['file_done'] / file_size if file_size else 0
        total_ratio = info['total_done'] / total_size if total_size else 0
        name = os.path.basename(info['file'].replace('\\', '/').rstrip('/')) if info['file'] else '-'
        self.file_progress.setValue(int(min(1.0, file_ratio) * 1000))
        self.file_progress.setFormat(f'{name}: {file_ratio * 100:.1f}%  '
                                     f'({human_size(info["file_done"])} / {human_size(file_size)})')
        self.total_progress.setValue(int(min(1.0, total_ratio) * 1000))
        self.total_progress.setFormat(f'Total: {total_ratio * 100:.1f}%  '
                                      f'({human_size(info["total_done"])} / {human_size(total_size)})')
        speed_text = f'{human_size(info["speed"])}/s'
        self.stats_label.setText(f'File {min(info["files_done"] + 1, info["files_total"])}/{info["files_total"]} | '
                                 f'{speed_text} | ETA {human_time(info["eta"])} | '
                                 f'elapsed {human_time(info["elapsed"])}')
        suffix = f' [{total_ratio * 100:.0f}%]'
        if suffix != self.status_suffix:
            self.status_suffix = suffix
            self.title_changed.emit()
        now = time.monotonic()
        if info['file'] and now - self._last_console_progress >= self.console_interval_spin.value():
            self._last_console_progress = now
            self.log(f'{name} {file_ratio * 100:5.1f}% | total {total_ratio * 100:5.1f}% '
                     f'({human_size(info["total_done"])}/{human_size(total_size)}) | {speed_text} | '
                     f'ETA {human_time(info["eta"])}', 'progress')

    @Slot(object)
    def on_transfer_done(self, stats):
        level = 'error' if stats['failed'] else ('warning' if stats['cancelled'] else 'success')
        elapsed = stats.get('elapsed', 0)
        avg = stats['bytes'] / elapsed if elapsed > 0 else 0
        message = (f'Transfer {"cancelled" if stats["cancelled"] else "finished"}: {stats["ok"]} ok, '
                   f'{stats["skipped"]} skipped, {stats["failed"]} failed, {human_size(stats["bytes"])} in '
                   f'{human_time(elapsed)} (avg {human_size(avg)}/s)')
        self.log(message, level)
        self.stats_label.setText(message)

    def _on_worker_finished(self):
        self.transfer_worker.deleteLater()
        self.transfer_worker = None
        self.cancel_button.setEnabled(bool(self.queue))
        self.status_suffix = ''
        self.title_changed.emit()
        if self.remote_connected:
            self.remote_refresh()
        self._start_next()

    def shutdown(self):
        self.queue.clear()
        if self.transfer_worker is not None:
            self.transfer_worker.cancel()
            self.transfer_worker.wait(15000)
        self.browser_thread.quit()
        self.browser_thread.wait(5000)
        self.browser_worker.shutdown()


class FTPClientWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        data = load_settings()
        self.setWindowTitle('pyFTPclient V' + read_version())
        self.resize(1300, 900)
        icon_path = get_running_path('icon.ico')
        if os.path.isfile(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.tabBar().setContextMenuPolicy(Qt.CustomContextMenu)
        self.tabs.tabBar().customContextMenuRequested.connect(self.tab_context_menu)
        new_tab_button = QToolButton()
        new_tab_button.setText('+')
        new_tab_button.setToolTip('New tab, duplicating the current session (Ctrl+T)')
        new_tab_button.clicked.connect(lambda: self.new_tab(duplicate=True))
        self.tabs.setCornerWidget(new_tab_button, Qt.TopRightCorner)
        self.setCentralWidget(self.tabs)

        QShortcut(QKeySequence('Ctrl+T'), self, activated=lambda: self.new_tab(duplicate=True))
        QShortcut(QKeySequence('Ctrl+W'), self, activated=lambda: self.close_tab(self.tabs.currentIndex()))
        QShortcut(QKeySequence('F5'), self, activated=lambda: self.current_tab() and self.current_tab().remote_refresh())

        sessions = data.get('sessions') or [{}]
        for session in sessions:
            self.add_tab(session)
        self.tabs.setCurrentIndex(min(int(data.get('current_tab', 0)), self.tabs.count() - 1))
        geometry = data.get('geometry')
        if geometry:
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode('ascii')))
        self.current_tab().log(f'pyFTPclient V{read_version()} started on {sys.platform}', 'info')

    def current_tab(self):
        return self.tabs.currentWidget()

    def add_tab(self, settings=None, auto_connect=False):
        tab = SessionTab(settings)
        index = self.tabs.addTab(tab, tab.title())
        tab.title_changed.connect(lambda t=tab: self._update_tab_title(t))
        self._update_tab_title(tab)
        self.tabs.setCurrentIndex(index)
        if auto_connect:
            tab.connect_remote()
        return tab

    def new_tab(self, duplicate=False):
        source = self.current_tab()
        if duplicate and source is not None:
            settings = source.collect_settings(include_password=True)
            settings['tab_name'] = ''
            tab = self.add_tab(settings, auto_connect=source.remote_connected)
            tab.log(f'Duplicated from tab "{source.title().strip()}"', 'info')
        else:
            self.add_tab({})

    def _update_tab_title(self, tab):
        index = self.tabs.indexOf(tab)
        if index < 0:
            return
        self.tabs.setTabText(index, tab.title())
        icon = QStyle.SP_DriveNetIcon if tab.remote_connected else QStyle.SP_ComputerIcon
        self.tabs.setTabIcon(index, self.style().standardIcon(icon))
        state = 'connected' if tab.remote_connected else 'not connected'
        self.tabs.setTabToolTip(index, f'{tab.host_edit.text() or "no host"} ({state})')

    def close_tab(self, index):
        tab = self.tabs.widget(index)
        if tab is None:
            return
        if tab.is_busy():
            answer = QMessageBox.question(self, 'Transfer running',
                                          f'Tab "{tab.title()}" has a running transfer. Cancel it and close the tab?')
            if answer != QMessageBox.Yes:
                return
        tab.shutdown()
        self.tabs.removeTab(index)
        tab.deleteLater()
        if self.tabs.count() == 0:
            self.add_tab({})

    def rename_tab(self, index):
        tab = self.tabs.widget(index)
        name, ok = QInputDialog.getText(self, 'Rename tab', 'Tab name (empty = host name):', text=tab.custom_name)
        if ok:
            tab.custom_name = name.strip()
            self._update_tab_title(tab)

    def tab_context_menu(self, pos):
        index = self.tabs.tabBar().tabAt(pos)
        menu = QMenu(self)
        menu.addAction('New tab (duplicate current)', lambda: self.new_tab(duplicate=True))
        menu.addAction('New empty tab', lambda: self.new_tab(duplicate=False))
        if index >= 0:
            menu.addSeparator()
            menu.addAction('Rename tab...', lambda: self.rename_tab(index))
            menu.addAction('Close tab', lambda: self.close_tab(index))
        menu.exec(self.tabs.tabBar().mapToGlobal(pos))

    def tabs_list(self):
        return [self.tabs.widget(i) for i in range(self.tabs.count())]

    def closeEvent(self, event):
        busy = [t.title() for t in self.tabs_list() if t.is_busy()]
        if busy:
            answer = QMessageBox.question(self, 'Transfers running',
                                          f'Transfers are running in: {", ".join(busy)}.\nCancel them and exit?')
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        save_settings({'geometry': bytes(self.saveGeometry().toBase64()).decode('ascii'),
                       'current_tab': self.tabs.currentIndex(),
                       'sessions': [t.collect_settings() for t in self.tabs_list()]})
        for tab in self.tabs_list():
            tab.shutdown()
        event.accept()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setApplicationName('pyFTPclient')
    window = FTPClientWindow()
    window.show()
    sys.exit(app.exec())

import sys
import os
import json
import html
import time
import ftplib
import posixpath
import threading
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
                           QTextCursor)
from PySide6.QtWidgets import (QApplication,
                               QWidget,
                               QLabel,
                               QMainWindow,
                               QPushButton,
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
                      parse_path_list,
                      human_size,
                      human_time,
                      MODE_COPY,
                      MODE_MOVE,
                      EXISTS_RESUME,
                      EXISTS_OVERWRITE,
                      EXISTS_SKIP,
                      DIRECTION_UPLOAD,
                      DIRECTION_DOWNLOAD)

SETTINGS_FILE = 'settings.json'
REMOTE_PREFIX = 'remote:'
REMOTE_MIME = 'application/x-pyftpclient-remote-paths'

DIRECTION_AUTO = 'auto'

LOG_COLORS = {'info': '#d4d4d4',
              'success': '#6a9955',
              'warning': '#dcdcaa',
              'error': '#f44747',
              'progress': '#569cd6'}


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


class RemoteBrowserWorker(QObject):
    """Owns the browsing FTP connection; lives in its own thread so the GUI never blocks."""
    connected = Signal(str)
    disconnected = Signal()
    listed = Signal(str, object)
    failed = Signal(str)
    log = Signal(str, str)
    busy = Signal(bool)

    def __init__(self):
        super().__init__()
        self.ftp = None
        self.settings = None

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

    def _drop(self):
        if self.ftp is not None:
            try:
                self.ftp.close()
            except Exception:
                pass
        self.ftp = None

    def _list(self, path):
        def do(ftp):
            abs_path = remote_abspath(ftp, path)
            return abs_path, list_dir(ftp, abs_path)
        abs_path, entries = self._call(f'Listing {path}', do)
        self.listed.emit(abs_path, entries)

    @Slot(object)
    def connect_to(self, settings):
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
            self.log.emit(f'Connected. Remote working directory: {cwd}', 'success')
            self.connected.emit(cwd)
        except Exception as e:
            self.settings = None
            self._drop()
            self.failed.emit(f'Connection failed: {e}')
            self.busy.emit(False)
            return
        self.busy.emit(False)
        try:
            self._list(cwd)
        except Exception as e:
            self.failed.emit(f'Listing failed: {e}')

    @Slot()
    def disconnect_from(self):
        safe_close(self.ftp)
        self.ftp = None
        self.settings = None
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

    def __init__(self, settings, options, jobs):
        super().__init__()
        self.settings = settings
        self.options = options
        self.jobs = jobs
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        totals = {'ok': 0, 'skipped': 0, 'failed': 0, 'bytes': 0, 'cancelled': False}
        started = time.monotonic()
        for index, (direction, paths, destination) in enumerate(self.jobs, 1):
            if self.cancel_event.is_set():
                totals['cancelled'] = True
                break
            label = 'Upload' if direction == DIRECTION_UPLOAD else 'Download'
            self.log.emit(f'=== Job {index}/{len(self.jobs)}: {label} ===', 'info')

            def on_progress(info, label=label, index=index):
                info['job'] = f'{label} {index}/{len(self.jobs)}'
                self.progress.emit(info)

            engine = TransferEngine(self.settings, self.options, self.log.emit, on_progress, self.cancel_event)
            stats = engine.run(direction, paths, destination)
            for key in ('ok', 'skipped', 'failed', 'bytes'):
                totals[key] += stats.get(key, 0)
            if stats.get('cancelled'):
                totals['cancelled'] = True
                break
        totals['elapsed'] = time.monotonic() - started
        self.done.emit(totals)


class PathInputBox(QPlainTextEdit):
    """Multi-line path input that accepts drops from the OS file manager and from both browsers."""

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setPlaceholderText('One path per line (or several "quoted paths" on a line). Files and folders are both '
                                'accepted.\nLocal paths are uploaded; lines starting with "remote:" are downloaded.\n'
                                'You can also drag & drop from the file manager or from the two browsers above.')

    def add_paths(self, paths):
        existing = self.toPlainText().rstrip('\n')
        current = set(line.strip() for line in existing.splitlines())
        new = [p for p in paths if p not in current]
        if not new:
            return
        self.setPlainText((existing + '\n' if existing else '') + '\n'.join(new))
        self.moveCursor(QTextCursor.End)

    def canInsertFromMimeData(self, source):
        return source.hasUrls() or source.hasFormat(REMOTE_MIME) or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source):
        if source.hasFormat(REMOTE_MIME):
            data = bytes(source.data(REMOTE_MIME)).decode('utf-8')
            self.add_paths([REMOTE_PREFIX + p for p in data.splitlines() if p])
        elif source.hasUrls():
            paths = [QDir.toNativeSeparators(u.toLocalFile()) for u in source.urls() if u.isLocalFile()]
            self.add_paths(paths)
        else:
            super().insertFromMimeData(source)


class RemoteTree(QTreeWidget):
    def mimeData(self, items):
        paths = [it.data(0, Qt.UserRole) for it in items if it.data(0, Qt.UserRole + 2) != '..']
        mime = QMimeData()
        mime.setData(REMOTE_MIME, QByteArray('\n'.join(paths).encode('utf-8')))
        mime.setText('\n'.join(REMOTE_PREFIX + p for p in paths))
        return mime

    def mimeTypes(self):
        return [REMOTE_MIME, 'text/plain']


class FTPClientWindow(QMainWindow):
    req_connect = Signal(object)
    req_disconnect = Signal()
    req_list = Signal(str)
    req_mkdir = Signal(str, str)
    req_rename = Signal(str, str, str)
    req_delete = Signal(object, str)

    def __init__(self):
        super().__init__()
        self.settings_data = load_settings()
        self.setWindowTitle('pyFTPclient V' + read_version())
        self.resize(1300, 900)
        icon_path = get_running_path('icon.ico')
        if os.path.isfile(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.remote_cwd = ''
        self.remote_connected = False
        self.transfer_worker = None
        self._last_console_progress = 0.0

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        main_layout.addWidget(self._build_connection_box())

        vertical_splitter = QSplitter(Qt.Vertical)
        browsers_splitter = QSplitter(Qt.Horizontal)
        browsers_splitter.addWidget(self._build_local_browser())
        browsers_splitter.addWidget(self._build_remote_browser())
        browsers_splitter.setSizes([650, 650])
        vertical_splitter.addWidget(browsers_splitter)
        vertical_splitter.addWidget(self._build_transfer_box())
        vertical_splitter.addWidget(self._build_console_box())
        vertical_splitter.setStretchFactor(0, 3)
        vertical_splitter.setStretchFactor(1, 2)
        vertical_splitter.setStretchFactor(2, 2)
        main_layout.addWidget(vertical_splitter)

        self._setup_browser_thread()
        self._apply_settings()
        self._set_remote_state(False)
        self.log(f'pyFTPclient V{read_version()} started on {sys.platform}', 'info')

    def _build_connection_box(self):
        box = QGroupBox('Connection')
        layout = QHBoxLayout(box)

        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText('ftp.example.com')
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

    def _build_local_browser(self):
        box = QGroupBox('Local')
        layout = QVBoxLayout(box)
        style = self.style()

        nav = QHBoxLayout()
        up_button = QPushButton()
        up_button.setIcon(style.standardIcon(QStyle.SP_FileDialogToParent))
        up_button.setToolTip('Parent folder')
        up_button.clicked.connect(self.local_up)
        home_button = QPushButton()
        home_button.setIcon(style.standardIcon(QStyle.SP_DirHomeIcon))
        home_button.setToolTip('Home folder')
        home_button.clicked.connect(lambda: self.set_local_dir(os.path.expanduser('~')))
        browse_button = QPushButton('...')
        browse_button.setToolTip('Pick folder')
        browse_button.clicked.connect(self.pick_local_dir)
        self.local_path_edit = QLineEdit()
        self.local_path_edit.returnPressed.connect(lambda: self.set_local_dir(self.local_path_edit.text()))
        nav.addWidget(up_button)
        nav.addWidget(home_button)
        nav.addWidget(self.local_path_edit)
        nav.addWidget(browse_button)
        layout.addLayout(nav)

        self.local_model = QFileSystemModel()
        self.local_model.setRootPath('')
        self.local_model.setFilter(QDir.AllEntries | QDir.NoDotAndDotDot | QDir.Hidden | QDir.System)
        self.local_view = QTreeView()
        self.local_view.setModel(self.local_model)
        self.local_view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.local_view.setDragEnabled(True)
        self.local_view.setDragDropMode(QAbstractItemView.DragOnly)
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
        layout.addWidget(self.local_view)

        buttons = QHBoxLayout()
        add_button = QPushButton('Add selected to input')
        add_button.clicked.connect(self.add_local_selection)
        upload_button = QPushButton('Upload selected  \u2192')
        upload_button.clicked.connect(lambda: self.quick_transfer(DIRECTION_UPLOAD))
        buttons.addWidget(add_button)
        buttons.addWidget(upload_button)
        layout.addLayout(buttons)
        return box

    def _build_remote_browser(self):
        box = QGroupBox('Remote')
        layout = QVBoxLayout(box)
        style = self.style()

        nav = QHBoxLayout()
        self.remote_up_button = QPushButton()
        self.remote_up_button.setIcon(style.standardIcon(QStyle.SP_FileDialogToParent))
        self.remote_up_button.setToolTip('Parent folder')
        self.remote_up_button.clicked.connect(self.remote_up)
        self.remote_refresh_button = QPushButton()
        self.remote_refresh_button.setIcon(style.standardIcon(QStyle.SP_BrowserReload))
        self.remote_refresh_button.setToolTip('Refresh')
        self.remote_refresh_button.clicked.connect(self.remote_refresh)
        self.remote_mkdir_button = QPushButton()
        self.remote_mkdir_button.setIcon(style.standardIcon(QStyle.SP_FileDialogNewFolder))
        self.remote_mkdir_button.setToolTip('New folder')
        self.remote_mkdir_button.clicked.connect(self.remote_mkdir)
        self.remote_path_edit = QLineEdit()
        self.remote_path_edit.returnPressed.connect(lambda: self.request_remote_list(self.remote_path_edit.text()))
        self.remote_busy_label = QLabel('')
        nav.addWidget(self.remote_up_button)
        nav.addWidget(self.remote_refresh_button)
        nav.addWidget(self.remote_mkdir_button)
        nav.addWidget(self.remote_path_edit)
        nav.addWidget(self.remote_busy_label)
        layout.addLayout(nav)

        self.remote_tree = RemoteTree()
        self.remote_tree.setHeaderLabels(['Name', 'Size', 'Modified'])
        self.remote_tree.setRootIsDecorated(False)
        self.remote_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.remote_tree.setDragEnabled(True)
        self.remote_tree.setDragDropMode(QAbstractItemView.DragOnly)
        self.remote_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.remote_tree.header().setStretchLastSection(False)
        self.remote_tree.itemDoubleClicked.connect(self.remote_double_clicked)
        self.remote_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.remote_tree.customContextMenuRequested.connect(self.remote_context_menu)
        layout.addWidget(self.remote_tree)

        buttons = QHBoxLayout()
        self.remote_add_button = QPushButton('Add selected to input')
        self.remote_add_button.clicked.connect(self.add_remote_selection)
        self.remote_download_button = QPushButton('\u2190  Download selected')
        self.remote_download_button.clicked.connect(lambda: self.quick_transfer(DIRECTION_DOWNLOAD))
        buttons.addWidget(self.remote_add_button)
        buttons.addWidget(self.remote_download_button)
        layout.addLayout(buttons)
        return box

    def _build_transfer_box(self):
        box = QGroupBox('Transfer')
        layout = QHBoxLayout(box)

        left = QVBoxLayout()
        input_header = QHBoxLayout()
        input_header.addWidget(QLabel('Paths to transfer (files and/or folders):'))
        input_header.addStretch()
        browse_files = QPushButton('Add files...')
        browse_files.clicked.connect(self.pick_input_files)
        browse_folder = QPushButton('Add folder...')
        browse_folder.clicked.connect(self.pick_input_folder)
        clear_input = QPushButton('Clear')
        clear_input.clicked.connect(lambda: self.path_input.clear())
        input_header.addWidget(browse_files)
        input_header.addWidget(browse_folder)
        input_header.addWidget(clear_input)
        left.addLayout(input_header)
        self.path_input = PathInputBox()
        left.addWidget(self.path_input)

        dest_grid = QGridLayout()
        self.remote_dest_edit = QLineEdit()
        self.remote_dest_edit.setPlaceholderText('remote folder (uploads go here)')
        remote_dest_btn = QPushButton('Use current remote')
        remote_dest_btn.clicked.connect(lambda: self.remote_dest_edit.setText(self.remote_cwd))
        self.local_dest_edit = QLineEdit()
        self.local_dest_edit.setPlaceholderText('local folder (downloads go here)')
        local_dest_btn = QPushButton('Use current local')
        local_dest_btn.clicked.connect(lambda: self.local_dest_edit.setText(self.local_path_edit.text()))
        local_dest_pick = QPushButton('...')
        local_dest_pick.clicked.connect(self.pick_local_dest)
        dest_grid.addWidget(QLabel('Upload to (remote):'), 0, 0)
        dest_grid.addWidget(self.remote_dest_edit, 0, 1, 1, 2)
        dest_grid.addWidget(remote_dest_btn, 0, 3)
        dest_grid.addWidget(QLabel('Download to (local):'), 1, 0)
        dest_grid.addWidget(self.local_dest_edit, 1, 1)
        dest_grid.addWidget(local_dest_pick, 1, 2)
        dest_grid.addWidget(local_dest_btn, 1, 3)
        left.addLayout(dest_grid)
        layout.addLayout(left, 3)

        right = QVBoxLayout()
        options_box = QGroupBox('Options')
        grid = QGridLayout(options_box)

        self.direction_combo = QComboBox()
        self.direction_combo.addItem('Auto (local \u2192 upload, remote: \u2192 download)', DIRECTION_AUTO)
        self.direction_combo.addItem('Upload all (local \u2192 remote)', DIRECTION_UPLOAD)
        self.direction_combo.addItem('Download all (remote \u2192 local)', DIRECTION_DOWNLOAD)

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
        self.verify_check = QCheckBox('Verify size after transfer')
        self.verify_check.setChecked(True)
        self.console_interval_spin = QDoubleSpinBox()
        self.console_interval_spin.setRange(0.5, 600)
        self.console_interval_spin.setValue(2)
        self.console_interval_spin.setSuffix(' s')
        self.console_interval_spin.setToolTip('How often progress lines are printed in the console')

        rows = (('Direction:', self.direction_combo),
                ('Mode:', mode_layout),
                ('Timeout:', self.timeout_spin),
                ('Retries:', self.retries_spin),
                ('Retry delay:', self.retry_delay_spin),
                ('If target exists:', self.exists_combo),
                ('Console progress every:', self.console_interval_spin))
        for row, (label, widget) in enumerate(rows):
            grid.addWidget(QLabel(label), row, 0)
            if isinstance(widget, QHBoxLayout):
                grid.addLayout(widget, row, 1)
            else:
                grid.addWidget(widget, row, 1)
        grid.addWidget(self.verify_check, len(rows), 0, 1, 2)
        right.addWidget(options_box)

        action_layout = QHBoxLayout()
        self.start_button = QPushButton('Start transfer')
        self.start_button.setStyleSheet('font-weight: bold; padding: 6px;')
        self.start_button.clicked.connect(self.start_from_input)
        self.cancel_button = QPushButton('Cancel')
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_transfer)
        action_layout.addWidget(self.start_button)
        action_layout.addWidget(self.cancel_button)
        right.addLayout(action_layout)

        self.file_progress = QProgressBar()
        self.file_progress.setRange(0, 1000)
        self.file_progress.setFormat('File: -')
        self.total_progress = QProgressBar()
        self.total_progress.setRange(0, 1000)
        self.total_progress.setFormat('Total: -')
        self.stats_label = QLabel('Idle')
        self.stats_label.setWordWrap(True)
        right.addWidget(self.file_progress)
        right.addWidget(self.total_progress)
        right.addWidget(self.stats_label)
        right.addStretch()
        layout.addLayout(right, 2)
        return box

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
        self.browser_worker.connected.connect(self.on_remote_connected)
        self.browser_worker.disconnected.connect(lambda: self._set_remote_state(False))
        self.browser_worker.listed.connect(self.on_remote_listed)
        self.browser_worker.failed.connect(self.on_remote_failed)
        self.browser_worker.log.connect(self.log)
        self.browser_worker.busy.connect(lambda b: self.remote_busy_label.setText('working...' if b else ''))
        self.browser_thread.start()

    def _apply_settings(self):
        s = self.settings_data
        self.host_edit.setText(s.get('host', ''))
        self.port_spin.setValue(int(s.get('port', 21)))
        self.user_edit.setText(s.get('user', ''))
        self.remember_pass_check.setChecked(bool(s.get('remember_password', False)))
        if self.remember_pass_check.isChecked():
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
        for combo, key in ((self.direction_combo, 'direction'), (self.exists_combo, 'exists_policy')):
            idx = combo.findData(s.get(key))
            if idx >= 0:
                combo.setCurrentIndex(idx)
        self.remote_dest_edit.setText(s.get('remote_dest', ''))
        local_dir = s.get('local_dir') or os.path.expanduser('~')
        if not os.path.isdir(local_dir):
            local_dir = os.path.expanduser('~')
        self.set_local_dir(local_dir)
        self.local_dest_edit.setText(s.get('local_dest', '') or local_dir)
        geometry = s.get('geometry')
        if geometry:
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode('ascii')))

    def _collect_settings(self):
        data = {'host': self.host_edit.text().strip(),
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
                'direction': self.direction_combo.currentData(),
                'exists_policy': self.exists_combo.currentData(),
                'remote_dest': self.remote_dest_edit.text(),
                'local_dest': self.local_dest_edit.text(),
                'local_dir': self.local_path_edit.text(),
                'geometry': bytes(self.saveGeometry().toBase64()).decode('ascii')}
        if data['remember_password']:
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
        tag = {'warning': 'WARN ', 'error': 'ERROR', 'success': 'OK   ', 'progress': 'PROG '}.get(level, 'INFO ')
        safe = html.escape(message).replace('\n', '<br>')
        self.console.appendHtml(f'<span style="color:#808080">[{stamp}]</span> '
                                f'<span style="color:{color}">{tag} {safe}</span>')
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
            self.log('Disconnected from remote browser', 'info')
            return
        settings = self.connection_settings()
        if not settings.host:
            QMessageBox.warning(self, 'Missing host', 'Please enter the FTP host.')
            return
        self.connect_button.setEnabled(False)
        self.req_connect.emit(settings)

    def _set_remote_state(self, connected):
        self.remote_connected = connected
        self.connect_button.setEnabled(True)
        self.connect_button.setText('Disconnect' if connected else 'Connect')
        for w in (self.remote_up_button, self.remote_refresh_button, self.remote_mkdir_button,
                  self.remote_path_edit, self.remote_add_button, self.remote_download_button):
            w.setEnabled(connected)
        for w in (self.host_edit, self.port_spin, self.user_edit, self.pass_edit, self.tls_check,
                  self.passive_check, self.encoding_combo):
            w.setEnabled(not connected)
        if not connected:
            self.remote_tree.clear()

    @Slot(str)
    def on_remote_connected(self, cwd):
        self._set_remote_state(True)
        self.remote_cwd = cwd
        if not self.remote_dest_edit.text():
            self.remote_dest_edit.setText(cwd)

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

    def remote_double_clicked(self, item, _column):
        if item.data(0, Qt.UserRole + 1):
            self.request_remote_list(item.data(0, Qt.UserRole))

    def selected_remote_items(self):
        return [(it.data(0, Qt.UserRole), bool(it.data(0, Qt.UserRole + 1)))
                for it in self.remote_tree.selectedItems() if it.data(0, Qt.UserRole + 2) != '..']

    def add_remote_selection(self):
        items = self.selected_remote_items()
        if not items:
            self.log('No remote items selected', 'warning')
            return
        self.path_input.add_paths([REMOTE_PREFIX + p for p, _ in items])

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

    def remote_context_menu(self, pos):
        if not self.remote_connected:
            return
        menu = QMenu(self)
        has_sel = bool(self.selected_remote_items())
        actions = [('Download (copy)', lambda: self.quick_transfer(DIRECTION_DOWNLOAD, MODE_COPY), has_sel),
                   ('Download (move)', lambda: self.quick_transfer(DIRECTION_DOWNLOAD, MODE_MOVE), has_sel),
                   ('Add to input', self.add_remote_selection, has_sel),
                   None,
                   ('New folder...', self.remote_mkdir, True),
                   ('Rename...', self.remote_rename, len(self.selected_remote_items()) == 1),
                   ('Delete...', self.remote_delete, has_sel),
                   None,
                   ('Refresh', self.remote_refresh, True)]
        self._fill_menu(menu, actions)
        menu.exec(self.remote_tree.viewport().mapToGlobal(pos))

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

    def pick_local_dest(self):
        path = QFileDialog.getExistingDirectory(self, 'Choose download folder', self.local_dest_edit.text())
        if path:
            self.local_dest_edit.setText(QDir.toNativeSeparators(path))

    def pick_input_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, 'Add files', self.local_path_edit.text())
        if files:
            self.path_input.add_paths([QDir.toNativeSeparators(f) for f in files])

    def pick_input_folder(self):
        path = QFileDialog.getExistingDirectory(self, 'Add folder', self.local_path_edit.text())
        if path:
            self.path_input.add_paths([QDir.toNativeSeparators(path)])

    def local_double_clicked(self, index):
        path = self.local_model.filePath(index)
        if self.local_model.isDir(index):
            self.set_local_dir(path)

    def selected_local_paths(self):
        return [QDir.toNativeSeparators(self.local_model.filePath(idx))
                for idx in self.local_view.selectionModel().selectedRows(0)]

    def add_local_selection(self):
        paths = self.selected_local_paths()
        if not paths:
            self.log('No local items selected', 'warning')
            return
        self.path_input.add_paths(paths)

    def local_context_menu(self, pos):
        menu = QMenu(self)
        has_sel = bool(self.selected_local_paths())
        actions = [('Upload (copy)', lambda: self.quick_transfer(DIRECTION_UPLOAD, MODE_COPY), has_sel),
                   ('Upload (move)', lambda: self.quick_transfer(DIRECTION_UPLOAD, MODE_MOVE), has_sel),
                   ('Add to input', self.add_local_selection, has_sel)]
        self._fill_menu(menu, actions)
        menu.exec(self.local_view.viewport().mapToGlobal(pos))

    def quick_transfer(self, direction, mode=None):
        if direction == DIRECTION_UPLOAD:
            paths = self.selected_local_paths()
            jobs = [(DIRECTION_UPLOAD, paths, self.remote_dest_edit.text().strip() or self.remote_cwd or '/')]
        else:
            paths = [p for p, _ in self.selected_remote_items()]
            jobs = [(DIRECTION_DOWNLOAD, paths, self.local_dest_edit.text().strip() or self.local_path_edit.text())]
        if not paths:
            self.log('Nothing selected', 'warning')
            return
        self.start_transfer(jobs, mode)

    def build_jobs_from_input(self):
        lines = parse_path_list(self.path_input.toPlainText())
        direction = self.direction_combo.currentData()
        uploads, downloads = [], []
        for line in lines:
            is_remote_tagged = line.lower().startswith(REMOTE_PREFIX)
            path = line[len(REMOTE_PREFIX):].strip() if is_remote_tagged else line
            if direction == DIRECTION_UPLOAD:
                uploads.append(path)
            elif direction == DIRECTION_DOWNLOAD:
                downloads.append(path)
            elif is_remote_tagged:
                downloads.append(path)
            elif os.path.exists(os.path.expanduser(path)):
                uploads.append(path)
            else:
                self.log(f'"{path}" does not exist locally; treating it as a remote path (download)', 'warning')
                downloads.append(path)
        jobs = []
        if uploads:
            jobs.append((DIRECTION_UPLOAD, uploads, self.remote_dest_edit.text().strip() or self.remote_cwd or '/'))
        if downloads:
            jobs.append((DIRECTION_DOWNLOAD, downloads,
                         self.local_dest_edit.text().strip() or self.local_path_edit.text() or os.getcwd()))
        return jobs

    def start_from_input(self):
        jobs = self.build_jobs_from_input()
        if not jobs:
            QMessageBox.information(self, 'Nothing to do', 'Enter at least one file or folder path.')
            return
        self.start_transfer(jobs)

    def start_transfer(self, jobs, mode=None):
        if self.transfer_worker is not None:
            QMessageBox.warning(self, 'Busy', 'A transfer is already running.')
            return
        settings = self.connection_settings()
        if not settings.host:
            QMessageBox.warning(self, 'Missing host', 'Please enter the FTP host.')
            return
        options = self.transfer_options(mode)
        if options.mode == MODE_MOVE:
            count = sum(len(paths) for _, paths, _ in jobs)
            answer = QMessageBox.question(self, 'Confirm move',
                                          f'MOVE {count} item(s)? Sources are deleted after each successful '
                                          f'transfer.')
            if answer != QMessageBox.Yes:
                return
        for direction, paths, dest in jobs:
            arrow = '->' if direction == DIRECTION_UPLOAD else '<-'
            self.log(f'Queued {direction} ({options.mode}) of {len(paths)} path(s) {arrow} {dest}', 'info')
        self.log(f'Timeout {settings.timeout:g}s, retries {options.retries}, retry delay {options.retry_delay:g}s, '
                 f'existing files: {options.exists_policy}', 'info')

        self.transfer_worker = TransferWorker(settings, options, jobs)
        self.transfer_worker.log.connect(self.log)
        self.transfer_worker.progress.connect(self.on_progress)
        self.transfer_worker.done.connect(self.on_transfer_done)
        self.transfer_worker.finished.connect(self._on_worker_finished)
        self._last_console_progress = 0.0
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.file_progress.setValue(0)
        self.total_progress.setValue(0)
        self.stats_label.setText('Starting ...')
        self.transfer_worker.start()

    def cancel_transfer(self):
        if self.transfer_worker is not None:
            self.log('Cancelling ...', 'warning')
            self.cancel_button.setEnabled(False)
            self.transfer_worker.cancel()

    @Slot(object)
    def on_progress(self, info):
        file_size = info['file_size']
        total_size = info['total_size']
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
        summary = (f'{info.get("job", "")} | file {min(info["files_done"] + 1, info["files_total"])}/'
                   f'{info["files_total"]} | {speed_text} | ETA {human_time(info["eta"])} | '
                   f'elapsed {human_time(info["elapsed"])}')
        self.stats_label.setText(summary)
        now = time.monotonic()
        if info['file'] and now - self._last_console_progress >= self.console_interval_spin.value():
            self._last_console_progress = now
            self.log(f'{name} {file_ratio * 100:5.1f}% | total {total_ratio * 100:5.1f}% '
                     f'({human_size(info["total_done"])}/{human_size(total_size)}) | {speed_text} | '
                     f'ETA {human_time(info["eta"])}', 'progress')

    @Slot(object)
    def on_transfer_done(self, totals):
        level = 'error' if totals['failed'] else ('warning' if totals['cancelled'] else 'success')
        elapsed = totals.get('elapsed', 0)
        avg = totals['bytes'] / elapsed if elapsed > 0 else 0
        message = (f'Transfer {"cancelled" if totals["cancelled"] else "finished"}: {totals["ok"]} ok, '
                   f'{totals["skipped"]} skipped, {totals["failed"]} failed, {human_size(totals["bytes"])} in '
                   f'{human_time(elapsed)} (avg {human_size(avg)}/s)')
        self.log(message, level)
        self.stats_label.setText(message)

    def _on_worker_finished(self):
        self.transfer_worker.deleteLater()
        self.transfer_worker = None
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        if self.remote_connected:
            self.remote_refresh()

    def closeEvent(self, event):
        if self.transfer_worker is not None:
            answer = QMessageBox.question(self, 'Transfer running', 'A transfer is running. Cancel it and exit?')
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.transfer_worker.cancel()
            self.transfer_worker.wait(15000)
        save_settings(self._collect_settings())
        self.browser_thread.quit()
        self.browser_thread.wait(5000)
        self.browser_worker.shutdown()
        event.accept()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setApplicationName('pyFTPclient')
    window = FTPClientWindow()
    window.show()
    sys.exit(app.exec())

import ftplib
import os
import posixpath
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional, Tuple

BLOCK_SIZE = 64 * 1024
RESUME_CHECK_BYTES = 64 * 1024

MODE_COPY = 'copy'
MODE_MOVE = 'move'

EXISTS_RESUME = 'resume'
EXISTS_OVERWRITE = 'overwrite'
EXISTS_SKIP = 'skip'

DIRECTION_UPLOAD = 'upload'
DIRECTION_DOWNLOAD = 'download'


@dataclass
class ConnectionSettings:
    host: str
    port: int = 21
    user: str = ''
    password: str = ''
    use_tls: bool = False
    passive: bool = True
    timeout: float = 30.0
    encoding: str = 'utf-8'


@dataclass
class TransferOptions:
    retries: int = 3
    retry_delay: float = 5.0
    mode: str = MODE_COPY
    exists_policy: str = EXISTS_RESUME
    verify_size: bool = True


@dataclass
class RemoteEntry:
    name: str
    is_dir: bool
    size: int = 0
    modified: str = ''
    is_link: bool = False


@dataclass
class DiskUsage:
    total: int
    used: int
    free: int
    path: str = ''


@dataclass
class FileTask:
    src: str
    dst: str
    size: int


class TransferCancelled(Exception):
    pass


class TransferError(Exception):
    pass


class ReusedSessionFTP_TLS(ftplib.FTP_TLS):
    """FTP_TLS that reuses the control connection TLS session on data connections (required by many servers)."""

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn,
                                            server_hostname=self.host,
                                            session=self.sock.session)
        return conn, size


def connect(settings: ConnectionSettings) -> ftplib.FTP:
    if settings.use_tls:
        ftp = ReusedSessionFTP_TLS(timeout=settings.timeout, encoding=settings.encoding)
    else:
        ftp = ftplib.FTP(timeout=settings.timeout, encoding=settings.encoding)
    try:
        ftp.connect(settings.host, int(settings.port))
        ftp.login(settings.user or 'anonymous', settings.password or '')
        if settings.use_tls:
            ftp.prot_p()
        ftp.set_pasv(settings.passive)
        ftp.voidcmd('TYPE I')
    except BaseException:
        safe_close(ftp)
        raise
    return ftp


def safe_close(ftp: Optional[ftplib.FTP]):
    if ftp is None:
        return
    try:
        ftp.quit()
    except Exception:
        try:
            ftp.close()
        except Exception:
            pass


def human_size(num: float) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(num) < 1024 or unit == 'TB':
            return f'{num:.0f} {unit}' if unit == 'B' else f'{num:.2f} {unit}'
        num /= 1024
    return f'{num:.2f} TB'


def human_gb(num: float) -> str:
    return f'{num / 1024 ** 3:,.2f}'


def human_time(seconds: Optional[float]) -> str:
    if seconds is None or seconds != seconds or seconds == float('inf'):
        return '--:--:--'
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f'{h:02d}:{m:02d}:{s:02d}'


def remote_join(*parts: str) -> str:
    return posixpath.join(*parts)


def remote_abspath(ftp: ftplib.FTP, path: str) -> str:
    path = path.strip() or '.'
    if not path.startswith('/'):
        path = posixpath.join(ftp.pwd(), path)
    norm = posixpath.normpath(path)
    return '/' if norm in ('.', '//') else norm


def _format_mlsd_time(value: str) -> str:
    try:
        return datetime.strptime(value[:14], '%Y%m%d%H%M%S').strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return value or ''


_UNIX_LIST_RE = re.compile(
    r'^(?P<perm>[\-ldbcpsDrwxXsStT]{10})\S*\s+\d+\s+(?:\S+\s+){1,2}?(?P<size>\d+)\s+'
    r'(?P<date>\w{3}\s+\d{1,2}\s+(?:\d{1,2}:\d{2}|\d{4}))\s(?P<name>.+)$')
_DOS_LIST_RE = re.compile(
    r'^(?P<date>\d{2}-\d{2}-\d{2,4})\s+(?P<time>\d{1,2}:\d{2}\s*[AaPp][Mm])\s+(?P<size><DIR>|\d+)\s+(?P<name>.+)$')


def parse_list_line(line: str) -> Optional[RemoteEntry]:
    m = _UNIX_LIST_RE.match(line)
    if m:
        perm = m.group('perm')
        name = m.group('name')
        is_link = perm[0] == 'l'
        if is_link and ' -> ' in name:
            name = name.split(' -> ', 1)[0]
        if name in ('.', '..'):
            return None
        return RemoteEntry(name=name, is_dir=perm[0] == 'd', size=int(m.group('size')),
                           modified=m.group('date'), is_link=is_link)
    m = _DOS_LIST_RE.match(line)
    if m:
        is_dir = m.group('size') == '<DIR>'
        return RemoteEntry(name=m.group('name'), is_dir=is_dir,
                           size=0 if is_dir else int(m.group('size')),
                           modified=f"{m.group('date')} {m.group('time')}")
    return None


def remote_is_dir(ftp: ftplib.FTP, path: str) -> bool:
    current = ftp.pwd()
    try:
        ftp.cwd(path)
        return True
    except ftplib.error_perm:
        return False
    finally:
        try:
            ftp.cwd(current)
        except ftplib.all_errors:
            pass


def list_dir(ftp: ftplib.FTP, path: str) -> List[RemoteEntry]:
    entries: List[RemoteEntry] = []
    try:
        for name, facts in ftp.mlsd(path, facts=['type', 'size', 'modify']):
            kind = facts.get('type', '').lower()
            if kind in ('cdir', 'pdir') or name in ('.', '..'):
                continue
            is_link = 'slink' in kind
            entries.append(RemoteEntry(name=name,
                                       is_dir=kind == 'dir',
                                       size=int(facts.get('size', 0) or 0),
                                       modified=_format_mlsd_time(facts.get('modify', '')),
                                       is_link=is_link))
    except ftplib.error_perm:
        entries = []
        lines: List[str] = []
        current = ftp.pwd()
        ftp.cwd(path)
        try:
            ftp.retrlines('LIST', lines.append)
        finally:
            ftp.cwd(current)
        for line in lines:
            entry = parse_list_line(line)
            if entry:
                entries.append(entry)
    for entry in entries:
        if entry.is_link:
            entry.is_dir = remote_is_dir(ftp, remote_join(path, entry.name))
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return entries


def remote_size(ftp: ftplib.FTP, path: str) -> Optional[int]:
    try:
        size = ftp.size(path)
        return int(size) if size is not None else None
    except ftplib.error_perm:
        return None


_REPLY_ERRORS = (ftplib.error_perm, ftplib.error_temp, ftplib.error_reply, ftplib.error_proto)


def remote_read_range(ftp: ftplib.FTP, path: str, offset: int, length: int) -> bytes:
    buf = bytearray()
    ftp.voidcmd('TYPE I')
    conn = ftp.transfercmd(f'RETR {path}', rest=offset or None)
    try:
        while len(buf) < length:
            chunk = conn.recv(min(BLOCK_SIZE, length - len(buf)))
            if not chunk:
                break
            buf += chunk
    finally:
        conn.close()
    try:
        ftp.voidresp()
    except _REPLY_ERRORS:
        pass
    return bytes(buf)


def local_read_range(path: str, offset: int, length: int) -> bytes:
    with open(path, 'rb') as f:
        f.seek(offset)
        return f.read(length)


def server_supports_df(ftp: ftplib.FTP) -> bool:
    try:
        resp = ftp.sendcmd('FEAT')
    except _REPLY_ERRORS:
        return False
    return any(' '.join(line.split()).upper() == 'SITE DF' for line in resp.splitlines()[1:])


def parse_disk_usage(resp: str) -> Optional[DiskUsage]:
    if not resp.startswith('213'):
        return None
    try:
        info = dict(kv.split('=', 1) for kv in resp[4:].strip().split(' ', 3))
        return DiskUsage(total=int(info['total']), used=int(info['used']), free=int(info['free']),
                         path=info.get('path', ''))
    except (ValueError, KeyError):
        return None


def remote_disk_usage(ftp: ftplib.FTP, path: str = '') -> Optional[DiskUsage]:
    try:
        resp = ftp.sendcmd(f'SITE DF {path}' if path else 'SITE DF')
    except _REPLY_ERRORS:
        return None
    return parse_disk_usage(resp)


def remote_makedirs(ftp: ftplib.FTP, path: str):
    path = posixpath.normpath(path)
    if path in ('/', '.', ''):
        return
    current = ftp.pwd()
    try:
        built = '/' if path.startswith('/') else ''
        for part in [p for p in path.split('/') if p]:
            built = posixpath.join(built, part) if built else part
            try:
                ftp.cwd(built)
            except ftplib.error_perm:
                ftp.mkd(built)
    finally:
        try:
            ftp.cwd(current)
        except ftplib.all_errors:
            pass


def remote_rmtree(ftp: ftplib.FTP, path: str, log: Optional[Callable[[str, str], None]] = None):
    for entry in list_dir(ftp, path):
        full = remote_join(path, entry.name)
        if entry.is_dir and not entry.is_link:
            remote_rmtree(ftp, full, log)
        else:
            ftp.delete(full)
            if log:
                log(f'Deleted remote file {full}', 'info')
    ftp.rmd(path)
    if log:
        log(f'Removed remote folder {path}', 'info')


class SpeedMeter:
    def __init__(self, window: float = 5.0):
        self.window = window
        self.samples: deque = deque()
        self.total = 0

    def add(self, nbytes: int):
        self.total += nbytes
        now = time.monotonic()
        self.samples.append((now, self.total))
        while len(self.samples) > 2 and now - self.samples[0][0] > self.window:
            self.samples.popleft()

    def speed(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
        now = time.monotonic()
        elapsed = max(now, t1) - t0
        return (b1 - b0) / elapsed if elapsed > 0 else 0.0


LogCallback = Callable[[str, str], None]
ProgressCallback = Callable[[dict], None]


class TransferEngine:
    """Runs uploads/downloads with retries, resume, progress, speed and ETA reporting. Not thread-safe; use one per job."""

    def __init__(self,
                 settings: ConnectionSettings,
                 options: TransferOptions,
                 log: LogCallback,
                 progress: ProgressCallback,
                 cancel_event: Optional[threading.Event] = None,
                 progress_interval: float = 0.25):
        self.settings = settings
        self.options = options
        self.log = log
        self.progress = progress
        self.cancel_event = cancel_event or threading.Event()
        self.progress_interval = progress_interval
        self.ftp: Optional[ftplib.FTP] = None

        self.tasks: List[FileTask] = []
        self.total_bytes = 0
        self.completed_bytes = 0
        self.files_done = 0
        self.current_task: Optional[FileTask] = None
        self.current_done = 0
        self.meter = SpeedMeter()
        self.start_time = 0.0
        self._last_progress = 0.0
        self.stats = {'ok': 0, 'skipped': 0, 'failed': 0, 'bytes': 0}

    def _check_cancel(self):
        if self.cancel_event.is_set():
            raise TransferCancelled()

    def _sleep(self, seconds: float):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self._check_cancel()
            time.sleep(min(0.1, max(0.0, end - time.monotonic())))

    def _ensure_connected(self) -> ftplib.FTP:
        if self.ftp is None:
            self.log(f'Connecting to {self.settings.host}:{self.settings.port} ...', 'info')
            self.ftp = connect(self.settings)
        return self.ftp

    def _drop_connection(self):
        if self.ftp is not None:
            try:
                self.ftp.close()
            except Exception:
                pass
        self.ftp = None

    def _with_reconnect(self, description: str, func):
        attempt = 0
        while True:
            self._check_cancel()
            try:
                return func(self._ensure_connected())
            except TransferCancelled:
                raise
            except ftplib.error_perm:
                raise
            except ftplib.all_errors as e:
                attempt += 1
                self._drop_connection()
                if attempt > self.options.retries:
                    raise
                self.log(f'{description} failed ({e!r}); retry {attempt}/{self.options.retries} '
                         f'in {self.options.retry_delay:g}s', 'warning')
                self._sleep(self.options.retry_delay)

    def _emit_progress(self, force: bool = False):
        now = time.monotonic()
        if not force and now - self._last_progress < self.progress_interval:
            return
        self._last_progress = now
        speed = self.meter.speed()
        total_done = self.completed_bytes + self.current_done
        remaining = max(0, self.total_bytes - total_done)
        eta = remaining / speed if speed > 0 else None
        task = self.current_task
        self.progress({
            'file': task.src if task else '',
            'file_done': self.current_done,
            'file_size': task.size if task else 0,
            'total_done': total_done,
            'total_size': self.total_bytes,
            'files_done': self.files_done,
            'files_total': len(self.tasks),
            'speed': speed,
            'eta': eta,
            'elapsed': now - self.start_time,
        })

    def _set_task_size(self, task: FileTask, size: Optional[int]):
        if size is None or size < 0 or size == task.size:
            return
        self.total_bytes += size - task.size
        task.size = size

    def _on_chunk(self, nbytes: int):
        self._check_cancel()
        self.current_done += nbytes
        task = self.current_task
        if task is not None and self.current_done > task.size:
            self._set_task_size(task, self.current_done)
        self.meter.add(nbytes)
        self._emit_progress()

    def _plan_upload(self, local_paths: List[str], remote_dest: str) -> Tuple[List[str], List[str]]:
        dirs: List[str] = []
        roots: List[str] = []
        for raw in local_paths:
            path = os.path.abspath(os.path.expanduser(raw))
            if os.path.isfile(path):
                size = os.path.getsize(path)
                self.tasks.append(FileTask(path, remote_join(remote_dest, os.path.basename(path)), size))
            elif os.path.isdir(path):
                base = os.path.basename(os.path.normpath(path)) or 'root'
                remote_root = remote_join(remote_dest, base)
                dirs.append(remote_root)
                roots.append(path)
                for dirpath, dirnames, filenames in os.walk(path):
                    dirnames.sort()
                    rel = os.path.relpath(dirpath, path)
                    remote_dir = remote_root if rel == '.' else remote_join(remote_root, *rel.split(os.sep))
                    if rel != '.':
                        dirs.append(remote_dir)
                    for name in sorted(filenames):
                        local_file = os.path.join(dirpath, name)
                        try:
                            size = os.path.getsize(local_file)
                        except OSError as e:
                            self.log(f'Cannot read {local_file}: {e}', 'error')
                            self.stats['failed'] += 1
                            continue
                        self.tasks.append(FileTask(local_file, remote_join(remote_dir, name), size))
            else:
                self.log(f'Local path not found, skipped: {raw}', 'error')
                self.stats['failed'] += 1
        return dirs, roots

    def _walk_remote(self, ftp: ftplib.FTP, remote_root: str, local_root: str, dirs: List[str]):
        dirs.append(local_root)
        for entry in list_dir(ftp, remote_root):
            self._check_cancel()
            remote_path = remote_join(remote_root, entry.name)
            local_path = os.path.join(local_root, entry.name)
            if entry.is_dir:
                self._walk_remote(ftp, remote_path, local_path, dirs)
            else:
                size = entry.size if entry.size else (remote_size(ftp, remote_path) or 0)
                self.tasks.append(FileTask(remote_path, local_path, size))

    def _plan_download(self, remote_paths: List[str], local_dest: str) -> Tuple[List[str], List[str]]:
        dirs: List[str] = []
        roots: List[str] = []
        for raw in remote_paths:
            def plan(ftp, raw=raw):
                path = remote_abspath(ftp, raw)
                if remote_is_dir(ftp, path):
                    base = posixpath.basename(path.rstrip('/')) or 'root'
                    before = len(self.tasks)
                    sub_dirs: List[str] = []
                    try:
                        self._walk_remote(ftp, path, os.path.join(local_dest, base), sub_dirs)
                    except BaseException:
                        del self.tasks[before:]
                        raise
                    dirs.extend(sub_dirs)
                    roots.append(path)
                else:
                    size = remote_size(ftp, path)
                    if size is None:
                        names = []
                        try:
                            names = ftp.nlst(path)
                        except ftplib.error_perm:
                            pass
                        if not names:
                            self.log(f'Remote path not found, skipped: {raw}', 'error')
                            self.stats['failed'] += 1
                            return
                        size = 0
                    self.tasks.append(FileTask(path, os.path.join(local_dest, posixpath.basename(path)), size))
            try:
                self._with_reconnect(f'Scanning {raw}', plan)
            except TransferCancelled:
                raise
            except ftplib.all_errors as e:
                self.log(f'Cannot scan remote path {raw}: {e}', 'error')
                self.stats['failed'] += 1
        return dirs, roots

    def _resolve_offset(self, existing: Optional[int], size: int, first_attempt: bool, label: str,
                        same_data: Callable[[int, int], bool]) -> Optional[int]:
        """Returns the resume offset, or None if the file must be skipped.

        same_data(offset, length) compares source and destination bytes; a destination that is not a prefix of the
        source is overwritten instead of being resumed or skipped.
        """
        if existing is None:
            return 0
        if not first_attempt:
            if 0 < existing < size:
                self.log(f'Resuming {label} from {human_size(existing)}', 'info')
                return existing
            return 0
        policy = self.options.exists_policy
        if policy == EXISTS_SKIP:
            self.log(f'Exists, skipped: {label}', 'warning')
            return None
        if policy == EXISTS_RESUME and 0 < existing <= size:
            check_from = max(0, existing - RESUME_CHECK_BYTES)
            try:
                identical = same_data(check_from, existing - check_from)
            except (OSError, ftplib.error_perm):
                identical = False
            if not identical:
                self.log(f'Existing file differs from source, overwriting: {label}', 'warning')
                return 0
            if existing == size:
                self.log(f'Already complete (same size), skipped: {label}', 'warning')
                return None
            self.log(f'Resuming {label} from {human_size(existing)}', 'info')
            return existing
        return 0

    def _upload_one(self, ftp: ftplib.FTP, task: FileTask, first_attempt: bool) -> bool:
        self._set_task_size(task, os.path.getsize(task.src))
        expected = task.size
        existing = remote_size(ftp, task.dst)

        def same_data(offset: int, length: int) -> bool:
            return local_read_range(task.src, offset, length) == remote_read_range(ftp, task.dst, offset, length)

        offset = self._resolve_offset(existing, task.size, first_attempt, task.dst, same_data)
        if offset is None:
            return False
        self.current_done = offset
        with open(task.src, 'rb') as f:
            f.seek(offset)
            if offset:
                try:
                    ftp.storbinary(f'STOR {task.dst}', f, BLOCK_SIZE, lambda b: self._on_chunk(len(b)), rest=offset)
                except ftplib.error_perm as e:
                    if not str(e).startswith(('500', '501', '502', '504')):
                        raise
                    self.log('Server rejected REST for STOR, falling back to APPE', 'warning')
                    f.seek(offset)
                    self.current_done = offset
                    ftp.storbinary(f'APPE {task.dst}', f, BLOCK_SIZE, lambda b: self._on_chunk(len(b)))
            else:
                ftp.storbinary(f'STOR {task.dst}', f, BLOCK_SIZE, lambda b: self._on_chunk(len(b)))
        if self.options.verify_size:
            final = remote_size(ftp, task.dst)
            if final is not None and final != expected:
                raise TransferError(f'size mismatch after upload (remote {final} != local {expected})')
        return True

    def _download_one(self, ftp: ftplib.FTP, task: FileTask, first_attempt: bool) -> bool:
        existing = os.path.getsize(task.dst) if os.path.isfile(task.dst) else None
        self._set_task_size(task, remote_size(ftp, task.src))
        expected = task.size

        def same_data(offset: int, length: int) -> bool:
            return local_read_range(task.dst, offset, length) == remote_read_range(ftp, task.src, offset, length)

        offset = self._resolve_offset(existing, task.size, first_attempt, task.dst, same_data)
        if offset is None:
            return False
        os.makedirs(os.path.dirname(task.dst) or '.', exist_ok=True)
        self.current_done = offset
        with open(task.dst, 'ab' if offset else 'wb') as f:
            def write(chunk: bytes):
                f.write(chunk)
                self._on_chunk(len(chunk))
            ftp.retrbinary(f'RETR {task.src}', write, BLOCK_SIZE, rest=offset or None)
        if self.options.verify_size and expected:
            final = os.path.getsize(task.dst)
            if final != expected:
                raise TransferError(f'size mismatch after download (local {final} != remote {expected})')
        return True

    def _run_task(self, task: FileTask, direction: str) -> bool:
        attempt = 0
        while True:
            self._check_cancel()
            self.current_done = 0
            try:
                ftp = self._ensure_connected()
                if direction == DIRECTION_UPLOAD:
                    return self._upload_one(ftp, task, attempt == 0)
                return self._download_one(ftp, task, attempt == 0)
            except TransferCancelled:
                raise
            except ftplib.error_perm as e:
                raise TransferError(f'permanent server error: {e}')
            except (TransferError, *ftplib.all_errors) as e:
                attempt += 1
                self._drop_connection()
                if attempt > self.options.retries:
                    raise TransferError(f'giving up after {self.options.retries} retries: {e}')
                self.log(f'Error on {posixpath.basename(task.src.replace(os.sep, "/"))}: {e!r}; '
                         f'retry {attempt}/{self.options.retries} in {self.options.retry_delay:g}s', 'warning')
                self._sleep(self.options.retry_delay)

    def _cleanup_after_move(self, direction: str, roots: List[str]):
        for root in roots:
            try:
                if direction == DIRECTION_UPLOAD:
                    for dirpath, _, _ in sorted(os.walk(root), key=lambda x: len(x[0]), reverse=True):
                        try:
                            os.rmdir(dirpath)
                        except OSError:
                            pass
                    if not os.path.exists(root):
                        self.log(f'Removed local folder {root}', 'info')
                else:
                    self._with_reconnect('Cleaning up remote folders',
                                         lambda ftp, r=root: self._remove_empty_remote(ftp, r))
            except TransferCancelled:
                raise
            except Exception as e:
                self.log(f'Cleanup of {root} incomplete: {e}', 'warning')

    def _remove_empty_remote(self, ftp: ftplib.FTP, path: str) -> bool:
        empty = True
        for entry in list_dir(ftp, path):
            if entry.is_dir and not entry.is_link:
                if not self._remove_empty_remote(ftp, remote_join(path, entry.name)):
                    empty = False
            else:
                empty = False
        if empty:
            try:
                ftp.rmd(path)
                self.log(f'Removed remote folder {path}', 'info')
            except ftplib.error_perm:
                return False
        return empty

    def run(self, direction: str, paths: List[str], destination: str) -> dict:
        self.start_time = time.monotonic()
        mode = self.options.mode
        verb = 'Moving' if mode == MODE_MOVE else 'Copying'
        try:
            if direction == DIRECTION_UPLOAD:
                remote_dest = self._with_reconnect('Resolving destination',
                                                   lambda ftp: remote_abspath(ftp, destination))
                self.log(f'{verb} {len(paths)} local path(s) to remote {remote_dest}', 'info')
                dirs, roots = self._plan_upload(paths, remote_dest)
                if dirs or self.tasks:
                    self._with_reconnect('Creating remote folders', lambda ftp: [remote_makedirs(ftp, d) for d in
                                                                                 ([remote_dest] + dirs)])
            else:
                local_dest = os.path.abspath(os.path.expanduser(destination))
                self.log(f'{verb} {len(paths)} remote path(s) to local {local_dest}', 'info')
                dirs, roots = self._plan_download(paths, local_dest)
                os.makedirs(local_dest, exist_ok=True)
                for d in dirs:
                    os.makedirs(d, exist_ok=True)

            self.total_bytes = sum(t.size for t in self.tasks)
            self.log(f'{len(self.tasks)} file(s), {human_size(self.total_bytes)} total', 'info')
            self._emit_progress(force=True)

            for task in self.tasks:
                self._check_cancel()
                self.current_task = task
                self.current_done = 0
                started = time.monotonic()
                try:
                    transferred = self._run_task(task, direction)
                except TransferError as e:
                    self.log(f'FAILED {task.src}: {e}', 'error')
                    self.stats['failed'] += 1
                    transferred = None
                if transferred:
                    elapsed = max(time.monotonic() - started, 1e-6)
                    self.stats['ok'] += 1
                    self.stats['bytes'] += task.size
                    self.log(f'Done {task.src} -> {task.dst} ({human_size(task.size)} in {human_time(elapsed)}, '
                             f'avg {human_size(task.size / elapsed)}/s)', 'success')
                    if mode == MODE_MOVE:
                        try:
                            if direction == DIRECTION_UPLOAD:
                                os.remove(task.src)
                            else:
                                self._with_reconnect('Deleting remote source', lambda ftp: ftp.delete(task.src))
                        except TransferCancelled:
                            raise
                        except Exception as e:
                            self.log(f'Transferred but could not delete source {task.src}: {e}', 'warning')
                elif transferred is False:
                    self.stats['skipped'] += 1
                self.completed_bytes += task.size
                self.current_task = None
                self.current_done = 0
                self.files_done += 1
                self._emit_progress(force=True)

            if mode == MODE_MOVE and roots:
                self._cleanup_after_move(direction, roots)
            self.stats['cancelled'] = False
        except TransferCancelled:
            self.log('Transfer cancelled by user', 'warning')
            self.stats['cancelled'] = True
        except Exception as e:
            self.log(f'Transfer aborted: {e!r}', 'error')
            self.stats['cancelled'] = False
            self.stats['failed'] += 1
        finally:
            if self.stats.get('cancelled'):
                self._drop_connection()
            else:
                safe_close(self.ftp)
                self.ftp = None
        self.stats['elapsed'] = time.monotonic() - self.start_time
        return self.stats

# pyFTPclient
A cross-platform (Windows + Linux) GUI FTP/FTPS client built with PySide6. Features:
- side-by-side local and remote file browsers (drag & drop, context menus, new folder / rename / delete on the remote side)
- a multi-line input box that takes any mix of **files and folders** (one per line, or several `"quoted paths"` on one line)
  - local paths get uploaded, lines prefixed with `remote:` get downloaded (direction can also be forced)
  - paths can be typed, pasted, added with file/folder pickers, or dragged from the OS file manager or the browsers
- **copy** or **move** (the source is deleted only after a verified successful transfer; emptied folders are cleaned up)
- configurable **timeout**, **number of retries** and **retry delay**
- automatic reconnect and **resume** (REST) of partial files on retry
- policy for existing targets: resume partial / skip identical, overwrite, or skip
- optional size verification after each file
- console-like log with colored messages, periodic **progress, speed and ETA** lines, save-to-file
- file and total progress bars with live speed / ETA / elapsed time
- explicit FTPS (TLS) and active / passive mode support, selectable filename encoding
- settings persisted in `settings.json` (password only if "Remember" is checked; stored in plain text)

# Usage
- [end-user] Can be used via the bundled executable (available in Releases)
- [end-user] Windows: run `Install.bat` and after that `START_FTPclient.bat`
- [end-user] Linux: run `./Install.sh` and after that `./START_FTPclient.sh`
  - on Debian/Ubuntu Qt may need `sudo apt install libxcb-cursor0 libegl1`
- [dev] Can be built as an executable by running the install script and after that `BUILD_release.bat` / `./BUILD_release.sh`

# Input box examples
```
C:\data\report.pdf
C:\data\photos
"D:\a b\one.txt" "D:\a b\two.txt"
/home/me/backups
remote:/pub/releases/v1.zip
remote:/pub/docs
```

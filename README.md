# RS Uploader

A small desktop app for uploading many files to ResourceSpace at once. You pick the files,
fill in the metadata once for the whole batch (or for a selection), choose the collection, and press
Upload. It talks to the ResourceSpace API directly, so you skip the one-file-at-a-time web form.

## Download

[![Download for Windows](https://img.shields.io/badge/Download-Windows-0078D6?style=for-the-badge&logo=windows&logoColor=white)](https://github.com/dkwage/rs-uploader/releases/latest/download/RS-Uploader-Windows.zip)
[![Download for macOS](https://img.shields.io/badge/Download-macOS-000000?style=for-the-badge&logo=apple&logoColor=white)](https://github.com/dkwage/rs-uploader/releases/latest/download/RS-Uploader-macOS.zip)

The macOS build is for Apple Silicon Macs (M1 or later). Older versions are on the
[releases page](https://github.com/dkwage/rs-uploader/releases).

## Getting started

1. Unzip the download and open the app: `RS Uploader.app` on a Mac (move it to Applications
   first), `RS Uploader.exe` inside the `RS Uploader` folder on Windows. The first time, the system
   warns you because the app isn't signed:
   - **Mac:** macOS says it can't verify the app. Click Done, open System Settings > Privacy &
     Security, scroll down and click **Open Anyway**, then confirm.
   - **Windows:** Chrome may flag the download; open Downloads (Ctrl+J) and keep the file. When
     "Windows protected your PC" appears, click **More info**, then **Run anyway**.
2. The connection window opens. The URL is already filled in. Enter your GGGI email as the username.
3. Get your API key: log in to ResourceSpace, click the person icon at the top right (My account),
   then **My Profile**. Click **Show hidden property** next to API key and paste the key into the
   app. If your profile has no API key, ask your ResourceSpace administrator to enable API access
   for your account.
4. Click **Test connection**, then **Save**.

The key is kept in the macOS Keychain (Credential Manager on Windows), not in a file. On a Mac you
may be asked for your login password when the app reads it. Choose **Always Allow**, otherwise the
question comes back on every launch. You'll be asked once more after installing a new build.
Windows doesn't ask.

The first launch takes about half a minute to load the folder list and the fields for each resource
type. Both are cached, so later launches open straight away and refresh in the background.

## Uploading

1. Choose the **Resource type** (Photo, Video, Document, ...). The metadata fields change with it.
2. Drag files or folders into the middle area, or use Add files / Add folder.
3. Select files (Cmd-click, Ctrl-click on Windows, or Shift-click for several), fill in fields on the right, and press
   **Apply to selected**. **Apply to all** does the same for every file. Fields you leave empty are
   not touched, so you can apply common values first and then file-specific ones.
4. Pick a collection in the left panel. Entries with a folder icon only contain other collections;
   click them to open. You can't upload into them directly.
5. Press **Upload**.

Each file name starts with a mark: red means a required field is still missing (hover the file to see
which), green means ready, a check mark means uploaded, a cross means it failed. Upload won't start
while any file is red.

If some files fail, press Upload again. Only the failed ones are sent, and each continues from the
step where it stopped, so you won't get duplicate resources.

### Things the app fills in for you

If you leave these blank:

- **Title**: the file name without the extension.
- **Version number**: taken from the file name (`report_v2.pdf` gives `2`, `Strategy-v1.3.docx`
  gives `1.3`); `1.0` if the name has no version.
- **Year**: the current year, if ResourceSpace already has it as an option. If it doesn't (for
  example in January before an admin adds the new year), the field stays empty and the file stays red.

Whatever you type yourself always wins. Hover a file to see the values that will be sent.

### Other options

- **Presets** save the current panel values under a name, for batches you upload often.
- **Favorites**: right-click a collection to pin it to the top of the left panel.
- **New collection** creates a collection under My collections.
- **After upload** sets the workflow state. *Pending review* (default) needs an admin to approve the
  resource before others see it, *Active* publishes it immediately, *Pending submission* keeps it as
  a draft you submit later in ResourceSpace.
- **Read camera/file metadata** lets ResourceSpace read what's embedded in the file (EXIF/IPTC: date
  taken, GPS, caption, photographer). Values from the panel still take priority.

## How fields are shown

The app follows the field setup in ResourceSpace:

- Fields that only apply in some cases are hidden until they do. Project Number appears after you
  set Project related? to Yes; Asia - Country appears after Region is set to Asia.
- Some resource types have the same field twice (for example two Project Number fields on Video,
  Audio and Document). The app shows it once and writes the value to both.
- Fields that ResourceSpace marks read-only or hidden on upload aren't shown. On Video, Audio and
  Document this includes Title, so those resources get no title. That's a ResourceSpace setting.

## Where things are stored

All in `~/.gggi_uploader/`:

- `config.json`: URL, username, presets, favorites, last used type and collection, cached folder list
- `fields_cache_v2.json`: cached fields and dropdown options, safe to delete
- `logs/upload_YYYYMMDD_HHMMSS.csv`: one row per file with the new resource ID or the error

## For maintainers

Two files: `rs_api.py` holds the API client and the upload logic (standard library only), and
`app.py` holds the PySide6 interface.

Run from source:

```sh
uv run --with PySide6 --with keyring python app.py
```

Check the API logic (request signing, field rules, auto-fill, resuming a failed upload):

```sh
python3 rs_api.py
```

Releases are built by GitHub Actions (`.github/workflows/release.yml`): push a tag such as `v1.0.1`
and it builds both apps and attaches `RS-Uploader-macOS.zip` and `RS-Uploader-Windows.zip` to a
new release, which the download buttons above always point to.

Build the app locally (output in `dist/`):

```sh
uv run --with PySide6 --with keyring --with pyinstaller --with pillow \
  pyinstaller --windowed --noconfirm --name "RS Uploader" --icon icon.png --add-data "icon.png:." app.py
```

PyInstaller can't cross-compile, so a local build only produces the app for the system you run it on. Unsigned builds trigger Gatekeeper on macOS and
SmartScreen on Windows; check with IT before sharing widely.

Notes on the API:

- Every call is signed: `sign = sha256(api_key + query_string)`. File uploads use
  `upload_multipart` (POST); everything else is a GET.
- `get_resource_type_fields` needs admin rights, so for normal users the app reads the field list
  from an existing resource of that type.
- The server answers in about 0.9 s per call with no rate limit observed up to 16 parallel calls,
  so the app runs lookups 8 at a time and uploads 3 files at a time.

## License

MIT. See [LICENSE](LICENSE).

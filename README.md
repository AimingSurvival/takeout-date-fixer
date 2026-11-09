# takeout-date-fixer

Restore the dates on photos and videos exported with **Google Takeout**.

Takeout strips the capture date from many files and puts it in a separate
`.json` file next to each one. Your gallery then sorts everything by the day you
downloaded it. This script reads those JSON files and puts the real date back.

* Single file, **Python standard library only** (3.9+). Windows, macOS, Linux.
* Optional [ExifTool](https://exiftool.org) support, to write the date (and GPS) *inside* each file.
* Dry-run mode, a summary of where every date came from, and a list of files it couldn't date.

## Quick start

```bash
git clone https://github.com/<you>/takeout-date-fixer
cd takeout-date-fixer

# 1. Preview. Changes nothing (it only writes unmatched.txt into the folder).
python fix_takeout_dates.py "D:\Takeout\Google Photos" --dry-run

# 2. Fix file dates
python fix_takeout_dates.py "D:\Takeout\Google Photos"
```

Extract **all** Takeout zips into the same `Takeout` folder first. A photo and its
JSON can end up in different zips.

### Also write the date inside the files (recommended)

File dates fix Explorer/Finder. Phone and web galleries read the date stored
*inside* the file, which needs ExifTool:

```bash
python fix_takeout_dates.py "D:\Takeout\Google Photos" --exif --exiftool "D:\Tools\exiftool\exiftool.exe"
```

ExifTool on Windows needs its `exiftool_files` folder next to `exiftool.exe`
(use the full zip from exiftool.org, rename `exiftool(-k).exe` to `exiftool.exe`).
The script checks ExifTool works before starting and reports how many files it
actually updated.

### Remove the JSON files

```bash
python fix_takeout_dates.py "D:\Takeout\Google Photos" --exif --exiftool "..." --delete-json
```

`--delete-json` only deletes sidecars after a successful run, and **refuses to**
if you asked for `--exif` and ExifTool didn't write the files. Deleting is
permanent and the JSON is the only copy of GPS data, descriptions and favourites,
so keep your original Takeout zips until you've checked the result.

## Where each date comes from

In order of trust:

| # | Source | Notes |
|---|--------|-------|
| 1 | JSON next to the file | Handles `.supplemental-metadata.json`, names truncated by Google, `(1)` duplicates, `-edited` copies, Live Photo videos |
| 2 | JSON in another folder | Matched by the original filename inside the JSON; only used when unambiguous |
| 3 | EXIF already inside a JPEG | Camera's own date |
| 4 | Date in the filename | `IMG_20260213_165553`, `PXL_…`, `IMG20221211222953`, `IMG_260106_193115`, `1708145044972.jpg`, `IMG-20230101-WA0001` (no time, so 12:00) |
| 5 | `--folder-year` (opt-in) | `Photos from 2023` → 1 Jan 2023. Last resort, approximate |

Files with no source are listed in `unmatched.txt`.

Time zones: Google's JSON times are UTC and are written as UTC (`+00:00`).
Dates parsed from filenames are your camera's local time and are treated that way.

## Options

| Option | What it does |
|--------|--------------|
| `--dry-run` | Show what would happen, change nothing |
| `--exif` | Write date/GPS into files with ExifTool (found on PATH) |
| `--exiftool PATH` | Use this ExifTool (implies `--exif`) |
| `--folder-year` | Last-resort year from `Photos from YYYY` folders |
| `--no-filename-dates` | Don't fall back to dates in filenames |
| `--delete-json` | Delete used JSON sidecars when done (guarded, see above) |
| `--exif-from-mtime` | **Recovery.** JSON already deleted? Write EXIF from each file's current modified date |

## Recovery: I already deleted the JSON files

If a previous run already set the file dates, they still sit in each file's
modified time. Run:

```bash
python fix_takeout_dates.py "D:\Takeout\Google Photos" --exif-from-mtime --exiftool "D:\Tools\exiftool\exiftool.exe" --dry-run
```

then again without `--dry-run`. It skips files that already have an EXIF date and
files newer than the Takeout export (they never got a real date). GPS from deleted
JSON can't be recovered this way, so re-extract the zips if you need it.

## Limitations

* Only JPEG EXIF is *read* (to find existing dates); ExifTool *writes* whatever it supports.
  Some formats (e.g. old `.3gp`/`.avi`) can't take EXIF/QuickTime dates; those still get file dates.
* The created date (not just modified) is set on Windows; on macOS only if Xcode's `SetFile` is installed.
* Photos with no JSON, no EXIF and no date in the filename stay undated.
* Albums that duplicate a photo are handled per folder; there is no de-duplication.

## Development

```bash
python -m unittest discover -s tests -v
```

CI runs the tests on Ubuntu, Windows and macOS.

## License

MIT

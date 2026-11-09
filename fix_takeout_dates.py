#!/usr/bin/env python3
r"""
fix_takeout_dates.py - restore photo/video dates lost in a Google Takeout export.

Pure Python 3.9+ standard library. Works on Windows, macOS and Linux.
Optional: ExifTool, to also write the date (and GPS) inside the files.

Date sources, in order of trust
  1. the .json sidecar next to the file (handles ".supplemental-metadata.json",
     truncated names, "(1)" duplicates, "-edited" copies, Live Photo videos)
  2. a sidecar in another folder with the same original filename (unambiguous only)
  3. the EXIF date already inside a JPEG
  4. a date in the filename (IMG_20260213_165553, PXL_..., 1708145044972.jpg, ...)
  5. --folder-year only: January 1st of the "Photos from YYYY" folder

Usage
  python fix_takeout_dates.py "C:\Takeout\Google Photos" --dry-run
  python fix_takeout_dates.py "C:\Takeout\Google Photos" --exif --exiftool "C:\Tools\exiftool.exe"
  python fix_takeout_dates.py "C:\Takeout\Google Photos" --exif-from-mtime --exiftool ...   (recovery)

Always run with --dry-run first. --delete-json only deletes sidecars after the
run succeeded (and, with --exif, only if ExifTool really wrote the files).
"""
__version__ = "1.0.0"
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

MEDIA_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tif",
    ".tiff", ".dng", ".raw", ".arw", ".cr2", ".nef", ".orf", ".rw2",
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp", ".mts", ".wmv", ".mpg",
    ".mpeg", ".webm", ".mp",
}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp", ".mts", ".wmv",
             ".mpg", ".mpeg", ".webm", ".mp"}
SUPP = "supplemental-metadata"
DUP_RE = re.compile(r"\((\d+)\)$")


# ------------------------------------------------- date from the filename
NAME_DT = re.compile(
    r"(?<!\d)((?:19|20)\d{2})[-_]?(0[1-9]|1[0-2])[-_]?(0[1-9]|[12]\d|3[01])"
    r"(?:[-_ T]?([01]\d|2[0-3])([0-5]\d)([0-5]\d)\d{0,3})?(?!\d)")


def date_from_filename(name):
    """IMG_20260213_165553, PXL_20250825_090921099, IMG20221211222953,
    video_20260322_142155, IMG-20230101-WA0001 (date only -> 12:00)."""
    m = NAME_DT.search(name)
    if not m:
        return None
    y, mo, d, h, mi, se = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(h or 12), int(mi or 0), int(se or 0))
        ts = int(dt.timestamp())
    except (ValueError, OverflowError, OSError):
        return None
    if ts < 0 or dt > datetime.now():
        return None
    return {"ts": ts, "lat": 0.0, "lon": 0.0, "alt": 0.0, "title": None, "local": True}


YYMMDD_RE = re.compile(
    r"(?<!\d)(\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])[_-]"
    r"([01]\d|2[0-3])([0-5]\d)([0-5]\d)(?!\d)")
EPOCH_RE = re.compile(r"^(\d{13}|\d{10})$")


def date_from_filename_extra(name):
    """Epoch-style names (1708145044972.jpg) and YYMMDD_HHMMSS (IMG_260106_193115)."""
    stem = os.path.splitext(name)[0]
    now = datetime.now()
    m = EPOCH_RE.match(stem)
    if m:
        v = int(stem)
        ts = v // 1000 if len(stem) == 13 else v
        if 946684800 <= ts <= now.timestamp():          # 2000 .. now
            return {"ts": ts, "lat": 0.0, "lon": 0.0, "alt": 0.0, "title": None}
        return None
    m = YYMMDD_RE.search(stem)
    if m:
        yy, mo, d, h, mi, se = (int(x) for x in m.groups())
        try:
            dt = datetime(2000 + yy, mo, d, h, mi, se)
        except ValueError:
            return None
        if dt <= now:
            return {"ts": int(dt.timestamp()), "lat": 0.0, "lon": 0.0, "alt": 0.0,
                    "title": None, "local": True}
    return None


def _exif_from_tiff(t):
    if len(t) < 8 or t[:2] not in (b"II", b"MM"):
        return None
    bo = "little" if t[:2] == b"II" else "big"
    rd = lambda o, n: int.from_bytes(t[o:o + n], bo)

    def ifd(off):
        out = {}
        if off + 2 > len(t):
            return out
        for k in range(rd(off, 2)):
            e = off + 2 + k * 12
            if e + 12 > len(t):
                break
            tag, typ, cnt = rd(e, 2), rd(e + 2, 2), rd(e + 4, 4)
            if typ == 2 and cnt >= 19:                      # ASCII date string
                vo = rd(e + 8, 4)
                out[tag] = t[vo:vo + 19].decode("ascii", "ignore")
            elif typ == 4 and cnt == 1:
                out[tag] = rd(e + 8, 4)
        return out

    i0 = ifd(rd(4, 4))
    ex = ifd(i0[0x8769]) if isinstance(i0.get(0x8769), int) else {}
    for val in (ex.get(0x9003), ex.get(0x9004), i0.get(0x0132)):
        if isinstance(val, str) and not val.startswith("0000"):
            try:
                dt = datetime.strptime(val, "%Y:%m:%d %H:%M:%S")
                return int(dt.timestamp())
            except ValueError:
                pass
    return None


def read_exif_date(path):
    """Date already stored inside a JPEG (stdlib-only EXIF reader)."""
    if os.path.splitext(path)[1].lower() not in (".jpg", ".jpeg"):
        return None
    try:
        with open(path, "rb") as f:
            data = f.read(262144)
    except OSError:
        return None
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            break
        if data[i + 1] == 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xDA:
            break
        seglen = int.from_bytes(data[i + 2:i + 4], "big")
        if marker == 0xE1 and data[i + 4:i + 10] == b"Exif\x00\x00":
            ts = _exif_from_tiff(data[i + 10:i + 2 + seglen])
            if ts:
                return {"ts": ts, "lat": 0.0, "lon": 0.0, "alt": 0.0,
                        "title": None, "local": True, "from_exif": True}
            return None
        i += 2 + seglen
    return None


FOLDER_YEAR_RE = re.compile(r"^Photos from (\d{4})$")


def date_from_folder_year(folder):
    m = FOLDER_YEAR_RE.match(os.path.basename(folder.rstrip("\\/")))
    if not m:
        return None
    dt = datetime(int(m.group(1)), 1, 1, 12, 0, 0)
    return {"ts": int(dt.timestamp()), "lat": 0.0, "lon": 0.0, "alt": 0.0,
            "title": None, "local": True}


def show(path):
    return path[4:] if path.startswith("\\\\?\\") else path


# ----------------------------------------------------------------- matching
def split_dup(name):
    """'photo(1).jpg' -> ('photo.jpg', '(1)'); 'photo.jpg' -> ('photo.jpg', '')."""
    stem, ext = os.path.splitext(name)
    m = DUP_RE.search(stem)
    if m:
        return stem[:m.start()] + ext, m.group(0)
    return name, ""


def normalise_key(json_name):
    """Strip '.json' and a trailing '(n)'; return (key, dup)."""
    key = json_name[:-5]
    m = DUP_RE.search(key)
    dup = ""
    if m:
        dup = m.group(0)
        key = key[:m.start()]
    return key, dup


def key_matches(key, media):
    """Does a JSON key (e.g. 'a.jpg.supplemental-metadat') belong to media 'a.jpg'?"""
    if key == media:
        return True
    if key.startswith(media + "."):
        rest = key[len(media) + 1:]
        return rest != "" and SUPP.startswith(rest)
    # Google truncates long names: the key can be a prefix of the media name
    if len(key) >= 30 and media.startswith(key):
        return True
    return False


def candidates_for(media_name):
    """Names to try for a media file (original, without -edited)."""
    names = [media_name]
    stem, ext = os.path.splitext(media_name)
    for tag in ("-edited", "-bearbeitet", "-modifié", "-editado", "-modificato"):
        if stem.endswith(tag):
            names.append(stem[:-len(tag)] + ext)
    return names


def find_json(media_name, json_index, title_index):
    """json_index: list of (key, dup, filename). title_index: title -> filename."""
    for cand in candidates_for(media_name):
        base, dup = split_dup(cand)
        for key, jdup, jname in json_index:
            if jdup == dup and key_matches(key, base):
                return jname
        if not dup and cand in title_index:
            return title_index[cand]
    # Live Photo / motion video: same stem as an image's json
    stem, ext = os.path.splitext(media_name)
    if ext.lower() in VIDEO_EXT:
        for other in (".jpg", ".jpeg", ".heic", ".heif", ".png", ".JPG", ".HEIC"):
            base, dup = split_dup(stem + other)
            for key, jdup, jname in json_index:
                if jdup == dup and key_matches(key, base):
                    return jname
    return None


# ------------------------------------------------------------- timestamps
def read_meta(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    ts = None
    for field in ("photoTakenTime", "creationTime"):
        try:
            ts = int(data[field]["timestamp"])
            if ts > 0:
                break
        except (KeyError, TypeError, ValueError):
            ts = None
    if ts is None:
        return None
    geo = data.get("geoData") or {}
    lat, lon = geo.get("latitude", 0.0), geo.get("longitude", 0.0)
    return {"ts": ts, "lat": lat, "lon": lon, "alt": geo.get("altitude", 0.0),
            "title": data.get("title")}


def build_global_index(root):
    """title -> {'ts': set of timestamps, 'meta': meta}, across ALL folders."""
    idx = {}
    for folder, _d, files in os.walk(root):
        for f in files:
            if not f.lower().endswith(".json"):
                continue
            m = read_meta(os.path.join(folder, f))
            if m and m["title"]:
                e = idx.setdefault(m["title"], {"ts": set(), "meta": m})
                e["ts"].add(m["ts"])
    return idx


def global_meta(name, gindex):
    """Find a JSON in ANY folder by the original title. Only used when
    unambiguous (every JSON with that title has the same timestamp)."""
    for cand in candidates_for(name):
        base, dup = split_dup(cand)
        if dup:
            continue
        e = gindex.get(cand)
        if e and len(e["ts"]) == 1:
            return e["meta"]
    return None


def set_creation_time(path, ts):
    """Best-effort 'date created'. Windows: ctypes. macOS: SetFile if present."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype = wintypes.HANDLE
        handle = k32.CreateFileW(path, 0x100, 0, None, 3, 0x80, None)  # FILE_WRITE_ATTRIBUTES
        if handle in (wintypes.HANDLE(-1).value, None, -1):
            return
        t = int((ts + 11644473600) * 10_000_000)
        ft = wintypes.FILETIME(t & 0xFFFFFFFF, t >> 32)
        k32.SetFileTime(handle, ctypes.byref(ft), ctypes.byref(ft), ctypes.byref(ft))
        k32.CloseHandle(handle)
    elif sys.platform == "darwin" and shutil.which("SetFile"):
        d = datetime.fromtimestamp(ts).strftime("%m/%d/%Y %H:%M:%S")
        subprocess.run(["SetFile", "-d", d, path], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def apply_fs_time(path, ts):
    set_creation_time(path, ts)          # first, so mtime isn't disturbed
    os.utime(path, (ts, ts))


# ------------------------------------------------------------------ exiftool
def exif_args(path, meta):
    ext = os.path.splitext(path)[1].lower()
    if meta.get("local") and ext not in VIDEO_EXT:
        dt = datetime.fromtimestamp(meta["ts"]).strftime("%Y:%m:%d %H:%M:%S")
        return [f"-AllDates={dt}"]
    dt = datetime.fromtimestamp(meta["ts"], tz=timezone.utc).strftime("%Y:%m:%d %H:%M:%S")
    args = []
    if ext in VIDEO_EXT:
        args += [f"-QuickTime:CreateDate={dt}", f"-QuickTime:ModifyDate={dt}",
                 f"-QuickTime:MediaCreateDate={dt}", f"-QuickTime:TrackCreateDate={dt}"]
    else:
        args += [f"-AllDates={dt}", "-OffsetTimeOriginal=+00:00"]
    if meta["lat"] or meta["lon"]:
        args += [f"-GPSLatitude={abs(meta['lat'])}",
                 f"-GPSLatitudeRef={'N' if meta['lat'] >= 0 else 'S'}",
                 f"-GPSLongitude={abs(meta['lon'])}",
                 f"-GPSLongitudeRef={'E' if meta['lon'] >= 0 else 'W'}"]
    return args


EXIFTOOL = "exiftool"


def exiftool_works():
    try:
        r = subprocess.run([EXIFTOOL, "-ver"], capture_output=True, text=True)
    except OSError as e:
        print(f"! Could not start exiftool: {e}")
        return False
    if r.returncode != 0 or not r.stdout.strip():
        print("! exiftool did not run correctly:")
        print("  " + (r.stderr or r.stdout).strip())
        return False
    print(f"exiftool {r.stdout.strip()} OK")
    return True


def run_exiftool(jobs):
    """jobs: list of (path, meta). One exiftool process, argfile-driven."""
    with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False, encoding="utf-8") as af:
        for path, meta in jobs:
            for a in exif_args(path, meta):
                af.write(a + "\n")
            af.write("-overwrite_original\n-P\n-m\n")
            af.write(show(path) + "\n-execute\n")
        argfile = af.name
    try:
        r = subprocess.run([EXIFTOOL, "-charset", "filename=utf8", "-@", argfile],
                           capture_output=True, text=True, errors="replace")
    finally:
        os.unlink(argfile)
    updated = sum(int(n) for n in re.findall(r"(\d+) (?:image |video )?files? updated", r.stdout))
    errors = len(re.findall(r"^Error", r.stdout + r.stderr, re.M))
    print(f"ExifTool updated {updated} of {len(jobs)} files ({errors} errors).")
    if updated == 0:
        print("! Nothing was written. Output was:\n" + (r.stderr or r.stdout)[:600])
    return updated


# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Restore dates on Google Takeout media.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("root", help="Takeout folder (e.g. the 'Google Photos' folder)")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    ap.add_argument("--exif", action="store_true", help="also write EXIF/GPS using exiftool (if installed)")
    ap.add_argument("--no-filename-dates", action="store_true",
                    help="don't fall back to dates found in filenames when no JSON matches")
    ap.add_argument("--folder-year", action="store_true",
                    help="LAST RESORT: files in 'Photos from YYYY' with no other date get YYYY-01-01")
    ap.add_argument("--exiftool", default=None, metavar="PATH",
                    help="full path to exiftool.exe if it is not on PATH")
    ap.add_argument("--exif-from-mtime", action="store_true",
                    help="RECOVERY: JSON already deleted. Write EXIF using each file's current "
                         "modified date (set by an earlier run). Needs exiftool.")
    ap.add_argument("--delete-json", action="store_true", help="delete matched JSON sidecars when done")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        sys.exit(f"Not a folder: {root}")
    if os.name == "nt" and not root.startswith("\\\\?\\"):
        root = "\\\\?\\" + root     # lets Windows open folders like "Family " (trailing space)

    global EXIFTOOL
    exif_wanted = args.exif or bool(args.exiftool) or args.exif_from_mtime
    exif_ok = True
    use_exif = exif_wanted
    if args.exiftool:
        EXIFTOOL = os.path.abspath(args.exiftool)
    if use_exif and not shutil.which(EXIFTOOL):
        print("! exiftool not found on PATH - skipping EXIF writing "
              "(file dates will still be fixed).")
        use_exif = False
    elif use_exif and not exiftool_works():
        print("! Skipping EXIF writing (file dates will still be fixed).")
        use_exif = False

    if args.exif_from_mtime:
        if not use_exif:
            sys.exit("ExifTool is required for --exif-from-mtime (see messages above).")
        cutoff = None
        m = re.search(r"takeout-(\d{8})T(\d{6})Z", root, re.I)
        if m:   # files newer than the export itself never got a real date
            cutoff = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S") \
                .replace(tzinfo=timezone.utc).timestamp()
        jobs, undated, has_exif = [], 0, 0
        for folder, _d, files in os.walk(root):
            for f in files:
                if os.path.splitext(f)[1].lower() not in MEDIA_EXT:
                    continue
                path = os.path.join(folder, f)
                try:
                    mt = int(os.path.getmtime(path))
                except OSError:
                    continue
                if cutoff and mt >= cutoff:
                    undated += 1
                    continue
                if read_exif_date(path):
                    has_exif += 1
                    continue
                meta = {"ts": mt, "lat": 0.0, "lon": 0.0, "alt": 0.0, "title": None}
                nm = date_from_filename(f) or date_from_filename_extra(f)
                if nm and nm.get("local") and abs(nm["ts"] - mt) <= 1:
                    meta["local"] = True        # date came from the filename (local time)
                jobs.append((path, meta))
        print(f"To write: {len(jobs)} | already have EXIF date: {has_exif} | "
              f"skipped (no real date): {undated}")
        if args.dry_run:
            print("(dry run - nothing written)")
            return
        if jobs:
            run_exiftool(jobs)
            for path, meta in jobs:
                apply_fs_time(path, meta["ts"])
        return

    print("Indexing JSON files...")
    gindex = build_global_index(root)
    counts = {}
    total = fixed = 0
    unmatched, exif_jobs, used_json = [], [], []
    json_total = json_orphans = 0

    for folder, _dirs, files in os.walk(root):
        json_index, claimed = [], set()
        title_index = {}
        for f in files:
            if f.lower().endswith(".json"):
                key, dup = normalise_key(f)
                json_index.append((key, dup, f))
                m = read_meta(os.path.join(folder, f))
                if m and m["title"] and not dup:
                    title_index.setdefault(m["title"], f)

        for f in files:
            if os.path.splitext(f)[1].lower() not in MEDIA_EXT:
                continue
            total += 1
            path = os.path.join(folder, f)
            jname = find_json(f, json_index, title_index)
            meta = read_meta(os.path.join(folder, jname)) if jname else None
            source = "json"
            if jname and meta:
                claimed.add(jname)
            else:
                meta, source = global_meta(f, gindex), "json-other-folder"
                if not meta:
                    meta, source = read_exif_date(path), "exif-in-file"
                if not meta and not args.no_filename_dates:
                    meta, source = date_from_filename(f), "filename"
                    if not meta:
                        meta = date_from_filename_extra(f)
                if not meta and args.folder_year:
                    meta, source = date_from_folder_year(folder), "folder-year"
            if not meta:
                unmatched.append(show(path))
                continue
            fixed += 1
            counts[source] = counts.get(source, 0) + 1
            if source == "json":
                used_json.append(os.path.join(folder, jname))
            when = datetime.fromtimestamp(meta["ts"]).strftime("%Y-%m-%d %H:%M:%S")
            if args.dry_run:
                print(f"[dry:{source}] {show(path)}  ->  {when}")
                continue
            apply_fs_time(path, meta["ts"])
            if use_exif and source != "exif-in-file":
                exif_jobs.append((path, meta))

        meta_json = [j for _k, _d, j in json_index if j != "metadata.json"]
        json_total += len(meta_json)
        json_orphans += len([j for j in meta_json if j not in claimed])

    if exif_jobs and not args.dry_run:
        print(f"Writing EXIF for {len(exif_jobs)} files...")
        exif_updated = run_exiftool(exif_jobs)
        if exif_updated < 0.9 * len(exif_jobs):
            exif_ok = False
        # exiftool with -P keeps mtime, but re-apply to be safe
        for path, meta in exif_jobs:
            apply_fs_time(path, meta["ts"])

    if exif_wanted and not use_exif:
        exif_ok = False                      # ExifTool was requested but unusable
    if args.delete_json and not args.dry_run:
        if not exif_ok:
            print("\n! JSON files NOT deleted: ExifTool did not write the dates into your "
                  "files, and the sidecars are the only copy of that data. Fix ExifTool and "
                  "run again.")
        else:
            removed = 0
            for j in set(used_json):
                try:
                    os.remove(j)
                    removed += 1
                except OSError:
                    pass
            print(f"\nDeleted {removed} JSON sidecars.")

    if unmatched:
        with open(os.path.join(show(root), "unmatched.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(unmatched))

    print(f"\nMedia files found      : {total}")
    print(f"Dates restored         : {fixed}{' (dry run)' if args.dry_run else ''}")
    for k in ("json", "json-other-folder", "exif-in-file", "filename", "folder-year"):
        if counts.get(k):
            print(f"   from {k:<18}: {counts[k]}")
    print(f"Still no date source   : {len(unmatched)}"
          + ("  (listed in unmatched.txt)" if unmatched else ""))
    print(f"JSON files / unused    : {json_total} / {json_orphans}")
    if unmatched and json_total < total * 0.8:
        print("\nNote: there are far fewer JSON files than media files. If your export "
              "was split into several zips (-001, -002, ...), extract ALL of them into "
              "the same Takeout folder and run again.")


if __name__ == "__main__":
    main()

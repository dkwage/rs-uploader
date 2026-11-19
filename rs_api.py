"""ResourceSpace API client + upload logic for the uploader app. Stdlib only.

Signing: sign = sha256(private_key + urlencoded query). Normal calls are GET
(same as scripts/resourcespace_check.py); upload_multipart is a POST
multipart/form-data with fields query, sign, user, file (file is streamed, not loaded in memory).

Self-check: python3 rs_api.py
"""
import hashlib, json, os, re, urllib.error, urllib.parse, urllib.request, uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

FIXED_LIST = {2, 3, 7, 9, 12}  # checkbox list, dropdown, category tree, dynamic keywords, radio
SINGLE_SELECT = {3, 12}         # dropdown, radio
DATE_TYPES = {4, 6, 10}
SKIP_TYPES = {13}               # warning message (display only)
ARCHIVE_STATES = [  # (value, label, tooltip)
    (-1, "Pending review (needs approval)", "Uploaded, but an admin must approve it before others can see it."),
    (0, "Active (live now)", "Public to RS users and searchable immediately."),
    (-2, "Pending submission (draft)", "Saved as a draft. You submit it for review later in ResourceSpace."),
]
DONE = 3                        # item["stage"]: 1 file uploaded, 2 metadata set, 3 in collection


class RSError(Exception):
    pass


class Client:
    def __init__(self, url, user, key, timeout=60):  # per socket read, not per whole upload
        base = url.strip().rstrip("/")
        self.base = (base[:-4] if base.endswith("/api") else base) + "/api/"  # accept site URL or .../api/
        self.user, self.key, self.timeout = user.strip(), key.strip(), timeout

    def _signed(self, function, params):
        query = urllib.parse.urlencode({"user": self.user, "function": function, **params})
        return query, hashlib.sha256((self.key + query).encode()).hexdigest()

    def call(self, function, **params):
        query, sign = self._signed(function, params)
        return self._open(urllib.request.Request(f"{self.base}?{query}&sign={sign}"))

    def upload(self, ref, path, no_exif=True):
        query, sign = self._signed("upload_multipart", {"ref": ref, "no_exif": int(no_exif), "revert": 0})
        boundary = uuid.uuid4().hex
        head, tail = multipart_parts(boundary, {"query": query, "sign": sign, "user": self.user}, Path(path).name)

        def body():
            yield head
            with open(path, "rb") as f:
                while chunk := f.read(1 << 20):
                    yield chunk
            yield tail

        size = len(head) + os.path.getsize(path) + len(tail)
        self._open(urllib.request.Request(self.base, data=body(), method="POST", headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(size)}))

    def _open(self, req):
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                text = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise RSError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None
        except urllib.error.URLError as e:
            raise RSError(f"connection error: {e.reason}") from None
        if not text.strip():
            return None  # upload_multipart answers 204 No Content
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise RSError(f"unexpected response: {text[:300]}") from None
        if isinstance(data, dict) and data.get("status") == "fail":
            msg = data.get("data")
            raise RSError(str(msg.get("message", msg) if isinstance(msg, dict) else msg))
        return data

    # --- lookups for the UI ---
    def resource_types(self):
        return [(int(t["ref"]), t["name"]) for t in self.call("get_resource_types") or []]

    def fields(self, restype):
        try:
            rows = self.call("get_resource_type_fields", by_resource_types=restype)
        except RSError:  # needs admin permission "a" (RS 10.3+): read the fields off an existing resource instead
            hits = self.call("do_search", search="", restypes=restype, fetchrows=1)
            hits = hits.get("data", []) if isinstance(hits, dict) else hits
            if not hits:
                raise RSError("Cannot read fields: no admin permission and no existing resource of this type.")
            rows = self.call("get_resource_field_data", resource=hits[0]["ref"])
        return clean_fields(rows)

    def options(self, field):
        nodes = self.call("get_nodes", ref=field, recursive=1) or []
        by_ref = {int(n["ref"]): n for n in nodes}

        def label(n):  # category trees: "Parent / Child"
            parts, seen = [n["name"]], set()
            while n.get("parent") and int(n["parent"]) in by_ref and int(n["parent"]) not in seen:
                seen.add(int(n["parent"]))
                n = by_ref[int(n["parent"])]
                parts.insert(0, n["name"])
            return " / ".join(parts)

        return [(r, label(n)) for r, n in by_ref.items()]

    def schema(self, restype):
        """Fields + fixed-list options for one resource type, JSON-ready (cached to disk by the app).
        Options load 8 at a time: ~5 s for a 20-dropdown type instead of ~20 s."""
        fields = self.fields(restype)
        fixed = [f["ref"] for f in fields if f["type"] in FIXED_LIST]
        with ThreadPoolExecutor(8) as ex:
            opts = dict(zip(fixed, ex.map(self.options, fixed)))
        return {"fields": fields, "options": {str(k): [list(o) for o in v] for k, v in opts.items()}}

    def destinations(self):
        """User collections (kind "mine") + the whole featured tree (kind "folder") as dicts
        {ref, name, parent, kind, category}. category = has sub-folders, so not an upload target.
        Featured tree is read level by level, 8 calls in parallel (GGGI: ~90 calls, ~20 s for ~590 folders)."""
        out = [{"ref": int(c["ref"]), "name": c["name"], "parent": None, "kind": "mine", "category": False}
               for c in self.call("get_user_collections") or []]
        seen, level = set(), [0]
        try:
            with ThreadPoolExecutor(8) as ex:
                for _ in range(10):  # depth cap guards against a parent loop
                    if not level:
                        break
                    res = list(ex.map(lambda p: (p, self.call("get_featured_collections", parent=p) or []), level))
                    level = []
                    for parent, kids in res:
                        for k in kids:
                            ref = int(k["ref"])
                            if ref in seen:
                                continue
                            seen.add(ref)
                            cat = str(k.get("has_children", 0)) not in ("0", "False", "false", "")
                            out.append({"ref": ref, "name": k["name"], "parent": int(parent) or None,
                                        "kind": "folder", "category": cat})
                            if cat:
                                level.append(ref)
        except RSError:
            pass  # RS older than 10.3 has no get_featured_collections
        return out


def dest_path(by_ref, ref):
    """'Events › 2026 › COP31' for a folder, 'My › name' for a user collection."""
    if ref is None:
        return "No collection"
    d, parts, seen = by_ref.get(ref), [], set()
    if d and d["kind"] == "mine":
        return f"My › {d['name']}"
    while d and d["ref"] not in seen:
        seen.add(d["ref"])
        parts.insert(0, d["name"])
        d = by_ref.get(d["parent"]) if d["parent"] else None
    return " › ".join(parts) or str(ref)


def multipart_parts(boundary, fields, filename):
    b = f"--{boundary}\r\n"
    head = "".join(f'{b}Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n' for k, v in fields.items())
    safe = filename.replace('"', "'").replace("\r", " ").replace("\n", " ")
    head += f'{b}Content-Disposition: form-data; name="file"; filename="{safe}"\r\n' \
            "Content-Type: application/octet-stream\r\n\r\n"
    return head.encode(), f"\r\n--{boundary}--\r\n".encode()


def clean_fields(rows):
    """Fields as the upload form needs them. Skips inactive, read-only and hide-on-upload fields.
    cond = RS display_condition ("region=Asia", "a=X|Y;b=Z"). twin_of = ref of an earlier field with the
    same title/type/condition (RS has copies like projectnumber + projectnumbercopy): shown once, value copied."""
    out, seen = [], {}
    for f in rows or []:
        ftype = int(f.get("type") or 0)
        if str(f.get("active", 1)) == "0" or str(f.get("read_only", 0)) == "1" or ftype in SKIP_TYPES \
                or str(f.get("hide_when_uploading", 0)) == "1":
            continue
        out.append({"ref": int(f.get("fref") or f["ref"]),  # get_resource_field_data names the field id "fref"
                    "name": f.get("name") or "", "title": f.get("title") or f.get("name") or "",
                    "type": ftype, "required": str(f.get("required")) == "1",
                    "order": int(f.get("order_by") or 0), "cond": f.get("display_condition") or ""})
    out.sort(key=lambda x: x["order"])
    for f in out:
        key = (f["title"].strip().lower(), f["type"] in FIXED_LIST, f["cond"])
        f["twin_of"] = seen.get(key)
        seen.setdefault(key, f["ref"])
    return out


def _labels(f, v, options):
    if isinstance(v, list):
        names = dict((int(i), n) for i, n in options.get(str(f["ref"]), []))
        return {str(names.get(int(i), i)).lower() for i in v}
    return {str(v).lower()} if v else set()


def shown(f, meta, fields, options, depth=0):
    """RS display condition: every clause name=A|B must match a value of field `name`."""
    if not f.get("cond") or depth > 5:
        return True
    by_name = {x["name"]: x for x in fields}
    for clause in f["cond"].split(";"):
        name, _, want = clause.partition("=")
        src = by_name.get(name.strip())
        if not src:
            continue  # condition on a field we don't have: don't hide
        have = _labels(src, meta.get(src["ref"]), options)
        if not have & {w.strip().lower() for w in want.split("|")} or not shown(src, meta, fields, options, depth + 1):
            return False
    return True


VERSION_RE = re.compile(r"(?i)(?:^|[\s_.(-])v(?:er(?:sion)?)?[\s_.-]?(\d+(?:\.\d+){0,2})(?=$|[\s_.)-])")


def auto_kind(f):
    t = f["title"].strip().lower()
    if f["type"] not in FIXED_LIST and t == "title":
        return "title"
    if f["type"] not in FIXED_LIST and "version" in t:
        return "version"
    if t == "year" and f["type"] not in DATE_TYPES:  # date fields want YYYY-MM-DD; don't guess a day
        return "year"


def auto_hint(f):
    """Placeholder text for fields the app fills when left blank."""
    return {"title": "Auto: file name", "version": "Auto: from file name (e.g. _v2) or 1.0",
            "year": f"Auto: {date.today().year}"}.get(auto_kind(f), "")


def auto_value(f, path, options):
    kind, stem = auto_kind(f), Path(path).stem
    if kind == "title":
        return stem
    if kind == "version":
        m = VERSION_RE.search(stem)
        return m.group(1) if m else "1.0"
    if kind == "year":
        y = str(date.today().year)
        if f["type"] in FIXED_LIST:  # only if RS has this year as an option; else stays blank (red badge)
            return [int(i) for i, n in options.get(str(f["ref"]), []) if str(n).strip() == y][:1] or None
        return y


def effective(fields, meta, path, options):
    """Metadata that will be sent: blank Title/Version/Year auto-filled (auto_value), hidden-by-condition
    fields dropped, twin fields get the primary's value (fixed lists matched by option name)."""
    out = dict(meta)
    for f in fields:
        if not out.get(f["ref"]) and not f["twin_of"]:
            v = auto_value(f, path, options)
            if v:
                out[f["ref"]] = v
    for f in fields:
        if f["twin_of"]:
            v = out.get(f["twin_of"])
            if isinstance(v, list):
                src = next(x for x in fields if x["ref"] == f["twin_of"])
                want = _labels(src, v, options)
                v = [int(i) for i, n in options.get(str(f["ref"]), []) if str(n).lower() in want]
            out[f["ref"]] = v
    return {f["ref"]: out[f["ref"]] for f in fields
            if out.get(f["ref"]) and shown(f, out, fields, options)}


def missing_required(fields, meta, path, options):
    eff = effective(fields, meta, path, options)
    return [f["title"] for f in fields if f["required"] and not f["twin_of"]
            and shown(f, meta, fields, options) and not eff.get(f["ref"])]


def _ok(result, what):
    if result is False:
        raise RSError(f"{what} returned false (permission?)")


def upload_item(c, item, fields, options, restype, archive, collection, no_exif):
    """item = {"path", "meta", "ref"?, "stage"?}. Mutated as it goes, so a retry resumes at the failed step
    instead of creating a duplicate resource."""
    if not item.get("ref"):
        ref = c.call("create_resource", resource_type=restype, archive=archive)
        if not str(ref).isdigit():
            raise RSError(f"create_resource returned {ref!r}")
        item["ref"], item["stage"] = int(ref), 0
    ref = item["ref"]
    if item["stage"] < 1:
        c.upload(ref, item["path"], no_exif)
        item["stage"] = 1
    if item["stage"] < 2:  # after the file, so user values win over embedded EXIF/IPTC
        meta, nodes = effective(fields, item["meta"], item["path"], options), []
        for f in fields:
            v = meta.get(f["ref"])
            if not v:
                continue
            if f["type"] in FIXED_LIST:
                nodes += v
            else:
                _ok(c.call("update_field", resource=ref, field=f["ref"], value=v), f"update_field {f['title']}")
        if nodes:
            _ok(c.call("add_resource_nodes", resource=ref, nodestring=",".join(map(str, nodes))), "add_resource_nodes")
        item["stage"] = 2
    if item["stage"] < DONE and collection:
        _ok(c.call("add_resource_to_collection", resource=ref, collection=collection), "add_resource_to_collection")
    item["stage"] = DONE


if __name__ == "__main__":
    c = Client("https://dam.example.org/", "u", "k")
    q, s = c._signed("get_nodes", {"ref": 5})
    assert c.base == "https://dam.example.org/api/" and q == "user=u&function=get_nodes&ref=5"
    assert Client("https://dam.example.org/api/", "u", "k").base == c.base
    assert s == hashlib.sha256(b"k" + q.encode()).hexdigest()
    head, tail = multipart_parts("B", {"query": q, "sign": s, "user": "u"}, 'a"b.jpg')
    assert b'name="query"\r\n\r\nuser=u&function=get_nodes&ref=5\r\n' in head and b"filename=\"a'b.jpg\"" in head
    assert tail == b"\r\n--B--\r\n"

    fields = clean_fields([
        {"ref": 8, "name": "title", "title": "Title", "type": 0, "required": 1, "order_by": 1},
        {"fref": 12, "ref": 999, "name": "country", "title": "Country", "type": 3, "required": "1", "order_by": 2},
        {"ref": 13, "name": "note", "title": "Note", "type": 1, "required": 0, "read_only": 1},
        {"ref": 14, "name": "kw", "title": "Keywords", "type": 2, "required": 0, "order_by": 3},
    ])
    assert [f["ref"] for f in fields] == [8, 12, 14]
    assert missing_required(fields, {}, "/x/IMG_1.jpg", {}) == ["Country"]  # title falls back to file name
    assert missing_required(fields, {12: [7]}, "/x/IMG_1.jpg", {}) == []

    # display conditions + twin fields (GGGI: projectnumber / projectnumbercopy shown when projectrelated=Yes)
    cf = clean_fields([
        {"ref": 98, "name": "projectrelated", "title": "Project related?", "type": 2, "required": 1, "order_by": 1},
        {"ref": 99, "name": "projectnumber", "title": "Project Number", "type": 9, "required": 1, "order_by": 2,
         "display_condition": "projectrelated=Yes"},
        {"ref": 116, "name": "projectnumbercopy", "title": "Project Number", "type": 9, "required": 1,
         "order_by": 3, "display_condition": "projectrelated=Yes"},
        {"ref": 50, "name": "filepath", "title": "File path", "type": 0, "hide_when_uploading": 1},
        {"ref": 60, "name": "note", "title": "Note", "type": 0, "required": 1, "order_by": 4,
         "display_condition": "projectrelated=No|Maybe"},
    ])
    opts = {"98": [[387, "Yes"], [394, "No"]], "99": [[1, "P-001"], [2, "P-002"]], "116": [[71, "P-002"]]}
    assert [(f["ref"], f["twin_of"]) for f in cf] == [(98, None), (99, None), (116, 99), (60, None)]
    assert missing_required(cf, {}, "/x/a.pdf", opts) == ["Project related?"]
    assert missing_required(cf, {98: [387]}, "/x/a.pdf", opts) == ["Project Number"]  # twin not listed twice
    assert missing_required(cf, {98: [394]}, "/x/a.pdf", opts) == ["Note"]
    eff = effective(cf, {98: [387], 99: [1, 2], 60: "dropped"}, "/x/a.pdf", opts)
    assert eff == {98: [387], 99: [1, 2], 116: [71]}, eff  # twin mapped by name; hidden Note dropped

    tree = {d["ref"]: d for d in [
        {"ref": 1, "name": "Events", "parent": None, "kind": "folder", "category": True},
        {"ref": 2, "name": "2026", "parent": 1, "kind": "folder", "category": True},
        {"ref": 3, "name": "COP31 / Day 1", "parent": 2, "kind": "folder", "category": False},
        {"ref": 9, "name": "Mine", "parent": None, "kind": "mine", "category": False}]}
    assert dest_path(tree, 3) == "Events › 2026 › COP31 / Day 1"
    assert dest_path(tree, 9) == "My › Mine" and dest_path(tree, None) == "No collection"

    class Fake:
        def __init__(self, fail_at=None):
            self.log, self.fail_at = [], fail_at

        def call(self, fn, **p):
            self.log.append(fn)
            if fn == self.fail_at:
                raise RSError("boom")
            return 101 if fn == "create_resource" else True

        def upload(self, ref, path, no_exif):
            self.log.append("upload")

    item = {"path": "/x/IMG_1.jpg", "meta": {12: [7], 14: [3, 4]}}
    f1 = Fake(fail_at="add_resource_to_collection")
    try:
        upload_item(f1, item, fields, {}, 1, -1, 55, True)
    except RSError:
        pass
    assert f1.log == ["create_resource", "upload", "update_field", "add_resource_nodes", "add_resource_to_collection"]
    assert item["ref"] == 101 and item["stage"] == 2
    f2 = Fake()
    upload_item(f2, item, fields, {}, 1, -1, 55, True)  # retry resumes, no duplicate resource
    assert f2.log == ["add_resource_to_collection"] and item["stage"] == DONE
    vf = {"ref": 121, "title": "Version number", "type": 0}
    for name, want in [("report_v2.pdf", "2"), ("Strategy-v1.3.docx", "1.3"), ("Plan version 2.pdf", "2"),
                       ("COP31_V2_final.pdf", "2"), ("Avocado.pdf", "1.0"), ("dev2.pdf", "1.0"),
                       ("brief_ver.3.pdf", "3"), ("report_v2.0.pdf", "2.0")]:
        assert auto_value(vf, "/x/" + name, {}) == want, (name, auto_value(vf, "/x/" + name, {}))
    yf = {"ref": 120, "title": "Year", "type": 3}
    y = date.today().year
    assert auto_value(yf, "/x/a.pdf", {"120": [[5, str(y - 1)], [6, str(y)]]}) == [6]
    assert auto_value(yf, "/x/a.pdf", {"120": [[5, "2005"]]}) is None  # year missing in RS: stays blank
    assert auto_kind({"title": "Year", "type": 4}) is None  # Photo "Year" is a date field: no auto value
    assert auto_hint(vf).startswith("Auto:") and auto_hint({"title": "Region", "type": 3}) == ""
    print("rs_api self-check OK")

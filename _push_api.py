"""Push the PagesLinker repo content to GitHub via the REST API (contents endpoint).

Reads the token from env GITHUB_TOKEN. Creates blobs -> tree -> commit -> ref,
which is more efficient than per-file contents PUT for many files.
"""
import base64
import json
import os
import sys
import urllib.request

TOKEN = os.environ["GITHUB_TOKEN"]
REPO = "zilanLY/PagesLinker"
API = f"https://api.github.com/repos/{REPO}"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "pageslinker-push",
    "Content-Type": "application/json",
}


def call(method: str, url: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            txt = r.read().decode()
            return r.status, (json.loads(txt) if txt else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def collect_files(root: str):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", "build", "dist", "selftest_work", "output")]
        for fn in filenames:
            if fn.endswith((".pyc", ".spec")):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            out.append((rel, full))
    return sorted(out)


def main() -> int:
    root = os.path.dirname(os.path.abspath(__file__))
    files = collect_files(root)
    print("files to push:", [f[0] for f in files])

    # get current head commit (if any)
    status, head = call("GET", f"{API}/git/ref/heads/main")
    parent_sha = None
    if status == 200:
        parent_sha = head["object"]["sha"]
        print("parent commit:", parent_sha)
    else:
        print("no main ref yet (fresh repo):", status)

    # create blobs
    tree_items = []
    for rel, full in files:
        with open(full, "rb") as f:
            content = base64.b64encode(f.read()).decode()
        st, blob = call("POST", f"{API}/git/blobs", {"content": content, "encoding": "base64"})
        if st != 201:
            print("BLOB FAIL", rel, st, blob)
            return 1
        tree_items.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        print("blob ok:", rel)

    # create tree
    body = {"tree": tree_items}
    if parent_sha:
        body["base_tree"] = call("GET", f"{API}/git/commits/{parent_sha}")[1]["tree"]["sha"]
    st, tree = call("POST", f"{API}/git/trees", body)
    if st != 201:
        print("TREE FAIL", st, tree)
        return 1
    print("tree:", tree["sha"])

    # create commit
    cbody = {"message": "PagesLinker: static site builder with chunked reassembly pages, GUI, selftest, CI",
             "tree": tree["sha"]}
    if parent_sha:
        cbody["parents"] = [parent_sha]
    st, commit = call("POST", f"{API}/git/commits", cbody)
    if st != 201:
        print("COMMIT FAIL", st, commit)
        return 1
    print("commit:", commit["sha"])

    # update ref
    if parent_sha:
        st, ref = call("PATCH", f"{API}/git/refs/heads/main", {"sha": commit["sha"], "force": True})
    else:
        st, ref = call("POST", f"{API}/git/refs", {"ref": "refs/heads/main", "sha": commit["sha"]})
    if st not in (200, 201):
        print("REF FAIL", st, ref)
        return 1
    print("REF OK ->", "https://github.com/" + REPO)
    return 0


if __name__ == "__main__":
    sys.exit(main())

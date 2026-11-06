import subprocess
import sys
from pathlib import Path

SCAN = Path(__file__).resolve().parents[1] / "tools" / "scan_public.py"


def setup_repo(tmp_path, files, staged=False):
    repo = tmp_path / "repo"
    repo.mkdir()
    priv = tmp_path / "priv"
    (priv / "raw/jds/applied").mkdir(parents=True)
    (priv / "raw/jds/top50_index.json").write_text('[{"company": "Gadget Holdings"}]')
    (priv / "denylist.local.txt").write_text("zzqxinvented\n")
    (priv / "dontprint.local.txt").write_text("qqzinvented\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    for rel, data in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    return repo, priv


def scan(repo, priv, *args):
    import os
    env = {**os.environ, "ASK_PRIVATE_DIR": str(priv)}
    return subprocess.run([sys.executable, str(SCAN), *args], cwd=repo, env=env, capture_output=True, text=True)


def test_scan_reports_pattern_index_not_match(tmp_path):
    repo, priv = setup_repo(tmp_path, {"a.md": "ok\ncall 617-867-5309 now\n", "b.md": "fine zzqxinvented here\n"})
    r = scan(repo, priv)
    assert r.returncode == 1
    assert "a.md:2: phone pattern #1" in r.stdout
    assert "b.md:1: private pattern #0" in r.stdout
    assert "867" not in r.stdout + r.stderr and "zzqx" not in r.stdout + r.stderr


def test_scan_company_and_home_path_and_email(tmp_path):
    home = "/" + "Users/" + "someone/x"
    repo, priv = setup_repo(tmp_path, {
        "a.md": "Gadget Holdings\n", "b.md": f"path {home}\n",
        "c.md": "mail bob@example.com and mike@forn.al and 1+x@users.noreply.github.com\n"})
    out = scan(repo, priv).stdout
    assert "a.md:1: applied-company pattern #" in out
    assert "b.md:1: home-path pattern" in out
    assert "c.md:1: email pattern" in out and out.count("c.md") == 1
    assert "someone" not in out and "example" not in out


def test_scan_exemptions(tmp_path):
    repo, priv = setup_repo(tmp_path, {
        "a.md": "call 555-555-0123 now\n",
        "tests/t.py": "x = '12 Main Street'\ny = 'bob@example.com'\nz = '617-867-5309'\n",
        "src.py": "x = '12 Main Street'\n",
        "tests/u.py": "x = 'zzqxinvented'\n"})
    out = scan(repo, priv).stdout
    assert "a.md" not in out
    assert "src.py:1: street pattern" in out
    assert "tests/t.py" not in out
    assert "tests/u.py:1: private pattern #0" in out


def test_scan_clean_file_passes_and_unknown_binary_is_a_hit(tmp_path):
    repo, priv = setup_repo(tmp_path, {"a.md": "nothing here\n", "ok.png": b"\x89PNG\x00\x01"})
    r = scan(repo, priv)
    assert r.returncode == 0 and r.stdout == ""
    repo, priv = setup_repo(_sub(tmp_path), {"b.bin": b"\x00\x01 617-867-5309"})
    r = scan(repo, priv)
    assert r.returncode == 1 and "b.bin:1: binary file not scanned" in r.stdout


def test_scan_path_names_are_scanned_and_never_printed(tmp_path):
    repo, priv = setup_repo(tmp_path, {"notes-gadget-holdings.md": "fine\n", "zzqxinvented.txt": "fine\n"})
    r = scan(repo, priv)
    assert r.returncode == 1
    assert "<path #1>: applied-company pattern #2" in r.stdout and "<path #2>: private pattern #0" in r.stdout
    assert "gadget" not in r.stdout.lower() and "zzqx" not in r.stdout


def test_scan_staged_catches_renamed_file(tmp_path):
    body = "".join(f"line number {i} of the file\n" for i in range(20))
    repo, priv = setup_repo(tmp_path, {"a.md": body})
    git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@x.y", *a], cwd=repo, check=True)
    git("commit", "-qm", "m")
    git("mv", "a.md", "b.md")
    (repo / "b.md").write_text(body + "call 617-867-5309\n")
    git("add", "b.md")
    status = subprocess.run(["git", "diff", "--cached", "--name-status"], cwd=repo, capture_output=True, text=True).stdout
    assert status.startswith("R"), status
    r = scan(repo, priv, "--staged")
    assert r.returncode == 1 and "b.md:21: phone pattern" in r.stdout


def test_scan_staged_reads_staged_blob(tmp_path):
    repo, priv = setup_repo(tmp_path, {"a.md": "call 617-867-5309\n"})
    (repo / "a.md").write_text("clean\n")  # worktree clean, staged blob dirty
    assert scan(repo, priv, "--staged").returncode == 1
    assert scan(repo, priv).returncode == 0  # full mode reads the working tree


def test_scan_staged_reads_path_that_looks_like_a_stage_spec(tmp_path):
    repo, priv = setup_repo(tmp_path, {"1:notes.md": "has zzqxinvented inside\n"})
    r = scan(repo, priv, "--staged")
    assert r.returncode == 1 and "1:notes.md:1: private pattern #0" in r.stdout


def test_scan_path_hit_labels_every_line_of_that_file(tmp_path):
    repo, priv = setup_repo(tmp_path, {"notes-gadget-holdings.md": "see Gadget Holdings and 617-867-5309\n",
                                       "gadget-holdings.bin": b"\x00\x01"})
    r = scan(repo, priv)
    assert r.returncode == 1
    assert "gadget" not in r.stdout.lower() and "<path #2>:1: phone" in r.stdout and "<path #1>:1: binary" in r.stdout


def _sub(tmp_path):
    d = tmp_path / "second"
    d.mkdir()
    return d


def test_scan_names_the_failing_pattern_line_but_not_its_text(tmp_path):
    repo, priv = setup_repo(tmp_path, {"a.md": "nothing here\n"})
    (priv / "dontprint.local.txt").write_text("# note\nqqzinvented\ncost $77 flat\n")
    r = scan(repo, priv)
    assert r.returncode == 1
    assert "cannot load private patterns: dontprint.local.txt line 3: does not match its own text" in r.stdout
    assert "77" not in r.stdout + r.stderr and "qqz" not in r.stdout + r.stderr

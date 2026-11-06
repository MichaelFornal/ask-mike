import math
import subprocess
import sys

import pytest

from fakes import FakeEmbedder


def test_fake_embedder_is_deterministic_and_normalised():
    e = FakeEmbedder()
    a = e.embed_query("Built retrieval over contracts")
    assert a == FakeEmbedder().embed_query("Built retrieval over contracts")
    assert len(a) == 384
    assert math.isclose(sum(x * x for x in a), 1.0, rel_tol=1e-9)
    assert e.embed_passages(["a b", "c"]) == [e._vec("a b"), e._vec("c")]


def test_embedder_construction_downloads_nothing(monkeypatch, tmp_path):
    code = (
        "import sys\n"
        "from askmike.retrieval.models import Embedder\n"
        "e = Embedder()\n"
        "assert e.name == 'BAAI/bge-small-en-v1.5'\n"
        "assert 'fastembed' not in sys.modules\n"
    )
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path / "c"))
    subprocess.run([sys.executable, "-c", code], check=True)
    assert not (tmp_path / "c").exists()


def test_cache_dir_honours_env_and_defaults(monkeypatch, tmp_path):
    from askmike.retrieval.models import cache_dir
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path / "x"))
    assert cache_dir() == tmp_path / "x"
    monkeypatch.delenv("FASTEMBED_CACHE_PATH")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert cache_dir() == tmp_path / ".cache" / "askmike" / "fastembed"


@pytest.mark.model
def test_real_embedder_dim():
    from askmike.retrieval.models import EMBED_DIM, Embedder
    e = Embedder()
    assert len(e.embed_query("hello")) == EMBED_DIM
    assert len(e.embed_passages(["a", "b"])) == 2

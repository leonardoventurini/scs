"""Source identity validation preserves aliases without allowing target escapes."""

from pathlib import Path

import pytest

from scs.source_paths import validated_source_path


def test_source_alias_validation_preserves_containment_and_missing_path_contracts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    alias = repo / "alias.py"
    alias.symlink_to(source.name)
    assert validated_source_path(alias.name, str(repo)) == str(alias)
    assert validated_source_path(str(alias)) == str(alias)
    outside = tmp_path / "outside.py"
    outside.write_text("external = 1\n", encoding="utf-8")
    (repo / "escape.py").symlink_to(outside)
    (repo / "dangling.py").symlink_to("missing.py")
    (repo / "loop.py").symlink_to("loop.py")
    for path in ("../outside.py", "escape.py", "dangling.py", "loop.py", "."):
        with pytest.raises(ValueError):
            validated_source_path(path, str(repo))
    assert validated_source_path("deleted.py", str(repo), require_file=False) == str(
        repo / "deleted.py"
    )
    with pytest.raises(ValueError):
        validated_source_path("../deleted.py", str(repo), require_file=False)

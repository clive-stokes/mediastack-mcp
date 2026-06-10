"""Tier 1 — scan path validation. No I/O beyond tmp_path."""

from app.filesystem import validate_scan_path


def test_traversal_escape_rejected(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    error = validate_scan_path(f"{root}/../etc", [str(root)])
    assert error is not None
    assert "outside allowed" in error


def test_symlink_escape_rejected_after_resolution(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    outside = tmp_path / "secrets"
    outside.mkdir()
    link = root / "sneaky"
    link.symlink_to(outside)

    error = validate_scan_path(str(link), [str(root)])
    assert error is not None
    assert "outside allowed" in error


def test_exact_root_accepted(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    assert validate_scan_path(str(root), [str(root)]) is None


def test_nested_path_accepted(tmp_path):
    root = tmp_path / "media"
    nested = root / "Movies" / "Subdir"
    nested.mkdir(parents=True)
    assert validate_scan_path(str(nested), [str(root)]) is None


def test_sibling_prefix_not_treated_as_nested(tmp_path):
    # /media-other must not pass for root /media (string-prefix trap)
    root = tmp_path / "media"
    root.mkdir()
    sibling = tmp_path / "media-other"
    sibling.mkdir()
    assert validate_scan_path(str(sibling), [str(root)]) is not None


def test_empty_roots_rejected(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    error = validate_scan_path(str(root), [])
    assert error is not None
    assert "MEDIA_ROOTS" in error


def test_blocked_system_path_rejected_even_when_configured_as_root():
    error = validate_scan_path("/etc", ["/etc"])
    assert error is not None
    assert "blocked" in error


def test_missing_path_rejected():
    assert validate_scan_path("", ["/media"]) is not None

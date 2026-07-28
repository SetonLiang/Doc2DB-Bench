from data_construction.src.utils.provenance import wrap_with_provenance


def test_provenance_helpers_exist() -> None:
    assert callable(wrap_with_provenance)

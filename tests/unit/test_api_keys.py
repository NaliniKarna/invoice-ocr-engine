from app.security.api_keys import generate_api_key, hash_key, parse_prefix, verify_key

P = "pepper-" + "p" * 32


def test_generated_key_format_and_hash_not_plaintext():
    g = generate_api_key(P)
    assert g.full_key.startswith("ocr_") and parse_prefix(g.full_key) == g.prefix
    assert g.key_hash != g.full_key and g.full_key not in g.key_hash and len(g.key_hash) == 64


def test_verify_roundtrip_and_wrong_key():
    g = generate_api_key(P)
    assert verify_key(g.full_key, g.key_hash, P)
    assert not verify_key(g.full_key[:-1] + "x", g.key_hash, P)
    assert not verify_key(g.full_key, g.key_hash, "other-pepper")
    assert not verify_key(g.full_key, None, P)


def test_keys_are_unique():
    assert len({generate_api_key(P).full_key for _ in range(200)}) == 200


def test_parse_prefix_rejects_garbage():
    for bad in (None, "", "abc", "ocr_zz_yy", "ocr_" + "g" * 12 + "_" + "a" * 43, "x" * 500):
        assert parse_prefix(bad) is None


def test_pepper_changes_hash():
    assert hash_key("k", "a") != hash_key("k", "b")

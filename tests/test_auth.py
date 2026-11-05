from app.security import hash_password, verify_password


def test_password_hash_verifies_correct_password():
    encoded = hash_password("wai-demo-password-123")

    assert encoded.startswith("scrypt$")
    assert verify_password("wai-demo-password-123", encoded)


def test_password_hash_rejects_incorrect_password():
    encoded = hash_password("wai-demo-password-123")

    assert not verify_password("not-the-password", encoded)


def test_password_hash_uses_unique_salts():
    first = hash_password("wai-demo-password-123")
    second = hash_password("wai-demo-password-123")

    assert first != second

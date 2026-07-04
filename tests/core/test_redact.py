"""敏感信息脱敏测试（§14.11-3）。"""

from automind.core.redact import has_secret, redact_secrets


class TestRedactSecrets:
    def test_openai_key_masked(self):
        out = redact_secrets("token=sk-proj-ABCDEF1234567890abcdefGHIJ here")
        assert "sk-proj-ABCDEF1234567890abcdefGHIJ" not in out
        assert "REDACTED" in out

    def test_anthropic_key_masked(self):
        out = redact_secrets("key sk-ant-api03-ABCDEF1234567890abcdef done")
        assert "sk-ant-api03-ABCDEF1234567890abcdef" not in out

    def test_aws_key_masked(self):
        out = redact_secrets("AKIAIOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_google_key_masked(self):
        key = "AIzaSyA1234567890abcdef1234567890abcdef1"
        out = redact_secrets(f"google {key}")
        assert key not in out

    def test_bearer_token_masked(self):
        out = redact_secrets("Authorization: Bearer abcdef0123456789ABCDEFxyz")
        assert "abcdef0123456789ABCDEFxyz" not in out
        assert "Bearer" in out  # 前缀保留

    def test_generic_password_masked(self):
        out = redact_secrets('password: "hunter2supersecret"')
        assert "hunter2supersecret" not in out

    def test_private_key_block_masked(self):
        text = (
            "-----BEGIN RSA PRIVATE KEY-----\nMIIBVAIBADANBg\n-----END RSA PRIVATE KEY-----"
        )
        out = redact_secrets(text)
        assert "MIIBVAIBADANBg" not in out
        assert "REDACTED" in out

    def test_clean_text_unchanged(self):
        text = "This is a normal sentence with numbers 12345 and words."
        assert redact_secrets(text) == text

    def test_non_string_passthrough(self):
        assert redact_secrets(None) is None
        assert redact_secrets(123) == 123  # type: ignore[arg-type]

    def test_has_secret(self):
        assert has_secret("sk-proj-ABCDEF1234567890abcdefGHIJ")
        assert not has_secret("nothing sensitive here")

import pytest

from openring.netguard import UnsafeURL, check_url


def test_netguard_blocks_cloud_metadata():
    with pytest.raises(UnsafeURL):
        check_url("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(UnsafeURL):
        check_url("http://[fe80::1]/metadata")


def test_netguard_allows_safe_urls():
    try:
        check_url("https://example.com/webhook")
    except UnsafeURL:
        pytest.fail("example.com incorrectly blocked")

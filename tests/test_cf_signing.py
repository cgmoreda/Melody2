import hashlib
from urllib.parse import urlencode

from services.cf_client import build_api_sig


def test_build_api_sig_matches_manual_hash() -> None:
    rand_prefix = "123456"
    method = "contest.standings"
    params = {
        "apiKey": "key",
        "contestId": 123,
        "count": 50,
        "from": 1,
        "showUnofficial": "false",
        "time": 1710000000,
    }
    secret = "secret"

    serialized = urlencode(sorted((k, str(v)) for k, v in params.items()))
    manual = hashlib.sha512(f"{rand_prefix}/{method}?{serialized}#{secret}".encode("utf-8")).hexdigest()

    assert build_api_sig(rand_prefix, method, params, secret) == f"{rand_prefix}{manual}"
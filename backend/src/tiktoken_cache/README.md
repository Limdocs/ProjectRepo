# Vendored tiktoken encoding cache

`o200k_base` is stored here so quiz content selection can count tokens without a
first-use download. tiktoken's `read_file_cached` names the file
`sha1(encoding_url).hexdigest()` and reads `TIKTOKEN_CACHE_DIR` before any
network call.

| | |
| --- | --- |
| Encoding | `o200k_base` |
| Source URL | `https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken` |
| Cache filename | `sha1(source URL as UTF-8)` = `fb374d419588a4632f3f557e76b4b70aebbca790` |
| Content sha256 | `446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d` (the hash tiktoken 0.11 checks before it will use the file) |
| tiktoken pin | `>=0.11.0,<0.12.0` (`gpt-4.1` maps to `o200k_base` from 0.11.0; 0.12+ has no cp39 wheel) |

Refresh from the cache directory:

```powershell
python -c "import hashlib,urllib.request,pathlib; url='https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken'; name=hashlib.sha1(url.encode()).hexdigest(); pathlib.Path(name).write_bytes(urllib.request.urlopen(url, timeout=120).read())"
```

`token_budget.get_token_counter` points `TIKTOKEN_CACHE_DIR` at this directory
only when that variable is unset, and raises `TOKENIZER_UNAVAILABLE` if the
blob is missing rather than downloading it.

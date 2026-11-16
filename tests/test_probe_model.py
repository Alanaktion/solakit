import base64
import json
import os
import struct
import zlib
from pathlib import Path

import pytest

from scripts.probe_model import image_probe, load_key
from solakit.evidence import canonical, digest
from solakit.prompts import PreparedPrompt, inspect_prompt


def test_probe_dotenv_reads_literal_value_without_executing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "existing")
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        'IGNORED=value\nexport OPENAI_API_KEY="$(touch should-not-exist)" # literal\n'
    )
    load_key(dotenv, "OPENAI_API_KEY")
    assert os.environ["OPENAI_API_KEY"] == "$(touch should-not-exist)"
    assert not Path("should-not-exist").exists()
    dotenv.write_text("OPENAI_API_KEY=two values\n")
    with pytest.raises(ValueError, match="literal"):
        load_key(dotenv, "OPENAI_API_KEY")


def test_image_capability_probe_is_a_valid_red_png_and_has_matching_evidence() -> None:
    body = canonical({"messages": [{"role": "user", "content": "What color?"}]})
    manifest = canonical({"body_sha256": digest(body), "accounting": {}})
    prompt = image_probe(PreparedPrompt(body, manifest))
    inspect_prompt(prompt)
    request = json.loads(prompt.body)
    parts = request["messages"][-1]["content"]
    assert parts[0] == {"type": "text", "text": "What color?"}
    png = base64.b64decode(parts[1]["image_url"]["url"].split(",", 1)[1], validate=True)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    offset = 8
    pixels = b""
    while offset < len(png):
        length = struct.unpack(">I", png[offset : offset + 4])[0]
        kind, data = png[offset + 4 : offset + 8], png[offset + 8 : offset + 8 + length]
        assert struct.unpack(">I", png[offset + 8 + length : offset + 12 + length])[
            0
        ] == zlib.crc32(kind + data)
        if kind == b"IDAT":
            pixels += data
        offset += length + 12
    assert zlib.decompress(pixels) == (b"\0" + b"\xff\0\0" * 64) * 64

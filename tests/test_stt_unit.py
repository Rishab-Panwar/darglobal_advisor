"""Offline STT helpers: brand-name correction, silence detection, language hints (no network)."""
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.stt import fix_brands, is_silent, language_codes  # noqa: E402


def test_fix_brands():
    cases = {
        "Quel projet d'art global propose-t-il en Espagne ?": "Quel projet Dar Global propose-t-il en Espagne ?",
        "tell me about dare global projects": "tell me about Dar Global projects",
        "is darglobal listed in London": "is Dar Global listed in London",
        "villas on was salt in Jeddah": "villas on Wasalt in Jeddah",
        "ما هي مشاريع داجلوبال في عمان": "ما هي مشاريع دار جلوبال في عمان",
        "Какие проекты у Дар Глобал": "Какие проекты у Dar Global",
        "ما هي مشاريع دار Global في سلطنه عمان؟": "ما هي مشاريع دار جلوبال في سلطنه عمان؟",
        "مشاريع Dar جلوبال": "مشاريع دار جلوبال",
    }
    for heard, want in cases.items():
        assert fix_brands(heard) == want, (heard, fix_brands(heard))
    for keep in ["global property market", "the art gallery", "salt water pool", "Dar Global"]:
        assert fix_brands(keep) == keep, keep


def test_silence():
    clip = (ROOT / "tests" / "audio" / "en.wav").read_bytes()
    assert not is_silent(clip)
    assert is_silent(clip[:44] + bytes(64000))
    noise = clip[:44] + b"".join(struct.pack("<h", v) for v in ([40, -40] * 16000))
    assert is_silent(noise)
    assert not is_silent(b"OggS" + bytes(5000))      # non-WAV formats are left to the engine


def test_language_hints():
    assert language_codes("ar") == ["ar-SA", "en-US"]
    assert language_codes("zh") == ["cmn-Hans-CN", "ar-SA"]
    assert language_codes("xx") == ["en-US", "ar-SA"]
    assert all(len(language_codes(l)) <= 2 for l in ("en", "ar", "fr", "ru", "zh"))   # Chirp's limit


if __name__ == "__main__":
    for k, f in dict(globals()).items():
        if k.startswith("test_"):
            f()
    print("stt unit tests passed")

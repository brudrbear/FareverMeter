"""Refuse to run a probe against a stale analysis_out.

The probe hosts resolve their own offsets and findices by NAME from the live
hlboot.dat, but they also prepend the generated `resolver_data.json` for the
things every probe needs: the native `anchors` that triangulate the
functions_ptrs table base, and `funcs` (the getHero accessors). Those come from
the file, not from the fresh parse — so after a Farever patch a probe mixes
fresh findices with stale ones, and the failure is silent and misleading:

  * Every native anchor findex shifts, so `findTableBase` subtracts the wrong
    multiple of 8 and returns a base that is self-consistently WRONG (the
    anchors that shifted by the same delta as the seed still agree with each
    other). Every findex the probe then looks up resolves to a different
    function.
  * `DATA.funcs` getHero then points at whatever moved into that slot. On the
    2026-07-29 patch that was `h2d.Console.handleCommand` and
    `ui.comp.Dropdown.clearOptions` — called as `()->pointer`, swallowed by the
    per-candidate try/except, so the hero simply never latched and the probe
    logged nothing for a full 600s run. It also means Interceptor.attach on an
    arbitrary function in a live game.

The shipping meter self-heals (it stamps hlboot.dat and re-runs the generators),
which is exactly why this is easy to miss: the meter keeps working while the
probes quietly read the pre-patch file it replaced.

`nfunctions`/`nnatives` are the cheap tell — a patch that moves findices around
almost always changes the function count, and the hosts have already parsed the
code object by the time they need this.
"""
from pathlib import Path
import json


def assert_resolver_current(code, analysis_out=None):
    """Abort unless resolver_data.json was generated from this hlboot.dat.

    `code` is a parsed HLCode. Raises SystemExit naming the fix; returns the
    loaded resolver data on success so callers can use it instead of re-reading.
    """
    out = Path(analysis_out or Path(__file__).resolve().parent.parent / "analysis_out")
    path = out / "resolver_data.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise SystemExit(f"[!] cannot read {path}: {e}\n"
                         "    Run: py hltools\\build_targets.py")

    have = (data.get("nfunctions"), data.get("nnatives"))
    want = (code.counts["nfunctions"], code.counts["nnatives"])
    if have != want:
        raise SystemExit(
            f"[!] {path.name} is STALE — it was generated from a different "
            f"hlboot.dat.\n"
            f"    generated from: nfunctions={have[0]} nnatives={have[1]}\n"
            f"    live game has:  nfunctions={want[0]} nnatives={want[1]}\n"
            "    Every anchor and getHero findex in it points at the wrong "
            "function now,\n"
            "    which reads as 'the probe found nothing' rather than as an "
            "error. Refusing\n"
            "    to attach. Regenerate first:\n"
            "        py hltools\\build_targets.py\n"
            "        py hltools\\emit_offsets.py")
    return data

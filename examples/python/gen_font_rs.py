"""Emit Rust source for the Hershey font subset, suitable for pasting into
src/font.rs. Run once; output goes to stdout.
"""

from hershey import FONT


def char_lit(c: str) -> str:
    if c == "'":
        return "'\\''"
    if c == "\\":
        return "'\\\\'"
    return f"'{c}'"


def main():
    print("// Generated from examples/python/hershey.py — re-run gen_font_rs.py.")
    print("pub fn glyph(c: char) -> (u8, &'static [&'static [(i8, i8)]]) {")
    print("    match c {")
    for ch in sorted(FONT.keys()):
        adv, strokes = FONT[ch]
        parts = []
        for stroke in strokes:
            pts = ", ".join(f"({x},{y})" for x, y in stroke)
            parts.append(f"&[{pts}]")
        inner = "&[" + ", ".join(parts) + "]"
        print(f"        {char_lit(ch)} => ({adv}, {inner}),")
    print("        _ => (12, &[]),")
    print("    }")
    print("}")


if __name__ == "__main__":
    main()

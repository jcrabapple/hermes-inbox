import math, re, sys
def oklch_to_srgb(L, C, h):
    a, b = C*math.cos(math.radians(h)), C*math.sin(math.radians(h))
    l_ = L + 0.3963377774*a + 0.2158037573*b
    m_ = L - 0.1055613458*a - 0.0638541728*b
    s_ = L - 0.0894841775*a - 1.2914855480*b
    l, m, s = l_**3, m_**3, s_**3
    r = 4.0767416621*l - 3.3077115913*m + 0.2309699292*s
    g = -1.2684380046*l + 2.6097574011*m - 0.3413193965*s
    bb = -0.0041960863*l - 0.7034186147*m + 1.7076147010*s
    return [min(1, max(0, x)) for x in (r, g, bb)]   # linear
def lum(lin): return 0.2126*lin[0]+0.7152*lin[1]+0.0722*lin[2]
def parse(v):
    m = re.match(r"oklch\(([\d.]+)%\s+([\d.]+)\s+([\d.]+)", v)
    return oklch_to_srgb(float(m[1])/100, float(m[2]), float(m[3]))
def ratio(a, b):
    la, lb = lum(parse(a)), lum(parse(b)); hi, lo = max(la, lb), min(la, lb)
    return (hi+0.05)/(lo+0.05)
def check(theme, t, pairs):
    bad = 0
    for fg, bg, need in pairs:
        r = ratio(t[fg], t[bg]); ok = r >= need; bad += not ok
        print(f"{theme:5} {fg:12} on {bg:11} {r:5.2f} {'ok' if ok else 'FAIL'} (need {need})")
    return bad
PAIRS = [("text","bg",7),("text","panel",7),("text","sel",7),("text-2","panel",4.5),("text-2","sunken",4.5),("text-2","hover",4.5),
 ("muted","panel",4.5),("muted","bg",4.5),("muted","sunken",4.5),("muted","sel",4.5),("muted","hover",4.5),
 ("accent-text","panel",4.5),("accent-text","bg",4.5),("accent-text","accent-soft",4.5),("accent-text","sel",4.5),
 ("warn","warn-soft",4.5),("warn","panel",4.5),("warn","sunken",4.5),("ok","ok-soft",4.5),("on-accent","accent",4.5),
 ("on-accent","warn",4.5),("bg","text",7),("accent","panel",3),("accent","sel",3),("line-strong","panel",3),("amber","panel",3)]

def palettes(css):
    out = {}
    for name, sel in (("dark", r':root, \[data-theme="dark"\]'), ("light", r'\[data-theme="light"\]')):
        block = re.search(sel + r"\s*\{(.*?)\}", css, re.S).group(1)
        out[name] = dict(re.findall(r"--([\w-]+):\s*(oklch\([^)]*\))", block))
    return out

if __name__ == "__main__":
    from pathlib import Path
    css = (Path(__file__).resolve().parent.parent / "hermes_inbox" / "static" / "app.css").read_text()
    bad = sum(check(n, t, PAIRS) for n, t in palettes(css).items())
    print("FAILURES:", bad)
    sys.exit(1 if bad else 0)

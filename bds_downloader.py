#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import re
import sys
import time
import shutil
import subprocess
from tqdm import tqdm
from pathlib import Path
from urllib.parse import urlparse

import requests

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit("[error] Run: pip install playwright requests tqdm && playwright install chromium")

BASE64_RE = re.compile(r"data:image/(webp|jpeg|jpg|png);base64,([A-Za-z0-9+/=]+)", re.I)
MIN_B64_LEN = 100000
MIN_VALID_LONG_EDGE = 1200
LOW_RES_BLOCKLIST = {(467,700), (467,701), (468,700), (468,701), (1050,700), (700,1050), (701,467), (700,467)}
_SLUG_SKIP = {"photo", "fotos", "videos", "galeria", "pt-br", "en", "modelos", "modelo"}

SEL_TERMS = [
    "a:has-text('ACEITO OS TERMOS')", "button:has-text('ACEITO OS TERMOS')",
    "a:has-text('Aceito os termos')", "button:has-text('Aceito os termos')",
    "button:has-text('Aceito')", "button:has-text('Concordo')",
]
SEL_COOKIES = [".lgpd-accept-btn-allow", "button:has-text('Aceitar todos os cookies')"]
SEL_ZOOM = [
    "img[src*='maximize']", "img[src*='expand']", ".fa-expand", ".fa-arrows-alt",
    "[class*='expand' i]", "[class*='maximize' i]", "[title*='expand' i]",
    "[title*='zoom' i]", "button:has(svg.carousel__icon)"
]
SEL_ZOOM_IMG = [
    ".modal-mask .carousel__slide--active img", ".modal-mask .carousel__slide--active canvas",
    ".modal-container .carousel__slide--active img", ".carousel__slide--active img",
    ".carousel__slide--active canvas", ".pswp__img", "canvas.pswp__img",
    ".fancybox-image", ".yarl__slide_image", ".modal-mask img", ".modal-mask canvas"
]
SEL_ZOOM_CLOSE = [
    ".carousel__close", ".pswp__button--close", ".slideclose", ".close-modal",
    ".close-black", "[aria-label='Close']", ".modal-container [class*='close']"
]
SEL_NEXT = [
    ".carousel__next", ".pswp__button--arrow--right", ".owl-next",
    "button:has(.fa-chevron-right)", "button:has(.angle-right)",
    "[aria-label*='next' i]", "[aria-label*='próximo' i]"
]

DEBUG = False
DEBUG_DIR = None
DEBUG_LOG_FH = None


def debug_log(msg: str):
    global DEBUG_LOG_FH
    line = str(msg)
    print(line)
    if DEBUG_LOG_FH:
        DEBUG_LOG_FH.write(line + "\n")
        DEBUG_LOG_FH.flush()


def sanitize_name(name: str) -> str:
    name = re.sub(r'[^A-Za-z0-9._-]+', '_', str(name).strip())
    return name[:160] or 'unnamed'


def dump_page_state(page, label: str, note: str = ""):
    if not DEBUG or not DEBUG_DIR:
        return
    stem = sanitize_name(label)
    png = DEBUG_DIR / f"{stem}.png"
    html = DEBUG_DIR / f"{stem}.html"
    txt = DEBUG_DIR / f"{stem}.txt"
    try:
        page.screenshot(path=str(png), full_page=True)
    except Exception as e:
        debug_log(f"[debug] screenshot failed for {label}: {e}")
    try:
        html.write_text(page.content(), encoding='utf-8')
    except Exception as e:
        debug_log(f"[debug] html dump failed for {label}: {e}")
    try:
        visible_sections = []
        for loc in page.locator(r'text=/Seç[aã]o\s*\d+/i >> visible=true').all():
            txt2 = (loc.text_content() or '').strip()
            if txt2:
                visible_sections.append(re.sub(r'\s+', ' ', txt2))
        visible_partes = []
        for loc in page.locator(r'text=/Parte\s+\d+/i >> visible=true').all():
            txt2 = (loc.text_content() or '').strip()
            if txt2:
                visible_partes.append(re.sub(r'\s+', ' ', txt2))
        txt.write_text(
            f"label={label}\nurl={page.url}\nnote={note}\nvisible_partes={visible_partes}\nvisible_sections={visible_sections}\n",
            encoding='utf-8'
        )
    except Exception as e:
        debug_log(f"[debug] txt dump failed for {label}: {e}")


def active_frame_signature(page):
    js_eval = (
        "el => {"
        " let src = el.getAttribute('data-src') || el.getAttribute('data-zoom') || el.src || '';"
        " let w = el.naturalWidth || el.clientWidth || 0;"
        " let h = el.naturalHeight || el.clientHeight || 0;"
        " if (el.tagName.toLowerCase() === 'canvas') {"
        "   src = el.toDataURL('image/jpeg', 0.6);"
        "   w = el.width || el.clientWidth || 0;"
        "   h = el.height || el.clientHeight || 0;"
        " }"
        " return { src: src, width: w, height: h };"
        "}"
    )
    for sel in SEL_ZOOM_IMG:
        try:
            locs = page.locator(f"{sel} >> visible=true").all()
            for loc in reversed(locs):
                info = loc.evaluate(js_eval)
                src = info.get('src', '')
                if not src:
                    continue
                if src.startswith('data:image'):
                    return ('b64', hashlib.md5(src.encode()).hexdigest(), info.get('width'), info.get('height'))
                return ('url', src[:300], info.get('width'), info.get('height'))
        except Exception:
            pass
    return None



SEL_VIDEO_DOWNLOAD_BTN = "#btn-download-video button, #btn-download-video, button:has-text('Download')"



def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def select_highest_video_resolution(page):
    option_groups = [
        ('4K', [r'text=/^\s*4k\s*$/i', r'text=/ultra\s*hd\s*4k/i', r'text=/2160p/i']),
        ('Full HD 1080p', [r'text=/full\s*hd\s*1080p/i', r'text=/1080p/i']),
        ('HD 720p', [r'text=/hd\s*720p/i', r'text=/720p/i']),
    ]
    openers = [
        r'text=/4k|2160p|1080p|720p|full\s*hd|ultra\s*hd/i',
        '[class*="resolution" i]',
        '[class*="quality" i]',
        '.text1',
        '.title-part',
        'button',
        'a',
        'span',
        'div',
    ]

    def click_option(patterns):
        for pat in patterns:
            try:
                loc = page.locator(f'{pat} >> visible=true').last
                if loc.count() == 0:
                    continue
                loc.scroll_into_view_if_needed(timeout=3000)
                loc.click(timeout=5000, force=True)
                time.sleep(2.5)
                return True
            except Exception:
                pass
        return False

    for label, patterns in option_groups:
        if click_option(patterns):
            return label

    for opener in openers:
        try:
            locs = page.locator(f'{opener} >> visible=true')
            count = min(locs.count(), 12)
            for i in range(count):
                try:
                    loc = locs.nth(i)
                    txt = (loc.text_content() or '').strip()
                    if not re.search(r'4k|2160p|1080p|720p|full\s*hd|ultra\s*hd|hd\s*720p', txt, re.I):
                        continue
                    loc.scroll_into_view_if_needed(timeout=3000)
                    loc.click(timeout=5000, force=True)
                    time.sleep(1.5)
                    for label, patterns in option_groups:
                        if click_option(patterns):
                            return label
                except Exception:
                    pass
        except Exception:
            pass
    return 'default'

def video_part_signature(page):
    js = r"""
() => {
  const video = document.querySelector('video');
  const poster = video ? (video.getAttribute('poster') || '') : '';
  const currentSrc = video ? (video.currentSrc || video.src || '') : '';
  const labelNodes = Array.from(document.querySelectorAll('.title-part, .text1, [class*="part"]'))
    .map(el => (el.textContent || '').replace(/\s+/g, ' ').trim())
    .filter(Boolean)
    .slice(0, 30);
  const thumbs = Array.from(document.querySelectorAll('.box-live img, img')).map(img => ({
    src: img.currentSrc || img.src || '',
    alt: img.alt || '',
    cls: img.className || '',
    w: img.naturalWidth || 0,
    h: img.naturalHeight || 0
  })).slice(0, 30);
  const pageText = (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').trim().slice(0, 1200);
  return {poster, currentSrc, labelNodes, thumbs, pageText, url: location.href};
}
"""
    try:
        data = page.evaluate(js) or {}
    except Exception:
        data = {}
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest(), data


def wait_for_video_part_change(page, before_sig, timeout=30.0, debug=False, debug_prefix=None):
    end = time.time() + timeout
    stable_new = None
    stable_hits = 0
    while time.time() < end:
        time.sleep(1.0)
        sig, data = video_part_signature(page)
        if sig != before_sig:
            if stable_new == sig:
                stable_hits += 1
            else:
                stable_new = sig
                stable_hits = 1
            if stable_hits >= 2:
                if debug and debug_prefix:
                    try:
                        page.screenshot(path=f'{debug_prefix}.jpeg', full_page=True)
                        with open(f'{debug_prefix}.txt', 'w', encoding='utf-8') as f:
                            f.write(json.dumps(data, ensure_ascii=False, indent=2))
                    except Exception:
                        pass
                return sig, data
    return None, None


def collect_part_targets(page, label):
    targets = []
    seen = set()
    selectors = [
        f'text="{label}"',
        f'.title-part:has-text("{label}")',
        f'.text1:has-text("{label}")',
        '.box-live',
        '.box-live img',
        'img',
    ]
    for sel in selectors:
        try:
            locs = page.locator(sel)
            count = min(locs.count(), 16)
            for i in range(count):
                try:
                    loc = locs.nth(i)
                    txt = (loc.text_content() or '').strip()
                except Exception:
                    txt = ''
                key = f'{sel}::{i}::{txt[:80]}'
                if key not in seen:
                    seen.add(key)
                    targets.append((sel, i))
        except Exception:
            pass
    return targets


def activate_video_part(page, label, debug=False, artifact_base=None):
    before_sig, before_data = video_part_signature(page)
    targets = collect_part_targets(page, label)
    for n, (sel, idx) in enumerate(targets, start=1):
        try:
            loc = page.locator(sel).nth(idx)
            if loc.count() == 0:
                continue
            loc.scroll_into_view_if_needed(timeout=3000)
            loc.click(timeout=5000, force=True)
            sig, data = wait_for_video_part_change(page, before_sig, timeout=30.0, debug=debug, debug_prefix=(f'{artifact_base}_activate_{n}' if artifact_base else None))
            if sig:
                return True, sig, data, (sel, idx)
        except Exception:
            pass
    return False, before_sig, before_data, None


def part_specificity_score(url: str, idx: int) -> int:
    u = (url or '').lower()
    p = idx + 1
    score = 0
    if f'4k{p}.mp4,.urlset/master.m3u8' in u:
        score += 100000
    if f'4k{p}.mp4' in u:
        score += 90000
    if f'fullhd{p}.mp4,.urlset/master.m3u8' in u:
        score += 60000
    if f'fullhd{p}.mp4' in u:
        score += 50000
    if f'hd{p}.mp4,.urlset/master.m3u8' in u:
        score += 30000
    if f'hd{p}.mp4' in u:
        score += 20000
    if p == 1 and '4k.mp4,.urlset/master.m3u8' in u:
        score += 15000
    if p == 1 and '4k.mp4' in u:
        score += 12000
    if p == 1 and 'fullhd.mp4,.urlset/master.m3u8' in u:
        score += 9000
    if p == 1 and 'fullhd.mp4' in u:
        score += 7000
    if '.m3u8' in u:
        score += 2500
    if '.mp4' in u:
        score += 1000
    if '4k' in u or '2160' in u:
        score += 500
    elif 'fullhd' in u or '1080' in u:
        score += 250
    elif 'hd' in u or '720' in u:
        score += 100
    return score

def model_video_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path
    if '/video' in path:
        return url
    if '/photo' in path:
        path = path.replace('/photo', '/video')
    elif '/fotos' in path:
        path = path.replace('/fotos', '/video')
    else:
        path = path.rstrip('/') + '/video'
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def unique_preserve(items):
    out, seen = [], set()
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def list_video_parts(page):
    labels = []
    try:
        locs = page.locator(r'text=/Parte\s+\d+/i >> visible=true').all()
    except Exception:
        locs = []
    for loc in locs:
        try:
            txt = re.sub(r'\s+', ' ', (loc.text_content() or '').strip())
        except Exception:
            txt = ''
        if re.match(r'^Parte\s+\d+$', txt, re.I):
            labels.append(txt)
    labels = unique_preserve(labels)
    labels.sort(key=lambda s: int(re.search(r'(\d+)', s).group(1)) if re.search(r'(\d+)', s) else 9999)
    return labels or ['Parte 1']


def click_part_label(page, label):
    selectors = [
        f'text="{label}"',
        f'.title-part:has-text("{label}")',
        f'.text1:has-text("{label}")',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).last
            if loc.count() == 0:
                continue
            loc.scroll_into_view_if_needed(timeout=3000)
            loc.click(timeout=5000, force=True)
            time.sleep(5.0)
            return True
        except Exception:
            continue
    return False


def extract_video_candidates_from_performance(page):
    js = """
() => {
  const out = [];
  const entries = performance.getEntriesByType('resource') || [];
  for (const e of entries) {
    const name = e && e.name ? String(e.name) : '';
    if (!name) continue;
    const low = name.toLowerCase();
    if (low.includes('.mp4') || low.includes('.m3u8') || low.includes('urlset/master.m3u8') || low.includes('/hls/videos/')) {
      out.push(name);
    }
  }
  return out;
}
"""
    try:
        return page.evaluate(js) or []
    except Exception:
        return []


def collect_media_candidates(page, network_log):
    urls = []
    try:
        current = page.locator('video').evaluate("els => els.map(v => v.currentSrc || v.src || '').filter(Boolean)")
        urls.extend(current)
    except Exception:
        pass
    urls.extend(extract_video_candidates_from_performance(page))
    for item in network_log:
        u = item.get('url', '')
        ctype = (item.get('content_type') or '').lower()
        if any(ext in u.lower() for ext in ('.mp4', '.m3u8', '.webm', '.mov')) or any(k in ctype for k in ('video/', 'mpegurl', 'mp2t')):
            urls.append(u)
    fixed = []
    for u in unique_preserve(urls):
        if ',.urlset/master.m3u8' in u:
            base_mp4 = u.replace(',.urlset/master.m3u8', '')
            fixed.append(u)
            fixed.append(base_mp4)
            fixed.append(base_mp4 + ',.urlset/master.m3u8')
        else:
            fixed.append(u)
    return unique_preserve(fixed)


def choose_best_video_candidate(candidates, part_idx):
    ordered = sorted(build_video_candidate_variants(candidates, part_idx), key=lambda u: part_specificity_score(u, part_idx), reverse=True)
    return ordered[0] if ordered else None


def build_video_candidate_variants(candidates, part_idx):
    idx1 = part_idx + 1
    out = []

    def add(url):
        if url and url not in out:
            out.append(url)

    for raw in unique_preserve(candidates):
        u = (raw or '').strip()
        if not u:
            continue
        add(u)
        low = u.lower()
        if ',.urlset/master.m3u8' in low:
            add(u.replace(',.urlset/master.m3u8', ''))
        elif low.endswith('.mp4'):
            add(u + ',.urlset/master.m3u8')

        m = re.search(r'(4k|fullhd|hd)(\d*)\.mp4', u, re.I)
        if not m:
            continue

        if idx1 == 1:
            replacements = ['4k.mp4', '4k1.mp4', 'fullhd.mp4', 'fullhd1.mp4', 'hd.mp4', 'hd1.mp4']
        else:
            replacements = [f'4k{idx1}.mp4', f'fullhd{idx1}.mp4', f'hd{idx1}.mp4']

        for repl in replacements:
            variant = re.sub(r'(4k|fullhd|hd)(\d*)\.mp4', repl, u, count=1, flags=re.I)
            add(variant)
            vlow = variant.lower()
            if ',.urlset/master.m3u8' in vlow:
                add(variant.replace(',.urlset/master.m3u8', ''))
            elif vlow.endswith('.mp4'):
                add(variant + ',.urlset/master.m3u8')

    return out


def download_best_video_candidate(candidates, session, dest_base: Path, part_idx: int):
    ordered = sorted(build_video_candidate_variants(candidates, part_idx), key=lambda u: part_specificity_score(u, part_idx), reverse=True)
    last_exc = None
    for candidate in ordered:
        try:
            out = download_stream_or_file(candidate, session, dest_base)
            return out, candidate, ordered
        except Exception as e:
            last_exc = e
            try:
                dest_base.with_suffix('.mp4').unlink(missing_ok=True)
            except Exception:
                pass
    if last_exc:
        raise last_exc
    raise ValueError('no downloadable video candidate')

def download_stream_or_file(url: str, session: requests.Session, dest_base: Path):
    out = dest_base.with_suffix('.mp4')
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg:
        input_url = url if '.m3u8' in url.lower() else (url + ',.urlset/master.m3u8' if url.lower().endswith('.mp4') and '.urlset/master.m3u8' not in url.lower() else url)
        cmd = [ffmpeg, '-y', '-protocol_whitelist', 'file,http,https,tcp,tls,crypto', '-i', input_url, '-c', 'copy', str(out)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and out.exists() and out.stat().st_size > 0:
            return out
    r = session.get(url, stream=True, timeout=60)
    r.raise_for_status()
    total = int(r.headers.get('content-length', '0') or 0)
    with open(out, 'wb') as f, tqdm(total=total if total > 0 else None, unit='B', unit_scale=True, unit_divisor=1024, desc=out.name, leave=True) as bar:
        for chunk in r.iter_content(1024 * 1024):
            if chunk:
                f.write(chunk)
                bar.update(len(chunk))
    return out


def click_download_and_capture(page, wait_s=8.0):
    seen = []
    def on_download(dl):
        seen.append(dl)
    page.on('download', on_download)
    try:
        try:
            page.locator(SEL_VIDEO_DOWNLOAD_BTN).first.click(timeout=5000, force=True)
        except Exception:
            return None
        end = time.time() + wait_s
        while time.time() < end:
            if seen:
                return seen[-1]
            time.sleep(0.25)
    finally:
        try:
            page.remove_listener('download', on_download)
        except Exception:
            pass
    return None


def scrape_videos(page, model_dir: Path, slug: str, http_session: requests.Session, delay: float, debug: bool = False):
    video_dir = model_dir / 'videos'
    video_dir.mkdir(parents=True, exist_ok=True)
    parts = list_video_parts(page)
    debug_log(f'[*] Video Parte tabs found: {len(parts)} -> {parts}')
    saved = 0
    saved_hashes = set()

    for existing in sorted(video_dir.glob('*.mp4')):
        try:
            saved_hashes.add(sha256_file(existing))
        except Exception:
            pass

    for idx, label in enumerate(parts):
        safe_part = re.sub(r'\s+', '_', label.strip())
        artifact_base = f'_debug_video_{safe_part.lower()}' if debug else None
        network_log = []
        def on_response(resp):
            try:
                network_log.append({'url': resp.url, 'content_type': resp.headers.get('content-type','')})
            except Exception:
                pass
        page.on('response', on_response)
        try:
            if idx == 0:
                debug_log(f'[*] Processing landing video {safe_part}')
                current_sig, current_data = video_part_signature(page)
                activated_by = ('landing', 0)
            else:
                ok, current_sig, current_data, activated_by = activate_video_part(page, label, debug=debug, artifact_base=artifact_base)
                if not ok:
                    debug_log(f' [warn] could not activate a distinct page state for {safe_part}; continuing with best available state')
            chosen_res = select_highest_video_resolution(page)
            debug_log(f'[*] Selected resolution for {safe_part}: {chosen_res}')
            if debug and artifact_base:
                try:
                    page.screenshot(path=f'{artifact_base}_state.jpeg', full_page=True)
                    with open(f'{artifact_base}_state.txt', 'w', encoding='utf-8') as f:
                        f.write(json.dumps({'activated_by': activated_by, 'signature': current_sig, 'resolution': chosen_res, 'state': current_data}, ensure_ascii=False, indent=2))
                except Exception:
                    pass

            attempts = 0
            downloaded = False
            tried_signatures = set()
            while attempts < 8 and not downloaded:
                attempts += 1
                current_sig, current_data = video_part_signature(page)
                tried_signatures.add(current_sig)
                chosen_res = select_highest_video_resolution(page)
                debug_log(f'[*] Processing video {safe_part} [attempt={attempts}, resolution={chosen_res}]')
                time.sleep(max(8.0, delay))
                candidates = collect_media_candidates(page, network_log)
                candidates_sorted = sorted(build_video_candidate_variants(candidates, idx), key=lambda u: part_specificity_score(u, idx), reverse=True)
                debug_log(f'[*] Video candidates for {safe_part}: {len(candidates_sorted)} -> {candidates_sorted[:8]}')
                best = candidates_sorted[0] if candidates_sorted else None

                if best:
                    out, used_candidate, _ordered = download_best_video_candidate(candidates, http_session, video_dir / f'{slug} - {safe_part.lower()}', idx)
                    debug_log(f'[*] Download URL for {safe_part}: {used_candidate}')
                    file_hash = sha256_file(out)
                    if file_hash in saved_hashes:
                        try:
                            out.unlink(missing_ok=True)
                        except Exception:
                            pass
                        debug_log(f' [warn] downloaded duplicate bytes for {safe_part}; traversing DOM for a new state')
                    else:
                        saved_hashes.add(file_hash)
                        debug_log(f' OK {out.name}')
                        saved += 1
                        downloaded = True
                        break

                next_found = False
                for sel, j in collect_part_targets(page, label):
                    try:
                        loc = page.locator(sel).nth(j)
                        if loc.count() == 0:
                            continue
                        before_sig, _ = video_part_signature(page)
                        loc.scroll_into_view_if_needed(timeout=3000)
                        loc.click(timeout=5000, force=True)
                        new_sig, _ = wait_for_video_part_change(page, before_sig, timeout=20.0, debug=debug, debug_prefix=(f'{artifact_base}_domwalk_{attempts}_{j}' if artifact_base else None))
                        if new_sig and new_sig not in tried_signatures:
                            chosen_res = select_highest_video_resolution(page)
                            debug_log(f'[*] DOM traversal found a new state for {safe_part} via {sel}[{j}] with resolution {chosen_res}')
                            next_found = True
                            break
                    except Exception:
                        pass
                if not next_found:
                    debug_log(f' [warn] no new DOM state found for {safe_part} after duplicate/weak candidate')
                    break

            if not downloaded:
                debug_log(f' [warn] unable to reach a unique downloadable video for {safe_part}')
        except Exception as e:
            debug_log(f' [warn] {safe_part}: {e}')
        finally:
            try:
                page.remove_listener('response', on_response)
            except Exception:
                pass
    return saved

def extract_slug(url: str) -> str:
    parts = urlparse(url).path.strip('/').split('/')
    return next((p for p in reversed(parts) if p.lower() not in _SLUG_SKIP), parts[-1])


def get_highres_url(url: str) -> str:
    if not url:
        return ""
    url = re.sub(r'/cdn-cgi/image/[^/]+/', '/', url)
    url = re.sub(r'/_nuxt/image/[^/]+/[^/]+/', '/', url)
    return url


def sniff_image_size(data: bytes):
    try:
        if data[:2] == b'\xff\xd8':
            i = 2
            n = len(data)
            while i < n - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i+1]
                i += 2
                while marker == 0xFF and i < n:
                    marker = data[i]
                    i += 1
                if marker in (0xD8, 0xD9):
                    continue
                if i + 1 >= n:
                    break
                seglen = (data[i] << 8) + data[i+1]
                if seglen < 2 or i + seglen > n:
                    break
                if marker in (0xC0,0xC1,0xC2,0xC3,0xC5,0xC6,0xC7,0xC9,0xCA,0xCB,0xCD,0xCE,0xCF):
                    if i + 7 < n:
                        h = (data[i+3] << 8) + data[i+4]
                        w = (data[i+5] << 8) + data[i+6]
                        return w, h
                    break
                i += seglen
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            chunk = data[12:16]
            if chunk == b'VP8 ' and len(data) >= 30:
                import struct
                w, h = struct.unpack('<HH', data[26:30])
                return (w & 0x3FFF) + 1, (h & 0x3FFF) + 1
            if chunk == b'VP8L' and len(data) >= 25:
                b0,b1,b2,b3 = data[21],data[22],data[23],data[24]
                w = 1 + (((b1 & 0x3F) << 8) | b0)
                h = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
                return w, h
            if chunk == b'VP8X' and len(data) >= 30:
                w = 1 + int.from_bytes(data[24:27], 'little')
                h = 1 + int.from_bytes(data[27:30], 'little')
                return w, h
    except Exception:
        pass
    return 0, 0


def is_highres_candidate(width: int, height: int, src: str = '') -> bool:
    width = int(width or 0)
    height = int(height or 0)
    if not width or not height:
        return False
    if (width, height) in LOW_RES_BLOCKLIST or (height, width) in LOW_RES_BLOCKLIST:
        return False
    long_edge = max(width, height)
    short_edge = min(width, height)
    if long_edge < MIN_VALID_LONG_EDGE:
        return False
    if short_edge < 1000:
        return False
    return True


def ensure_zoom_mode(page):
    for _ in range(4):
        if try_click(page, SEL_ZOOM, 2000):
            time.sleep(2.0)
            return True
        try:
            active_img = page.locator(".carousel__slide--active img >> visible=true").last
            active_img.dblclick(timeout=1200, force=True)
            time.sleep(2.0)
            return True
        except Exception:
            pass
        time.sleep(0.8)
    return False


def get_best_image_url_from_page(page):
    candidates = []
    selectors = [
        '.carousel__slide--active img', '.modal-mask .carousel__slide--active img',
        '.modal-container .carousel__slide--active img', '.carousel__slide img',
        '.modal-mask img', '.modal-container img', '.pswp__img', '.fancybox-image', '.yarl__slide_image'
    ]
    for sel in selectors:
        try:
            locs = page.locator(f"{sel} >> visible=true").all()
        except Exception:
            locs = []
        for loc in reversed(locs):
            try:
                info = loc.evaluate("el => ({src: el.getAttribute('data-zoom') || el.getAttribute('data-src') || el.currentSrc || el.src || '', w: el.naturalWidth || el.clientWidth || 0, h: el.naturalHeight || el.clientHeight || 0})")
            except Exception:
                continue
            src = (info.get('src') or '').strip()
            if not src:
                continue
            w = int(info.get('w') or 0)
            h = int(info.get('h') or 0)
            if src.startswith('data:image'):
                m = BASE64_RE.match(src)
                if m and len(m.group(2)) >= MIN_B64_LEN:
                    payload = base64.b64decode(m.group(2))
                    w2, h2 = sniff_image_size(payload)
                    if is_highres_candidate(w2 or w, h2 or h, src):
                        ext = 'jpg' if m.group(1).lower() == 'jpeg' else m.group(1).lower()
                        return ext, payload
            elif src.startswith('http') or src.startswith('/'):
                if src.startswith('/'):
                    base = urlparse(page.url)
                    src = f"{base.scheme}://{base.netloc}{src}"
                src = get_highres_url(src)
                bonus = 1000000 if '.webp' in src.lower() else 0
                candidates.append((bonus + w*h, src, w, h))
    candidates.sort(reverse=True)
    for _, src, w, h in candidates:
        if is_highres_candidate(w, h, src):
            return 'src_url', src.encode()
    return None


def download_url_checked(url: str, session: requests.Session, dest: Path):
    r = session.get(url, stream=True, timeout=30)
    r.raise_for_status()
    data = b''.join(chunk for chunk in r.iter_content(8192) if chunk)
    w, h = sniff_image_size(data)
    if not is_highres_candidate(w, h, url):
        raise ValueError(f'low-res image blocked: {w}x{h} from {url}')
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return w, h


def save_bytes_checked(data: bytes, dest: Path):
    w, h = sniff_image_size(data)
    if not is_highres_candidate(w, h, dest.name):
        raise ValueError(f'low-res image blocked: {w}x{h} for {dest.name}')
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return w, h


def safe_goto(page, url: str):
    page.goto(url, wait_until='domcontentloaded', timeout=45000)
    try:
        page.wait_for_selector('body', state='visible', timeout=10000)
    except PWTimeout:
        pass
    debug_log('[*] Waiting 5 s for page to fully load ...')
    time.sleep(5)


def try_click(page, selectors, timeout=3000):
    for sel in selectors:
        try:
            locs = page.locator(f"{sel} >> visible=true").all()
            if locs:
                locs[-1].click(timeout=timeout, force=True)
                return True
        except Exception:
            pass
    return False


def dismiss_overlays(page):
    debug_log('[*] Waiting 5 s for modals to appear ...')
    time.sleep(5)
    hit = try_click(page, SEL_TERMS)
    debug_log(f"[+] Terms modal  : {'dismissed' if hit else 'not found'}")
    if hit:
        time.sleep(2)
    hit = try_click(page, SEL_COOKIES)
    debug_log(f"[+] Cookie banner: {'dismissed' if hit else 'not found'}")
    if hit:
        time.sleep(2)


def save_bytes(data: bytes, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def download_url(url: str, session: requests.Session, dest: Path):
    r = session.get(url, stream=True, timeout=30)
    r.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, 'wb') as f:
        for chunk in r.iter_content(8192):
            if chunk:
                f.write(chunk)


def read_counter(page):
    try:
        dots = page.locator(".carousel__pagination-item >> visible=true").count()
        if dots > 0:
            return 1, dots
    except Exception:
        pass
    for sel in [".pswp__counter", ".x-of-y", ".number-image", "[class*='counter' i]"]:
        try:
            locs = page.locator(f"{sel} >> visible=true").all()
            if locs:
                txt = (locs[-1].text_content() or '').strip()
                m = re.search(r'(\d+)\s*[\/de|-]\s*(\d+)', txt, re.I)
                if m:
                    return int(m.group(1)), int(m.group(2))
        except Exception:
            pass
    return 1, 0


def click_next(page):
    try:
        page.keyboard.press('ArrowRight')
        return True
    except Exception:
        pass
    return try_click(page, SEL_NEXT, 2500)


def find_parte_tabs(page):
    labels = []
    seen = set()
    try:
        candidates = page.locator(r'text=/Parte\s+\d+/i >> visible=true').all()
    except Exception:
        candidates = []
    for loc in candidates:
        try:
            txt = (loc.text_content() or '').strip()
            if not re.match(r'^Parte\s+\d+$', txt, re.I):
                continue
            txt = re.sub(r'\s+', ' ', txt)
            if txt in seen:
                continue
            seen.add(txt)
            labels.append(txt)
        except Exception:
            pass
    labels.sort(key=lambda s: int(re.search(r'(\d+)', s).group(1)) if re.search(r'(\d+)', s) else 9999)
    return labels


def find_section_tiles(page):
    labels = []
    seen = set()
    try:
        locs = page.locator(r"text=/Seç[aã]o\s*\d+/i >> visible=true").all()
    except Exception:
        locs = []
    for loc in locs:
        try:
            txt = (loc.text_content() or '').strip()
            m = re.search(r'Seç[aã]o\s*\d+', txt, re.I)
            if not m:
                continue
            label = re.sub(r'\s+', ' ', m.group(0)).strip()
            if label in seen:
                continue
            seen.add(label)
            labels.append(label)
        except Exception:
            pass
    labels.sort(key=lambda s: int(re.search(r'(\d+)', s).group(1)) if re.search(r'(\d+)', s) else 9999)
    return labels, ('text=/Seção/i' if labels else None)


def tile_label(tile, idx):
    try:
        txt = str(tile).strip()
        m = re.search(r'Seç[aã]o\s*\d+', txt, re.I)
        if m:
            txt = m.group(0)
        txt = re.sub(r'\s+', '_', txt)
        txt = re.sub(r'[^\w-]', '', txt)
        return txt or f'secao_{idx:02d}'
    except Exception:
        return f'secao_{idx:02d}'


def tile_is_locked(tile):
    try:
        cls = tile.get_attribute('class') or ''
        if 'optionsitem--locked' in cls:
            return True
        txt = (tile.text_content() or '').lower()
        return ('seção' in txt or 'secao' in txt) and ('comprar' in txt or 'assine' in txt)
    except Exception:
        return False


def grab_image(page, retries=15, interval=0.5):
    js_eval = (
        "el => {"
        " let src = el.getAttribute('data-src') || el.getAttribute('data-zoom') || el.src || '';"
        " let w = el.naturalWidth || el.clientWidth || 0;"
        " let h = el.naturalHeight || el.clientHeight || 0;"
        " if (el.tagName.toLowerCase() == 'canvas') {"
        "   src = el.toDataURL('image/jpeg', 0.95);"
        "   w = el.width || el.clientWidth || 0;"
        "   h = el.height || el.clientHeight || 0;"
        " }"
        " return { src: src, width: w, height: h };"
        "}"
    )
    for _ in range(retries):
        for sel in SEL_ZOOM_IMG:
            try:
                locs = page.locator(f"{sel} >> visible=true").all()
                if not locs:
                    continue
                for loc in reversed(locs):
                    img_info = loc.evaluate(js_eval)
                    src = img_info.get('src', '')
                    width = img_info.get('width', 0)
                    height = img_info.get('height', 0)
                    if not src or 'placeholder' in src or 'spinner' in src:
                        continue
                    if max(width, height) < 800:
                        continue
                    m = BASE64_RE.match(src)
                    if m:
                        if len(m.group(2)) >= MIN_B64_LEN:
                            ext = 'jpg' if m.group(1).lower() == 'jpeg' else m.group(1).lower()
                            return ext, base64.b64decode(m.group(2))
                        continue
                    if src.startswith('http') or src.startswith('/'):
                        src = get_highres_url(src)
                        if src.startswith('/'):
                            base_url = urlparse(page.url)
                            src = f"{base_url.scheme}://{base_url.netloc}{src}"
                        return 'src_url', src.encode()
            except Exception:
                pass
        time.sleep(interval)
    return None


def force_close_modal(page):
    page.keyboard.press('Escape')
    time.sleep(0.2)
    page.keyboard.press('Escape')
    time.sleep(0.5)
    try_click(page, SEL_ZOOM_CLOSE, 1500)
    time.sleep(0.8)


def scrape_current_section(page, key, slug, global_idx, model_dir, http_session, delay, global_seen_urls, global_seen_hashes):
    zoom = ensure_zoom_mode(page)

    _, total = read_counter(page)
    loop_count = total if total > 0 else 30
    debug_log(f' -> {key}: {total if total > 0 else "Unknown"} photo(s) [zoom={zoom}]')
    dump_page_state(page, f'{key}_opened', note=f'zoom={zoom} total={total} loop_count={loop_count}')

    last_sig = None
    stuck_repeats = 0
    consecutive_misses = 0

    for i in range(1, loop_count + 1):
        before_sig = active_frame_signature(page)
        debug_log(f' [frame] {key} idx={i} before={before_sig}')
        res = grab_image(page)
        stem = f'{slug} - {global_idx:03d}'

        if not res:
            consecutive_misses += 1
            debug_log(' MISS (Timeout waiting for High-Res)')
            dump_page_state(page, f'{key}_miss_{i:03d}', note='grab_image returned None')
            if consecutive_misses >= 3:
                debug_log(' [!] Multiple misses detected. Breaking early.')
                dump_page_state(page, f'{key}_miss_break_{i:03d}', note='multiple misses')
                break
            if i < loop_count:
                click_next(page)
                time.sleep(max(1.5, delay))
            continue

        consecutive_misses = 0
        ext, payload = res
        reacquire_attempted = False

        while True:
            if ext == 'src_url':
                url = payload.decode()
                clean_url = url.split('?')[0]
                sig = ('url', clean_url)
                out = model_dir / f"{stem}.{clean_url.rsplit('.',1)[-1] or 'jpg'}"
                try:
                    w, h = download_url_checked(url, http_session, out)
                except Exception as e:
                    msg = str(e)
                    if ('low-res image blocked' in msg) and (not reacquire_attempted):
                        reacquire_attempted = True
                        debug_log(f' [retry] {stem} low-res URL candidate; forcing zoom re-acquire')
                        ensure_zoom_mode(page)
                        time.sleep(1.2)
                        res2 = get_best_image_url_from_page(page)
                        if res2:
                            ext, payload = res2
                            continue
                    debug_log(f' SKIP {stem} ({msg})')
                    break
                if sig == last_sig:
                    stuck_repeats += 1
                else:
                    last_sig = sig
                    stuck_repeats = 0
                debug_log(f' [sig] {key} idx={i} sig={sig} stuck_repeats={stuck_repeats}')
                if clean_url in global_seen_urls:
                    try:
                        out.unlink(missing_ok=True)
                    except Exception:
                        pass
                    debug_log(f' SKIP {stem} (Global Duplicate URL)')
                else:
                    global_seen_urls.add(clean_url)
                    debug_log(f' OK {out.name} [{w}x{h}]')
                    global_idx += 1
                break
            else:
                payload_hash = hashlib.md5(payload).hexdigest()
                sig = ('b64', payload_hash)
                out = model_dir / f'{stem}.{ext}'
                try:
                    w, h = save_bytes_checked(payload, out)
                except Exception as e:
                    msg = str(e)
                    if ('low-res image blocked' in msg) and (not reacquire_attempted):
                        reacquire_attempted = True
                        debug_log(f' [retry] {stem} low-res B64 candidate; forcing zoom re-acquire')
                        ensure_zoom_mode(page)
                        time.sleep(1.2)
                        res2 = get_best_image_url_from_page(page)
                        if res2:
                            ext, payload = res2
                            continue
                    debug_log(f' SKIP {stem} ({msg})')
                    break
                if sig == last_sig:
                    stuck_repeats += 1
                else:
                    last_sig = sig
                    stuck_repeats = 0
                debug_log(f' [sig] {key} idx={i} sig={sig} stuck_repeats={stuck_repeats}')
                if payload_hash in global_seen_hashes:
                    try:
                        out.unlink(missing_ok=True)
                    except Exception:
                        pass
                    debug_log(f' SKIP {stem} (Global Duplicate Base64)')
                else:
                    global_seen_hashes.add(payload_hash)
                    debug_log(f' OK {out.name} [{w}x{h}]')
                    global_idx += 1
                break

        if stuck_repeats >= 3:
            debug_log(' [!] Same frame repeated after navigation. Breaking early.')
            dump_page_state(page, f'{key}_repeat_break_{i:03d}', note=f'sig={last_sig}')
            break

        if i < loop_count:
            click_next(page)
            time.sleep(max(1.5, delay))
            after_sig = active_frame_signature(page)
            debug_log(f' [next] {key} idx={i} after={after_sig}')

    force_close_modal(page)
    time.sleep(0.5)
    return global_idx


def scrape_model(url, profile_dir, output_dir, headless, delay, debug, media="photos"):
    global DEBUG, DEBUG_DIR, DEBUG_LOG_FH
    slug = extract_slug(url)
    model_dir = Path(output_dir) / slug
    model_dir.mkdir(parents=True, exist_ok=True)

    DEBUG = debug
    if DEBUG:
        DEBUG_DIR = model_dir / '_debug_artifacts'
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        DEBUG_LOG_FH = open(DEBUG_DIR / 'run.log', 'w', encoding='utf-8')

    debug_log(f'[*] Model  : {slug}')
    debug_log(f'[*] Output : {model_dir.resolve()}')
    debug_log(f'[*] Media  : {media}')

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=headless,
            viewport={'width': 1366, 'height': 1000},
            args=['--disable-blink-features=AutomationControlled'],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        http_session = requests.Session()
        http_session.headers['User-Agent'] = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'

        start_url = model_video_url(url) if media == 'videos' else url
        debug_log(f'[*] Navigating to {start_url} ...')
        safe_goto(page, start_url)
        for c in ctx.cookies():
            http_session.cookies.set(c['name'], c['value'], domain=c.get('domain', ''), path=c.get('path', '/'))

        if debug:
            dump_page_state(page, '_debug_01_loaded', note='after initial load')
            debug_log('[debug] _debug_01_loaded')

        dismiss_overlays(page)

        if debug:
            dump_page_state(page, '_debug_02_clean', note='after overlay dismiss')
            debug_log('[debug] _debug_02_clean')

        debug_log('[*] Waiting 3 s for gallery UI to render ...')
        time.sleep(3)

        if media == 'videos':
            saved = scrape_videos(page, model_dir, slug, http_session, delay, debug)
            ctx.close()
            if DEBUG_LOG_FH:
                DEBUG_LOG_FH.close()
                DEBUG_LOG_FH = None
            print('')
            print(f'[OK] Done -- {saved} video file(s) saved to {(model_dir / "videos").resolve()}')
            return

        tabs = find_parte_tabs(page)
        if tabs:
            debug_log(f'[*] Parte tabs found: {len(tabs)} -> {tabs}')
        else:
            debug_log('[*] No Parte tabs found; will process current view as Parte_1')
            tabs = ['Parte 1']

        global_idx = 1
        done = set()
        global_seen_urls = set()
        global_seen_hashes = set()

        for label in tabs:
            safe_part = re.sub(r'\s+', '_', label.strip())
            debug_log(f'\n[*] Processing: {safe_part}')
            if label != 'Parte 1' or len(tabs) > 1:
                try:
                    force_close_modal(page)
                    part_loc = page.locator(f'text="{label}" >> visible=true').last
                    debug_log(f'[debug] clicking part {label}')
                    part_loc.click(timeout=7000, force=True)
                    time.sleep(3.0)
                    dump_page_state(page, f'{safe_part}_after_click', note=f'clicked {label}')
                except Exception as e:
                    debug_log(f'[warn] could not click {label}: {e}')
                    dump_page_state(page, f'{safe_part}_click_fail', note=str(e))
                    continue

            tiles, used = find_section_tiles(page)
            debug_log(f' Sections found ({used}): {len(tiles)} -> {tiles}')
            dump_page_state(page, f'{safe_part}_sections_list', note=str(tiles))
            if not tiles:
                debug_log(f' [warn] no tiles in {safe_part} — skipping')
                continue

            for idx, tile in enumerate(tiles, 1):
                label2 = tile_label(tile, idx)
                key = f'{safe_part}_{label2}'
                if key in done:
                    continue
                done.add(key)
                try:
                    force_close_modal(page)
                    tile_loc = page.locator(f'text="{tile}" >> visible=true').last
                    count = page.locator(f'text="{tile}" >> visible=true').count()
                    debug_log(f'[debug] clicking section {tile} for key {key}; locator_count={count}')
                    tile_loc.scroll_into_view_if_needed(timeout=7000)
                    tile_loc.click(timeout=7000, force=True)
                    time.sleep(2.5)
                    dump_page_state(page, f'{key}_after_click', note=f'clicked section {tile}')
                    debug_log(f"\n [{'LOCKED' if tile_is_locked(tile_loc) else 'OPEN '}] {key}")
                    global_idx = scrape_current_section(
                        page, key, slug, global_idx, model_dir, http_session, delay,
                        global_seen_urls, global_seen_hashes
                    )
                except Exception as e:
                    debug_log(f' [warn] {key}: {e}')
                    dump_page_state(page, f'{key}_click_fail', note=str(e))

        if debug:
            dump_page_state(page, '_debug_03_done', note='final state')
            debug_log('[debug] _debug_03_done')

        ctx.close()

    done_count = global_idx - 1
    if DEBUG_LOG_FH:
        DEBUG_LOG_FH.close()
        DEBUG_LOG_FH = None
    print(f'\n[OK] Done -- {done_count} file(s) saved to {model_dir.resolve()}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Download Bella da Semana high-res photos (instrumented debug build)')
    ap.add_argument('--setup', action='store_true')
    ap.add_argument('--profile', default='./bds_profile')
    ap.add_argument('--url')
    ap.add_argument('--output', default='./bds_photos')
    ap.add_argument('--headless', action='store_true')
    ap.add_argument('--delay', type=float, default=1.5)
    ap.add_argument('--debug', action='store_true')
    ap.add_argument('--media', choices=['photos', 'videos'], default='photos')
    args = ap.parse_args()

    if args.setup:
        print('Setup skipped in run block')
    elif not args.url:
        ap.error('--url is required unless using --setup')
    else:
        scrape_model(args.url, args.profile, args.output, args.headless, args.delay, args.debug, args.media)

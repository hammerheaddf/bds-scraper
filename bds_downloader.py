#!/usr/bin/env python3
import argparse
import base64
import hashlib
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests
from tqdm import tqdm

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit("[error] Run: pip install playwright requests tqdm && playwright install chromium")

BASE64_RE = re.compile(r"data:image/(webp|jpeg|jpg|png);base64,([A-Za-z0-9+/=]+)", re.I)
# Base64 strings for 1MB images are usually > 1,000,000 characters. 
# Set a high minimum to avoid grabbing base64 thumbnails/spinners.
MIN_B64_LEN = 100000 
_SLUG_SKIP = {"photo", "fotos", "videos", "galeria", "pt-br", "en", "modelos", "modelo"}

SEL_TERMS = [
    "a:has-text('ACEITO OS TERMOS')", "button:has-text('ACEITO OS TERMOS')",
    "a:has-text('Aceito os termos')", "button:has-text('Aceito os termos')",
    "button:has-text('Aceito')", "button:has-text('Concordo')",
]
SEL_COOKIES = [".lgpd-accept-btn-allow", "button:has-text('Aceitar todos os cookies')"]

# "expanding X arrow below the image"
SEL_ZOOM = [
    "img[src*='maximize']",
    "img[src*='expand']",
    ".fa-expand",
    ".fa-arrows-alt",
    "[class*='expand' i]",
    "[class*='maximize' i]",
    "[title*='expand' i]",
    "[title*='zoom' i]",
    "button:has(svg.carousel__icon)"
]

# Target active slides, and include canvas elements in case they are drawing the image
SEL_ZOOM_IMG = [
    ".modal-mask .carousel__slide--active img",
    ".modal-mask .carousel__slide--active canvas",
    ".modal-container .carousel__slide--active img",
    ".carousel__slide--active img",
    ".carousel__slide--active canvas",
    ".pswp__img", 
    "canvas.pswp__img",
    ".fancybox-image", 
    ".yarl__slide_image",
    ".modal-mask img",
    ".modal-mask canvas"
]

SEL_ZOOM_CLOSE = [
    ".carousel__close", ".pswp__button--close", ".slideclose", 
    ".close-modal", ".close-black", "[aria-label='Close']", ".modal-container [class*='close']"
]

SEL_NEXT = [
    ".carousel__next",
    ".pswp__button--arrow--right", 
    ".owl-next", 
    "button:has(.fa-chevron-right)",
    "button:has(.angle-right)",
    "[aria-label*='next' i]",
    "[aria-label*='próximo' i]"
]

def extract_slug(url: str) -> str:
    parts = urlparse(url).path.strip('/').split('/')
    return next((p for p in reversed(parts) if p.lower() not in _SLUG_SKIP), parts[-1])

def get_highres_url(url: str) -> str:
    if not url: return ""
    url = re.sub(r'/cdn-cgi/image/[^/]+/', '/', url)
    url = re.sub(r'/_nuxt/image/[^/]+/[^/]+/', '/', url)
    return url

def safe_goto(page, url: str):
    page.goto(url, wait_until='domcontentloaded', timeout=45000)
    try:
        page.wait_for_selector('body', state='visible', timeout=10000)
    except PWTimeout:
        pass
    print('[*] Waiting 5 s for page to fully load ...')
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
    print('[*] Waiting 5 s for modals to appear ...')
    time.sleep(5)
    hit = try_click(page, SEL_TERMS)
    print(f"[+] Terms modal  : {'dismissed' if hit else 'not found'}")
    if hit: time.sleep(2)
    hit = try_click(page, SEL_COOKIES)
    print(f"[+] Cookie banner: {'dismissed' if hit else 'not found'}")
    if hit: time.sleep(2)

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
    except: pass

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
        return 'seção' in txt and ('comprar' in txt or 'assine' in txt)
    except Exception:
        return False

def grab_image(page, retries=15, interval=0.5):
    # Safely evaluate javascript without using nested python triple-quotes
    js_eval = (
        "el => {"
        "  let src = el.getAttribute('data-src') || el.getAttribute('data-zoom') || el.src || '';"
        "  let w = el.naturalWidth || el.clientWidth || 0;"
        "  let h = el.naturalHeight || el.clientHeight || 0;"
        "  if (el.tagName.toLowerCase() === 'canvas') {"
        "    src = el.toDataURL('image/jpeg', 0.95);"
        "    w = el.width || el.clientWidth || 0;"
        "    h = el.height || el.clientHeight || 0;"
        "  }"
        "  return { src: src, width: w, height: h };"
        "}"
    )

    # Increased retries to handle the delay while base64 decodes
    for attempt in range(retries):
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

                    # ENFORCE HIGH-RES: Reject if longest side is < 800px (Wait for actual render)
                    if max(width, height) < 800:
                        continue

                    # 1. Base64 Handling
                    m = BASE64_RE.match(src)
                    if m:
                        if len(m.group(2)) >= MIN_B64_LEN:
                            ext = 'jpg' if m.group(1).lower() == 'jpeg' else m.group(1).lower()
                            return ext, base64.b64decode(m.group(2))
                        else:
                            # If it's a tiny base64 placeholder, ignore it and wait for the high-res to load
                            continue 

                    # 2. Standard HTTP URL Handling
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
    zoom = False
    for _ in range(3):
        if try_click(page, SEL_ZOOM, 2000):
            zoom = True
            time.sleep(2.5)
            break
        time.sleep(1.0)

    if not zoom:
        try:
            active_img = page.locator(".carousel__slide--active img >> visible=true").last
            active_img.dblclick(timeout=1000, force=True)
            zoom = True
            time.sleep(2.0)
        except Exception:
            pass

    _, total = read_counter(page)
    loop_count = total if total > 0 else 30
    tqdm.write(f'  -> {key}: {total if total > 0 else "Unknown"} photo(s) [zoom={zoom}]')

    last_sig = None
    stuck_repeats = 0
    consecutive_misses = 0

    for i in range(1, loop_count + 1):
        res = grab_image(page)
        stem = f'{slug} - {global_idx:03d}'

        if not res:
            consecutive_misses += 1
            tqdm.write('    MISS (Timeout waiting for High-Res)')
            if consecutive_misses >= 3:
                tqdm.write('    [!] Multiple misses detected. Breaking early.')
                break
            if i < loop_count:
                click_next(page)
                time.sleep(max(1.5, delay))
            continue

        consecutive_misses = 0
        ext, payload = res

        if ext == 'src_url':
            url = payload.decode()
            clean_url = url.split('?')[0]
            sig = ('url', clean_url)
        else:
            payload_hash = hashlib.md5(payload).hexdigest()
            sig = ('b64', payload_hash)

        if sig == last_sig:
            stuck_repeats += 1
        else:
            last_sig = sig
            stuck_repeats = 0

        if stuck_repeats >= 3:
            tqdm.write('    [!] Same frame repeated after navigation. Breaking early.')
            break

        if ext == 'src_url':
            if clean_url in global_seen_urls:
                tqdm.write(f'    SKIP {stem} (Global Duplicate URL)')
            else:
                global_seen_urls.add(clean_url)
                out = model_dir / f"{stem}.{clean_url.rsplit('.',1)[-1] or 'jpg'}"
                download_url(url, http_session, out)
                tqdm.write(f'    OK {out.name}')
                global_idx += 1
        else:
            if payload_hash in global_seen_hashes:
                tqdm.write(f'    SKIP {stem} (Global Duplicate Base64)')
            else:
                global_seen_hashes.add(payload_hash)
                out = model_dir / f'{stem}.{ext}'
                save_bytes(payload, out)
                tqdm.write(f'    OK {out.name}')
                global_idx += 1

        if i < loop_count:
            click_next(page)
            time.sleep(max(1.5, delay))

    force_close_modal(page)
    time.sleep(0.5)
    return global_idx

def scrape_model(url, profile_dir, output_dir, headless, delay, debug):
    slug = extract_slug(url)
    model_dir = Path(output_dir) / slug
    model_dir.mkdir(parents=True, exist_ok=True)
    print(f'[*] Model  : {slug}')
    print(f'[*] Output : {model_dir.resolve()}')
    
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
        
        print(f'[*] Navigating to {url} ...')
        safe_goto(page, url)
        
        for c in ctx.cookies():
            http_session.cookies.set(c['name'], c['value'], domain=c.get('domain', ''), path=c.get('path', '/'))
            
        if debug:
            page.screenshot(path=str(model_dir / '_debug_01_loaded.png'), full_page=True)
            print('[debug] _debug_01_loaded.png')
            
        dismiss_overlays(page)
        
        if debug:
            page.screenshot(path=str(model_dir / '_debug_02_clean.png'), full_page=True)
            print('[debug] _debug_02_clean.png')
            
        print('[*] Waiting 3 s for gallery UI to render ...')
        time.sleep(3)

        tabs = find_parte_tabs(page)
        if tabs:
            print(f'[*] Parte tabs found: {len(tabs)}')
        else:
            print('[*] No Parte tabs found; will process current view as Parte_1')
            tabs = ['Parte 1']

        global_idx = 1
        done = set()
        global_seen_urls = set()
        global_seen_hashes = set()

        for label in tabs:
            safe_part = re.sub(r'\s+', '_', label.strip())
            tqdm.write(f'\n[*] Processing: {safe_part}')
            if label != 'Parte 1' or len(tabs) > 1:
                try:
                    force_close_modal(page)
                    page.locator(f'text="{label}" >> visible=true').last.click(timeout=7000, force=True)
                    time.sleep(3.0)
                except Exception as e:
                    tqdm.write(f'[warn] could not click {label}: {e}')
                    continue

            tiles, used = find_section_tiles(page)
            print(f" Sections found ({used}): {len(tiles)}")
            if not tiles:
                tqdm.write(f' [warn] no tiles in {safe_part} — skipping')
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
                    tile_loc.scroll_into_view_if_needed(timeout=7000)
                    tile_loc.click(timeout=7000, force=True)
                    time.sleep(2.5)
                    tqdm.write(f"\n [{'LOCKED' if tile_is_locked(tile_loc) else 'OPEN '}] {key}")

                    global_idx = scrape_current_section(
                        page, key, slug, global_idx, model_dir, http_session, delay,
                        global_seen_urls, global_seen_hashes
                    )

                except Exception as e:
                    tqdm.write(f' [warn] {key}: {e}')

        if debug:
            page.screenshot(path=str(model_dir / '_debug_03_done.png'), full_page=True)
            print('[debug] _debug_03_done.png')
            
        ctx.close()
        
    print(f'\n[OK] Done -- {global_idx - 1} file(s) saved to {model_dir.resolve()}')

if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Download Bella da Semana high-res photos')
    ap.add_argument('--setup', action='store_true')
    ap.add_argument('--profile', default='./bds_profile')
    ap.add_argument('--url')
    ap.add_argument('--output', default='./bds_photos')
    ap.add_argument('--headless', action='store_true')
    ap.add_argument('--delay', type=float, default=1.5)
    ap.add_argument('--debug', action='store_true')
    args = ap.parse_args()
    
    if args.setup:
        print("Setup skipped in run block")
    elif not args.url:
        ap.error('--url is required unless using --setup')
    else:
        scrape_model(args.url, args.profile, args.output, args.headless, args.delay, args.debug)

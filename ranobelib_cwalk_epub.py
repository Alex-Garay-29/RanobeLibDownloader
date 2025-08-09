#!/usr/bin/env python3
"""
Ranobelib EPUB crawler: start from a first chapter URL, then iterate chapters by
incrementing cN -> cN+1 (preserving bid/ui). For each chapter, extract the title,
text content, and images, and assemble a single EPUB.

- Uses Selenium (Firefox) to load pages reliably.
- Uses requests to download images (reusing Selenium cookies).
- Simulates human behavior with random delays and scrolling.
- EPUB ToC entries are "глава N" (in Russian), while in-chapter title stays as on site.

Note: Respect the website's ToS and rate limits. For personal/educational use only.
"""

import argparse
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, urljoin

import requests
from bs4 import BeautifulSoup
from ebooklib import epub
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# Constants
RANOBELIB_ORIGIN = "https://ranobelib.me"
CHAPTER_DIR = "text"   # EPUB/text/...
IMAGES_DIR = "images"  # EPUB/images/...
STYLE_DIR = "style"    # EPUB/style/...
TOC_LABEL = "глава"    # ToC item label prefix: "глава 1", "глава 2", ...

# Utilities

def human_sleep(a: float = 1.0, b: float = 2.2) -> None:
    """Small random sleep to mimic human behavior."""
    time.sleep(random.uniform(a, b))


def longer_sleep() -> None:
    """Longer random sleep to avoid triggering anti-bot heuristics."""
    time.sleep(random.uniform(3.0, 6.0))


def make_driver(headless: bool = False, user_agent: Optional[str] = None) -> webdriver.Firefox:
    """Create and configure a Firefox WebDriver."""
    opts = FirefoxOptions()
    if headless:
        opts.add_argument("-headless")
    # Mild anti-detection tweaks
    opts.set_preference("dom.webdriver.enabled", False)
    opts.set_preference("useAutomationExtension", False)
    opts.set_preference("intl.accept_languages", "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7")
    if user_agent:
        opts.set_preference("general.useragent.override", user_agent)

    driver = webdriver.Firefox(options=opts)
    driver.set_page_load_timeout(60)
    driver.set_script_timeout(60)
    return driver


def wait_present(driver, by, selector, timeout=45):
    """Wait until element is present in DOM."""
    return WebDriverWait(driver, timeout).until(EC.presence_of_element_located((by, selector)))


def scroll_like_human(driver, steps: Optional[int] = None) -> None:
    """Gradually scroll the page to trigger lazy-loading and mimic reading."""
    steps = steps or random.randint(2, 4)
    height = driver.execute_script("return document.body.scrollHeight")
    for i in range(steps):
        y = int((i + 1) * height / steps)
        driver.execute_script("window.scrollTo(0, arguments[0]);", y)
        human_sleep(0.4, 0.9)


def sync_cookies(driver, sess: requests.Session) -> None:
    """Copy cookies from Selenium to requests.Session for image fetching."""
    for c in driver.get_cookies():
        try:
            sess.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
        except Exception:
            sess.cookies.set(c["name"], c["value"], path=c.get("path", "/"))


def guess_mime_from_url(url: str) -> str:
    """Infer image MIME from URL path extension."""
    path = urlparse(url).path.lower()
    if path.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if path.endswith(".png"):
        return "image/png"
    if path.endswith(".webp"):
        return "image/webp"
    if path.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


def slugify(s: str) -> str:
    """URL/file-system friendly slug. Keeps Unicode letters."""
    s = s.lower().strip()
    s = re.sub(r"[^\w\- ]+", "", s)
    s = s.replace(" ", "-")
    return re.sub(r"-+", "-", s)


def extract_book_title(driver) -> str:
    """Extract the Russian book title from the reading page."""
    soup = BeautifulSoup(driver.page_source, "html.parser")
    el = soup.select_one("div.va_ve")
    if el and el.get_text(strip=True):
        return el.get_text(strip=True)
    return driver.title or "ranobelib-book"

# Data types

@dataclass
class ChapterResult:
    """Container for a parsed chapter."""
    title: str        # As on site, e.g. "Том 1 Глава 1"
    toc_title: str    # For ToC, e.g. "глава 1"
    content_html: str # XHTML body fragment (will be wrapped for EPUB)

# URL helpers

VC_RE = re.compile(r"/read/v0*(\d+)/c0*(\d+)", re.I)

def parse_vc(path: str) -> Optional[Tuple[int, int]]:
    """Parse (v, c) from a /read/vX/cY path."""
    m = VC_RE.search(path)
    return (int(m.group(1)), int(m.group(2))) if m else None


def build_url_with_c(base_url: str, new_c: int) -> str:
    """
    Replace the chapter number in the path with new_c, preserving everything else
    (locale, slug, volume number v, and query parameters such as bid/ui).
    """
    u = urlparse(base_url)
    m = VC_RE.search(u.path)
    if not m:
        raise ValueError("Cannot parse v/c from URL")
    v = int(m.group(1))
    new_path = re.sub(r"/read/v0*\d+/c0*\d+", f"/read/v{v}/c{new_c}", u.path, count=1, flags=re.I)
    return urlunparse(u._replace(path=new_path))

# DOM helpers

def is_descendant(parent, node) -> bool:
    """Check if node is within parent in the parsed HTML tree."""
    for d in parent.descendants:
        if d is node:
            return True
    return False


def fetch_image(sess: requests.Session,
                driver,
                src_url: str,
                img_map: Dict[str, str],
                images_bin: Dict[str, Tuple[str, bytes]],
                counter: List[int]) -> Optional[str]:
    """Download image once and register it into the EPUB image map. Return EPUB-relative filename."""
    if not src_url:
        return None
    full = urljoin(RANOBELIB_ORIGIN, src_url)
    if full in img_map:
        return img_map[full]

    headers = {
        "User-Agent": driver.execute_script("return navigator.userAgent;"),
        "Referer": driver.current_url,
    }
    try:
        r = sess.get(full, headers=headers, timeout=45)
        r.raise_for_status()
    except Exception:
        return None

    mime = (r.headers.get("Content-Type") or guess_mime_from_url(full)).split(";")[0].strip()
    ext = ".jpg"
    if "png" in mime:
        ext = ".png"
    elif "webp" in mime:
        ext = ".webp"
    elif "gif" in mime:
        ext = ".gif"

    counter[0] += 1
    fname = f"{IMAGES_DIR}/img_{counter[0]:05d}{ext}"
    images_bin[fname] = (mime, r.content)
    img_map[full] = fname
    return fname


def parse_current_chapter(driver,
                          sess: requests.Session,
                          seq_index: int,
                          img_map: Dict[str, str],
                          images_bin: Dict[str, Tuple[str, bytes]],
                          counter: List[int]) -> ChapterResult:
    """Parse current chapter page: title, content, and images."""
    wait_present(driver, By.CSS_SELECTOR, "div.node-doc.text-content", timeout=45)
    scroll_like_human(driver)
    human_sleep(0.6, 1.2)

    soup = BeautifulSoup(driver.page_source, "html.parser")

    # Chapter heading displayed on the page
    header = soup.select_one("h1.l2_cc")
    chapter_title = header.get_text(strip=True) if header and header.get_text(strip=True) else "Глава"

    # Main content
    content_el = soup.select_one("div.node-doc.text-content")
    if not content_el:
        raise RuntimeError("Chapter content not found (div.node-doc.text-content)")

    # Images inside the content
    for img in content_el.find_all("img"):
        src = img.get("src") or img.get("data-src")
        fn = fetch_image(sess, driver, src, img_map, images_bin, counter)
        if fn:
            img["src"] = f"../{fn}"  # chapters in text/, images in images/
            for a in ["loading", "style", "class", "data-src", "decoding", "referrerpolicy"]:
                if a in img.attrs:
                    del img.attrs[a]
        else:
            img.decompose()

    # Possible trailing images outside the content (e.g., bonus images at the end)
    extra_imgs = []
    for ext_img in soup.select("img._loaded.node-image-item__img, img.node-image-item__img, img"):
        if is_descendant(content_el, ext_img):
            continue
        src = ext_img.get("src") or ext_img.get("data-src") or ""
        if "/uploads/ranobe/" in src:
            extra_imgs.append(ext_img)

    for ext_img in extra_imgs:
        src = ext_img.get("src") or ext_img.get("data-src")
        fn = fetch_image(sess, driver, src, img_map, images_bin, counter)
        if fn:
            p = soup.new_tag("p")
            p.append(soup.new_tag("img", src=f"../{fn}"))
            content_el.append(p)

    # Cleanup scripts/styles
    for t in content_el.find_all(["script", "style"]):
        t.decompose()

    content_html = f"""
    <h2>{chapter_title}</h2>
    {str(content_el)}
    """
    toc_title = f"{TOC_LABEL} {seq_index}"

    return ChapterResult(title=chapter_title, toc_title=toc_title, content_html=content_html)

# EPUB packing

def _make_xhtml_doc(title: str, inner: str) -> bytes:
    """Wrap body fragment into a minimal valid XHTML document."""
    inner = (inner or "").strip()
    text_like = re.sub(r"<[^>]+>", "", inner).strip()
    if not inner or (not text_like and "<img" not in inner):
        inner = "<p>&nbsp;</p>"
    html = f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <title>{title}</title>
  <meta charset="utf-8"/>
  <link rel="stylesheet" type="text/css" href="../{STYLE_DIR}/style.css"/>
</head>
<body>
{inner}
</body>
</html>"""
    return html.encode("utf-8")


def build_epub(book_title: str,
               chapters: List[ChapterResult],
               images_bin: Dict[str, Tuple[str, bytes]],
               out_path: str):
    """Assemble and write an EPUB file."""
    book = epub.EpubBook()
    book.set_identifier(f"rb-{random.randint(100000, 999999)}")
    book.set_title(book_title)
    book.set_language("ru")
    book.add_author("Ranobelib (parsed)")

    # Styles
    css = """
    body { font-family: serif; line-height: 1.5; }
    h2 { margin: 0.8em 0 0.8em 0; font-size: 1.25em; }
    img { max-width: 100%; height: auto; display: block; margin: 0.6em auto; }
    p { margin: 0.6em 0; }
    """
    style_item = epub.EpubItem(
        uid="style_css",
        file_name=f"{STYLE_DIR}/style.css",
        media_type="text/css",
        content=css.encode("utf-8"),
    )
    book.add_item(style_item)

    # Images
    for i, (fname, (mime, content)) in enumerate(sorted(images_bin.items()), start=1):
        book.add_item(epub.EpubItem(uid=f"img_{i:05d}", file_name=fname, media_type=mime, content=content))

    # Chapters
    epub_chs = []
    for idx, ch in enumerate(chapters, start=1):
        file_name = f"{CHAPTER_DIR}/ch_{idx:05d}.xhtml"
        c = epub.EpubHtml(title=ch.toc_title, file_name=file_name, uid=f"ch_{idx:05d}", lang="ru")
        c.set_content(_make_xhtml_doc(ch.toc_title, ch.content_html))
        book.add_item(c)
        epub_chs.append(c)

    # TOC / spine / nav
    book.toc = tuple(epub_chs)
    book.spine = ["nav"] + epub_chs
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    epub.write_epub(out_path, book, {})

# Main run

def run(first_chapter_url: str,
        out_dir: str,
        headless: bool,
        max_chapters: Optional[int],
        c_end: Optional[int],
        auto_next_volume: bool):
    """Drive the browser and iterate cN -> cN+1 until exhausted or limited."""
    driver = make_driver(headless=headless)
    sess = requests.Session()
    try:
        driver.get(first_chapter_url)
        human_sleep(1.5, 3.0)

        book_title = extract_book_title(driver)
        base_name = slugify(book_title) or "ranobelib-book"
        out_path = os.path.join(out_dir, f"{base_name}.epub")
        print(f"Book: {book_title}")
        print(f"Output: {out_path}")

        start_parsed = urlparse(first_chapter_url)
        start_vc = parse_vc(start_parsed.path)
        if not start_vc:
            raise ValueError("Start URL doesn't look like a reading page (/read/vX/cY)")
        v, c = start_vc

        sync_cookies(driver, sess)
        try:
            sess.headers.update({"User-Agent": driver.execute_script("return navigator.userAgent;")})
        except Exception:
            pass

        visited_paths = set()
        chapters: List[ChapterResult] = []
        img_map: Dict[str, str] = {}
        images_bin: Dict[str, Tuple[str, bytes]] = {}
        counter = [0]

        seq = 1
        while True:
            if c_end and c > c_end:
                print(f"Reached c_end={c_end}")
                break

            target_url = build_url_with_c(first_chapter_url, c)
            print(f"[{seq}] Loading: {target_url}")
            driver.get(target_url)

            try:
                wait_present(driver, By.CSS_SELECTOR, "div.node-doc.text-content", timeout=45)
            except TimeoutException:
                if auto_next_volume:
                    # Try next volume v+1/c1
                    new_path = re.sub(r"/read/v0*\d+/c0*\d+", f"/read/v{v+1}/c1",
                                      urlparse(first_chapter_url).path, count=1, flags=re.I)
                    candidate = urlunparse(urlparse(first_chapter_url)._replace(path=new_path))
                    print(f"No content for c={c}. Trying next volume: {candidate}")
                    driver.get(candidate)
                    try:
                        wait_present(driver, By.CSS_SELECTOR, "div.node-doc.text-content", timeout=30)
                        v += 1
                        c = 1
                    except TimeoutException:
                        print("Looks like there are no more chapters.")
                        break
                else:
                    print("No content found. Stopping.")
                    break

            path_now = urlparse(driver.current_url).path
            if path_now in visited_paths:
                print("Detected repeat of the same chapter path (redirect/loop). Stopping.")
                break
            visited_paths.add(path_now)

            sync_cookies(driver, sess)
            ch = parse_current_chapter(driver, sess, seq, img_map, images_bin, counter)
            chapters.append(ch)
            print(f"Added: {ch.title}")

            human_sleep(1.0, 2.0)
            if seq % 10 == 0:
                longer_sleep()
            if max_chapters and seq >= max_chapters:
                print(f"Reached chapter limit: {max_chapters}")
                break

            c += 1
            seq += 1

        os.makedirs(out_dir, exist_ok=True)
        build_epub(book_title, chapters, images_bin, out_path)
        print(f"Done: {out_path}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(
        description="Download a Ranobelib novel into EPUB by incrementing cN -> cN+1."
    )
    parser.add_argument("--url", required=True, help="URL of the first chapter (reading page).")
    parser.add_argument("--out", default="output", help="Directory to store the resulting EPUB.")
    parser.add_argument("--headless", type=int, default=0, help="1 = headless, 0 = visible Firefox.")
    parser.add_argument("--max-chapters", type=int, default=None, help="Max number of chapters to download.")
    parser.add_argument("--cend", type=int, default=None, help="Stop after this chapter number (same volume).")
    parser.add_argument("--auto-next-volume", type=int, default=1, help="Try v+1/c1 if a chapter doesn't load.")
    args = parser.parse_args()

    run(
        first_chapter_url=args.url,
        out_dir=args.out,
        headless=bool(args.headless),
        max_chapters=args.max_chapters,
        c_end=args.cend,
        auto_next_volume=bool(args.auto_next_volume),
    )


if __name__ == "__main__":
    main()
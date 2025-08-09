# Ranobelib EPUB Crawler

  

Download Ranobelib chapters into a single EPUB, starting from a given first chapter URL and iterating by simply incrementing the chapter number in the path (`/read/v{v}/c{n} -> c{n+1}`), preserving the same translation (bid/ui).

  

- Selenium (Firefox) is used for robust page loading.

- Requests is used to fetch images (cookies are synchronized from the browser).

- Human-like delays and scrolling to avoid anti-bot triggers.

- EPUB ToC entries are in Russian: "глава N". In-chapter headings are original (e.g., "Том 1 Глава 1").

  

Use this responsibly and respect the website's Terms of Service.

  

## Features

- Start from the first chapter URL; auto-iterate cN -> cN+1.

- Optional auto-switch to the next volume (v+1/c1) when a chapter doesn’t load.

- Grabs text and images (including trailing images at the end of a chapter).

- Produces a valid EPUB with TOC and styles.

  

## Requirements

- Python 3.10+

- Firefox + geckodriver

- Packages: selenium, requests, beautifulsoup4, ebooklib, lxml

  

## Installation

```bash

python -m venv .venv

source .venv/bin/activate

pip install -r requirements.txt

# or:

# pip install selenium requests beautifulsoup4 ebooklib lxml
```

#### On Arch/Manjaro:

```bash
sudo pacman -S firefox geckodriver python-pip
```

## Usage

```bash

python ranobelib_cwalk_epub.py \
  --url "https://ranobelib.me/ru/<slug>/read/v1/c1?bid=XXXX&ui=YYYY" \
  --out "./output" \
  --headless 0 \
  --max-chapters 20 \
  --cend 30 \
  --auto-next-volume 1
```

#### Arguments:

- --url: First chapter reading URL (required).
- --out: Output directory for the EPUB (default: ./output).
- --headless: 1 = headless Firefox, 0 = visible (default: 0).
- --max-chapters: Stop after N chapters (optional).
- --cend: Stop at a specific chapter number in the current volume (optional).
- --auto-next-volume: If 1, try v+1/c1 when a chapter fails to load (default: 1).

## Notes

- ToC entries will be labeled as "Chapter N". Adjust `TOC_LABEL` in the script if you prefer a different text (e.g., "Chapter").
- The script keeps the same translation branch by preserving `bid/ui` from the first URL.
- Random delays and scrolling are used to behave politely.

###  Disclaimer

This project is for personal/educational use. Check and follow the website's Terms of Service and comply with applicable laws. You are responsible for how you use this code.


import json
import os
import re
import sys
from base64 import b64decode
from io import BytesIO
from datetime import datetime, timezone

import gspread
import requests
from google import genai
from google.oauth2.service_account import Credentials
from PIL import Image, ImageDraw, ImageFont


# ============================================================
# CONFIGURATION
# ============================================================

SPREADSHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1S77B3Lj17ChY-yb2mteCF-PM85nYyLmeJkHYv-TiQ4s/edit"
)

WORKSHEET_NAME = "Content"

GEMINI_MODEL = "gemini-3.6-flash"

CLOUDFLARE_IMAGE_MODEL = "@cf/black-forest-labs/flux-1-schnell"

CLOUDFLARE_IMAGE_STEPS = 8

FINAL_IMAGE_SIZE = "1024x768"

MAX_TWEET_LENGTH_PER_LANGUAGE = 240

TELEGRAM_CAPTION_LIMIT = 1024

TEST_ID = "TEST"

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT_JSON"
)

GEMINI_API_KEY = os.environ.get(
    "GEMINI_API_KEY"
)

CLOUDFLARE_ACCOUNT_ID = os.environ.get(
    "CLOUDFLARE_ACCOUNT_ID"
)

CLOUDFLARE_API_TOKEN = os.environ.get(
    "CLOUDFLARE_API_TOKEN"
)

TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN"
)

TELEGRAM_CHAT_ID = os.environ.get(
    "TELEGRAM_CHAT_ID"
)


# ============================================================
# VALIDATION
# ============================================================

def validate_environment():
    required = {
        "GOOGLE_SERVICE_ACCOUNT_JSON": GOOGLE_SERVICE_ACCOUNT_JSON,
        "GEMINI_API_KEY": GEMINI_API_KEY,
        "CLOUDFLARE_ACCOUNT_ID": CLOUDFLARE_ACCOUNT_ID,
        "CLOUDFLARE_API_TOKEN": CLOUDFLARE_API_TOKEN,
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    }

    missing = [
        name for name, value in required.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing)
        )


# ============================================================
# GOOGLE SHEETS
# ============================================================

def get_worksheet():
    service_account_info = json.loads(
        GOOGLE_SERVICE_ACCOUNT_JSON
    )

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=scopes,
    )

    client = gspread.authorize(credentials)

    spreadsheet = client.open_by_url(SPREADSHEET_URL)

    return spreadsheet.worksheet(WORKSHEET_NAME)


def get_headers(worksheet):
    headers = worksheet.row_values(1)

    required_headers = [
        "ID",
        "Topic",
        "Angle",
        "Status",
        "Tweet_Text",
        "Sent_Date",
        "Error",
    ]

    missing = [
        h for h in required_headers
        if h not in headers
    ]

    if missing:
        raise RuntimeError(
            "Missing columns in Google Sheets: "
            + ", ".join(missing)
        )

    return headers


def get_first_pending_row(worksheet, headers):
    records = worksheet.get_all_records()

    for index, row in enumerate(records, start=2):
        status = str(row.get("Status", "")).strip()

        if status.lower() == "pending":
            return index, row

    return None, None


def get_test_row(worksheet, headers):
    records = worksheet.get_all_records()

    for index, row in enumerate(records, start=2):
        row_id = str(row.get("ID", "")).strip()

        if row_id == TEST_ID:
            return index, row

    return None, None


def update_cell(worksheet, row_number, column_number, value):
    worksheet.update_cell(
        row_number,
        column_number,
        value,
    )


def update_row_after_success(
    worksheet,
    row_number,
    headers,
    tweet_text,
):
    tweet_column = headers.index("Tweet_Text") + 1
    date_column = headers.index("Sent_Date") + 1
    status_column = headers.index("Status") + 1
    error_column = headers.index("Error") + 1

    sent_date = datetime.now(
        timezone.utc
    ).isoformat()

    update_cell(
        worksheet,
        row_number,
        tweet_column,
        tweet_text,
    )

    update_cell(
        worksheet,
        row_number,
        date_column,
        sent_date,
    )

    update_cell(
        worksheet,
        row_number,
        status_column,
        "Done",
    )

    update_cell(
        worksheet,
        row_number,
        error_column,
        "",
    )


def update_row_error(
    worksheet,
    row_number,
    headers,
    error_message,
):
    status_column = headers.index("Status") + 1
    error_column = headers.index("Error") + 1

    update_cell(
        worksheet,
        row_number,
        status_column,
        "Pending",
    )

    update_cell(
        worksheet,
        row_number,
        error_column,
        error_message[:500],
    )


# ============================================================
# GEMINI
# ============================================================

def clean_tweet(text):
    if not text:
        raise RuntimeError(
            "Gemini returned an empty response."
        )

    text = text.strip()

    # Remove Markdown code fences if Gemini adds them.
    text = re.sub(
        r"^```(?:text)?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"\s*```$",
        "",
        text,
    )

    text = text.strip()

    # Remove surrounding quotation marks.
    if (
        len(text) >= 2
        and text[0] in {'"', "“", "«"}
        and text[-1] in {'"', "”", "»"}
    ):
        text = text[1:-1].strip()

    # Normalize whitespace while preserving intentional line breaks.
    text = re.sub(
        r"\r\n?",
        "\n",
        text,
    )

    text = re.sub(
        r"[ \t]+",
        " ",
        text,
    )

    text = re.sub(
        r"(?im)^(UA|EN):\s*(\S)",
        r"\1:\n\2",
        text,
    )

    lines = []

    for line in text.splitlines():
        line = line.strip()

        if not line:
            continue

        line = re.sub(
            r"(?<!\w)#\w+",
            "",
            line,
        ).strip()

        if (
            len(line) >= 2
            and line[0] in {'"', "“", "«"}
            and line[-1] in {'"', "”", "»"}
        ):
            line = line[1:-1].strip()

        if line:
            lines.append(line)

    text = "\n".join(lines)

    return format_sentences_on_new_lines(text)


def format_sentences_on_new_lines(text):
    formatted_lines = []

    for line in text.splitlines():
        stripped = line.strip()

        if not stripped:
            continue

        if re.fullmatch(r"(UA|EN|UK|Українська|English):", stripped, re.I):
            formatted_lines.append(stripped)
            continue

        sentences = re.split(
            r"(?<=[.!?])\s+",
            stripped,
        )

        for sentence in sentences:
            sentence = sentence.strip()

            if sentence:
                formatted_lines.append(sentence)

    return "\n".join(formatted_lines).strip()


def get_language_section(tweet, label):
    pattern = rf"(?ims)^{label}:\s*(.*?)(?=^(?:UA|EN):|\Z)"
    match = re.search(pattern, tweet)

    if not match:
        return ""

    return match.group(1).strip()


def validate_tweet(tweet):
    errors = []

    if not tweet:
        errors.append("Tweet is empty.")

    ua_text = get_language_section(tweet, "UA")
    en_text = get_language_section(tweet, "EN")

    if not ua_text:
        errors.append("Tweet must include a UA section.")

    if not en_text:
        errors.append("Tweet must include an EN section.")

    for label, section in (("UA", ua_text), ("EN", en_text)):
        if (
            section
            and len(section) > MAX_TWEET_LENGTH_PER_LANGUAGE
        ):
            errors.append(
                f"{label} tweet is {len(section)} characters; "
                f"maximum is {MAX_TWEET_LENGTH_PER_LANGUAGE}."
            )

    hashtag_count = len(
        re.findall(r"(?<!\w)#\w+", tweet)
    )

    if hashtag_count:
        errors.append(
            "Tweet must not contain hashtags."
        )

    return errors


def generate_tweet(topic, angle):
    client = genai.Client(
        api_key=GEMINI_API_KEY
    )

    prompt = f"""
You are a concise bilingual social media writer.

Main content niche:
AI, Skills & the Future of Work.

Generate TWO short versions of the same tweet based on:

Topic:
{topic}

Angle:
{angle}

Requirements:
- First version in Ukrainian.
- Second version in English.
- Maximum 240 characters per language version.
- One concise, strong insight.
- No hashtags.
- Natural modern Ukrainian.
- Natural modern English.
- Clear and intellectually interesting.
- Prefer a strong observation over generic motivation.
- Each sentence must start on a new line.
- No greeting.
- No introduction.
- No explanation.
- No quotation marks around the tweet.
- No bullet points.
- No emojis unless genuinely necessary.
- Output ONLY this format:
UA:
Ukrainian tweet
EN:
English tweet
"""

    last_error = None

    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )

            tweet = clean_tweet(
                response.text
            )

            errors = validate_tweet(tweet)

            if not errors:
                return tweet

            last_error = "; ".join(errors)

            prompt = f"""
Rewrite the tweet below so it strictly satisfies ALL rules.

Original:
{tweet}

Rules:
- Include both UA and EN sections.
- Ukrainian version in the UA section.
- English version in the EN section.
- Maximum 240 characters per language version.
- No hashtags.
- One concise insight.
- Each sentence on a new line.
- No quotation marks.
- No explanation.
- Output ONLY the corrected tweet.

Topic:
{topic}

Angle:
{angle}
"""

        except Exception as exc:
            last_error = str(exc)

    raise RuntimeError(
        "Gemini could not produce a valid tweet: "
        + str(last_error)
    )


# ============================================================
# CLOUDFLARE IMAGE GENERATION
# ============================================================

def extract_image_title(tweet, topic):
    en_text = get_language_section(tweet, "EN")

    for line in en_text.splitlines():
        line = line.strip()

        if line:
            return line[:90]

    return str(topic).strip()[:90]


def build_image_prompt(topic, angle, tweet):
    return f"""
Create a polished editorial social media visual for this idea.

Topic:
{topic}

Angle:
{angle}

Text context:
{tweet}

Visual requirements:
- Horizontal 4:3 composition.
- Main image should occupy the full canvas.
- Sophisticated, modern, realistic editorial style.
- Theme: AI, skills, and the future of work.
- No text, no captions, no logos, no watermarks.
- Leave the lower half visually calm enough for a title overlay.
"""


def generate_base_image(topic, angle, tweet):
    prompt = build_image_prompt(
        topic,
        angle,
        tweet,
    )

    response = requests.post(
        (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{CLOUDFLARE_ACCOUNT_ID}/ai/run/"
            f"{CLOUDFLARE_IMAGE_MODEL}"
        ),
        headers={
            "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "prompt": prompt,
            "steps": CLOUDFLARE_IMAGE_STEPS,
        },
        timeout=180,
    )

    if response.status_code != 200:
        raise RuntimeError(
            "Cloudflare Workers AI API error: "
            + response.text[:500]
        )

    data = response.json()
    result = data.get("result", {})
    image = result.get("image")

    if not data.get("success") or not image:
        raise RuntimeError(
            "Cloudflare returned no image data: "
            + str(data)[:500]
        )

    return b64decode(image)


def get_title_font(size):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    ]

    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue

    return ImageFont.load_default()


def wrap_title(text, font, max_width, draw):
    words = text.split()
    lines = []
    current = ""

    for word in words:
        candidate = (
            f"{current} {word}".strip()
        )
        bbox = draw.textbbox(
            (0, 0),
            candidate,
            font=font,
        )

        if bbox[2] <= max_width:
            current = candidate
            continue

        if current:
            lines.append(current)

        current = word

    if current:
        lines.append(current)

    return lines[:4]


def add_gradient_and_title(image_bytes, title):
    image = Image.open(
        BytesIO(image_bytes)
    ).convert("RGBA")

    target_width, target_height = [
        int(value)
        for value in FINAL_IMAGE_SIZE.split("x")
    ]

    source_width, source_height = image.size
    target_ratio = target_width / target_height
    source_ratio = source_width / source_height

    if source_ratio < target_ratio:
        crop_height = int(source_width / target_ratio)
        crop_top = (source_height - crop_height) // 2
        image = image.crop(
            (
                0,
                crop_top,
                source_width,
                crop_top + crop_height,
            )
        )
    elif source_ratio > target_ratio:
        crop_width = int(source_height * target_ratio)
        crop_left = (source_width - crop_width) // 2
        image = image.crop(
            (
                crop_left,
                0,
                crop_left + crop_width,
                source_height,
            )
        )

    image = image.resize(
        (target_width, target_height),
        Image.LANCZOS,
    )

    width, height = image.size
    overlay = Image.new(
        "RGBA",
        image.size,
        (0, 0, 0, 0),
    )
    pixels = overlay.load()
    gradient_top = 0

    for y in range(gradient_top, height):
        progress = (
            y - gradient_top
        ) / (
            height - gradient_top
        )
        alpha = int(235 * progress)

        for x in range(width):
            pixels[x, y] = (0, 0, 0, alpha)

    image = Image.alpha_composite(
        image,
        overlay,
    )

    draw = ImageDraw.Draw(image)
    margin = 54
    font_size = 58

    while font_size >= 34:
        font = get_title_font(font_size)
        lines = wrap_title(
            title,
            font,
            width - margin * 2,
            draw,
        )
        line_height = int(font_size * 1.18)
        total_height = line_height * len(lines)

        if total_height <= 270:
            break

        font_size -= 4

    y = height - margin - total_height

    for line in lines:
        draw.text(
            (margin, y),
            line,
            font=font,
            fill=(255, 255, 255, 255),
        )
        y += line_height

    output = BytesIO()
    image.convert("RGB").save(
        output,
        format="PNG",
        optimize=True,
    )
    output.seek(0)

    return output


def generate_tweet_image(topic, angle, tweet):
    base_image = generate_base_image(
        topic,
        angle,
        tweet,
    )
    title = extract_image_title(
        tweet,
        topic,
    )

    return add_gradient_and_title(
        base_image,
        title,
    )


def send_content_to_telegram(topic, angle, tweet):
    try:
        photo = generate_tweet_image(
            topic,
            angle,
            tweet,
        )

        send_photo_to_telegram(
            photo,
            tweet,
        )

        return "photo"

    except Exception as exc:
        print(
            "WARNING: Image generation or photo delivery failed. "
            "Sending text only."
        )
        print(
            f"Image error: {exc}"
        )

        send_text_to_telegram(tweet)

        return "text"


# ============================================================
# TELEGRAM
# ============================================================

def send_to_telegram(tweet):
    return send_text_to_telegram(tweet)


def send_text_to_telegram(tweet):
    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": tweet,
    }

    response = requests.post(
        url,
        json=payload,
        timeout=30,
    )

    if response.status_code != 200:
        raise RuntimeError(
            "Telegram API error: "
            + response.text[:500]
        )

    data = response.json()

    if not data.get("ok"):
        raise RuntimeError(
            "Telegram rejected message: "
            + str(data)
        )

    return data


def send_photo_to_telegram(photo, caption):
    if len(caption) > TELEGRAM_CAPTION_LIMIT:
        raise RuntimeError(
            f"Telegram caption is {len(caption)} characters; "
            f"maximum is {TELEGRAM_CAPTION_LIMIT}."
        )

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    )

    response = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "caption": caption,
        },
        files={
            "photo": (
                "content.png",
                photo,
                "image/png",
            ),
        },
        timeout=60,
    )

    if response.status_code != 200:
        raise RuntimeError(
            "Telegram photo API error: "
            + response.text[:500]
        )

    data = response.json()

    if not data.get("ok"):
        raise RuntimeError(
            "Telegram rejected photo: "
            + str(data)
        )

    return data


# ============================================================
# TEST MODE
# ============================================================

def run_test():
    print("=" * 60)
    print("SYSTEM TEST")
    print("=" * 60)

    print("\n[1/5] Connecting to Google Sheets...")

    worksheet = get_worksheet()
    headers = get_headers(worksheet)

    print("OK - Google Sheets connected.")

    print("\n[2/5] Finding TEST row...")

    row_number, row = get_test_row(
        worksheet,
        headers,
    )

    if not row_number:
        raise RuntimeError(
            "TEST row not found. "
            "Expected ID = TEST."
        )

    topic = str(
        row.get("Topic", "")
    ).strip()

    angle = str(
        row.get("Angle", "")
    ).strip()

    print(
        f"OK - TEST row found at row {row_number}."
    )
    print(f"Topic: {topic}")

    print("\n[3/5] Testing Gemini...")

    tweet = generate_tweet(
        topic,
        angle,
    )

    print("OK - Gemini generated:")
    print()
    print(tweet)
    print()
    print(
        f"Character count: {len(tweet)}"
    )

    print("\n[4/5] Generating image and testing Telegram...")

    delivery_mode = send_content_to_telegram(
        topic,
        angle,
        tweet,
    )

    print(
        f"OK - Telegram {delivery_mode} delivered."
    )

    print("\n[5/5] Google Sheets integrity...")

    # TEST mode intentionally does NOT modify
    # the TEST row.

    print(
        "OK - TEST row was not modified."
    )

    print("\n" + "=" * 60)
    print("TEST PASSED")
    print("=" * 60)


# ============================================================
# PRODUCTION MODE
# ============================================================

def run_production():
    print("=" * 60)
    print("PRODUCTION RUN")
    print("=" * 60)

    worksheet = get_worksheet()
    headers = get_headers(worksheet)

    print("\nFinding first Pending topic...")

    row_number, row = get_first_pending_row(
        worksheet,
        headers,
    )

    if not row_number:
        print(
            "No Pending topics found."
        )
        print(
            "Nothing to do. Exiting safely."
        )
        return

    topic = str(
        row.get("Topic", "")
    ).strip()

    angle = str(
        row.get("Angle", "")
    ).strip()

    print(
        f"Found row: {row_number}"
    )
    print(
        f"Topic: {topic}"
    )
    print(
        f"Angle: {angle}"
    )

    status_column = headers.index("Status") + 1

    update_cell(
        worksheet,
        row_number,
        status_column,
        "Processing",
    )

    try:
        print("\nGenerating tweet with Gemini...")

        tweet = generate_tweet(
            topic,
            angle,
        )

        print(
            f"Generated tweet ({len(tweet)} chars):"
        )
        print(tweet)

        print("\nGenerating image with Cloudflare Workers AI...")

        delivery_mode = send_content_to_telegram(
            topic,
            angle,
            tweet,
        )

        print(
            f"Telegram {delivery_mode} delivery successful."
        )

        print("\nUpdating Google Sheets...")

        update_row_after_success(
            worksheet,
            row_number,
            headers,
            tweet,
        )

        print(
            "Google Sheets updated."
        )

        print("\n" + "=" * 60)
        print("PRODUCTION RUN SUCCESSFUL")
        print("=" * 60)

    except Exception as exc:
        error_message = str(exc)

        print("\nERROR:")
        print(error_message)

        print(
            "\nReturning row to Pending..."
        )

        update_row_error(
            worksheet,
            row_number,
            headers,
            error_message,
        )

        raise


# ============================================================
# ENTRY POINT
# ============================================================

def main():
    validate_environment()

    mode = (
        os.environ.get(
            "RUN_MODE",
            "production",
        )
        .strip()
        .lower()
    )

    if mode == "test":
        run_test()

    elif mode in {
        "production",
        "scheduled",
    }:
        run_production()

    else:
        raise RuntimeError(
            f"Unknown RUN_MODE: {mode}"
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            f"\nFATAL ERROR: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

#!/usr/bin/env python3
import os
import json
import logging
import feedparser
import requests
import asyncio
from telegram import Bot
from telegram.constants import ParseMode
import re
import html

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')

TELEGRAM_CHAT_ID_RAW = os.environ.get('TELEGRAM_CHAT_ID', '')
TELEGRAM_CHAT_IDS = [c.strip() for c in TELEGRAM_CHAT_ID_RAW.split(',') if c.strip()]

CHECK_INTERVAL = int(os.environ.get('CHECK_INTERVAL', 3600))
FEEDS_FILE = os.environ.get('FEEDS_FILE', '/app/data/feeds.txt')
INCLUDE_DESCRIPTION = os.environ.get('INCLUDE_DESCRIPTION', 'false').lower() == 'true'
DISABLE_NOTIFICATION = os.environ.get('DISABLE_NOTIFICATION', 'false').lower() == 'true'
MAX_MESSAGE_LENGTH = 4096

# Keywords are ONLY from Railway Variables.
# If KEYWORDS is empty or not set -> no filtering.
KEYWORDS = [
    k.strip().lower()
    for k in os.environ.get('KEYWORDS', '').split(',')
    if k.strip()
]

HISTORY_FILE = os.environ.get('HISTORY_FILE', '/app/data/sent_items.json')

# OpenAI translation settings.
# Translation is OFF by default and works only when TRANSLATE_TO_RU=true.
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
OPENAI_MODEL = os.environ.get('OPENAI_MODEL', 'gpt-4.1-mini')
TRANSLATE_TO_RU = os.environ.get('TRANSLATE_TO_RU', 'false').lower() == 'true'


def strip_html(html_content: str) -> str:
    text = re.sub(r'<[^>]+>', '', html_content)
    text = html.unescape(text)
    return ' '.join(text.split())


def translate_to_russian(text: str) -> str:
    if not TRANSLATE_TO_RU:
        return text

    if not OPENAI_API_KEY:
        logger.error("TRANSLATE_TO_RU=true, but OPENAI_API_KEY is missing")
        return text

    if not text.strip():
        return text

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENAI_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Переводи на естественный русский язык новости о грузоперевозках, "
                            "логистике, дорогах, топливе, FMCSA, DOT и trucking industry. "
                            "Не добавляй комментарии. Не объясняй. Верни только перевод."
                        ),
                    },
                    {
                        "role": "user",
                        "content": text,
                    },
                ],
                "temperature": 0,
            },
            timeout=30,
        )

        if response.status_code != 200:
            logger.error(f"OpenAI API error {response.status_code}: {response.text}")
            return text

        data = response.json()
        translated = data["choices"][0]["message"]["content"].strip()
        return translated or text

    except Exception as e:
        logger.error(f"Translation error: {e}")
        return text


def load_feeds():
    try:
        with open(FEEDS_FILE, 'r') as f:
            feeds = [
                line.strip()
                for line in f.readlines()
                if line.strip() and not line.strip().startswith('#')
            ]
            logger.info(f"Loaded {len(feeds)} feeds from {FEEDS_FILE}")
            return feeds
    except FileNotFoundError:
        logger.warning(f"Feed file {FEEDS_FILE} not found. Creating empty file...")
        try:
            os.makedirs(os.path.dirname(FEEDS_FILE), exist_ok=True)
        except Exception:
            pass
        with open(FEEDS_FILE, 'w') as f:
            f.write("# Add your RSS feeds here, one per line\n")
        return []
    except Exception as e:
        logger.error(f"Error loading feeds: {e}")
        return []


def load_sent_items():
    try:
        with open(HISTORY_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_sent_items(sent_items):
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    except Exception:
        pass
    with open(HISTORY_FILE, 'w') as f:
        json.dump(sent_items, f)


async def send_telegram_message(bot, chat_id, message):
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode=ParseMode.MARKDOWN,
            disable_notification=DISABLE_NOTIFICATION
        )
        return True
    except Exception as e:
        logger.error(f"Error sending notification to {chat_id}: {e}")
        return False


async def send_to_all_chats(bot, message: str):
    ok_any = False
    for chat_id in TELEGRAM_CHAT_IDS:
        ok = await send_telegram_message(bot, chat_id, message)
        ok_any = ok_any or ok
        await asyncio.sleep(0.3)
    return ok_any


async def send_grouped_messages(bot, messages_by_feed):
    if not messages_by_feed:
        logger.info("No new content to notify")
        return True

    for feed_title, entries in messages_by_feed.items():
        if not entries:
            continue

        header_title = translate_to_russian(feed_title)
        header = f"📢 *New content from {header_title}*\n\n"
        entries_text = ""

        for entry in entries:
            title = translate_to_russian(entry['title'])
            entry_text = f"• *{title}*\n"

            if INCLUDE_DESCRIPTION and entry.get('description'):
                desc = strip_html(entry['description'])
                if len(desc) > 300:
                    desc = desc[:297] + '...'
                desc = translate_to_russian(desc)
                entry_text += f"  _{desc}_\n"

            entry_text += f"\n  {entry['link']}\n\n"

            if len(header) + len(entries_text) + len(entry_text) > MAX_MESSAGE_LENGTH:
                await send_to_all_chats(bot, header + entries_text)
                entries_text = entry_text
            else:
                entries_text += entry_text

        if entries_text:
            await send_to_all_chats(bot, header + entries_text)

        await asyncio.sleep(1)

    return True


async def check_feeds(bot):
    sent_items = load_sent_items()
    feeds = load_feeds()

    if not feeds:
        logger.warning("No feeds to check. Add feeds to the configuration file.")
        return sent_items

    messages_by_feed = {}

    for feed_url in feeds:
        if not feed_url.strip():
            continue

        logger.info(f"Checking feed: {feed_url}")

        try:
            feed = feedparser.parse(feed_url)

            if not feed.entries:
                logger.warning(f"No entries found in feed: {feed_url}")
                continue

            feed_title = feed.feed.title if hasattr(feed.feed, 'title') else feed_url
            sent_items.setdefault(feed_url, [])
            messages_by_feed.setdefault(feed_title, [])

            for entry in feed.entries:
                entry_id = entry.id if hasattr(entry, 'id') else entry.link
                if entry_id in sent_items[feed_url]:
                    continue

                title = entry.title if hasattr(entry, 'title') else "No title"
                link = entry.link if hasattr(entry, 'link') else ""

                description = ""
                if INCLUDE_DESCRIPTION:
                    description = getattr(entry, 'description', '') or getattr(entry, 'summary', '')

                text_to_check = (title + " " + strip_html(description)).lower()
                if KEYWORDS and not any(keyword in text_to_check for keyword in KEYWORDS):
                    continue

                messages_by_feed[feed_title].append({
                    'title': title,
                    'link': link,
                    'description': description
                })

                sent_items[feed_url].append(entry_id)

        except Exception as e:
            logger.error(f"Error checking feed {feed_url}: {e}")

    await send_grouped_messages(bot, messages_by_feed)
    return sent_items


async def main_async():
    logger.info("Starting RSS feed monitoring")
    logger.info(
        f"Configuration: INCLUDE_DESCRIPTION={INCLUDE_DESCRIPTION}, "
        f"DISABLE_NOTIFICATION={DISABLE_NOTIFICATION}, "
        f"TRANSLATE_TO_RU={TRANSLATE_TO_RU}, "
        f"OPENAI_MODEL={OPENAI_MODEL}"
    )
    logger.info(f"Using FEEDS_FILE={FEEDS_FILE}, HISTORY_FILE={HISTORY_FILE}, KEYWORDS={KEYWORDS}")
    logger.info(f"Using TELEGRAM_CHAT_IDS={TELEGRAM_CHAT_IDS}")

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        logger.error("Missing environment variables. Make sure to set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await send_to_all_chats(
        bot,
        "🤖 *RSS Monitoring Bot started!*\nActive feed monitoring. Configuration loaded from file."
    )

    while True:
        sent_items = await check_feeds(bot)
        save_sent_items(sent_items)
        logger.info(f"Next check in {CHECK_INTERVAL} seconds")
        await asyncio.sleep(CHECK_INTERVAL)


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()

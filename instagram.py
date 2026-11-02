import re


def valid_instagram_url(url):
    pattern = (
        r"https?://(?:www\.)?instagram\.com/"
        r"[A-Za-z0-9._]+/?"
    )
    return re.fullmatch(pattern, url.strip(), re.I) is not None


async def get_stories(username):
    raise RuntimeError(
        "Instagram Stories uchun ruxsatli API ulanishi kerak. "
        "Hozircha Stories olish funksiyasi ulanmagan."
    )

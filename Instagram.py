
import re


def valid_instagram_url(url):
    pattern = (
        r"https?://(?:www\.)?instagram\.com/"
        r"[A-Za-z0-9._]+/?"
    )
    return re.fullmatch(pattern, url.strip(), re.I) is not None


async def get_stories(username):
    # Stories olish uchun Instagram ruxsat bergan
    # API integratsiyasi talab qilinadi.
    raise RuntimeError(
        "Instagram Stories API hali ulanmagan. "
        "Faqat ruxsatli API orqali olish mumkin."
    )

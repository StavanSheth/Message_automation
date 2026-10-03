"""Instagram Profile Reader for extracting visible profile details from DOM."""

import re
from typing import Dict, Any, Optional
from backend.browser.session import BrowserSessionInstance
from backend.events.logger import get_logger

logger = get_logger("instagram_profile_reader")


def parse_metric_string(text: str) -> Optional[int]:
    """
    Parse metric strings such as '1,234', '10.5K', '2M' into integer counts.
    """
    if not text or not isinstance(text, str):
        return None

    cleaned = text.strip().replace(",", "").replace(" ", "").upper()
    try:
        if cleaned.endswith("K"):
            return int(float(cleaned[:-1]) * 1_000)
        elif cleaned.endswith("M"):
            return int(float(cleaned[:-1]) * 1_000_000)
        elif cleaned.endswith("B"):
            return int(float(cleaned[:-1]) * 1_000_000_000)
        else:
            # Extract leading digits
            match = re.search(r"^\d+", cleaned)
            if match:
                return int(match.group(0))
    except (ValueError, TypeError):
        pass
    return None


class InstagramProfileReader:
    """Extracts structured identity data and signals from active Instagram profile page."""

    def extract_profile(self, session: BrowserSessionInstance) -> Dict[str, Any]:
        """
        Execute DOM query on the active browser page to collect observed profile details:
        - url
        - username
        - display_name
        - follower_count
        - following_count
        - post_count
        - bio
        - is_verified
        - is_private
        - can_message
        - page_missing
        """
        if not session or not session.is_alive():
            return {"page_missing": True, "error": "Browser session not alive"}

        raw_data = session.evaluate(
            """() => {
                const result = {
                    url: window.location.href,
                    username: '',
                    display_name: '',
                    follower_count_text: '',
                    following_count_text: '',
                    post_count_text: '',
                    bio: '',
                    is_verified: false,
                    is_private: false,
                    can_message: false,
                    page_missing: false,
                };

                const bodyText = document.body ? document.body.innerText : '';
                if (bodyText.includes("Sorry, this page isn't available") || document.title.includes('Page Not Found')) {
                    result.page_missing = true;
                    return result;
                }

                // 1. Username
                const h2 = document.querySelector('header h2, section h2');
                if (h2) {
                    result.username = (h2.innerText || '').trim();
                }

                // 2. Verified badge
                const verifiedIcon = document.querySelector('header [aria-label*="Verified"], header svg[aria-label*="Verified"]');
                result.is_verified = !!verifiedIcon;

                // 3. Header sections: metrics (posts, followers, following)
                const headerSpans = Array.from(document.querySelectorAll('header section ul li, header ul li'));
                for (const li of headerSpans) {
                    const text = (li.innerText || '').toLowerCase();
                    const titleVal = li.querySelector('span[title]') ? li.querySelector('span[title]').getAttribute('title') : '';
                    if (text.includes('follower')) {
                        result.follower_count_text = titleVal || text.replace('followers', '').replace('follower', '').trim();
                    } else if (text.includes('following')) {
                        result.following_count_text = titleVal || text.replace('following', '').trim();
                    } else if (text.includes('post')) {
                        result.post_count_text = text.replace('posts', '').replace('post', '').trim();
                    }
                }

                // 4. Display Name & Bio
                const bioContainer = document.querySelector('header section > div:last-child, header section > div.x6s0dn4');
                if (bioContainer) {
                    const lines = Array.from(bioContainer.querySelectorAll('span, div')).map(el => (el.innerText || '').trim()).filter(Boolean);
                    if (lines.length > 0) {
                        result.display_name = lines[0];
                        result.bio = lines.slice(1).join('\\n');
                    }
                }
                if (!result.display_name) {
                    const titleEl = document.querySelector('header section span[class*="x1lliihq"]');
                    if (titleEl) result.display_name = (titleEl.innerText || '').trim();
                }

                // 5. Private Account detection
                if (bodyText.includes('This account is private') || bodyText.includes('Follow to see their photos and videos')) {
                    result.is_private = true;
                }

                // 6. Direct Message capability (check for Message button / DM action)
                const buttons = Array.from(document.querySelectorAll('header button, header div[role="button"], main button'));
                for (const btn of buttons) {
                    const btnText = (btn.innerText || '').toLowerCase().trim();
                    if (btnText === 'message' || btnText.includes('message')) {
                        result.can_message = true;
                        break;
                    }
                }

                // 7. Resilient Fallback: OpenGraph and Meta tags
                const ogTitle = document.querySelector('meta[property="og:title"]')?.getAttribute('content') || '';
                const ogDesc = document.querySelector('meta[property="og:description"]')?.getAttribute('content') || '';
                const ogUrl = document.querySelector('meta[property="og:url"]')?.getAttribute('content') || '';

                if (ogUrl) result.url = ogUrl;

                // Extract username from og:title if not found in DOM
                if (!result.username && ogTitle) {
                    const match = ogTitle.match(/\\(@([^)]+)\\)/);
                    if (match) {
                        result.username = match[1].trim();
                    } else if (ogTitle.includes('• Instagram')) {
                        result.username = ogTitle.split('•')[0].replace('@', '').trim();
                    }
                }

                // Extract display name from og:title if not found in DOM
                if (!result.display_name && ogTitle) {
                    const nameMatch = ogTitle.match(/^([^(@]+)\\s*\\(@/);
                    if (nameMatch) {
                        result.display_name = nameMatch[1].trim();
                    }
                }

                // Extract metrics from og:description if DOM extraction was empty
                if (!result.follower_count_text && ogDesc) {
                    const fMatch = ogDesc.match(/([\\d.,]+[KkMmBb]?)\\s*Followers/i);
                    if (fMatch) result.follower_count_text = fMatch[1];
                    const fgMatch = ogDesc.match(/([\\d.,]+[KkMmBb]?)\\s*Following/i);
                    if (fgMatch) result.following_count_text = fgMatch[1];
                    const pMatch = ogDesc.match(/([\\d.,]+[KkMmBb]?)\\s*Posts/i);
                    if (pMatch) result.post_count_text = pMatch[1];
                }

                return result;
            }"""
        )

        if not raw_data or not isinstance(raw_data, dict):
            return {"page_missing": True, "error": "Invalid DOM extraction result"}

        follower_num = parse_metric_string(raw_data.get("follower_count_text", ""))
        following_num = parse_metric_string(raw_data.get("following_count_text", ""))
        post_num = parse_metric_string(raw_data.get("post_count_text", ""))

        return {
            "url": raw_data.get("url", ""),
            "username": raw_data.get("username", ""),
            "display_name": raw_data.get("display_name", ""),
            "follower_count": follower_num,
            "following_count": following_num,
            "post_count": post_num,
            "bio": raw_data.get("bio", ""),
            "is_verified": bool(raw_data.get("is_verified", False)),
            "is_private": bool(raw_data.get("is_private", False)),
            "can_message": bool(raw_data.get("can_message", False)),
            "page_missing": bool(raw_data.get("page_missing", False)),
        }

    read_profile = extract_profile

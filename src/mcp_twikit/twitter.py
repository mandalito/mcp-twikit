from fastmcp import FastMCP, Context
import twikit
from twikit.media import Video, Photo
import os
from pathlib import Path
import logging
from typing import Optional, List
import time
import tempfile
import re

# Create an MCP server
mcp = FastMCP("mcp-twikit")
logger = logging.getLogger(__name__)
httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.WARNING)

USERNAME = os.getenv('TWITTER_USERNAME')
EMAIL = os.getenv('TWITTER_EMAIL')
PASSWORD = os.getenv('TWITTER_PASSWORD')
USER_AGENT = os.getenv('USER_AGENT')
COOKIES_PATH = Path.home() / '.mcp-twikit' / 'cookies.json'

# Rate limit tracking
RATE_LIMITS = {}
RATE_LIMIT_WINDOW = 15 * 60  # 15 minutes in seconds

async def get_twitter_client() -> twikit.Client:
    """Initialize and return an authenticated Twitter client."""
    client = twikit.Client('en-US', user_agent=USER_AGENT)

    if COOKIES_PATH.exists():
        client.load_cookies(COOKIES_PATH)
    else:
        try:
            await client.login(
                auth_info_1=USERNAME,
                auth_info_2=EMAIL,
                password=PASSWORD
            )
        except Exception as e:
            logger.error(f"Failed to login: {e}")
            raise
        COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
        client.save_cookies(COOKIES_PATH)

    return client

def check_rate_limit(endpoint: str) -> bool:
    """Check if we're within rate limits for a given endpoint."""
    now = time.time()
    if endpoint not in RATE_LIMITS:
        RATE_LIMITS[endpoint] = []

    # Remove old timestamps
    RATE_LIMITS[endpoint] = [t for t in RATE_LIMITS[endpoint] if now - t < RATE_LIMIT_WINDOW]

    # Check limits based on endpoint
    if endpoint == 'tweet':
        return len(RATE_LIMITS[endpoint]) < 300  # 300 tweets per 15 minutes
    elif endpoint == 'dm':
        return len(RATE_LIMITS[endpoint]) < 1000  # 1000 DMs per 15 minutes
    return True

# Existing search and read tools
@mcp.tool()
async def search_twitter(query: str, sort_by: str = 'Top', count: int = 10, ctx: Context = None) -> str:
    """Search twitter with a query. Sort by 'Top' or 'Latest'"""
    try:
        client = await get_twitter_client()
        tweets = await client.search_tweet(query, product=sort_by, count=count)
        return convert_tweets_to_markdown(tweets)
    except Exception as e:
        logger.error(f"Failed to search tweets: {e}")
        return f"Failed to search tweets: {e}"

@mcp.tool()
async def get_user_tweets(username: str, tweet_type: str = 'Tweets', count: int = 10, ctx: Context = None) -> str:
    """Get tweets from a specific user's timeline."""
    try:
        client = await get_twitter_client()
        username = username.lstrip('@')
        user = await client.get_user_by_screen_name(username)
        if not user:
            return f"Could not find user {username}"

        tweets = await client.get_user_tweets(
            user_id=user.id,
            tweet_type=tweet_type,
            count=count
        )
        return convert_tweets_to_markdown(tweets)
    except Exception as e:
        logger.error(f"Failed to get user tweets: {e}")
        return f"Failed to get user tweets: {e}"

@mcp.tool()
async def get_all_user_tweets(username: str, tweet_type: str = 'Tweets', ctx: Context = None) -> str:
    """Get ALL tweets from a user by auto-paginating through all pages. Supports tweet_type: 'Tweets', 'Replies', 'Media', 'Likes'."""
    try:
        client = await get_twitter_client()
        username = username.lstrip('@')
        user = await client.get_user_by_screen_name(username)
        if not user:
            return f"Could not find user {username}"

        all_tweets = []
        try:
            tweets = await client.get_user_tweets(
                user_id=user.id,
                tweet_type=tweet_type,
                count=40
            )
            all_tweets.extend(tweets)
        except (IndexError, KeyError):
            return f"Total: 0 tweets\n\nNo {tweet_type.lower()} found for @{username}."

        error = None
        while tweets:
            try:
                tweets = await tweets.next()
                if not tweets:
                    break
                all_tweets.extend(tweets)
            except (IndexError, KeyError):
                break  # The last page has no next cursor
            except Exception as e:
                error = e
                break

        if not all_tweets:
            return f"Total: 0 tweets\n\nNo {tweet_type.lower()} found for @{username}."

        warning = f"WARNING: partial list, pagination stopped on an error: {error}\n\n" if error else ""
        return f"{warning}Total: {len(all_tweets)} tweets\n\n{convert_tweets_to_markdown(all_tweets)}"
    except Exception as e:
        logger.error(f"Failed to get all user tweets: {e}")
        return f"Failed to get all user tweets: {e}"

@mcp.tool()
async def get_timeline(count: int = 20) -> str:
    """Get tweets from your home timeline (For You)."""
    try:
        client = await get_twitter_client()
        tweets = await client.get_timeline(count=count)
        return convert_tweets_to_markdown(tweets)
    except Exception as e:
        logger.error(f"Failed to get timeline: {e}")
        return f"Failed to get timeline: {e}"

@mcp.tool()
async def get_latest_timeline(count: int = 20) -> str:
    """Get tweets from your home timeline (Following)."""
    try:
        client = await get_twitter_client()
        tweets = await client.get_latest_timeline(count=count)
        return convert_tweets_to_markdown(tweets)
    except Exception as e:
        logger.error(f"Failed to get latest timeline: {e}")
        return f"Failed to get latest timeline: {e}"

@mcp.tool()
async def get_all_bookmarks(ctx: Context = None) -> str:
    """Get ALL of your bookmarks by auto-paginating through all pages."""
    try:
        client = await get_twitter_client()
        tweets = await client.get_bookmarks(count=40)

        all_tweets = []
        seen_ids = set()
        error = None
        while tweets:
            new_tweets = [t for t in tweets if t.id not in seen_ids]
            if not new_tweets:
                break
            seen_ids.update(t.id for t in new_tweets)
            all_tweets.extend(new_tweets)
            try:
                tweets = await tweets.next()
            except (IndexError, KeyError):
                break  # The last page has no next cursor
            except Exception as e:
                error = e
                break

        if not all_tweets:
            return "Total: 0 bookmarks"

        warning = f"WARNING: partial list, pagination stopped on an error: {error}\n\n" if error else ""
        return f"{warning}Total: {len(all_tweets)} bookmarks\n\n{convert_tweets_to_markdown(all_tweets)}"
    except Exception as e:
        logger.error(f"Failed to get bookmarks: {e}")
        return f"Failed to get bookmarks: {e}"

# Write tools
@mcp.tool()
async def post_tweet(
    text: str,
    media_paths: Optional[List[str]] = None,
    reply_to: Optional[str] = None,
    tags: Optional[List[str]] = None
) -> str:
    """Post a tweet with optional media, reply, and tags."""
    try:
        if not check_rate_limit('tweet'):
            return "Rate limit exceeded for tweets. Please wait before posting again."

        client = await get_twitter_client()

        # Handle tags by converting to mentions
        if tags:
            mentions = ' '.join(f"@{tag.lstrip('@')}" for tag in tags)
            text = f"""{text}
{mentions}"""

        # Upload media if provided
        media_ids = []
        if media_paths:
            for path in media_paths:
                media_id = await client.upload_media(path, wait_for_completion=True)
                media_ids.append(media_id)

        # Create the tweet
        tweet = await client.create_tweet(
            text=text,
            media_ids=media_ids if media_ids else None,
            reply_to=reply_to
        )
        RATE_LIMITS.setdefault('tweet', []).append(time.time())
        return f"Successfully posted tweet: {tweet.id}"
    except Exception as e:
        logger.error(f"Failed to post tweet: {e}")
        return f"Failed to post tweet: {e}"

@mcp.tool()
async def delete_tweet(tweet_id: str) -> str:
    """Delete a tweet by its ID."""
    try:
        client = await get_twitter_client()
        await client.delete_tweet(tweet_id)
        return f"Successfully deleted tweet {tweet_id}"
    except Exception as e:
        logger.error(f"Failed to delete tweet: {e}")
        return f"Failed to delete tweet: {e}"

@mcp.tool()
async def send_dm(user_id: str, message: str, media_path: Optional[str] = None) -> str:
    """Send a direct message to a user."""
    try:
        if not check_rate_limit('dm'):
            return "Rate limit exceeded for DMs. Please wait before sending again."

        client = await get_twitter_client()

        media_id = None
        if media_path:
            media_id = await client.upload_media(media_path, wait_for_completion=True)

        await client.send_dm(
            user_id=user_id,
            text=message,
            media_id=media_id
        )
        RATE_LIMITS.setdefault('dm', []).append(time.time())
        return f"Successfully sent DM to user {user_id}"
    except Exception as e:
        logger.error(f"Failed to send DM: {e}")
        return f"Failed to send DM: {e}"

@mcp.tool()
async def delete_dm(message_id: str) -> str:
    """Delete a direct message by its ID."""
    try:
        client = await get_twitter_client()
        await client.delete_dm(message_id)
        return f"Successfully deleted DM {message_id}"
    except Exception as e:
        logger.error(f"Failed to delete DM: {e}")
        return f"Failed to delete DM: {e}"

@mcp.tool()
async def like_tweet(tweet_id: str) -> str:
    """Like/favorite a tweet by its ID."""
    try:
        client = await get_twitter_client()
        await client.favorite_tweet(tweet_id)
        return f"Successfully liked tweet {tweet_id}"
    except Exception as e:
        logger.error(f"Failed to like tweet: {e}")
        return f"Failed to like tweet: {e}"

@mcp.tool()
async def unlike_tweet(tweet_id: str) -> str:
    """Unlike/unfavorite a tweet by its ID."""
    try:
        client = await get_twitter_client()
        await client.unfavorite_tweet(tweet_id)
        return f"Successfully unliked tweet {tweet_id}"
    except Exception as e:
        logger.error(f"Failed to unlike tweet: {e}")
        return f"Failed to unlike tweet: {e}"

@mcp.tool()
async def delete_bookmark(tweet_id: str) -> str:
    """Remove a tweet from your bookmarks by its ID."""
    try:
        client = await get_twitter_client()
        await client.delete_bookmark(tweet_id)
        return f"Successfully removed tweet {tweet_id} from bookmarks"
    except Exception as e:
        logger.error(f"Failed to delete bookmark: {e}")
        return f"Failed to delete bookmark: {e}"

async def _resolve_account(client, username: Optional[str], tweet_id: Optional[str]):
    """Find one account from its username, or from the author of a tweet."""
    if bool(username) == bool(tweet_id):
        raise ValueError("Provide either username or tweet_id, not both")
    if username:
        return await client.get_user_by_screen_name(username.lstrip('@'))
    tweets = await client.get_tweets_by_ids([tweet_id])
    if not tweets or tweets[0] is None:
        raise ValueError(f"Could not find tweet {tweet_id}")
    return tweets[0].user

async def _apply_to_account(method: str, label: str, username: Optional[str], tweet_id: Optional[str]) -> str:
    """Run one account action on a single account and name the account it was applied to."""
    try:
        client = await get_twitter_client()
        user = await _resolve_account(client, username, tweet_id)
        await getattr(client, method)(user.id)
        return f"Successfully {label} @{user.screen_name}"
    except Exception as e:
        logger.error(f"Failed {method}: {e}")
        return f"Failed {method}: {e}"

@mcp.tool()
async def follow_user(username: Optional[str] = None, tweet_id: Optional[str] = None) -> str:
    """Follow one account, given its username or the ID of one of its tweets."""
    return await _apply_to_account('follow_user', 'followed', username, tweet_id)

@mcp.tool()
async def unfollow_user(username: Optional[str] = None, tweet_id: Optional[str] = None) -> str:
    """Unfollow one account, given its username or the ID of one of its tweets."""
    return await _apply_to_account('unfollow_user', 'unfollowed', username, tweet_id)

@mcp.tool()
async def mute_user(username: Optional[str] = None, tweet_id: Optional[str] = None) -> str:
    """Mute one account, given its username or the ID of one of its tweets."""
    return await _apply_to_account('mute_user', 'muted', username, tweet_id)

@mcp.tool()
async def unmute_user(username: Optional[str] = None, tweet_id: Optional[str] = None) -> str:
    """Unmute one account, given its username or the ID of one of its tweets."""
    return await _apply_to_account('unmute_user', 'unmuted', username, tweet_id)

@mcp.tool()
async def block_user(username: Optional[str] = None, tweet_id: Optional[str] = None) -> str:
    """Block one account, given its username or the ID of one of its tweets."""
    return await _apply_to_account('block_user', 'blocked', username, tweet_id)

@mcp.tool()
async def unblock_user(username: Optional[str] = None, tweet_id: Optional[str] = None) -> str:
    """Unblock one account, given its username or the ID of one of its tweets."""
    return await _apply_to_account('unblock_user', 'unblocked', username, tweet_id)

@mcp.tool()
async def get_thread(tweet_id: str) -> str:
    """Get a tweet and its full thread (self-thread continuation + replies). Returns the main tweet, thread continuation, and top replies."""
    try:
        client = await get_twitter_client()
        tweet = await client.get_tweet_by_id(tweet_id)

        result = []
        result.append("## Main Tweet")
        result.append(f"### @{tweet.user.screen_name} (ID: {tweet.id})")
        result.append(f"**{tweet.created_at}**")
        result.append(tweet.full_text)
        if tweet.media:
            for media in tweet.media:
                result.append(f"![media]({media.media_url})")

        if tweet.thread:
            result.append("\n## Thread Continuation")
            for t in tweet.thread:
                result.append(f"### @{t.user.screen_name} (ID: {t.id})")
                result.append(f"**{t.created_at}**")
                result.append(t.full_text)
                if t.media:
                    for media in t.media:
                        result.append(f"![media]({media.media_url})")
                result.append("---")

        if tweet.replies:
            result.append("\n## Top Replies")
            for reply_group in tweet.replies:
                result.append(f"### @{reply_group.user.screen_name} (ID: {reply_group.id})")
                result.append(f"**{reply_group.created_at}**")
                result.append(reply_group.full_text)
                if reply_group.media:
                    for media in reply_group.media:
                        result.append(f"![media]({media.media_url})")
                result.append("---")

        return "\n".join(result)
    except Exception as e:
        logger.error(f"Failed to get thread: {e}")
        return f"Failed to get thread: {e}"

@mcp.tool()
async def download_tweet_media(tweet_id: str) -> str:
    """Download all photos from a tweet to /tmp for OCR/viewing. Returns file paths of downloaded images."""
    try:
        client = await get_twitter_client()
        # Search for the tweet to get media objects
        # We use search since get_tweet_by_id has parsing issues
        tweets = await client.get_tweets_by_ids([tweet_id])
        if not tweets:
            return f"Could not find tweet {tweet_id}"

        tweet = tweets[0]
        if not tweet.media:
            return f"Tweet {tweet_id} has no media"

        downloaded = []
        for i, media in enumerate(tweet.media):
            if isinstance(media, Photo):
                ext = 'jpg'
                filename = f"tweet_{tweet_id}_photo_{i}.{ext}"
                filepath = os.path.join(tempfile.gettempdir(), filename)
                await media.download(filepath)
                downloaded.append(filepath)
            elif isinstance(media, Video):
                downloaded.append(f"[VIDEO] {media.media_url} (use get_tweet_captions for subtitles)")

        if not downloaded:
            return f"Tweet {tweet_id} has no downloadable photos"

        return f"Downloaded {len(downloaded)} media files:\n" + "\n".join(downloaded)
    except Exception as e:
        logger.error(f"Failed to download media: {e}")
        return f"Failed to download media: {e}"

@mcp.tool()
async def get_tweet_captions(tweet_id: str) -> str:
    """Extract video captions/subtitles from a tweet. Returns the full transcript text if available."""
    try:
        client = await get_twitter_client()
        tweets = await client.get_tweets_by_ids([tweet_id])
        if not tweets:
            return f"Could not find tweet {tweet_id}"

        tweet = tweets[0]
        if not tweet.media:
            return f"Tweet {tweet_id} has no media"

        captions_found = []
        for media in tweet.media:
            if isinstance(media, Video):
                try:
                    subtitles = await media.get_subtitles()
                    if subtitles:
                        lines = [caption.text for caption in subtitles]
                        captions_found.append("\n".join(lines))
                    else:
                        captions_found.append("[No captions available for this video]")
                except Exception as sub_err:
                    captions_found.append(f"[Failed to get captions: {sub_err}]")

        if not captions_found:
            return f"Tweet {tweet_id} has no video media"

        return "\n\n---\n\n".join(captions_found)
    except Exception as e:
        logger.error(f"Failed to get captions: {e}")
        return f"Failed to get captions: {e}"

@mcp.tool()
async def get_tweet_article(tweet_id: str) -> str:
    """Extract the full text of an X Article linked in a tweet. X Articles are long-form posts on x.com/i/article/..."""
    try:
        client = await get_twitter_client()
        tweets = await client.get_tweets_by_ids([tweet_id])
        if not tweets:
            return f"Could not find tweet {tweet_id}"

        tweet = tweets[0]
        data = tweet._data

        # Extract article from Draft.js block format
        article = data.get('article', {})
        article_results = article.get('article_results', {}).get('result', {})
        content_state = article_results.get('content_state', {})
        blocks = content_state.get('blocks', [])

        if not blocks:
            # Fallback: check note_tweet
            note = data.get('note_tweet', {}).get('note_tweet_results', {}).get('result', {})
            if note and note.get('text'):
                return f"## Article Content\n\n{note['text']}"
            return f"Tweet {tweet_id} has no article content"

        # Convert Draft.js blocks to markdown
        lines = []
        for block in blocks:
            text = block.get('text', '').strip()
            if not text:
                continue
            block_type = block.get('type', 'unstyled')

            if block_type == 'header-one':
                lines.append(f"# {text}")
            elif block_type == 'header-two':
                lines.append(f"## {text}")
            elif block_type == 'header-three':
                lines.append(f"### {text}")
            elif block_type == 'unordered-list-item':
                lines.append(f"- {text}")
            elif block_type == 'ordered-list-item':
                lines.append(f"1. {text}")
            elif block_type == 'blockquote':
                lines.append(f"> {text}")
            elif block_type == 'atomic':
                continue  # skip embedded media placeholders
            else:
                lines.append(text)
            lines.append("")

        if not lines:
            return f"Tweet {tweet_id} article has no text content"

        author = tweet.user.screen_name
        return f"## X Article by @{author}\n\n" + "\n".join(lines)
    except Exception as e:
        logger.error(f"Failed to get article: {e}")
        return f"Failed to get article: {e}"

async def resolve_tco_link(client: twikit.Client, tco_url: str) -> str:
    """Resolve a t.co shortlink to its final URL using the authenticated client."""
    try:
        response = await client.http.get(tco_url, follow_redirects=False)
        location = response.headers.get('location', tco_url)
        return location
    except Exception:
        return tco_url

def _extract_article_text(data: dict) -> str | None:
    """Extract X Article text from tweet data dict. Returns None if no article."""
    article = data.get('article', {})
    blocks = article.get('article_results', {}).get('result', {}).get('content_state', {}).get('blocks', [])
    if not blocks:
        return None
    lines = []
    for block in blocks:
        text = block.get('text', '').strip()
        if not text:
            continue
        bt = block.get('type', 'unstyled')
        if bt == 'header-one': lines.append(f"# {text}")
        elif bt == 'header-two': lines.append(f"## {text}")
        elif bt == 'header-three': lines.append(f"### {text}")
        elif bt == 'unordered-list-item': lines.append(f"- {text}")
        elif bt == 'ordered-list-item': lines.append(f"1. {text}")
        elif bt == 'blockquote': lines.append(f"> {text}")
        elif bt == 'atomic': continue
        else: lines.append(text)
        lines.append("")
    return "\n".join(lines) if lines else None

def _get_note_text(data: dict) -> str | None:
    """Extract long-form note tweet text."""
    note = data.get('note_tweet', {}).get('note_tweet_results', {}).get('result', {})
    return note.get('text') if note else None

async def _resolve_tweet_urls(client, tweet) -> list[str]:
    """Get expanded URLs from a tweet, with t.co fallback."""
    try:
        urls = tweet.urls
    except (KeyError, AttributeError, TypeError):
        urls = None
    result = []
    if urls:
        for u in urls:
            expanded = u.get('expanded_url', u.get('url'))
            if expanded:
                result.append(expanded)
    else:
        tco_links = re.findall(r'https://t\.co/\w+', tweet.full_text)
        for link in tco_links:
            resolved = await resolve_tco_link(client, link)
            result.append(resolved)
    return result

def _tweet_summary(tweet) -> dict:
    """Build a summary dict for a single tweet."""
    media_types = []
    if tweet.media:
        for m in tweet.media:
            if isinstance(m, Photo): media_types.append("photo")
            elif isinstance(m, Video): media_types.append("video")
            else: media_types.append(m.type or "unknown")
    return {
        "id": tweet.id,
        "author": tweet.user.screen_name,
        "date": tweet.created_at,
        "text": tweet.full_text,
        "media": media_types,
        "is_reply": tweet.in_reply_to is not None,
        "reply_to_id": tweet.in_reply_to,
        "is_quote": tweet.is_quote_status,
    }

@mcp.tool()
async def resolve_tweet_content(tweet_id: str) -> str:
    """One-shot full content resolution for a tweet. Returns tweet text, parent/quoted content, X article text, resolved URLs, and media info — everything needed to decide if content is extractable."""
    try:
        client = await get_twitter_client()
        tweets = await client.get_tweets_by_ids([tweet_id])
        if not tweets or not tweets[0]:
            return f"Could not find tweet {tweet_id}"

        tweet = tweets[0]
        sections = []

        # --- Main tweet ---
        sections.append(f"## Tweet @{tweet.user.screen_name} (ID: {tweet.id})")
        sections.append(f"**{tweet.created_at}**")
        sections.append(tweet.full_text)

        # URLs
        urls = await _resolve_tweet_urls(client, tweet)
        for u in urls:
            sections.append(f"🔗 {u}")

        # Media
        if tweet.media:
            for m in tweet.media:
                if isinstance(m, Photo): sections.append(f"📷 photo: {m.media_url}")
                elif isinstance(m, Video): sections.append(f"🎬 video: {m.media_url}")

        # X Article on main tweet
        article = _extract_article_text(tweet._data)
        if article:
            sections.append(f"\n## X Article by @{tweet.user.screen_name}")
            sections.append(article)

        # Note tweet (long text)
        note = _get_note_text(tweet._data)
        if note and note != tweet.full_text:
            sections.append(f"\n## Long-form note")
            sections.append(note)

        # --- Quoted tweet ---
        if tweet.is_quote_status and tweet.quote:
            q = tweet.quote
            sections.append(f"\n## Quoted Tweet @{q.user.screen_name} (ID: {q.id})")
            sections.append(f"**{q.created_at}**")
            sections.append(q.full_text)
            q_urls = await _resolve_tweet_urls(client, q)
            for u in q_urls:
                sections.append(f"🔗 {u}")
            q_article = _extract_article_text(q._data)
            if q_article:
                sections.append(f"\n## X Article by @{q.user.screen_name} (quoted)")
                sections.append(q_article)
            q_note = _get_note_text(q._data)
            if q_note and q_note != q.full_text:
                sections.append(f"\n## Long-form note (quoted)")
                sections.append(q_note)

        # --- Parent tweet (if reply) ---
        parent_id = tweet.in_reply_to
        if parent_id:
            try:
                parent_tweets = await client.get_tweets_by_ids([parent_id])
                if parent_tweets and parent_tweets[0]:
                    p = parent_tweets[0]
                    sections.append(f"\n## Parent Tweet @{p.user.screen_name} (ID: {p.id})")
                    sections.append(f"**{p.created_at}**")
                    sections.append(p.full_text)
                    p_urls = await _resolve_tweet_urls(client, p)
                    for u in p_urls:
                        sections.append(f"🔗 {u}")
                    p_article = _extract_article_text(p._data)
                    if p_article:
                        sections.append(f"\n## X Article by @{p.user.screen_name} (parent)")
                        sections.append(p_article)
                    p_note = _get_note_text(p._data)
                    if p_note and p_note != p.full_text:
                        sections.append(f"\n## Long-form note (parent)")
                        sections.append(p_note)
            except Exception:
                sections.append(f"\n[Could not fetch parent tweet {parent_id}]")

        return "\n".join(sections)
    except Exception as e:
        logger.error(f"Failed to resolve tweet content: {e}")
        return f"Failed to resolve tweet content: {e}"

@mcp.tool()
async def resolve_tweets_batch(tweet_ids: List[str]) -> str:
    """Resolve full content for multiple tweets in one call. Returns all content separated by ===TWEET===. Much faster than calling resolve_tweet_content one by one."""
    try:
        client = await get_twitter_client()
        # Fetch all tweets in one batch
        all_tweets = await client.get_tweets_by_ids(tweet_ids)

        results = []
        for i, tweet in enumerate(all_tweets):
            if tweet is None:
                results.append(f"## Tweet {tweet_ids[i]}\n[Not found]")
                results.append("===TWEET===")
                continue

            sections = []
            sections.append(f"## Tweet @{tweet.user.screen_name} (ID: {tweet.id})")
            sections.append(f"**{tweet.created_at}**")
            sections.append(tweet.full_text)

            # URLs
            urls = await _resolve_tweet_urls(client, tweet)
            for u in urls:
                sections.append(f"🔗 {u}")

            # Media
            if tweet.media:
                for m in tweet.media:
                    if isinstance(m, Photo): sections.append(f"📷 photo: {m.media_url}")
                    elif isinstance(m, Video): sections.append(f"🎬 video: {m.media_url}")

            # X Article
            article = _extract_article_text(tweet._data)
            if article:
                sections.append(f"\n## X Article by @{tweet.user.screen_name}")
                sections.append(article)

            # Note tweet
            note = _get_note_text(tweet._data)
            if note and note != tweet.full_text:
                sections.append(f"\n## Long-form note")
                sections.append(note)

            # Quoted tweet
            if tweet.is_quote_status and tweet.quote:
                q = tweet.quote
                sections.append(f"\n## Quoted @{q.user.screen_name} (ID: {q.id})")
                sections.append(q.full_text[:300])
                q_article = _extract_article_text(q._data)
                if q_article:
                    sections.append(f"\n## X Article by @{q.user.screen_name} (quoted)")
                    sections.append(q_article)

            # Parent tweet
            parent_id = tweet.in_reply_to
            if parent_id:
                try:
                    parent_tweets = await client.get_tweets_by_ids([parent_id])
                    if parent_tweets and parent_tweets[0]:
                        p = parent_tweets[0]
                        sections.append(f"\n## Parent @{p.user.screen_name} (ID: {p.id})")
                        sections.append(p.full_text[:300])
                        p_article = _extract_article_text(p._data)
                        if p_article:
                            sections.append(f"\n## X Article by @{p.user.screen_name} (parent)")
                            sections.append(p_article)
                except Exception:
                    sections.append(f"\n[Could not fetch parent {parent_id}]")

            results.append("\n".join(sections))
            results.append("===TWEET===")

        return "\n".join(results)
    except Exception as e:
        logger.error(f"Failed to resolve tweets batch: {e}")
        return f"Failed to resolve tweets batch: {e}"

@mcp.tool()
async def get_tweet_links(tweet_id: str) -> str:
    """Extract all expanded URLs from a tweet (resolves t.co shortlinks to real URLs)."""
    try:
        client = await get_twitter_client()
        tweets = await client.get_tweets_by_ids([tweet_id])
        if not tweets:
            return f"Could not find tweet {tweet_id}"

        tweet = tweets[0]
        try:
            urls = tweet.urls
        except (KeyError, AttributeError, TypeError):
            urls = None

        result = []

        if urls:
            for url_entity in urls:
                expanded = url_entity.get('expanded_url', url_entity.get('url', 'unknown'))
                display = url_entity.get('display_url', '')
                result.append(f"- {expanded} ({display})")
        else:
            # Fallback: extract t.co links from text and resolve them
            tco_links = re.findall(r'https://t\.co/\w+', tweet.full_text)
            for link in tco_links:
                resolved = await resolve_tco_link(client, link)
                result.append(f"- {resolved}")

        if not result:
            return f"Tweet {tweet_id} has no URLs"

        return "\n".join(result)
    except Exception as e:
        logger.error(f"Failed to get tweet links: {e}")
        return f"Failed to get tweet links: {e}"

@mcp.tool()
async def get_parent_tweet(tweet_id: str) -> str:
    """Get the parent tweet that a reply is responding to. Follows the reply chain to find the original content."""
    try:
        client = await get_twitter_client()
        tweets = await client.get_tweets_by_ids([tweet_id])
        if not tweets:
            return f"Could not find tweet {tweet_id}"

        tweet = tweets[0]
        result = []

        # Check if it's a reply
        parent_id = tweet.in_reply_to
        if parent_id:
            parent_tweets = await client.get_tweets_by_ids([parent_id])
            if parent_tweets and parent_tweets[0]:
                parent = parent_tweets[0]
                result.append(f"## Parent Tweet (reply target)")
                result.append(f"### @{parent.user.screen_name} (ID: {parent.id})")
                result.append(f"**{parent.created_at}**")
                result.append(parent.full_text)

                # Check if parent has an article
                parent_article = parent._data.get('article', {})
                if parent_article.get('article_results', {}).get('result', {}).get('content_state', {}).get('blocks'):
                    result.append(f"\n📄 [Parent has X Article - use get_tweet_article with ID {parent.id}]")

                # Check parent URLs
                try:
                    parent_urls = parent.urls
                except (KeyError, AttributeError, TypeError):
                    parent_urls = None
                if parent_urls:
                    for url_entity in parent_urls:
                        expanded = url_entity.get('expanded_url')
                        if expanded:
                            result.append(f"🔗 {expanded}")
                else:
                    tco_links = re.findall(r'https://t\.co/\w+', parent.full_text)
                    for link in tco_links:
                        result.append(f"🔗 {link} (t.co)")

                # Check if parent is also a reply (chain)
                grandparent_id = parent.in_reply_to
                if grandparent_id:
                    result.append(f"\n↩️ Parent is also a reply to tweet {grandparent_id}")

        # Check if it's a quote tweet
        if tweet.is_quote_status:
            quoted = tweet.quote
            if quoted:
                result.append(f"\n## Quoted Tweet")
                result.append(f"### @{quoted.user.screen_name} (ID: {quoted.id})")
                result.append(f"**{quoted.created_at}**")
                result.append(quoted.full_text)

                # Check if quoted tweet has an article
                quoted_article = quoted._data.get('article', {})
                if quoted_article.get('article_results', {}).get('result', {}).get('content_state', {}).get('blocks'):
                    result.append(f"\n📄 [Quoted tweet has X Article - use get_tweet_article with ID {quoted.id}]")

                try:
                    quoted_urls = quoted.urls
                except (KeyError, AttributeError, TypeError):
                    quoted_urls = None
                if quoted_urls:
                    for url_entity in quoted_urls:
                        expanded = url_entity.get('expanded_url')
                        if expanded:
                            result.append(f"🔗 {expanded}")
                else:
                    tco_links = re.findall(r'https://t\.co/\w+', quoted.full_text)
                    for link in tco_links:
                        result.append(f"🔗 {link} (t.co)")

        if not result:
            return f"Tweet {tweet_id} is not a reply or quote tweet"

        return "\n".join(result)
    except Exception as e:
        logger.error(f"Failed to get parent tweet: {e}")
        return f"Failed to get parent tweet: {e}"

def convert_tweets_to_markdown(tweets) -> str:
    """Convert a list of tweets to markdown format with full text, expanded URLs, and media details."""
    result = []
    for tweet in tweets:
        try:
            result.append(f"### @{tweet.user.screen_name} (ID: {tweet.id})")
            result.append(f"**{tweet.created_at}**")
            result.append(tweet.full_text)

            # Show reply/quote context
            if tweet.in_reply_to:
                result.append(f"↩️ reply to tweet {tweet.in_reply_to}")
            if tweet.is_quote_status and tweet.quote:
                q = tweet.quote
                result.append(f"💬 quoting @{q.user.screen_name}: {q.full_text[:150]}...")

            # Show expanded URLs (with fallback to t.co extraction from text)
            try:
                urls = tweet.urls
            except (KeyError, AttributeError, TypeError):
                urls = None
            if urls:
                for url_entity in urls:
                    expanded = url_entity.get('expanded_url')
                    if expanded:
                        result.append(f"🔗 {expanded}")
            else:
                tco_links = re.findall(r'https://t\.co/\w+', tweet.full_text)
                for link in tco_links:
                    result.append(f"🔗 {link} (t.co - use get_tweet_links to resolve)")

            # Show media with type and actual media URLs
            if tweet.media:
                for media in tweet.media:
                    media_type = media.type or 'unknown'
                    if isinstance(media, Photo):
                        result.append(f"📷 [photo] {media.media_url}")
                    elif isinstance(media, Video):
                        result.append(f"🎬 [video] {media.media_url}")
                    else:
                        result.append(f"[{media_type}] {media.media_url}")

        except Exception as e:
            result.append(f"### [Tweet ID: {getattr(tweet, 'id', 'unknown')}] (skipped: {e})")

        result.append("---")
    return "\n".join(result)

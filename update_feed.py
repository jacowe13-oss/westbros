#!/usr/bin/env python3
"""Synchronize Blogger episodes to Internet Archive, confirming deletions twice."""
import os
import json
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import format_datetime
from datetime import datetime
from html import unescape

BLOG_ID = '1850249675015842133'
ITEM = 'westbros-complete-rss'
FILENAME = 'westbros-complete-rss.xml'
STATE_FILE = '.westbros-sync-state.json'
MAX_REMOVALS_PER_RUN = 5
MIN_SAFE_EPISODES = 200
BASE = f'https://archive.org/download/{ITEM}/{FILENAME}'
ATOM = '{http://www.w3.org/2005/Atom}'
OPENSEARCH = '{http://a9.com/-/spec/opensearchrss/1.0/}'
ITUNES = '{http://www.itunes.com/dtds/podcast-1.0.dtd}'
SHOW_TITLE = 'westbros'
ET.register_namespace('itunes', 'http://www.itunes.com/dtds/podcast-1.0.dtd')
ET.register_namespace('content', 'http://purl.org/rss/1.0/modules/content/')


def get(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'WestBrosFeedUpdater/1.0', 'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(req, timeout=45) as response:
        return response.read()


def entries_from_blogger():
    entries = []
    start = 1
    expected = None
    while True:
        url = f'https://www.blogger.com/feeds/{BLOG_ID}/posts/default?max-results=150&start-index={start}'
        root = ET.fromstring(get(url))
        total = root.findtext(OPENSEARCH + 'totalResults')
        if total is not None:
            expected = int(total)
        batch = root.findall(ATOM + 'entry')
        if not batch:
            break
        entries.extend(batch)
        start += len(batch)
        if len(batch) < 150:
            break
        if start > 10000:
            raise RuntimeError('Unexpectedly large Blogger feed')
    if expected is None or len(entries) != expected or expected < MIN_SAFE_EPISODES:
        raise RuntimeError(f'Incomplete Blogger response: {len(entries)} entries; expected {expected}, minimum 262')
    return entries


def blog_link(entry):
    for link in entry.findall(ATOM + 'link'):
        if link.get('rel') == 'alternate' and link.get('href'):
            return link.get('href')
    return None


def normalize(url):
    return (url or '').split('#', 1)[0].rstrip('/')


def new_rss_item(entry):
    title = entry.findtext(ATOM + 'title') or 'Untitled episode'
    link = blog_link(entry)
    content = entry.findtext(ATOM + 'content') or entry.findtext(ATOM + 'summary') or ''
    # Blogger's HTML includes the episode MP3 link.
    matches = re.findall(r'''(?:https?:)?//[^\s"'<>]+?\.mp3(?:\?[^\s"'<>]*)?''', unescape(content), re.I)
    if not matches:
        raise RuntimeError(f'No MP3 found for new post: {title}')
    mp3 = matches[0]
    if mp3.startswith('//'):
        mp3 = 'https:' + mp3
    published = entry.findtext(ATOM + 'published') or entry.findtext(ATOM + 'updated')
    if not published or not link:
        raise RuntimeError(f'Missing date or link for new post: {title}')
    date = datetime.fromisoformat(published.replace('Z', '+00:00'))
    item = ET.Element('item')
    ET.SubElement(item, 'title').text = title
    ET.SubElement(item, 'link').text = link
    ET.SubElement(item, 'guid', {'isPermaLink': 'false'}).text = entry.findtext(ATOM + 'id') or link
    ET.SubElement(item, 'pubDate').text = format_datetime(date)
    ET.SubElement(item, 'description').text = content
    ET.SubElement(item, 'enclosure', {'url': mp3, 'length': '0', 'type': 'audio/mpeg'})
    return item


def key_for_item(item):
    return (item.findtext('guid') or normalize(item.findtext('link')) or '').strip()


def key_for_entry(entry):
    return (entry.findtext(ATOM + 'id') or normalize(blog_link(entry)) or '').strip()


def save_state(keys):
    # Checked into GitHub so missing episodes are remembered across scheduled runs.
    with open(STATE_FILE, 'w', encoding='utf-8') as file:
        json.dump({'missing_once': sorted(keys)}, file, indent=2)
        file.write('\n')


def main():
    original = get(BASE + '?nocache=' + str(int(time.time())))
    rss = ET.fromstring(original)
    channel = rss.find('channel')
    if rss.tag != 'rss' or channel is None:
        raise RuntimeError('Internet Archive source is not RSS')
    # Keep the podcast's display title lowercase, even when no episodes change.
    title_node = channel.find('title')
    if title_node is None:
        title_node = ET.Element('title')
        channel.insert(0, title_node)
    title_changed = title_node.text != SHOW_TITLE
    title_node.text = SHOW_TITLE
    itunes_title = channel.find(ITUNES + 'title')
    if itunes_title is not None:
        title_changed = title_changed or itunes_title.text != SHOW_TITLE
        itunes_title.text = SHOW_TITLE
    existing = channel.findall('item')
    if len(existing) < MIN_SAFE_EPISODES:
        raise RuntimeError(f'Archive has only {len(existing)} items; refusing to update')

    posts = entries_from_blogger()
    blogger_keys = [key_for_entry(p) for p in posts]
    existing_keys = [key_for_item(x) for x in existing]
    if (not all(blogger_keys) or not all(existing_keys)
            or len(set(blogger_keys)) != len(blogger_keys)
            or len(set(existing_keys)) != len(existing_keys)):
        raise RuntimeError('Missing or duplicate episode identifiers; refusing to synchronize')
    blogger_set = set(blogger_keys)
    existing_set = set(existing_keys)
    try:
        with open(STATE_FILE, encoding='utf-8') as file:
            previous_missing = set(json.load(file).get('missing_once', []))
    except FileNotFoundError:
        previous_missing = set()

    missing_now = existing_set - blogger_set
    # Only remove if it was also missing in the previous successful check.
    removals = missing_now & previous_missing
    if len(removals) > MAX_REMOVALS_PER_RUN:
        raise RuntimeError(f'{len(removals)} deletions detected; manual review required')
    if len(existing) - len(removals) < MIN_SAFE_EPISODES:
        raise RuntimeError('Deletion would breach minimum archive size; refusing upload')

    # Persist confirmation state even when nothing needs uploading.
    save_state(missing_now - removals)
    for item in existing:
        if key_for_item(item) in removals:
            channel.remove(item)

    additions = []
    for entry in posts:
        if key_for_entry(entry) not in existing_set:
            additions.append(new_rss_item(entry))
    for item in reversed(additions):
        channel.insert(0, item)

    if not additions and not removals and not title_changed:
        print(f'No upload needed. Archive: {len(existing)}; Blogger: {len(posts)}; '
              f'awaiting deletion confirmation: {len(missing_now)}')
        return

    ET.indent(rss, space='  ')
    ET.ElementTree(rss).write(FILENAME, encoding='utf-8', xml_declaration=True)
    access = os.environ.get('IA_ACCESS_KEY')
    secret = os.environ.get('IA_SECRET_KEY')
    if not access or not secret:
        raise RuntimeError('Missing Internet Archive GitHub secrets')
    from internetarchive import get_item, get_session
    session = get_session(config={'s3': {'access': access, 'secret': secret}})
    item = get_item(ITEM, archive_session=session)
   responses = item.upload(FILENAME, headers={'x-archive-keep-old-version': '1'}, verbose=False)
    for response in responses:
        response.raise_for_status()
    print(f'Upload accepted: show title {SHOW_TITLE!r}; added {len(additions)}, removed {len(removals)}; '
          f'total RSS episodes: {len(existing) - len(removals) + len(additions)}')
    print('Internet Archive and FeedBurner may cache the previous version temporarily.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Updater failed safely: {exc}', file=sys.stderr)
        sys.exit(1)
